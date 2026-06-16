#!/usr/bin/env python3
"""
审稿记忆系统 — 追踪所有审稿意见的提出、解决和演变。

每个项目维护一个 review_memory.md，记录:
- 每一条审稿意见（哪个审稿人、哪一轮提出的）
- 当前状态（OPEN / PARTIAL / RESOLVED）
- 解决方式（文本修改 / 新实验 / 新引用 / 新分析）
- 历史变更记录

内部审稿员在审稿前先注入审稿记忆，避免给出矛盾意见。

用法:
    # 从审稿报告中提取 issue 并添加到记忆
    python review_memory.py extract review/round_001/internal/ --workspace <ws>

    # 手动添加一条 issue
    python review_memory.py add "方法缺少公式" --source "R1-Methodology" --workspace <ws>

    # 标记 issue 为已解决
    python review_memory.py resolve "R1-I01" --how "补全了公式" --workspace <ws>

    # 生成审稿记忆上下文（注入到审稿员 prompt 中）
    python review_memory.py context --workspace <ws>

    # 统计解决率
    python review_memory.py stats --workspace <ws>
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class ReviewMemory:
    """管理单个项目的审稿记忆。"""

    def __init__(self, workspace: str):
        self.ws = Path(workspace)
        self.review_dir = self.ws / "review"
        self.review_dir.mkdir(parents=True, exist_ok=True)
        self.memory_path = self.review_dir / "review_memory.md"
        self.json_path = self.review_dir / "review_memory.json"

    # ── 读取 ──

    def read(self) -> list[dict]:
        """读取所有 issue 记录。"""
        if self.json_path.exists():
            try:
                return json.loads(self.json_path.read_text())
            except Exception:
                return []
        return []

    def write(self, issues: list[dict]):
        """写入 issue 记录并同步生成 markdown。"""
        self.json_path.write_text(json.dumps(issues, indent=2, ensure_ascii=False))
        self._sync_markdown(issues)

    def _sync_markdown(self, issues: list[dict]):
        """同步生成人类可读的 review_memory.md。"""
        total = len(issues)
        resolved = sum(1 for i in issues if i.get("status") == "RESOLVED")
        pct = (resolved / total * 100) if total > 0 else 100

        lines = [
            "# 审稿记忆",
            f"总计 {total} 条意见 | 已解决 {resolved}/{total} ({pct:.0f}%)",
            f"最后更新: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            "",
            "## 按轮次",
        ]

        by_round = {}
        for issue in issues:
            r = issue.get("round", "?")
            by_round.setdefault(r, []).append(issue)

        for r in sorted(by_round.keys()):
            lines.append(f"### 第 {r} 轮")
            for issue in by_round[r]:
                status = issue.get("status", "OPEN")
                icon = {"RESOLVED": "✅", "PARTIAL": "⚠️", "OPEN": "❌"}.get(status, "❓")
                lines.append(f"- {icon} **{issue.get('id', '?')}**: {issue.get('description', '?')[:120]}")
                src = issue.get("source", "?")
                lines.append(f"  来源: {src} | 状态: {status}")
                if issue.get("resolution"):
                    lines.append(f"  解决: {issue['resolution'][:150]}")
                lines.append("")

        lines.append("## 详细记录")
        for issue in issues:
            status = issue.get("status", "OPEN")
            icon = {"RESOLVED": "✅", "PARTIAL": "⚠️", "OPEN": "❌"}.get(status, "❓")
            lines.append(f"### {icon} {issue.get('id', '?')}: {issue.get('description', '?')[:120]}")
            lines.append(f"- 来源: {issue.get('source', '?')}")
            lines.append(f"- 轮次: {issue.get('round', '?')}")
            lines.append(f"- 状态: {status}")
            if issue.get("resolution"):
                lines.append(f"- 解决方式: {issue['resolution']}")
            if issue.get("history"):
                lines.append(f"- 变更历史: {issue['history']}")
            lines.append("")

        self.memory_path.write_text("\n".join(lines))

    # ── Issue 管理 ──

    def add(self, description: str, source: str = "unknown",
            round_num: str = "0", category: str = "unknown") -> str:
        """添加一条 issue，生成唯一 ID。"""
        issues = self.read()

        # 生成 ID: R{round}-I{序号}
        same_round = [i for i in issues if i.get("round") == round_num]
        idx = len(same_round) + 1
        issue_id = f"R{round_num}-I{idx:02d}"

        # 去重：相同描述不重复添加
        for existing in issues:
            if existing.get("description", "")[:80] == description[:80]:
                return existing.get("id", issue_id)

        issue = {
            "id": issue_id,
            "description": description[:300],
            "source": source,
            "round": round_num,
            "category": category,
            "status": "OPEN",
            "resolution": "",
            "history": [f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}: 提出 ({source})"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        issues.append(issue)
        self.write(issues)
        return issue_id

    def resolve(self, issue_id: str, how: str = ""):
        """标记 issue 为已解决。"""
        issues = self.read()
        for issue in issues:
            if issue.get("id") == issue_id:
                issue["status"] = "RESOLVED"
                issue["resolution"] = how[:200]
                issue["history"].append(
                    f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}: 已解决 — {how[:100]}"
                )
                issue["updated_at"] = datetime.now(timezone.utc).isoformat()
                self.write(issues)
                return True
        return False

    def update_status(self, issue_id: str, status: str, note: str = ""):
        """更新 issue 状态。"""
        issues = self.read()
        for issue in issues:
            if issue.get("id") == issue_id:
                issue["status"] = status
                if note:
                    issue["history"].append(
                        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}: {status} — {note[:100]}"
                    )
                issue["updated_at"] = datetime.now(timezone.utc).isoformat()
                self.write(issues)
                return True
        return False

    # ── 提取 ──

    def extract_from_review(self, review_path: str, round_num: str = "0"):
        """从审稿报告中提取 issue 并添加到记忆。"""
        path = Path(review_path)
        if not path.exists():
            print(f"文件不存在: {review_path}")
            return

        content = path.read_text()
        # 提取 PROBLEM-IMPACT-FIX 模式的 issue
        pattern = re.compile(
            r'\[PROBLEM\]\s*(.+?)\s*→\s*\[IMPACT\]\s*(.+?)\s*→\s*\[FIX\]\s*(.+?)(?=\n\n|\n\[|\Z)',
            re.DOTALL | re.IGNORECASE
        )
        matches = pattern.findall(content)
        if not matches:
            # 回退：匹配编号列表中的问题
            matches = re.findall(
                r'(?:^|\n)\s*(?:\d+\.\s*|\*\*\d+\.\s*|\-\s*\[PROBLEM\]\s*)(.+?)(?:\n|$)',
                content, re.MULTILINE
            )
            matches = [(m.strip(), "", "") for m in matches if len(m.strip()) > 20]

        # 提取审稿人信息
        reviewer = "unknown"
        rm = re.search(r'(?:Reviewer|审稿人)[: ]*(.+?)(?:\n|$)', content)
        if rm:
            reviewer = rm.group(1).strip()[:50]

        count = 0
        for problem, impact, fix in matches[:20]:  # 最多 20 条
            desc = problem.strip()[:200]
            self.add(
                description=desc,
                source=f"R{round_num}-{reviewer}",
                round_num=round_num,
                category="auto-extracted",
            )
            count += 1

        print(f"从 {review_path.name} 提取了 {count} 条 issue")
        return count

    # ── 上下文 ──

    def context(self, max_issues: int = 30) -> str:
        """生成注入审稿员的上下文文本。

        格式化为审稿员系统 prompt 的前置段落，告诉审稿员:
        - 之前已经提过哪些意见
        - 哪些已解决、哪些未解决
        - 不要重复已解决的 issue
        """
        issues = self.read()
        if not issues:
            return ""

        resolved = [i for i in issues if i.get("status") == "RESOLVED"]
        open_issues = [i for i in issues if i.get("status") != "RESOLVED"]

        lines = [
            "## ⚠️ 审稿记忆 — 请先阅读以下内容再开始审稿",
            "",
            f"本项目已有 {len(issues)} 条审稿意见被记录。",
            f"其中 {len(resolved)} 条已解决，{len(open_issues)} 条未解决。",
            "",
            "### 已解决的 issue — 请勿重复提出",
            "以下问题已在之前轮次中提出并解决。如果你在论文中再次发现相同问题，"
            "请指出问题仍然存在，但应引用原始 issue ID，而不是当作新问题。",
            "",
        ]

        for issue in resolved[:15]:
            lines.append(
                f"- ✅ **{issue.get('id')}**: {issue.get('description', '?')[:150]}\n"
                f"  解决方式: {issue.get('resolution', '未记录')[:150]}"
            )

        if open_issues:
            lines.append("")
            lines.append("### 未解决的 issue — 请检查是否已改善")
            lines.append("以下问题尚未标记为已解决。请评估是否有改善，但不需要重复提出。")
            lines.append("")
            for issue in open_issues[:15]:
                lines.append(
                    f"- ❌ **{issue.get('id')}**: {issue.get('description', '?')[:150]}"
                )

        return "\n".join(lines)

    # ── 统计 ──

    def stats(self) -> dict:
        """返回统计数据。"""
        issues = self.read()
        total = len(issues)
        resolved = sum(1 for i in issues if i.get("status") == "RESOLVED")
        by_round = {}
        for i in issues:
            r = i.get("round", "?")
            by_round[r] = by_round.get(r, 0) + 1
        return {
            "total": total,
            "resolved": resolved,
            "resolution_pct": (resolved / total * 100) if total > 0 else 100,
            "by_round": by_round,
        }


# ── CLI ──

def main():
    import argparse
    parser = argparse.ArgumentParser(description="审稿记忆管理器")
    parser.add_argument("--workspace", "-w", required=True, help="项目 workspace 路径")

    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("add", help="添加 issue")
    p.add_argument("description")
    p.add_argument("--source", default="manual")
    p.add_argument("--round", default="0")

    p = sub.add_parser("resolve", help="标记为已解决")
    p.add_argument("issue_id")
    p.add_argument("--how", default="")

    p = sub.add_parser("update", help="更新状态")
    p.add_argument("issue_id")
    p.add_argument("--status", default="PARTIAL")
    p.add_argument("--note", default="")

    p = sub.add_parser("extract", help="从审稿报告提取 issue")
    p.add_argument("review_path")
    p.add_argument("--round", default="0")

    sub.add_parser("context", help="生成审稿记忆上下文")
    sub.add_parser("stats", help="显示统计")
    sub.add_parser("list", help="列出所有 issue")

    args = parser.parse_args()
    rm = ReviewMemory(args.workspace)

    if args.cmd == "add":
        rid = rm.add(args.description, args.source, args.round)
        print(f"添加: {rid}")
    elif args.cmd == "resolve":
        ok = rm.resolve(args.issue_id, args.how)
        print(f"{'✅' if ok else '❌'} {args.issue_id}")
    elif args.cmd == "update":
        ok = rm.update_status(args.issue_id, args.status, args.note)
        print(f"{'✅' if ok else '❌'} {args.issue_id}")
    elif args.cmd == "extract":
        rm.extract_from_review(args.review_path, args.round)
    elif args.cmd == "context":
        print(rm.context())
    elif args.cmd == "stats":
        s = rm.stats()
        print(f"总计: {s['total']} | 已解决: {s['resolved']} ({s['resolution_pct']:.0f}%)")
        for r, c in sorted(s['by_round'].items()):
            print(f"  轮次 {r}: {c} 条")
    elif args.cmd == "list":
        for i in rm.read():
            status = i.get("status", "?")
            icon = {"RESOLVED": "✅", "PARTIAL": "⚠️", "OPEN": "❌"}.get(status, "❓")
            print(f"  {icon} {i.get('id')}: {i.get('description')[:120]}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
