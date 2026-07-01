#!/usr/bin/env python3
"""
ReAct-based hypothesis generation engine for SLAIResearch.
Given a literature review, iteratively searches for more papers,
deep-reads highly relevant ones, and generates concrete research hypotheses.

Phases:
  1. INITIAL_ANALYSIS  — analyze gaps in existing literature
  2. REACT_LOOP        — iteratively search, read, refine (up to N rounds)
  3. PDF_DEEP_READ     — download + deep-read top-K most relevant papers
  4. HYPOTHESIS_GEN    — generate 3-5 concrete hypotheses with rationale

Usage:
    python hypothesis_engine.py \\
        --topic "Attention for long sequences" \\
        --literature-dir workspace/topic/literature/ \\
        --work-dir workspace/topic/hypothesis/ \\
        --max-react-rounds 3 --top-k-pdfs 5
"""

import os
import sys
import json
import re
import hashlib
import time
import subprocess
import logging
import textwrap
from pathlib import Path
from datetime import datetime, timezone

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import (
    CLAUDE_CMD, CLAUDE_MODEL,
    HYPOTHESIS_MAX_REACT_ROUNDS, HYPOTHESIS_TOP_K_PDFS, HYPOTHESIS_MAX_PAPERS,
)
from search_papers import (
    search_arxiv, search_semantic_scholar, search_openalex,
    merge_results, papers_to_json, load_papers_json,
    download_top_pdfs, download_paper_pdf, extract_pdf_text, _paper_fingerprint,
    _is_valid_arxiv_id, _looks_like_acl_id,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [hypothesis] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("hypothesis_engine")


# ======================================================================
# ReAct state management
# ======================================================================

class ReactState:
    """Checkpointable state for the ReAct loop."""

    def __init__(self, work_dir: Path):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.work_dir / "react_state.json"
        self.data = self._load_or_init()

    def _load_or_init(self) -> dict:
        if self.state_path.exists():
            logger.info("Resuming from saved ReAct state: %s", self.state_path)
            return json.loads(self.state_path.read_text())
        return {
            "phase": "INITIAL_ANALYSIS",
            "round": 0,
            "known_facts": [],
            "identified_gaps": [],
            "search_history": [],
            "all_papers": [],
            "papers_downloaded": [],
            "deep_read_notes": "",
            "candidate_hypotheses": [],
        }

    def save(self):
        self.state_path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False))

    def set_phase(self, phase: str):
        self.data["phase"] = phase
        self.save()

    def add_search(self, query: str, papers: list[dict], insights: str):
        self.data["search_history"].append({
            "query": query,
            "papers_found": len(papers),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "insights": insights,
        })
        # Merge papers (dedup by paper_id)
        existing_ids = {p.get("paper_id", "") for p in self.data["all_papers"]}
        for p in papers:
            if p.get("paper_id", "") not in existing_ids:
                self.data["all_papers"].append(p)
                existing_ids.add(p.get("paper_id", ""))
        self.save()

    def add_facts(self, facts: list[str]):
        self.data["known_facts"].extend(facts)
        self.save()

    def add_gaps(self, gaps: list[str]):
        self.data["identified_gaps"].extend(gaps)
        self.save()

    def add_pdf(self, pdf_path: str):
        self.data["papers_downloaded"].append(pdf_path)
        self.save()

    def set_deep_read_notes(self, notes: str):
        self.data["deep_read_notes"] = notes
        self.save()

    def set_hypotheses(self, hypotheses: list[dict]):
        self.data["candidate_hypotheses"] = hypotheses
        self.save()

    def get_context(self) -> str:
        """Build a human-readable context string for the prompt."""
        ctx = f"Phase: {self.data['phase']}\n"
        ctx += f"Round: {self.data['round']}\n\n"

        if self.data["known_facts"]:
            ctx += "## Known Facts\n"
            for f in self.data["known_facts"]:
                ctx += f"  - {f}\n"
            ctx += "\n"

        if self.data["identified_gaps"]:
            ctx += "## Identified Research Gaps\n"
            for g in self.data["identified_gaps"]:
                ctx += f"  - {g}\n"
            ctx += "\n"

        if self.data["search_history"]:
            ctx += "## Search History\n"
            for h in self.data["search_history"]:
                ctx += f"  Query: {h['query']}\n"
                ctx += f"  Papers found: {h['papers_found']}\n"
                ctx += f"  Insights: {h['insights'][:300]}\n\n"

        if self.data["deep_read_notes"]:
            ctx += f"## Deep-Read Notes\n{self.data['deep_read_notes'][:5000]}\n\n"

        # Include experimental evidence summary if available
        evidence_path = self.work_dir / "evidence_summary.json"
        if evidence_path.exists():
            try:
                ev_data = json.loads(evidence_path.read_text())
                ev_papers = ev_data.get("papers", [])
                if ev_papers:
                    ctx += "## Extracted Experimental Evidence\n\n"
                    for p in ev_papers:
                        title = p.get("title", "")[:80]
                        ev = p.get("evidence", {})
                        ctx += f"### {title}\n"
                        # Primary metrics (top 3)
                        metrics = ev.get("primary_metrics", [])
                        if metrics:
                            ctx += "**Metrics:**\n"
                            for m in metrics[:3]:
                                ctx += (f"- {m.get('metric_name','?')}: {m.get('value','?')}"
                                        f" (baseline: {m.get('baseline','?')}, Δ: {m.get('improvement','?')},"
                                        f" dataset: {m.get('dataset','?')})\n")
                        # Key claims (top 3)
                        claims = ev.get("key_claims", [])
                        if claims:
                            ctx += "**Key Claims:**\n"
                            for c in claims[:3]:
                                ctx += f"- {c}\n"
                        # Limitations (top 2)
                        limitations = ev.get("limitations", [])
                        if limitations:
                            ctx += "**Limitations:**\n"
                            for lim in limitations[:2]:
                                ctx += f"- {lim}\n"
                        ctx += "\n"
            except (json.JSONDecodeError, Exception) as e:
                logger.warning("Failed to load evidence summary for context: %s", e)

        return ctx


