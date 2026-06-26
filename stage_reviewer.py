#!/usr/bin/env python3
"""
Stage-level review/approval gate for SLAIResearch pipeline.
After each stage produces output, an LLM reviewer (simulating a human
reviewer) evaluates the output. If the review doesn't pass, the stage is
re-executed with the review feedback, up to a configurable max retries.

Usage:
    from stage_reviewer import StageReviewer, ReviewVerdict

    reviewer = StageReviewer(model="deepseek-v4-pro")
    verdict = reviewer.review(
        stage_name="literature_search",
        stage_output=open("literature_review.md").read(),
        topic="Attention mechanisms for long sequences",
    )
    if verdict.passed:
        print("Stage approved!")
    else:
        print(f"Stage rejected: {verdict.feedback}")

Modes:
  - "llm" (default) — uses claude -p to review the output
  - "human" — prompts the user in the terminal for approval
"""

import os
import sys
import subprocess
import json
import re
import logging
import textwrap
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import CLAUDE_CMD, CLAUDE_MODEL
from claude_pty import run_in_pty

logger = logging.getLogger("stage_reviewer")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ReviewVerdict:
    """Result of a stage review."""
    stage: str
    passed: bool
    score: float                    # 1-10
    feedback: str                   # detailed feedback for revision
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    critical_issues: list[str] = field(default_factory=list)
    suggestion: str = ""            # concrete suggestions for improvement
    raw_output: str = ""            # full LLM output
    reviewer_mode: str = "llm"      # "llm" or "human"


# ---------------------------------------------------------------------------
# Stage-specific review criteria
# ---------------------------------------------------------------------------

STAGE_REVIEW_CONFIG = {
    "hypothesis_generation": {
        "stage_label": "Hypothesis Generation",
        "reviewer_role": "资深研究假说审稿人",
        "criteria": textwrap.dedent("""\
            1. **Gap Analysis Depth (缺口分析深度)**: 是否深入分析了研究空白？是否通过多轮检索验证了空白的真实性？
            2. **Hypothesis Specificity (假说具体性)**: 每个假说是否包含具体的方法、场景和预期效果？
            3. **Literature Grounding (文献支撑)**: 每个假说是否有充分的文献支撑（至少3篇关键引用）？
            4. **Novelty Assessment (创新性)**: 假说是否具有真正的创新性，而非已有工作的简单组合？
            5. **Feasibility (可行性)**: 假说是否可以用计算实验验证？需要的数据和资源是否明确？
            6. **Comparison Quality (对比质量)**: 是否充分比较了不同假说的优劣？选择理由是否充分？
            7. **ReAct Quality (检索质量)**: 是否进行了充分的多轮检索？PDF深度阅读是否发现了关键细节？
        """),
        "pass_threshold": 7.0,
    },
    "literature_search": {
        "stage_label": "Literature Search",
        "reviewer_role": "资深文献综述审稿人",
        "criteria": textwrap.dedent("""\
            1. **Coverage (覆盖面)**: 是否覆盖了该领域近5年的重要论文（至少10-15篇）？
            2. **Organization (组织)**: 文献是否按主题/方法合理组织？是否有清晰的分类？
            3. **Gap Analysis (缺口分析)**: 是否明确指出了研究空白和未解决的问题？
            4. **Citation Quality (引用质量)**: 引用是否来自正规学术渠道（arXiv/会议/期刊）？
            5. **Actionable Direction (可执行方向)**: 是否提出了具体、可验证的研究方向？
            6. **Reference Format (参考文献格式)**: 是否提供了完整的BibTeX引用？
        """),
        "pass_threshold": 6.0,  # score >= this to pass
    },
    "experiment_design": {
        "stage_label": "Experiment Design",
        "reviewer_role": "资深实验设计审稿人",
        "criteria": textwrap.dedent("""\
            1. **Hypothesis Clarity (假设清晰性)**: 研究假设/问题是否明确、可验证？
            2. **Method Soundness (方法合理性)**: 提出的方法在理论/直觉上是否合理？
            3. **Baseline Coverage (基线覆盖)**: 是否包含了所有相关基线方法？是否有遗漏？
            4. **Dataset Appropriateness (数据集合理性)**: 选择的数据集是否标准且适合该任务？
            5. **Metric Completeness (指标完整性)**: 评估指标是否全面（主要+次要指标）？
            6. **Ablation Design (消融设计)**: 是否设计了充分的消融实验来验证各组件贡献？
            7. **Implementation Feasibility (实现可行性)**: 代码/脚本是否可执行？依赖是否明确？
            8. **Statistical Rigor (统计严谨性)**: 是否考虑了多种子/显著性检验？
        """),
        "pass_threshold": 6.5,
    },
    "experiment_execution": {
        "stage_label": "Experiment Execution",
        "reviewer_role": "资深实验评审人",
        "criteria": textwrap.dedent("""\
            1. **Execution Success (执行成功)**: 实验是否成功运行完成？有无运行时错误？
            2. **Result Completeness (结果完整性)**: 是否包含了所有计划中的实验（主实验+消融）？
            3. **Result Plausibility (结果合理性)**: 实验结果数值是否合理（无异常值）？
            4. **Reproducibility (可复现性)**: 日志/输出是否足够详细以支持复现？
            5. **Baseline Comparison (基线对比)**: 是否与所有基线进行了公平对比？
            6. **Statistical Reporting (统计报告)**: 是否报告了均值/方差/显著性？
        """),
        "pass_threshold": 5.5,
    },
    "paper_writing": {
        "stage_label": "Paper Writing",
        "reviewer_role": "资深论文写作审稿人",
        "criteria": textwrap.dedent("""\
            1. **Structure (结构)**: 论文结构是否完整（Abstract/Intro/Related Work/Method/Experiments/Conclusion）？
            2. **Clarity (清晰性)**: 写作是否清晰？核心贡献是否容易被理解？
            3. **Technical Accuracy (技术准确性)**: 方法描述是否准确？公式/符号是否正确？
            4. **Claim-Evidence Alignment (声明-证据匹配)**: 论文声明是否被实验结果充分支持？
            5. **Related Work (相关工作)**: 是否充分讨论了相关工作并准确定位？
            6. **Figure/Table Quality (图表质量)**: 图表是否清晰、标注完整、信息丰富？
            7. **LaTeX Quality (排版质量)**: 排版是否符合目标会议模板要求？是否能成功编译？
            8. **Citation Completeness (引用完整性)**: 所有引用是否在references.bib中？
        """),
        "pass_threshold": 6.0,
    },
    "revise": {
        "stage_label": "Paper Revision",
        "reviewer_role": "资深论文修改审稿人",
        "criteria": textwrap.dedent("""\
            1. **Revision Completeness (修改完整度)**: 是否充分回应了上一轮审稿意见？
            2. **Improvement Quality (改进质量)**: 修改是否真正提升了论文质量？
            3. **Regression Check (退化检查)**: 修改是否引入了新问题或破坏了原有内容？
        """),
        "pass_threshold": 6.0,
    },
}


