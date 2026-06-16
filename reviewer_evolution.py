#!/usr/bin/env python3
"""
审稿员进化系统 — 管理审稿员 Prompt 的创建、更新和自我迭代。

审稿员 Prompt 集中存储在 reviewer_prompts.json 中。
每次外审结束后，系统分析外审意见，自动优化内部审稿员的 Prompt，
实现系统的自我迭代升级，不断汲取外部审稿意见的能力。

用法:
    # 列出所有审稿员
    python reviewer_evolution.py list

    # 添加新审稿员
    python reviewer_evolution.py add --name "Statistical Expert" \\
        --role "统计显著性专家" --focus "p-value, effect size, multiple testing"

    # 从外审意见中学习，优化某个审稿员
    python reviewer_evolution.py evolve --reviewer "Methodology Expert" \\
        --from-external review/round_001/external.md

    # 自动分析所有外审，批量优化审稿员
    python reviewer_evolution.py auto-evolve --workspace <project_dir>

    # 查看审稿员 Prompt
    python reviewer_evolution.py show "Methodology Expert"
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent
PROMPTS_FILE = PROJECT_ROOT / "reviewer_prompts.json"


class ReviewerEvolution:
    """管理审稿员 Prompt 的生命周期。"""

    def __init__(self, prompts_file: Path = PROMPTS_FILE):
        self.prompts_file = prompts_file
        if not self.prompts_file.exists():
            self._init_default()

    def _init_default(self):
        """如果不存在，从 internal_review.py 加载默认审稿员。"""
        print("reviewer_prompts.json 不存在，请先运行 extract 从 internal_review.py 提取")
        sys.exit(1)

    # ── 读取 ──

    def load(self) -> dict:
        """加载所有审稿员。"""
        return json.loads(self.prompts_file.read_text())

    def save(self, data: dict):
        """保存审稿员数据。"""
        data["meta"]["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.prompts_file.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def get(self, name: str) -> Optional[dict]:
        """获取特定审稿员。"""
        data = self.load()
        for r in data["reviewers"]:
            if r["name"].lower() == name.lower():
                return r
        return None

    def list(self) -> list[dict]:
        """列出所有审稿员。"""
        return self.load()["reviewers"]

    # ── 添加 ──

    def add(self, name: str, role: str, focus: str, prompt: str = "",
            based_on: str = "") -> dict:
        """添加一个新审稿员。如果 based_on 指定，克隆已有审稿员的 prompt 作为基础。"""
        data = self.load()

        # 检查是否已存在
        for r in data["reviewers"]:
            if r["name"].lower() == name.lower():
                print(f"审稿员 '{name}' 已存在")
                return r

        # 基于已有审稿员克隆
        base_prompt = ""
        if based_on:
            existing = self.get(based_on)
            if existing:
                base_prompt = existing.get("prompt", "")
                focus = focus or existing.get("focus", "")

        # 默认 prompt 模板
        if not prompt and not base_prompt:
            prompt = f"""You are a senior reviewer specializing in {focus}.
Evaluate the paper focusing on {focus}. Use format:
[PROBLEM] specific issue → [IMPACT] why it matters → [FIX] concrete suggestion