# ======================================================================
# Prompt builders
# ======================================================================

def _build_initial_analysis_prompt(topic: str, lit_review: str, papers_json: str) -> str:
    return f"""你是一位资深科研假说生成专家。你需要分析已有文献，发现研究空白。

研究主题: {topic}

## 文献综述
{lit_review[:15000]}

## 论文元数据 (前30篇)
{papers_json[:15000]}

## 任务
请深入分析以上文献，完成以下任务：

1. **领域概览**: 总结当前研究的主要方向和方法
2. **关键发现**: 列出5-10个最重要的已知事实/方法
3. **研究空白**: 识别至少5个未解决的研究问题或空白
4. **初步假说方向**: 提出2-3个有前景的研究方向

请按以下格式输出：
```
KNOWN_FACTS:
- 事实1
- 事实2
...

RESEARCH_GAPS:
- 空白1: 描述 + 为什么重要
- 空白2: 描述 + 为什么重要
...

PROMISING_DIRECTIONS:
- 方向1: 描述 + 可能的方法 + 预期贡献
- 方向2: ...
```

只输出分析结果，不要输出其他内容。"""


def _build_react_thought_prompt(topic: str, react_context: str) -> str:
    return f"""你是一位科研探索专家。你需要判断当前对研究主题的了解是否足够，以及下一步应该检索什么。

研究主题: {topic}

{react_context}

## 任务
1. 评估: 当前信息是否足够生成高质量的研究假说？
2. 如果不够: 还需要了解什么具体信息？请生成2-3个精确的检索查询
3. 如果足够: 输出 SUFFICIENT

请按以下格式输出：
```
ASSESSMENT: <brief assessment of current knowledge state>

NEED_MORE: <YES|NO>

SEARCH_QUERIES:
- 具体检索查询1
- 具体检索查询2
- 具体检索查询3

RATIONALE: <why these queries are needed>
```

只输出以上格式的内容。"""


def _build_gap_deepening_prompt(topic: str, all_papers: list[dict], react_context: str) -> str:
    return f"""你是一位科研空白分析专家。基于所有收集到的文献，深入比较分析研究空白。

研究主题: {topic}

{react_context}

## 所有论文标题和摘要
{json.dumps([{'title': p.get('title',''), 'abstract': (p.get('abstract','') or '')[:300], 'year': p.get('year',''), 'citations': p.get('citations',0)} for p in all_papers[:40]], indent=2, ensure_ascii=False)}

## 任务
系统地比较这些论文，找出：
1. **未被探索的组合**: 哪些方法组合还没有被尝试过？
2. **未解决的问题**: 哪些问题被多次提及但没有解决方案？
3. **矛盾发现**: 哪些论文的结果/结论存在不一致？
4. **可迁移的方法**: 哪些方法可以从其他领域迁移过来？

请按以下格式输出：
```
UNEXPLORED_COMBINATIONS:
- 组合1: 方法A + 方法B → 可能解决什么问题
...

UNSOLVED_PROBLEMS:
- 问题1: 描述 + 被哪些论文提及
...

CONTRADICTIONS:
- 矛盾1: 论文X说...但论文Y说...
...

METHOD_TRANSFER:
- 迁移1: 领域A的方法X → 可用于本领域解决问题Y
...
```

只输出分析结果。"""


def _build_hypothesis_gen_prompt(topic: str, react_context: str) -> str:
    return f"""你是一位资深科研假说生成专家。基于所有文献分析和空白识别，生成具体的研究假说。

研究主题: {topic}

{react_context}

## 任务
生成3-5个具体、可验证、有创新性的研究假说。每个假说必须：

1. **明确具体**: 包含具体的方法、场景、预期效果
2. **文献支撑**: 明确说明基于哪些已有工作
3. **可验证**: 可以用计算实验或实验验证
4. **有创新**: 不是已有工作的简单组合

请按以下JSON格式输出（严格JSON格式，不要输出其他内容）：
```json
{{
  "hypotheses": [
    {{
      "id": "H1",
      "title": "假说标题（学术风格）",
      "description": "详细描述（2-3段）",
      "method_outline": "提出的方法概述",
      "rationale": "为什么这个假说值得研究（基于文献空白）",
      "key_references": ["支撑文献1", "支撑文献2", "支撑文献3"],
      "expected_outcome": "预期结果",
      "feasibility": "可行性评估（计算资源、数据、时间）",
      "novelty_score": 8,
      "impact_score": 7
    }}
  ],
  "selected_hypothesis": {{
    "id": "H1",
    "justification": "为什么选择这个假说作为首选"
  }},
  "gap_coverage": "这些假说如何覆盖了已识别的研究空白"
}}
```

评分标准 (1-10):
- novelty_score: 与已有工作的差异化程度
- impact_score: 如果成功，对领域的推动程度

只输出JSON，不要输出其他内容。"""


# ======================================================================
# Search action (called during ReAct loop)
# ======================================================================