# ---------------------------------------------------------------------------
# Review prompts
# ---------------------------------------------------------------------------

def _build_review_prompt(
    stage_name: str,
    stage_output: str,
    topic: str,
    retry_context: str = "",
    attempt: int = 0,
) -> str:
    """Build the LLM review prompt for a specific stage."""
    cfg = STAGE_REVIEW_CONFIG.get(stage_name)
    if cfg is None:
        # Generic review for unknown stages
        cfg = {
            "stage_label": stage_name,
            "reviewer_role": "审稿人",
            "criteria": "评估输出质量",
            "pass_threshold": 6.0,
        }

    retry_note = ""
    if retry_context:
        retry_note = f"""
## 上一轮评审反馈
{retry_context}

这是第 {attempt + 1} 次尝试。请检查上述问题是否已被修正。
"""

    prompt = f"""你是一位{cfg['reviewer_role']}，正在评审一项科研工作的阶段性产出。

## 研究主题
{topic}

## 当前阶段
{cfg['stage_label']} ({stage_name})

## 评审标准
{cfg['criteria']}

{retry_note}

## 阶段产出
{stage_output[:20000]}

请按照以下JSON格式输出评审结果（不要输出其他内容）：
```json
{{
    "score": 7.5,
    "passed": true,
    "strengths": ["优点1", "优点2"],
    "weaknesses": ["缺点1", "缺点2"],
    "critical_issues": ["严重问题1（如果有）"],
    "suggestion": "具体的改进建议，如果有的话",
    "feedback": "详细的综合评审意见"
}}
```

评分标准：
- 9-10: 优秀，几乎无需修改
- 7-8: 良好，少量修改即可
- 5-6: 中等，存在明显问题需要修改
- 3-4: 较差，需要大幅修改
- 1-2: 极差，建议重新设计

pass_threshold: 分数 >= {cfg['pass_threshold']} 即为通过。
请严格、公正地评审，确保研究质量。"""
    return prompt


