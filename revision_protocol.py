#!/usr/bin/env python3
"""
TODO-Driven Revision Protocol for SLAIResearch.

Features ported from chen-research-skills review-skill:
  1. Structured TODO list extraction from review feedback
  2. Critical/Major/Minor classification
  3. Pre-submission checklist with quota-awareness
  4. Full pipeline re-iteration logic (revise → search → experiment → write → review)
  5. Revision notes generation (mapping reviewer concerns to changes)

Usage:
    from revision_protocol import RevisionProtocol

    rp = RevisionProtocol(workspace_dir="workspace/my_topic")
    todo = rp.extract_todo_from_reviews(review_dir)
    rp.save_todo(todo, round_num=0)
    ready, missing = rp.pre_submission_check(todo)
"""

from __future__ import annotations

import re
import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("revision_protocol")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TODOItem:
    """A single actionable revision item extracted from review feedback."""
    id: str                         # e.g. "E-001" or "I-001"
    source: str                     # "external" or "internal"
    reviewer: str = ""              # reviewer name (for internal)
    description: str = ""
    classification: str = "major"   # "critical", "major", "minor"
    change_type: str = "text"       # "supplementary_experiment", "literature_gap",
                                    # "method_clarification", "figure_table", "text_polish"
    status: str = "pending"         # "pending", "in_progress", "completed", "deferred"
    resolution_note: str = ""       # what was done
    location: str = ""              # where in the paper (section, line)


@dataclass
class TODOList:
    """Structured TODO list for a review round."""
    round_num: int = 0
    items: list[TODOItem] = field(default_factory=list)
    created_at: str = ""
    completed_at: str = ""

    @property
    def critical(self) -> list[TODOItem]:
        return [i for i in self.items if i.classification == "critical"]

    @property
    def major(self) -> list[TODOItem]:
        return [i for i in self.items if i.classification == "major"]

    @property
    def minor(self) -> list[TODOItem]:
        return [i for i in self.items if i.classification == "minor"]

    @property
    def completed(self) -> list[TODOItem]:
        return [i for i in self.items if i.status == "completed"]

    @property
    def pending(self) -> list[TODOItem]:
        return [i for i in self.items if i.status in ("pending", "in_progress")]

    @property
    def completion_pct(self) -> float:
        if not self.items:
            return 100.0
        return len(self.completed) / len(self.items) * 100


# ---------------------------------------------------------------------------
# Classification rules
# ---------------------------------------------------------------------------

# Keywords that indicate criticality
CRITICAL_PATTERNS = [
    r'\b(?:missing|absent|lacks?|no|without)\b.*\b(?:baseline|comparison|experiment)\b',
    r'\b(?:fundamental|fatal)\s+flaw',
    r'\b(?:cannot|does\s+not)\s+(?:compile|run|reproduce)\b',
    r'\bclaims?\s+(?:unsupported|unsubstantiated|fabricated)\b',
    r'\bdata\s+leakage\b',
    r'\b(?:must|required|essential|critical)\b.*\b(?:add|include|fix|run)\b',
]

MAJOR_PATTERNS = [
    r'\b(?:incomplete|insufficient|weak)\s+(?:ablation|evaluation|comparison|analysis)\b',
    r'\bmissing\s+(?:citation|reference|related\s+work)\b',
    r'\b(?:unclear|ambiguous|confusing)\s+(?:method|notation|description)\b',
    r'\b(?:should|ought|needs?\s+to)\b.*\b(?:add|improve|clarify|expand|rewrite)\b',
    r'\b(?:overclaim|overstatement|exaggeration)\b',
    r'\b(?:fairness|significance|statistical)\s+(?:concern|issue)\b',
]


def classify_issue(text: str) -> str:
    """Classify a review issue as critical, major, or minor based on keyword patterns."""
    text_lower = text.lower()

    for pattern in CRITICAL_PATTERNS:
        if re.search(pattern, text_lower):
            return "critical"

    for pattern in MAJOR_PATTERNS:
        if re.search(pattern, text_lower):
            return "major"

    return "minor"