def _execute_search(query: str, max_results: int = 15, save_json: Path | None = None) -> list[dict]:
    """Execute a multi-source search and return merged results."""
    logger.info("Searching: '%s' ...", query[:80])
    all_papers = []

    for fn, name in [(search_arxiv, "arXiv"), (search_semantic_scholar, "S2"), (search_openalex, "OpenAlex")]:
        try:
            results = fn(query, max_results)
            all_papers.extend(results)
            logger.info("  %s: %d results", name, len(results))
        except Exception as e:
            logger.warning("  %s failed: %s", name, e)
        time.sleep(1)  # Be polite

    merged = merge_results(all_papers)
    logger.info("  Merged: %d unique papers", len(merged))

    if save_json:
        papers_to_json(merged, save_json)

    return merged


# ======================================================================
# claude -p caller
# ======================================================================

def _call_claude(prompt: str, timeout: int = 300) -> str:
    """Call claude -p and return stdout."""
    cmd = [CLAUDE_CMD, "-p", "--model", CLAUDE_MODEL, "--output-format", "text"]
    logger.info("Calling Claude Code ...")
    result = subprocess.run(
        cmd + [prompt], capture_output=True, text=True,
        timeout=timeout, cwd=str(PROJECT_ROOT), env={**os.environ},
    )
    output = result.stdout or ""
    if result.returncode != 0 and not output:
        logger.warning("claude exited %d: %s", result.returncode, result.stderr[:200])
        return result.stderr or ""
    return output