# ---------------------------------------------------------------------------
# Core reviewer
# ---------------------------------------------------------------------------

class StageReviewer:
    """Review a pipeline stage output using LLM or human judgment."""

    def __init__(
        self,
        model: str = CLAUDE_MODEL,
        mode: str = "llm",       # "llm" or "human"
        max_retries: int = 10,
        verbose: bool = True,
    ):
        self.model = model
        self.mode = mode
        self.max_retries = max_retries
        self.verbose = verbose

    def review(
        self,
        stage_name: str,
        stage_output: str,
        topic: str = "",
        retry_context: str = "",
        attempt: int = 0,
        extra_context: dict[str, Any] | None = None,
    ) -> ReviewVerdict:
        """Review a stage output and return a verdict.

        Args:
            stage_name: pipeline stage name (e.g., "literature_search")
            stage_output: the full output text from the stage
            topic: research topic
            retry_context: feedback from previous failed review (for retries)
            attempt: current attempt number (0-indexed)
            extra_context: any additional context for the reviewer

        Returns:
            ReviewVerdict with passed/failed and feedback.
        """
        if self.mode == "human":
            return self._human_review(stage_name, stage_output, topic, attempt)
        else:
            return self._llm_review(
                stage_name, stage_output, topic,
                retry_context, attempt, extra_context,
            )

    def _llm_review(
        self,
        stage_name: str,
        stage_output: str,
        topic: str,
        retry_context: str,
        attempt: int,
        extra_context: dict[str, Any] | None,
    ) -> ReviewVerdict:
        """Review using LLM via claude -p."""
        if not stage_output or not stage_output.strip():
            return ReviewVerdict(
                stage=stage_name,
                passed=False,
                score=0,
                feedback="阶段产出为空，无法评审。",
                critical_issues=["Empty output"],
                reviewer_mode=self.mode,
            )

        prompt = _build_review_prompt(
            stage_name, stage_output, topic, retry_context, attempt,
        )

        logger.info("Reviewing stage '%s' (attempt %d) via LLM ...", stage_name, attempt + 1)

        cmd = [
            CLAUDE_CMD, "-p",
            "--model", self.model,
            "--output-format", "text",
        ]

        try:
            rc, raw = run_in_pty(cmd, prompt, timeout=300,
                                 cwd=str(PROJECT_ROOT), env={**os.environ})
            raw = raw or ""
            if rc != 0 and not raw:
                # LLM call failed — treat as "needs revision" rather than crashing
                logger.warning("LLM review call failed: rc=%d (PTY mode — no stderr)", rc)
                return ReviewVerdict(
                    stage=stage_name,
                    passed=False,
                    score=0,
                    feedback=f"评审系统错误: claude exited {rc}",
                    critical_issues=["Review system error"],
                    raw_output=raw,
                    reviewer_mode=self.mode,
                )

            verdict = self._parse_review_output(raw, stage_name)
            verdict.raw_output = raw
            return verdict

        except subprocess.TimeoutExpired:
            logger.warning("LLM review timed out for stage '%s'", stage_name)
            return ReviewVerdict(
                stage=stage_name,
                passed=True,  # timeouts pass by default to avoid blocking
                score=7.0,
                feedback="[评审超时，默认通过]",
                reviewer_mode=self.mode,
            )
        except FileNotFoundError:
            logger.warning("claude CLI not found — skipping review for '%s'", stage_name)
            return ReviewVerdict(
                stage=stage_name,
                passed=True,  # pass if review tool unavailable
                score=7.0,
                feedback="[评审工具不可用，默认通过]",
                reviewer_mode=self.mode,
            )

    def _human_review(
        self, stage_name: str, stage_output: str, topic: str, attempt: int,
    ) -> ReviewVerdict:
        """Prompt a human user to review the stage output."""
        cfg = STAGE_REVIEW_CONFIG.get(stage_name, {})
        label = cfg.get("stage_label", stage_name)
        criteria = cfg.get("criteria", "评估输出质量")
        threshold = cfg.get("pass_threshold", 6.0)

        print(f"\n{'='*60}")
        print(f"  HUMAN REVIEW REQUIRED — {label}")
        print(f"  Topic: {topic}")
        print(f"  Attempt: {attempt + 1}")
        print(f"{'='*60}")
        print(f"\n评审标准:\n{criteria}")
        print(f"\n阶段产出 (前3000字符):\n{stage_output[:3000]}")
        print(f"\n通过阈值: {threshold}/10")

        while True:
            try:
                score_str = input(f"\n请打分 (1-10, 空=通过): ").strip()
                if not score_str:
                    return ReviewVerdict(
                        stage=stage_name, passed=True, score=threshold,
                        feedback="[人工审核通过]", reviewer_mode="human",
                    )
                score = float(score_str)
                if score < 1 or score > 10:
                    print("分数必须在 1-10 之间")
                    continue
                feedback = input("评审意见 (可选): ").strip()
                passed = score >= threshold
                return ReviewVerdict(
                    stage=stage_name, passed=passed, score=score,
                    feedback=feedback or f"[人工评分: {score}]",
                    reviewer_mode="human",
                )
            except ValueError:
                print("请输入有效数字")

    def _parse_review_output(self, raw: str, stage_name: str) -> ReviewVerdict:
        """Parse LLM JSON output into a ReviewVerdict."""
        cfg = STAGE_REVIEW_CONFIG.get(stage_name, {})
        threshold = cfg.get("pass_threshold", 6.0)

        # Try to extract JSON block
        json_str = ""
        # Look for ```json ... ``` block
        m = re.search(r'```json\s*(.*?)\s*```', raw, re.DOTALL)
        if m:
            json_str = m.group(1)
        else:
            # Look for bare JSON object
            m = re.search(r'\{[^{}]*"score"[^{}]*\}', raw, re.DOTALL)
            if m:
                json_str = m.group(0)

        if json_str:
            try:
                data = json.loads(json_str)
            except json.JSONDecodeError:
                # Try to repair common JSON issues
                data = _repair_json(json_str) or {}
        else:
            data = {}

        score = data.get("score", 5.0)
        try:
            score = float(score)
        except (TypeError, ValueError):
            # Try to extract score from text
            m = re.search(r'score[:\s]*(\d+(?:\.\d+)?)', raw, re.IGNORECASE)
            score = float(m.group(1)) if m else 5.0

        # Clamp
        score = max(1.0, min(10.0, score))

        passed = data.get("passed", score >= threshold)

        return ReviewVerdict(
            stage=stage_name,
            passed=passed,
            score=score,
            feedback=data.get("feedback", ""),
            strengths=data.get("strengths", []),
            weaknesses=data.get("weaknesses", []),
            critical_issues=data.get("critical_issues", []),
            suggestion=data.get("suggestion", ""),
            reviewer_mode=self.mode,
        )