Output:
## {name} Review
### Strengths (3-5)
### Weaknesses (3-5)
### Detailed Issues
### Score (1-10)
### Recommendation (Accept / Weak Accept / Borderline / Reject)"""

        reviewer = {
            "name": name,
            "role": role,
            "focus": focus,
            "prompt": prompt or base_prompt,
            "version": "1.0",
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "evolution_notes": f"初始化创建" + (f" (基于 {based_on})" if based_on else ""),
        }
        data["reviewers"].append(reviewer)
        self.save(data)
        print(f"✅ 新审稿员: {name}")
        return reviewer

    # ── 进化 ──

    def evolve(self, name: str, external_review_path: str) -> dict:
        """从外审意见中学习，优化审稿员 Prompt。

        分析外审意见中提出的问题维度，如果审稿员之前没有覆盖到，
        将这些维度添加到审稿员的 prompt 中。
        """
        reviewer = self.get(name)
        if not reviewer:
            print(f"审稿员 '{name}' 不存在")
            return {}

        review_text = ""
        rp = Path(external_review_path)
        if rp.is_file():
            review_text = rp.read_text()[-5000:]

        if not review_text:
            print("外审文件为空或不可读")
            return reviewer

        # 提取外审关注的新维度
        import re
        dimensions = set()
        patterns = [
            # 审稿人指出的"你应该检查 X"
            r'(?i)(?:should|need to|must|ought to)\s+(?:check|evaluate|assess|verify|examine)\s+(.+?)(?:\.|;|\n)',
            # 审稿人提到的缺失评估维度
            r'(?i)(?:missing|lacking|absent|no discussion of|no analysis of)\s+(.+?)(?:\.|;|\n)',
            # 审稿人提出的新要求
            r'(?i)(?:the paper would benefit from|the authors should add)\s+(.+?)(?:\.|;|\n)',
        ]
        for pat in patterns:
            for match in re.findall(pat, review_text):
                dim = match.strip()[:100]
                if len(dim) > 20:
                    dimensions.add(dim)

        # 如果发现新维度，追加到 prompt
        if dimensions:
            old_version = reviewer.get("version", "1.0")
            new_version = f"{float(old_version) + 0.1:.1f}"

            new_section = "\n\n## Additionally (learned from external review)\n"
            new_section += "Pay special attention to:\n"
            for i, d in enumerate(sorted(dimensions)[:5], 1):
                new_section += f"{i}. {d}\n"

            reviewer["prompt"] = reviewer["prompt"] + new_section
            reviewer["version"] = new_version
            reviewer["evolution_notes"] = (
                f"v{new_version}: 从外审学习了 {len(dimensions)} 个新检查维度"
            )

            # 保存
            data = self.load()
            for i, r in enumerate(data["reviewers"]):
                if r["name"] == name:
                    data["reviewers"][i] = reviewer
                    break
            self.save(data)
            print(f"✅ {name} v{old_version} → v{new_version} (学了 {len(dimensions)} 个新维度)")

        return reviewer

    def auto_evolve(self, workspace: str):
        """自动分析项目的所有外审意见，批量优化审稿员。"""
        ws = Path(workspace)
        review_dir = ws / "review"
        if not review_dir.is_dir():
            print("无审稿目录")
            return

        # 找到所有外审文件
        external_reviews = []
        for f in sorted(review_dir.rglob("*.md")):
            if "external" in f.name.lower():
                external_reviews.append(f)

        if not external_reviews:
            print("未找到外审文件")
            return

        print(f"找到 {len(external_reviews)} 个外审文件\n")

        # 对每个审稿员，从所有外审中学习
        for reviewer in self.list():
            print(f"进化 {reviewer['name']}...")
            for ext_review in external_reviews[-3:]:  # 最近 3 个
                self.evolve(reviewer["name"], str(ext_review))
        print("\n✅ 自动进化完成")


# ── CLI ──

def main():
    import argparse
    parser = argparse.ArgumentParser(description="审稿员进化系统")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("list", help="列出所有审稿员")

    p = sub.add_parser("show", help="查看审稿员 Prompt")
    p.add_argument("name")

    p = sub.add_parser("add", help="添加新审稿员")
    p.add_argument("--name", required=True)
    p.add_argument("--role", default="")
    p.add_argument("--focus", default="")
    p.add_argument("--prompt", default="")
    p.add_argument("--based-on", default="")

    p = sub.add_parser("evolve", help="从外审意见中优化审稿员")
    p.add_argument("--reviewer", required=True)
    p.add_argument("--from-external", required=True)

    p = sub.add_parser("auto-evolve", help="自动批量优化")
    p.add_argument("--workspace", "-w", required=True)

    args = parser.parse_args()
    evo = ReviewerEvolution()

    if args.cmd == "list":
        for r in evo.list():
            print(f"  {r['name']:30s} v{r.get('version','1.0'):5s} {r.get('role','')}")

    elif args.cmd == "show":
        r = evo.get(args.name)
        if r:
            print(f"=== {r['name']} (v{r.get('version','1.0')}) ===")
            print(f"Role: {r.get('role','')}")
            print(f"Focus: {r.get('focus','')}")
            print(f"Evolved: {r.get('evolution_notes','none')}")
            print(f"\n--- PROMPT ---\n{r.get('prompt','')[:3000]}")
        else:
            print(f"审稿员 '{args.name}' 不存在")

    elif args.cmd == "add":
        evo.add(args.name, args.role, args.focus, args.prompt, args.based_on)

    elif args.cmd == "evolve":
        evo.evolve(args.reviewer, args.from_external)

    elif args.cmd == "auto-evolve":
        evo.auto_evolve(args.workspace)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