def _parse_json_block(text: str) -> dict:
    """Extract and parse a JSON block from LLM output."""
    import re
    m = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Try bare JSON
    m = re.search(r'\{.*"hypotheses".*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {}


def _parse_known_facts(text: str) -> list[str]:
    """Extract facts from LLM output."""
    import re
    facts = []
    in_section = False
    for line in text.split("\n"):
        if "KNOWN_FACTS" in line.upper():
            in_section = True
            continue
        if in_section:
            if line.strip().startswith("-"):
                facts.append(line.strip()[1:].strip())
            elif line.strip() and not line.strip().startswith("#") and not line.strip().startswith("```"):
                if any(kw in line.upper() for kw in ["RESEARCH_GAPS", "PROMISING_DIRECTIONS", "ASSESSMENT"]):
                    break
    return facts


def _parse_gaps(text: str) -> list[str]:
    """Extract research gaps from LLM output."""
    import re
    gaps = []
    in_section = False
    for line in text.split("\n"):
        if "RESEARCH_GAPS" in line.upper() or "UNSOLVED" in line.upper():
            in_section = True
            continue
        if in_section:
            if line.strip().startswith("-") or line.strip().startswith("*"):
                gaps.append(line.strip()[1:].strip())
            elif line.strip() and not line.strip().startswith("#") and not line.strip().startswith("```"):
                if any(kw in line.upper() for kw in ["PROMISING_DIRECTIONS", "UNEXPLORED", "CONTRADICTIONS",
                                                       "METHOD_TRANSFER", "ASSESSMENT", "SEARCH_QUERIES"]):
                    break
    return gaps


def _parse_need_more(text: str) -> tuple[bool, list[str]]:
    """Parse NEED_MORE and SEARCH_QUERIES from ReAct thought output."""
    need_more = "NEED_MORE: YES" in text.upper() or "NEED_MORE:YES" in text.upper()
    queries = []
    in_queries = False
    for line in text.split("\n"):
        if "SEARCH_QUERIES" in line.upper():
            in_queries = True
            continue
        if in_queries:
            if line.strip().startswith("-"):
                q = line.strip()[1:].strip()
                if q:
                    queries.append(q)
            elif line.strip() and not line.strip().startswith("#"):
                if "RATIONALE" in line.upper():
                    break
    return need_more, queries


# ======================================================================
# HypothesisEngine — main orchestrator
# ======================================================================

class HypothesisEngine:
    """ReAct-based hypothesis generation engine."""

    def __init__(
        self,
        topic: str,
        literature_dir: Path,
        work_dir: Path,
        max_react_rounds: int = 3,
        top_k_pdfs: int = 5,
        max_papers_per_search: int = 15,
    ):
        self.topic = topic
        self.literature_dir = Path(literature_dir)
        self.work_dir = Path(work_dir)
        self.max_react_rounds = max_react_rounds
        self.top_k_pdfs = top_k_pdfs
        self.max_papers_per_search = max_papers_per_search

        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.state = ReactState(work_dir)
        self.metadata_dir = self.work_dir / "metadata"
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
        self.pdf_dir = self.work_dir / "pdfs"
        self.pdf_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> dict:
        """Execute all phases and return the final hypotheses."""
        if self.state.data["phase"] == "DONE":
            logger.info("Hypothesis generation already completed. Loading results.")
            return self._load_final_output()

        # Phase 1: Initial Analysis
        if self.state.data["phase"] == "INITIAL_ANALYSIS":
            self._phase_initial_analysis()

        # Phase 2: ReAct Loop
        if self.state.data["phase"] in ("REACT_LOOP", "INITIAL_ANALYSIS"):
            self._phase_react_loop()

        # Phase 3: PDF Deep-Read
        if self.state.data["phase"] in ("PDF_DEEP_READ", "REACT_LOOP"):
            self._phase_pdf_deep_read()

        # Phase 4: Hypothesis Generation
        if self.state.data["phase"] in ("HYPOTHESIS_GEN", "PDF_DEEP_READ"):
            self._phase_hypothesis_gen()

        self.state.set_phase("DONE")
        return self._load_final_output()

    # ---- Phase 1 ----

    def _phase_initial_analysis(self):
        """Analyze existing literature for gaps."""
        logger.info("=" * 50)
        logger.info("Phase 1: Initial Literature Analysis")
        logger.info("=" * 50)

        lit_review_path = self.literature_dir / "literature_review.md"
        lit_review = lit_review_path.read_text() if lit_review_path.exists() else ""

        # Load paper metadata from JSON if available
        papers_json_path = self.literature_dir / "papers_metadata.json"
        if papers_json_path.exists():
            papers = load_papers_json(papers_json_path)
            papers_json = papers_json_path.read_text()[:15000]
            logger.info("Loaded %d papers from metadata JSON", len(papers))
        else:
            # Fallback: extract paper info from literature_review.md
            logger.warning("No papers_metadata.json found — extracting from literature_review.md")
            papers = _extract_papers_from_markdown(lit_review)
            papers_json = json.dumps(papers, indent=2, ensure_ascii=False)[:15000]
            logger.info("Extracted %d papers from markdown", len(papers))

        # Load into state
        if not self.state.data["all_papers"] and papers:
            self.state.data["all_papers"] = papers
            self.state.save()

        prompt = _build_initial_analysis_prompt(self.topic, lit_review, papers_json)
        output = _call_claude(prompt)

        facts = _parse_known_facts(output)
        gaps = _parse_gaps(output)

        if facts:
            self.state.add_facts(facts)
        if gaps:
            self.state.add_gaps(gaps)

        # Save analysis output
        (self.work_dir / "initial_analysis.md").write_text(output)

        self.state.set_phase("REACT_LOOP")
        logger.info("Phase 1 complete: %d facts, %d gaps identified", len(facts), len(gaps))

    # ---- Phase 2 ----

    def _phase_react_loop(self):
        """Iteratively search for more papers based on knowledge gaps."""
        logger.info("=" * 50)
        logger.info("Phase 2: ReAct Loop (max %d rounds)", self.max_react_rounds)
        logger.info("=" * 50)

        for round_num in range(self.max_react_rounds):
            self.state.data["round"] = round_num + 1
            self.state.save()

            logger.info("--- ReAct Round %d/%d ---", round_num + 1, self.max_react_rounds)

            # 2a: Deepen gap analysis with accumulated papers
            if round_num == 0:
                deepen_prompt = _build_gap_deepening_prompt(
                    self.topic, self.state.data["all_papers"], self.state.get_context()
                )
                deepen_output = _call_claude(deepen_prompt)
                new_gaps = _parse_gaps(deepen_output)
                if new_gaps:
                    self.state.add_gaps(new_gaps)
                (self.work_dir / f"gap_analysis_r{round_num+1}.md").write_text(deepen_output)

            # 2b: THOUGHT — what do we still need?
            thought_prompt = _build_react_thought_prompt(self.topic, self.state.get_context())
            thought_output = _call_claude(thought_prompt)
            (self.work_dir / f"react_thought_r{round_num+1}.md").write_text(thought_output)

            need_more, queries = _parse_need_more(thought_output)
            logger.info("Need more info: %s, queries: %d", need_more, len(queries))

            if not need_more or not queries:
                logger.info("Sufficient knowledge gathered. Exiting ReAct loop.")
                break

            # 2c: ACT — execute searches
            for q in queries[:3]:  # Max 3 queries per round
                metadata_path = self.metadata_dir / f"search_r{round_num+1}_{_slugify_query(q)}.json"
                papers = _execute_search(q, self.max_papers_per_search, save_json=metadata_path)
                insights = f"Found {len(papers)} papers for query: {q}"
                self.state.add_search(q, papers, insights)
                time.sleep(3)  # Be extra polite between queries

            logger.info("Round %d complete. Total papers: %d",
                        round_num + 1, len(self.state.data["all_papers"]))

        # After ReAct loop, save all accumulated papers to JSON
        all_papers_json = self.work_dir / "all_papers.json"
        papers_to_json(self.state.data["all_papers"], all_papers_json)

        self.state.set_phase("PDF_DEEP_READ")
        logger.info("Phase 2 complete. Total unique papers: %d", len(self.state.data["all_papers"]))

    # ---- Phase 3 ----

    def _score_papers_relevance(self, papers: list[dict]) -> dict[int, float]:
        """Batch LLM call: score ALL papers' relevance to the research topic.
        Returns dict mapping list index → relevance score (1-10).
        Falls back to uniform 5.0 on any failure."""
        if not papers:
            return {}

        logger.info("Scoring %d papers for relevance to: %s", len(papers), self.topic[:60])

        # Build compact paper list for the prompt
        paper_entries = []
        for i, p in enumerate(papers):
            title = p.get("title", "")[:150]
            abstract = (p.get("abstract") or "")[:500]
            citations = p.get("citations") or 0
            source = p.get("source", "")
            paper_entries.append(
                f"[{i}] {title}\n"
                f"    来源: {source} | 引用: {citations}\n"
                f"    摘要: {abstract}\n"
            )

        prompt = f"""你是一位科研文献相关性评估专家。

研究主题: {self.topic}

请评估以下每篇论文与上述研究主题的相关性。基于标题和摘要判断，考虑：
- 是否直接针对该主题的核心问题
- 方法或场景是否与该主题密切相关
- 实验设置是否适用于该主题

论文列表（共{len(papers)}篇）：

{chr(10).join(paper_entries)}

请对每篇论文从1-10打分：
- 10 = 完全相关，直接解决该主题的核心问题
- 7-9 = 高度相关，方法或场景密切相关
- 4-6 = 中等相关，有部分重叠但非直接针对
- 1-3 = 低相关，仅边缘涉及或完全不相关

以如下JSON格式输出（严格JSON，不要其他内容）：
```json
{{
  "relevance_scores": [
    {{"index": 0, "score": 9, "reason": "直接提出解决该核心问题的新方法..."}},
    {{"index": 1, "score": 6, "reason": "虽然涉及相关技术但聚焦于..."}}
  ]
}}
```

只输出JSON，不要其他内容。"""

        try:
            output = _call_claude(prompt, timeout=300)
            parsed = _parse_json_block(output)
            scores = parsed.get("relevance_scores", [])
            if not scores:
                logger.warning("Relevance scoring returned empty. Using uniform scores.")
                return {i: 5.0 for i in range(len(papers))}

            result = {}
            for entry in scores:
                idx = entry.get("index", -1)
                score = float(entry.get("score", 5))
                result[idx] = min(10.0, max(1.0, score))

            # Fill any missing indices
            for i in range(len(papers)):
                if i not in result:
                    result[i] = 5.0

            logger.info("Relevance scores: min=%.1f max=%.1f avg=%.1f",
                        min(result.values()), max(result.values()),
                        sum(result.values()) / max(len(result), 1))
            return result

        except Exception as e:
            logger.warning("Relevance scoring failed: %s. Using uniform scores.", e)
            return {i: 5.0 for i in range(len(papers))}

    def _select_top_k_papers(
        self, papers: list[dict], relevance_scores: dict[int, float], top_k: int,
    ) -> list[int]:
        """Compute combined score (relevance × 0.5 + norm_citations × 0.3 + source_quality × 0.2)
        and return indices of top-K papers. Logs the selection for transparency."""
        n = len(papers)
        if n == 0:
            return []

        # Min-max normalize citations to [0, 10]
        citations = [p.get("citations") or 0 for p in papers]
        max_cit = max(citations) if citations else 1
        min_cit = min(citations) if citations else 0
        cit_range = max_cit - min_cit if max_cit != min_cit else 1

        def _source_quality(p: dict) -> float:
            aid = p.get("arxiv_id", "")
            if _is_valid_arxiv_id(aid):
                return 10.0
            if _looks_like_acl_id(aid):
                return 8.0
            if p.get("open_access_pdf_url"):
                return 6.0
            return 3.0

        scored = []
        for i, p in enumerate(papers):
            rel = relevance_scores.get(i, 5.0)
            norm_cit = ((p.get("citations") or 0) - min_cit) / cit_range * 10.0
            src_q = _source_quality(p)
            combined = rel * 0.5 + norm_cit * 0.3 + src_q * 0.2
            scored.append((combined, i))

        scored.sort(key=lambda x: x[0], reverse=True)
        top_indices = [idx for _, idx in scored[:top_k]]

        # Transparency log
        logger.info("Top-%d papers by combined score (relevance × 0.5 + citation × 0.3 + source × 0.2):",
                    min(top_k, len(top_indices)))
        for rank, (combined, idx) in enumerate(scored[:top_k], 1):
            p = papers[idx]
            rel = relevance_scores.get(idx, 5.0)
            cit = p.get("citations") or 0
            src = _source_quality(p)
            logger.info("  %d. [idx=%d] score=%.2f (rel=%.1f cit=%d src=%.0f) %s",
                        rank, idx, combined, rel, cit, src, p.get("title", "")[:80])

        return top_indices

    def _extract_paper_evidence(self, paper: dict, full_text: str) -> dict | None:
        """LLM extracts chapter summaries + structured experimental evidence from full text.
        Saves result to {evidence_dir}/{paper_id}_evidence.json.
        Returns the evidence dict on success, None on failure."""
        if not full_text or len(full_text.strip()) < 100:
            logger.warning("Text too short for evidence extraction: %d chars", len(full_text))
            return None

        paper_id = paper.get("paper_id", _paper_fingerprint(paper))
        title = paper.get("title", "")
        authors = ", ".join((paper.get("authors") or [])[:5])
        safe_title = title.replace('"', '\\"')

        # Truncate text to 35000 chars to stay within reasonable context
        text_snippet = full_text[:35000]

        prompt = f"""你是一位科研论文结构化分析专家。请分析以下论文全文，提取关键章节和实验证据。

研究主题: {self.topic}

论文标题: {safe_title}
论文ID: {paper_id}
作者: {authors}

## 论文全文内容
{text_snippet}

## 任务

### 任务1: 提取关键章节核心内容
提炼以下章节的核心内容（用200-500字中文总结）：
1. **方法/方法论** (Method/Approach): 论文的核心方法是什么？有什么关键技术创新？
2. **实验设置** (Experimental Setup): 使用了什么数据集？评测指标是什么？和哪些基线方法对比？
3. **主要结果** (Key Results): 主要实验发现是什么？有哪些定量结果和关键结论？

### 任务2: 提取结构化实验证据
从论文中提取以下量化信息（缺失的字段用空数组[]表示）：
- **primary_metrics**: 主要评测指标（指标名、数值、基线值、改进幅度、数据集名）
- **ablation_studies**: 消融实验（组件名、对性能的影响、具体指标变化）
- **statistical_tests**: 统计显著性检验（检验方法、p值、结论）
- **baseline_comparisons**: 与基线方法的对比（基线名、双方分数、指标名）
- **key_claims**: 作者的核心声明或结论（每条一句话）
- **limitations**: 作者指出的局限性或未来工作方向

请以如下严格JSON格式输出（缺失字段用空数组，不要输出其他内容）：
```json
{{
  "paper_id": "{paper_id}",
  "title": "{safe_title}",
  "chapters": {{
    "method": "核心方法描述...",
    "experimental_setup": "实验设置描述...",
    "key_results": "主要结果描述..."
  }},
  "evidence": {{
    "primary_metrics": [
      {{"metric_name": "指标名", "value": "数值", "baseline": "基线值", "improvement": "改进幅度", "dataset": "数据集名"}}
    ],
    "ablation_studies": [
      {{"component": "组件名", "impact": "影响描述", "metric_change": "指标变化"}}
    ],
    "statistical_tests": [
      {{"test_name": "检验方法", "p_value": "p值", "conclusion": "结论"}}
    ],
    "baseline_comparisons": [
      {{"baseline_name": "基线名", "scores": "双方分数", "metric": "指标"}}
    ],
    "key_claims": ["核心声明1", "核心声明2"],
    "limitations": ["局限1", "局限2"]
  }}
}}
```
只输出JSON。"""

        try:
            output = _call_claude(prompt, timeout=300)
            parsed = _parse_json_block(output)
            if not parsed:
                logger.warning("Failed to parse evidence JSON for: %s", title[:60])
                return None
            if "chapters" not in parsed and "evidence" not in parsed:
                logger.warning("Evidence JSON missing required keys for: %s", title[:60])
                return None

            # Ensure evidence sub-fields exist with defaults
            parsed.setdefault("paper_id", paper_id)
            parsed.setdefault("title", title)
            parsed.setdefault("chapters", {"method": "", "experimental_setup": "", "key_results": ""})
            ev = parsed.setdefault("evidence", {})
            for field in ["primary_metrics", "ablation_studies", "statistical_tests",
                          "baseline_comparisons", "key_claims", "limitations"]:
                ev.setdefault(field, [])

            # Save to disk
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            safe_name = paper_id.replace(":", "_").replace("/", "_")
            ev_path = self.evidence_dir / f"{safe_name}_evidence.json"
            ev_path.write_text(json.dumps(parsed, indent=2, ensure_ascii=False))
            logger.info("Evidence saved: %s", ev_path.name)

            return parsed

        except Exception as e:
            logger.warning("Evidence extraction error for %s: %s", title[:60], e)
            return None

    def _generate_evidence_summary(self, evidence_list: list[dict]) -> dict:
        """Compile all papers' evidence into a single summary and write to disk."""
        summary = {
            "topic": self.topic,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_papers": len(evidence_list),
            "papers": evidence_list,
        }
        path = self.work_dir / "evidence_summary.json"
        path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
        logger.info("Evidence summary saved: %s (%d papers)", path, len(evidence_list))
        return summary

    def _build_deep_read_compare_prompt(self, chapters_data: list[dict]) -> str:
        """Build the cross-paper comparison prompt from extracted chapter content."""
        # Build combined chapter content
        sections = []
        for ch in chapters_data:
            sections.append(
                f"### {ch.get('title', 'Unknown')} ({ch.get('paper_id', '?')})\n\n"
                f"**方法/方法论**:\n{ch.get('method', '未提取')}\n\n"
                f"**实验设置**:\n{ch.get('experimental_setup', '未提取')}\n\n"
                f"**主要结果**:\n{ch.get('key_results', '未提取')}\n\n---\n"
            )
        combined = "\n".join(sections)

        return f"""你是一位科研文献深度分析专家。请仔细阅读以下高相关度论文的提取章节内容，进行深度对比分析。

研究主题: {self.topic}

{self.state.get_context()}

## 论文提取章节内容

{combined}

## 任务
请对这些论文进行深度对比分析：
1. **方法对比**: 每篇论文的核心方法是什么？关键区别和联系？
2. **实验对比**: 各论文的实验设置、使用数据集、评测指标的异同
3. **主要发现**: 哪些结论是一致的？哪些存在矛盾或不一致？
4. **空白确认**: 基于全文阅读，之前识别的空白是否依然成立？是否有新发现的空白？
5. **假说验证**: 哪些初步假说方向被全文内容支持或否定？

请按以下格式输出：
```
METHOD_COMPARISON:
| 论文 | 核心方法 | 关键创新 | 局限性 |
| ... | ... | ... | ... |

CONSENSUS:
- 一致结论1
- 一致结论2

CONTRADICTIONS:
- 矛盾1: 论文X说...但论文Y说...

GAP_CONFIRMATION:
- 空白1: 确认/否定/修正 → 原因

HYPOTHESIS_SUPPORT:
- 假说方向1: 支持/反对 → 全文证据
```

只输出以上格式的分析内容。"""

    def _phase_pdf_deep_read(self):
        """Phase 3: Relevance-weighted scoring → download → evidence extraction → compare."""
        logger.info("=" * 50)
        logger.info("Phase 3: PDF Deep-Read (top %d, relevance-weighted)", self.top_k_pdfs)
        logger.info("=" * 50)

        all_papers = self.state.data["all_papers"]
        if not all_papers:
            logger.warning("No papers collected. Skipping PDF deep-read.")
            self.state.set_phase("HYPOTHESIS_GEN")
            return

        # ---- Step 1: Count downloadable papers (for logging) ----
        arxiv_papers = [p for p in all_papers
                        if p.get("arxiv_id") and _is_valid_arxiv_id(p["arxiv_id"])]
        acl_papers = [p for p in all_papers
                      if p.get("arxiv_id") and _looks_like_acl_id(p["arxiv_id"])]
        oa_papers = [p for p in all_papers if p.get("open_access_pdf_url")]
        downloadable = len(arxiv_papers) + len(acl_papers) + len(
            [p for p in oa_papers if p not in arxiv_papers and p not in acl_papers]
        )
        logger.info("Papers total: %d, arXiv: %d, ACL: %d, open-access: %d → downloadable: %d",
                    len(all_papers), len(arxiv_papers), len(acl_papers), len(oa_papers), downloadable)

        if not arxiv_papers and not acl_papers and not oa_papers:
            logger.warning("No papers with known downloadable sources — skipping PDF download.")
            logger.info("(Papers from paywalled sources are not directly downloadable)")
            logger.info("Proceeding with abstract-based deep analysis instead...")
            self.state.data["deep_read_notes"] = (
                f"No downloadable PDFs available ({len(all_papers)} papers from paywalled sources). "
                "Deep analysis based on abstracts and metadata only."
            )
            self.state.save()
            self.state.set_phase("HYPOTHESIS_GEN")
            return

        # ---- Step 2: Relevance scoring (NEW) ----
        relevance_scores = self._score_papers_relevance(all_papers)

        # ---- Step 3: Combined score → select top-K (NEW) ----
        top_indices = self._select_top_k_papers(all_papers, relevance_scores, self.top_k_pdfs)
        if not top_indices:
            logger.warning("Top-K selection returned empty. Falling back to citation-based sort.")
            # Fallback: use the existing download_top_pdfs function
            top_indices = list(range(min(len(all_papers), self.top_k_pdfs)))

        top_papers = [all_papers[i] for i in top_indices]

        # ---- Step 4: Download PDFs for selected papers ----
        pdfs = []
        for p in top_papers:
            path = download_paper_pdf(p, self.pdf_dir)
            if path:
                pdfs.append(path)
                self.state.add_pdf(str(path))
            time.sleep(1)  # Be polite to servers

        logger.info("Downloaded %d/%d PDFs", len(pdfs), len(top_papers))

        # ---- Step 5: Full text extraction + evidence extraction (NEW) ----
        self.evidence_dir = self.work_dir / "evidence"
        self.evidence_dir.mkdir(parents=True, exist_ok=True)

        evidence_list = []
        chapters_for_compare = []  # For cross-paper comparison

        for pdf_path in pdfs:
            # Map filename back to paper metadata
            stem = Path(pdf_path).stem
            paper = None
            for p in top_papers:
                fp = _paper_fingerprint(p).replace(":", "_").replace("/", "_")
                if fp == stem:
                    paper = p
                    break
            if not paper:
                # Try matching by arxiv_id in filename
                for p in top_papers:
                    aid = p.get("arxiv_id", "")
                    if aid and aid == stem:
                        paper = p
                        break
            if not paper:
                logger.warning("Cannot match PDF %s to any paper, skipping", pdf_path.name)
                # Still extract text for comparison as fallback
                text = extract_pdf_text(Path(pdf_path))
                if text and len(text.strip()) >= 100:
                    chapters_for_compare.append({
                        "paper_id": stem,
                        "title": pdf_path.stem,
                        "method": text[:3000],
                        "experimental_setup": "",
                        "key_results": "",
                    })
                continue

            # Extract full text (NO 8000-char truncation)
            full_text = extract_pdf_text(Path(pdf_path))
            if not full_text or len(full_text.strip()) < 100:
                logger.warning("Empty or near-empty text from: %s", paper.get("title", "")[:60])
                chapters_for_compare.append({
                    "paper_id": paper.get("paper_id", stem),
                    "title": paper.get("title", ""),
                    "method": (full_text or "")[:3000],
                    "experimental_setup": "",
                    "key_results": "",
                })
                continue

            logger.info("Extracting evidence from: %s (%d chars)",
                        paper.get("title", "")[:80], len(full_text))
            evidence = self._extract_paper_evidence(paper, full_text)

            if evidence:
                evidence_list.append(evidence)
                chapters = evidence.get("chapters", {})
                chapters_for_compare.append({
                    "paper_id": paper.get("paper_id", stem),
                    "title": paper.get("title", ""),
                    "method": chapters.get("method", ""),
                    "experimental_setup": chapters.get("experimental_setup", ""),
                    "key_results": chapters.get("key_results", ""),
                })
            else:
                # Fallback: use truncated raw text for comparison
                chapters_for_compare.append({
                    "paper_id": paper.get("paper_id", stem),
                    "title": paper.get("title", ""),
                    "method": full_text[:3000],
                    "experimental_setup": "",
                    "key_results": "",
                })

        # ---- Step 6: Generate evidence_summary.json (NEW) ----
        if evidence_list:
            self._generate_evidence_summary(evidence_list)

        # ---- Step 7: Cross-paper comparison using extracted chapters (MODIFIED) ----
        if chapters_for_compare:
            compare_prompt = self._build_deep_read_compare_prompt(chapters_for_compare)
            deep_read_output = _call_claude(compare_prompt)
            self.state.set_deep_read_notes(deep_read_output)
            (self.work_dir / "deep_read_analysis.md").write_text(deep_read_output)
        else:
            logger.warning("No extracted content for cross-paper comparison.")
            self.state.data["deep_read_notes"] = "PDF deep-read: text extraction yielded no usable content."
            self.state.save()

        self.state.set_phase("HYPOTHESIS_GEN")
        logger.info("Phase 3 complete. Deep-read %d papers, evidence extracted from %d.",
                    len(pdfs), len(evidence_list))

    # ---- Phase 4 ----

    def _phase_hypothesis_gen(self):
        """Generate concrete research hypotheses."""
        logger.info("=" * 50)
        logger.info("Phase 4: Hypothesis Generation")
        logger.info("=" * 50)

        prompt = _build_hypothesis_gen_prompt(self.topic, self.state.get_context())
        output = _call_claude(prompt, timeout=600)
        (self.work_dir / "hypothesis_raw_output.md").write_text(output)

        hypotheses = _parse_json_block(output)
        if hypotheses:
            self.state.set_hypotheses(hypotheses.get("hypotheses", []))
            # Save final structured output
            final_output = {
                "topic": self.topic,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "total_papers_analyzed": len(self.state.data["all_papers"]),
                "search_rounds": len(self.state.data["search_history"]),
                "pdfs_deep_read": len(self.state.data["papers_downloaded"]),
                "known_facts": self.state.data["known_facts"],
                "identified_gaps": self.state.data["identified_gaps"],
                **hypotheses,
            }
            (self.work_dir / "hypothesis_output.json").write_text(
                json.dumps(final_output, indent=2, ensure_ascii=False)
            )
        else:
            # Fallback: save the raw output
            final_output = {
                "topic": self.topic,
                "raw_output": output,
                "total_papers_analyzed": len(self.state.data["all_papers"]),
                "known_facts": self.state.data["known_facts"],
                "identified_gaps": self.state.data["identified_gaps"],
            }
            (self.work_dir / "hypothesis_output.json").write_text(
                json.dumps(final_output, indent=2, ensure_ascii=False)
            )

        logger.info("Phase 4 complete. %d hypotheses generated.",
                    len(final_output.get("hypotheses", [])))

    def _load_final_output(self) -> dict:
        """Load the final hypothesis output."""
        output_path = self.work_dir / "hypothesis_output.json"
        if output_path.exists():
            return json.loads(output_path.read_text())
        return {}


# ======================================================================
# Utilities
# ======================================================================

def _extract_papers_from_markdown(md_text: str) -> list[dict]:
    """Extract paper metadata from literature_review.md as fallback."""
    import re
    papers = []
    # Pattern: "### N. Title" followed by authors/year/abstract
    sections = re.split(r'\n### \d+\. ', md_text)
    for section in sections[1:]:  # skip header
        lines = section.strip().split('\n')
        if not lines:
            continue
        title = lines[0].strip()
        abstract = ""
        year = ""
        for line in lines[1:]:
            # Authors line: "**Authors** — year | ..."
            m = re.search(r'\*\*(.+?)\*\*.*?(\d{4})', line)
            if m:
                year = m.group(2)
            # Abstract is the paragraph after "Link" or authors line
            if len(line) > 50 and not line.startswith('**') and not line.startswith('[') and not line.startswith('---'):
                abstract = line[:500]
                break
        if title:
            papers.append({
                "title": title,
                "year": year,
                "abstract": abstract,
                "authors": [],
                "url": "",
                "arxiv_id": "",
                "source": "literature_review",
                "citations": 0,
                "paper_id": f"title:{hashlib.md5(title.lower().encode()).hexdigest()[:12]}",
            })
    return papers


def _slugify_query(query: str) -> str:
    """Short safe filename from a search query."""
    import re
    s = query.strip().lower().replace(" ", "_")[:40]
    s = re.sub(r'[^a-z0-9_-]', '', s)
    return s.strip('_-')


# ======================================================================
# CLI
# ======================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="ReAct-based hypothesis generation engine for SLAIResearch"
    )
    parser.add_argument("--topic", "-t", required=True, help="Research topic")
    parser.add_argument("--literature-dir", "-l", required=True, help="Literature directory (from stage 1)")
    parser.add_argument("--work-dir", "-w", required=True, help="Working directory for hypothesis artifacts")
    parser.add_argument("--max-react-rounds", type=int, default=HYPOTHESIS_MAX_REACT_ROUNDS,
                        help="Max ReAct search rounds")
    parser.add_argument("--top-k-pdfs", type=int, default=HYPOTHESIS_TOP_K_PDFS,
                        help="Number of PDFs to download for deep reading")
    parser.add_argument("--max-papers", type=int, default=HYPOTHESIS_MAX_PAPERS,
                        help="Max papers per search")
    args = parser.parse_args()

    engine = HypothesisEngine(
        topic=args.topic,
        literature_dir=Path(args.literature_dir),
        work_dir=Path(args.work_dir),
        max_react_rounds=args.max_react_rounds,
        top_k_pdfs=args.top_k_pdfs,
        max_papers_per_search=args.max_papers,
    )

    result = engine.run()

    hypotheses = result.get("hypotheses", [])
    if hypotheses:
        print(f"\n{'='*60}")
        print(f"  Hypothesis Generation Complete")
        print(f"  Topic: {args.topic}")
        print(f"  Papers analyzed: {result.get('total_papers_analyzed', 0)}")
        print(f"  Search rounds: {result.get('search_rounds', 0)}")
        print(f"  PDFs deep-read: {result.get('pdfs_deep_read', 0)}")
        print(f"  Hypotheses generated: {len(hypotheses)}")
        print(f"{'='*60}")
        print()

        for h in hypotheses:
            print(f"  [{h.get('id', '?')}] {h.get('title', 'Untitled')}")
            print(f"       Novelty: {h.get('novelty_score', '?')}/10  Impact: {h.get('impact_score', '?')}/10")
            print(f"       {h.get('description', '')[:150]}...")
            print()

        selected = result.get("selected_hypothesis", {})
        if selected:
            print(f"  ★ Selected: [{selected.get('id', '?')}] — {selected.get('justification', '')[:200]}")
        print(f"\n  Full output: {args.work_dir}/hypothesis_output.json")
    else:
        print(f"Hypothesis generation completed but no structured hypotheses were extracted.")
        print(f"Check raw output in: {args.work_dir}/")


if __name__ == "__main__":
    main()