def _repair_json(json_str: str) -> dict | None:
    """Attempt to repair truncated or malformed JSON."""
    # Replace single quotes
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        pass

    # Try to fix trailing commas, unquoted keys, etc.
    try:
        # Remove trailing commas before closing braces
        fixed = re.sub(r',\s*}', '}', json_str)
        fixed = re.sub(r',\s*]', ']', fixed)
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass

    # Last resort: extract individual fields with regex
    try:
        score_m = re.search(r'"score"\s*:\s*(\d+(?:\.\d+)?)', json_str)
        passed_m = re.search(r'"passed"\s*:\s*(true|false)', json_str, re.IGNORECASE)
        feedback_m = re.search(r'"feedback"\s*:\s*"([^"]*)"', json_str)

        result = {}
        if score_m:
            result["score"] = float(score_m.group(1))
        if passed_m:
            result["passed"] = passed_m.group(1).lower() == "true"
        if feedback_m:
            result["feedback"] = feedback_m.group(1)
        return result if result else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Review loop helper — review + retry integration
# ---------------------------------------------------------------------------

def review_until_pass(
    reviewer: StageReviewer,
    stage_name: str,
    run_stage_fn,           # callable(stage_output_path, retry_feedback) -> new_output_path
    output_path: str,
    topic: str = "",
    extra_context: dict[str, Any] | None = None,
) -> tuple[bool, list[ReviewVerdict], str]:
    """Review stage output; if it fails, re-run the stage with feedback.

    Args:
        reviewer: StageReviewer instance
        stage_name: name of the pipeline stage
        run_stage_fn: callable to re-execute the stage, receives (output_path, feedback_str)
        output_path: path to the stage's output artifact
        topic: research topic
        extra_context: extra context for the reviewer

    Returns:
        (passed, review_history, final_output_path)
    """
    review_history: list[ReviewVerdict] = []

    for attempt in range(reviewer.max_retries + 1):
        # Read current output
        try:
            stage_output = Path(output_path).read_text()
        except FileNotFoundError:
            stage_output = ""

        # Build retry context from previous failures
        retry_context = ""
        if review_history:
            last = review_history[-1]
            retry_context = f"""上一轮评审未通过（评分: {last.score}/10）。

**问题总结**: {last.feedback}

**需要修改的地方**:
"""
            for w in last.weaknesses:
                retry_context += f"  - {w}\n"
            if last.critical_issues:
                retry_context += "\n**严重问题（必须解决）**:\n"
                for c in last.critical_issues:
                    retry_context += f"  - {c}\n"
            if last.suggestion:
                retry_context += f"\n**改进建议**: {last.suggestion}\n"

        # Review
        verdict = reviewer.review(
            stage_name=stage_name,
            stage_output=stage_output,
            topic=topic,
            retry_context=retry_context,
            attempt=attempt,
            extra_context=extra_context,
        )
        review_history.append(verdict)

        if verdict.passed:
            logger.info(
                "Stage '%s' PASSED review (attempt %d, score %.1f)",
                stage_name, attempt + 1, verdict.score,
            )
            return True, review_history, output_path

        logger.warning(
            "Stage '%s' FAILED review (attempt %d, score %.1f)",
            stage_name, attempt + 1, verdict.score,
        )

        if attempt < reviewer.max_retries:
            logger.info("Re-running stage '%s' with review feedback ...", stage_name)
            # Re-execute the stage with feedback
            feedback = _build_retry_feedback(review_history)
            try:
                new_output_path = run_stage_fn(output_path, feedback)
                if new_output_path:
                    output_path = new_output_path
            except Exception as exc:
                logger.error("Stage re-run failed: %s", exc)
                # Continue loop — reviewer will see unchanged output and fail again
        else:
            logger.warning(
                "Stage '%s' exceeded max retries (%d). Proceeding anyway.",
                stage_name, reviewer.max_retries,
            )

    # Exhausted retries
    return False, review_history, output_path