def classify_change_type(text: str) -> str:
    """Determine what type of change is needed to address an issue."""
    text_lower = text.lower()
    if re.search(r'\b(?:experiment|baseline|ablation|run|train|evaluate|benchmark)\b', text_lower):
        return "supplementary_experiment"
    if re.search(r'\b(?:citation|reference|related\s+work|literature|paper\s+by|cite)\b', text_lower):
        return "literature_gap"
    if re.search(r'\b(?:method|algorithm|notation|formula|theorem|proof|definition)\b', text_lower):
        return "method_clarification"
    if re.search(r'\b(?:figure|table|plot|chart|diagram|visualization|graph)\b', text_lower):
        return "figure_table"
    return "text_polish"


# ---------------------------------------------------------------------------
# Revision Protocol
# ---------------------------------------------------------------------------

class RevisionProtocol:
    """Manages the TODO-driven revision workflow for a paper workspace."""

    def __init__(self, workspace_dir: str | Path):
        self.workspace = Path(workspace_dir)
        self.review_dir = self.workspace / "review"

    # ------------------------------------------------------------------
    # TODO Extraction
    # ------------------------------------------------------------------

    def extract_todo_from_reviews(
        self,
        review_dir: str | Path | None = None,
        round_num: int | None = None,
    ) -> TODOList:
        """Extract structured TODO items from review files.

        Reads external.md and internal/*.md, extracts actionable criticisms,
        classifies them, and builds a prioritized TODO list.
        """
        rd = Path(review_dir) if review_dir else self.review_dir
        if round_num is not None:
            rd = rd / f"round_{round_num:03d}"

        todo = TODOList(
            round_num=round_num or 0,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

        # Parse external review
        ext_path = rd / "external.md"
        if ext_path.exists():
            ext_issues = self._parse_review_issues(ext_path.read_text(), "external")
            for i, issue in enumerate(ext_issues, 1):
                todo.items.append(TODOItem(
                    id=f"E-{i:03d}",
                    source="external",
                    description=issue["text"],
                    classification=issue["severity"],
                    change_type=classify_change_type(issue["text"]),
                ))

        # Parse internal reviews
        internal_dir = rd / "internal"
        if internal_dir.exists():
            for rev_file in sorted(internal_dir.glob("*.md")):
                if rev_file.name.startswith("merged"):
                    continue
                reviewer_name = rev_file.stem.replace("_", " ").title()
                text = rev_file.read_text()
                int_issues = self._parse_review_issues(text, "internal")
                for i, issue in enumerate(int_issues, 1):
                    todo.items.append(TODOItem(
                        id=f"I-{len(todo.items)+1:03d}",
                        source="internal",
                        reviewer=reviewer_name,
                        description=issue["text"],
                        classification=issue["severity"],
                        change_type=classify_change_type(issue["text"]),
                    ))

        # Sort: critical first, then major, then minor
        severity_order = {"critical": 0, "major": 1, "minor": 2}
        todo.items.sort(key=lambda x: severity_order.get(x.classification, 2))

        logger.info(
            "Extracted %d TODO items (%d critical, %d major, %d minor)",
            len(todo.items),
            len(todo.critical), len(todo.major), len(todo.minor),
        )
        return todo

    def _parse_review_issues(self, text: str, source: str) -> list[dict]:
        """Extract actionable issues from review text.

        Looks for PROBLEM→IMPACT→FIX patterns, bullet points with issues,
        and labeled weaknesses sections.
        """
        issues: list[dict] = []

        # Pattern 1: PROBLEM → IMPACT → FIX format
        for m in re.finditer(
            r'\[PROBLEM\]\s*(.*?)\s*→\s*\[IMPACT\]\s*(.*?)\s*→\s*\[FIX\]\s*(.*?)(?=\n\n|\n\[|$)',
            text, re.DOTALL | re.IGNORECASE,
        ):
            full_text = f"[PROBLEM] {m.group(1)} [IMPACT] {m.group(2)} [FIX] {m.group(3)}"
            issues.append({
                "text": full_text[:500],
                "severity": classify_issue(full_text),
            })

        # Pattern 2: Weaknesses / Issues sections with bullet points
        for section in re.finditer(
            r'(?:###?\s*(?:Weaknesses|Issues|Detailed\s+Issues|Critical\s+Issues|Must-fix\s+issues).*?)'
            r'(?=###?\s*(?:Strengths|Suggestions|Score|Recommendation|$)|\Z)',
            text, re.DOTALL | re.IGNORECASE,
        ):
            section_text = section.group(0)
            # Extract bullet points
            for bullet in re.finditer(
                r'[-*]\s+((?:(?!\n[-*]).)+)',
                section_text, re.DOTALL,
            ):
                issue_text = bullet.group(1).strip()[:500]
                if len(issue_text) > 20:  # Skip too-short bullets
                    # Avoid duplicates
                    if not any(issue_text[:80] in i["text"][:80] for i in issues):
                        issues.append({
                            "text": issue_text,
                            "severity": classify_issue(issue_text),
                        })

        # Pattern 3: "Missing X" / "No Y" patterns in the full text
        for m in re.finditer(
            r'\b(?:Missing|Lacks?|No|Without|Fails?\s+to)\s+[^.]+\.',
            text, re.IGNORECASE,
        ):
            issue_text = m.group(0).strip()[:300]
            if len(issue_text) > 30:
                if not any(issue_text[:60] in i["text"][:60] for i in issues):
                    issues.append({
                        "text": issue_text,
                        "severity": classify_issue(issue_text),
                    })

        return issues

    # ------------------------------------------------------------------
    # TODO Persistence
    # ------------------------------------------------------------------

    def save_todo(self, todo: TODOList, round_num: int | None = None) -> Path:
        """Save TODO list to review/round_NNN/TODO.md."""
        rd = self.review_dir / f"round_{(round_num if round_num is not None else todo.round_num):03d}"
        rd.mkdir(parents=True, exist_ok=True)
        path = rd / "TODO.md"
        path.write_text(self._format_todo_md(todo))
        return path

    def _format_todo_md(self, todo: TODOList) -> str:
        """Format a TODOList as markdown."""
        lines = [
            f"# Revision TODO — Round {todo.round_num:03d}",
            f"",
            f"**Created**: {todo.created_at}",
            f"**Items**: {len(todo.items)} total "
            f"({len(todo.critical)} critical, {len(todo.major)} major, {len(todo.minor)} minor)",
            f"**Completed**: {len(todo.completed)} ({todo.completion_pct:.0f}%)",
            f"",
        ]

        for severity in ("critical", "major", "minor"):
            items = [i for i in todo.items if i.classification == severity]
            if not items:
                continue

            emoji = {"critical": "🔴", "major": "🟡", "minor": "🟢"}.get(severity, "")
            label = {
                "critical": "Critical (blocks resubmission)",
                "major": "Major (significantly weakens paper)",
                "minor": "Minor (cosmetic / clarity)",
            }.get(severity, severity)

            lines.append(f"## {emoji} {label}")
            lines.append("")

            for item in items:
                check = "x" if item.status == "completed" else " "
                reviewer_info = f" ({item.reviewer})" if item.reviewer else ""
                lines.append(
                    f"- [{check}] **[{item.id}]**{reviewer_info} "
                    f"{item.description[:200]}"
                )
                if item.resolution_note:
                    lines.append(f"  → {item.resolution_note}")
                if item.location:
                    lines.append(f"  📍 {item.location}")

            lines.append("")

        # Change type summary
        lines.append("## Change Type Breakdown")
        lines.append("")
        type_counts: dict[str, int] = {}
        for item in todo.items:
            type_counts[item.change_type] = type_counts.get(item.change_type, 0) + 1
        for ctype, count in sorted(type_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- **{ctype.replace('_', ' ').title()}**: {count} items")
        lines.append("")

        # Pre-submission checklist
        lines.extend(self._format_checklist(todo))

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Pre-Submission Checklist
    # ------------------------------------------------------------------

    def pre_submission_check(self, todo: TODOList) -> tuple[bool, list[str]]:
        """Check if the paper is ready for re-submission to paperreview.ai.

        Returns (ready: bool, blocking_issues: list[str]).
        """
        blocking: list[str] = []

        # All critical items must be resolved
        unresolved_critical = [i for i in todo.critical if i.status != "completed"]
        if unresolved_critical:
            blocking.append(
                f"{len(unresolved_critical)} critical item(s) unresolved: "
                + ", ".join(i.id for i in unresolved_critical[:5])
            )

        # At least 90% of major items must be resolved
        if todo.major:
            major_completed = sum(1 for i in todo.major if i.status == "completed")
            major_pct = major_completed / len(todo.major) * 100
            if major_pct < 90:
                blocking.append(
                    f"Only {major_pct:.0f}% of major items resolved "
                    f"({major_completed}/{len(todo.major)}) — need ≥90%"
                )

        # Quota awareness: must have at least one substantive change
        has_substantive = any(
            i.status == "completed" and i.change_type in (
                "supplementary_experiment", "literature_gap"
            )
            for i in todo.items
        )
        has_substantive = has_substantive or (
            sum(1 for i in todo.items if i.status == "completed"
                and i.change_type == "method_clarification") >= 3
        )

        if not has_substantive:
            blocking.append(
                "QUOTA GUARD: No substantive changes detected. "
                "At minimum, every resubmission must include: "
                "≥1 supplementary experiment, OR ≥3 literature gaps filled, "
                "OR a substantially rewritten methodology section. "
                "Text-only polish does NOT justify consuming paperreview.ai quota."
            )

        # Paper must compile
        paper_tex = self.workspace / "paper" / "paper.tex"
        if not paper_tex.exists():
            blocking.append("paper.tex not found — paper must exist and compile")

        return len(blocking) == 0, blocking

    def _format_checklist(self, todo: TODOList) -> list[str]:
        """Format the pre-submission checklist section."""
        ready, blocking = self.pre_submission_check(todo)
        lines = [
            "## Pre-Submission Checklist",
            "",
            f"**Overall Status**: {'✅ READY' if ready else '❌ NOT READY'}",
            "",
        ]

        checklist_items = [
            ("All Critical items resolved",
             not any(i.status != "completed" for i in todo.critical)),
            ("≥90% Major items resolved",
             len([i for i in todo.major if i.status == "completed"]) >= len(todo.major) * 0.8
             if todo.major else True),
            ("Substantive changes made (not just text polish)",
             any(i.status == "completed" and i.change_type in (
                 "supplementary_experiment", "literature_gap")
                 for i in todo.items) or
             sum(1 for i in todo.items if i.status == "completed"
                 and i.change_type == "method_clarification") >= 3),
            ("Paper compiles successfully", True),  # check separately
            ("TODO.md is up to date", True),
            ("Revision notes prepared", True),
        ]

        for label, ok in checklist_items:
            mark = "✅" if ok else "❌"
            lines.append(f"{mark} {label}")

        if blocking:
            lines.append("")
            lines.append("### Blocking Issues")
            for b in blocking:
                lines.append(f"- ❌ {b}")

        return lines

    # ------------------------------------------------------------------
    # Revision Notes
    # ------------------------------------------------------------------

    def generate_revision_notes(
        self, todo: TODOList, round_num: int,
    ) -> str:
        """Generate revision_notes.md mapping each review concern to the change made."""
        lines = [
            f"# Revision Notes — Round {round_num:03d} → Round {round_num+1:03d}",
            f"",
            f"## Response to External Reviewer",
            f"",
            "| Issue | Action | Location |",
            "|-------|--------|----------|",
        ]
        for item in todo.items:
            if item.source == "external" and item.status == "completed":
                lines.append(
                    f"| {item.description[:100]}... "
                    f"| {item.resolution_note[:100]} "
                    f"| {item.location or 'paper.tex'} |"
                )

        lines.extend(["", "## Response to Internal Reviewers", ""])
        for item in todo.items:
            if item.source == "internal" and item.status == "completed":
                reviewer_short = item.reviewer[:30] if item.reviewer else "N/A"
                lines.append(
                    f"| {reviewer_short} | {item.description[:80]}... "
                    f"| {item.resolution_note[:80]} "
                    f"| {item.location or 'paper.tex'} |"
                )

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Full Pipeline Re-Iteration Logic
# ---------------------------------------------------------------------------

def should_reiterate_pipeline(todo: TODOList) -> tuple[bool, str]:
    """Determine if the pipeline should fully re-iterate after revision.

    Returns (should_reiterate, reason).

    Per chen-research-skills design: after review failure, the pipeline
    re-enters at Stage 1 (RESEARCH), not Stage 3 (WRITE). It searches for
    literature the reviewers flagged as missing, designs and runs supplementary
    experiments, rewrites with new evidence, and re-reviews.

    Text-only revision is NOT sufficient — must have substantive changes.
    """
    has_literature_gaps = any(
        i.classification in ("critical", "major")
        and i.change_type == "literature_gap"
        for i in todo.pending
    )
    has_experiment_gaps = any(
        i.classification in ("critical", "major")
        and i.change_type == "supplementary_experiment"
        for i in todo.pending
    )
    has_method_gaps = any(
        i.classification in ("critical", "major")
        and i.change_type == "method_clarification"
        for i in todo.pending
    )

    if has_experiment_gaps:
        return True, "Supplementary experiments needed → re-enter at EXPERIMENT stage"
    if has_literature_gaps:
        return True, "Literature gaps found → re-enter at RESEARCH stage"
    if has_method_gaps:
        return True, "Methodology revisions needed → re-enter at WRITE stage"

    text_only = all(
        i.change_type in ("text_polish", "figure_table")
        for i in todo.pending
    )
    if text_only:
        return False, "Only text/format changes remaining → revise directly without full re-iteration"

    return True, "Unresolved items remain → full re-iteration recommended"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="Extract TODO from reviews and check submission readiness",
    )
    parser.add_argument("workspace", help="Path to paper workspace")
    parser.add_argument("--round", "-r", type=int, default=0, help="Review round number")
    parser.add_argument("--check", action="store_true", help="Run pre-submission check only")
    parser.add_argument("--notes", action="store_true", help="Generate revision notes")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    rp = RevisionProtocol(args.workspace)
    todo = rp.extract_todo_from_reviews(round_num=args.round)

    if args.check:
        ready, blocking = rp.pre_submission_check(todo)
        if args.json:
            print(json.dumps({"ready": ready, "blocking": blocking}, indent=2))
        else:
            print(f"Ready: {ready}")
            for b in blocking:
                print(f"  ❌ {b}")
        return

    if args.notes:
        print(rp.generate_revision_notes(todo, args.round))
        return

    # Default: save TODO.md
    path = rp.save_todo(todo, args.round)
    print(f"TODO saved to {path}")
    print(f"  {len(todo.critical)} critical, {len(todo.major)} major, {len(todo.minor)} minor")
    ready, blocking = rp.pre_submission_check(todo)
    print(f"  Pre-submission check: {'✅ READY' if ready else '❌ NOT READY'}")


if __name__ == "__main__":
    main()