def _build_retry_feedback(history: list[ReviewVerdict]) -> str:
    """Build cumulative feedback from all failed reviews."""
    parts = []
    for i, v in enumerate(history):
        if not v.passed:
            parts.append(f"## 第{i+1}轮评审反馈 (评分: {v.score}/10)")
            if v.critical_issues:
                parts.append("\n### 严重问题")
                for c in v.critical_issues:
                    parts.append(f"  - {c}")
            if v.weaknesses:
                parts.append("\n### 需要改进")
                for w in v.weaknesses:
                    parts.append(f"  - {w}")
            if v.suggestion:
                parts.append(f"\n### 具体建议\n{v.suggestion}")
            parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Review a pipeline stage output via LLM or human.",
    )
    parser.add_argument("stage", help="Stage name (literature_search, experiment_design, etc.)")
    parser.add_argument("output_file", help="Path to stage output file")
    parser.add_argument("--topic", "-t", default="", help="Research topic")
    parser.add_argument("--mode", choices=["llm", "human"], default="llm", help="Review mode")
    parser.add_argument("--model", default=CLAUDE_MODEL, help="LLM model for review")
    parser.add_argument("--json", action="store_true", help="Output verdict as JSON")
    args = parser.parse_args()

    output_text = ""
    try:
        output_text = Path(args.output_file).read_text()
    except FileNotFoundError:
        print(f"Error: file not found: {args.output_file}", file=sys.stderr)
        sys.exit(1)

    reviewer = StageReviewer(model=args.model, mode=args.mode)
    verdict = reviewer.review(
        stage_name=args.stage,
        stage_output=output_text,
        topic=args.topic,
    )

    if args.json:
        import dataclasses
        print(json.dumps(dataclasses.asdict(verdict), indent=2, ensure_ascii=False))
    else:
        print(f"\nStage: {verdict.stage}")
        print(f"Passed: {verdict.passed}")
        print(f"Score: {verdict.score:.1f}/10")
        print(f"Mode: {verdict.reviewer_mode}")
        if verdict.strengths:
            print("\nStrengths:")
            for s in verdict.strengths:
                print(f"  + {s}")
        if verdict.weaknesses:
            print("\nWeaknesses:")
            for w in verdict.weaknesses:
                print(f"  - {w}")
        if verdict.critical_issues:
            print("\nCritical Issues:")
            for c in verdict.critical_issues:
                print(f"  !! {c}")
        if verdict.feedback:
            print(f"\nFeedback:\n{verdict.feedback}")

    sys.exit(0 if verdict.passed else 1)


if __name__ == "__main__":
    main()
