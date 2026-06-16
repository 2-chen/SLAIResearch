#!/usr/bin/env python3
"""
Dual-Source Review Synthesis for SLAIResearch.

Features ported from chen-research-skills review-skill synthesis mode:
  1. Cross-source comparison (external vs internal)
  2. Agreement / conflict analysis
  3. Common issues (flagged by both sources)
  4. Source-specific issues
  5. Revision roadmap with impact estimates
  6. Score summary table

Usage:
    from review_synthesis import ReviewSynthesizer

    syn = ReviewSynthesizer(review_dir="workspace/my_topic/review")
    synthesis = syn.synthesize(round_num=0)
    syn.save(synthesis)
"""

from __future__ import annotations

import re
import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("review_synthesis")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ReviewerScore:
    """Score from a single reviewer."""
    name: str
    source: str  # "external" or "internal"
    score: float = 0.0
    verdict: str = "pending"
    top_issues: list[str] = field(default_factory=list)


@dataclass
class SynthesisResult:
    """Complete synthesis of internal + external reviews."""
    round_num: int = 0
    external_verdict: str = "pending"
    external_score: float = 0.0
    internal_consensus: str = "pending"
    internal_avg_score: float = 0.0
    all_scores: list[ReviewerScore] = field(default_factory=list)

    # Agreement analysis
    common_issues: list[str] = field(default_factory=list)
    external_only_issues: list[str] = field(default_factory=list)
    internal_only_issues: list[str] = field(default_factory=list)

    # Conflict analysis
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    # Revision roadmap
    revision_roadmap: list[dict[str, Any]] = field(default_factory=list)

    generated_at: str = ""


# ---------------------------------------------------------------------------
# Synthesizer
# ---------------------------------------------------------------------------

class ReviewSynthesizer:
    """Synthesize internal and external reviews into a unified analysis."""

    def __init__(self, review_dir: str | Path):
        self.review_dir = Path(review_dir)

    def synthesize(self, round_num: int) -> SynthesisResult:
        """Read internal and external reviews for a round and produce synthesis."""
        rd = self.review_dir / f"round_{round_num:03d}"
        result = SynthesisResult(
            round_num=round_num,
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

        # ── Parse External Review ──
        ext_path = rd / "external.md"
        if ext_path.exists():
            ext_text = ext_path.read_text()
            result.external_verdict = self._extract_verdict(ext_text)
            # Try to extract a numeric score
            ext_score_match = re.search(r'(?:Score|score)\s*:\s*(\d+(?:\.\d+)?)', ext_text)
            if ext_score_match:
                result.external_score = float(ext_score_match.group(1))
            ext_issues = self._extract_issues(ext_text)
            result.all_scores.append(ReviewerScore(
                name="paperreview.ai",
                source="external",
                score=result.external_score,
                verdict=result.external_verdict,
                top_issues=ext_issues[:5],
            ))

        # ── Parse Internal Reviews ──
        internal_dir = rd / "internal"
        if internal_dir.exists():
            internal_issues_all: list[str] = []
            for rev_file in sorted(internal_dir.glob("*.md")):
                if rev_file.name.startswith("merged"):
                    continue
                text = rev_file.read_text()
                reviewer_name = rev_file.stem.replace("_", " ").title()

                score = 0.0
                score_match = re.search(r'(?:Score|score)\s*:\s*(\d+(?:\.\d+)?)', text)
                if score_match:
                    try:
                        score = float(score_match.group(1))
                    except ValueError:
                        pass

                issues = self._extract_issues(text)
                internal_issues_all.extend(issues)

                result.all_scores.append(ReviewerScore(
                    name=reviewer_name,
                    source="internal",
                    score=score,
                    top_issues=issues[:5],
                ))

            # Compute internal consensus
            internal_scores = [s.score for s in result.all_scores if s.source == "internal" and s.score > 0]
            if internal_scores:
                result.internal_avg_score = sum(internal_scores) / len(internal_scores)
                result.internal_consensus = self._score_to_verdict(result.internal_avg_score)

        # ── Common Issues (flagged by both external AND ≥2 internal) ──
        ext_issue_texts = self._normalize_issues(
            [i for s in result.all_scores if s.source == "external" for i in s.top_issues]
        )
        int_issue_texts = self._normalize_issues(
            [i for s in result.all_scores if s.source == "internal" for i in s.top_issues]
        )

        for ei in ext_issue_texts:
            # Check if any internal issue is semantically similar
            matches = [ii for ii in int_issue_texts if self._is_similar_issue(ei, ii)]
            if len(matches) >= 2:
                result.common_issues.append(ei[:200])
                # Remove matched internal issues
                for m in matches:
                    if m in int_issue_texts:
                        int_issue_texts.remove(m)

        result.external_only_issues = [
            i[:200] for i in ext_issue_texts
            if not any(self._is_similar_issue(i, ci) for ci in result.common_issues)
        ]
        result.internal_only_issues = int_issue_texts[:200]

        # ── Conflicts ──
        result.conflicts = self._detect_conflicts(result)

        # ── Revision Roadmap ──
        result.revision_roadmap = self._build_roadmap(result)

        return result

    # ------------------------------------------------------------------
    # Issue extraction
    # ------------------------------------------------------------------

    def _extract_issues(self, text: str) -> list[str]:
        """Extract actionable issues from review text."""
        issues: list[str] = []

        # Pattern: PROBLEM → IMPACT → FIX
        for m in re.finditer(
            r'\[PROBLEM\]\s*(.*?)\s*→\s*\[IMPACT\]\s*(.*?)\s*→\s*\[FIX\]\s*(.*?)(?=\n\n|\n\[|$)',
            text, re.DOTALL | re.IGNORECASE,
        ):
            issues.append(f"[PROBLEM] {m.group(1).strip()} → [FIX] {m.group(3).strip()}")

        # Pattern: bullet points in weaknesses/issues sections
        for section in re.finditer(
            r'(?:###?\s*(?:Weaknesses|Issues|Critical\s+Issues|Must-fix).*?)'
            r'(?=###?\s*(?:Strengths|Suggestions|Score)|\Z)',
            text, re.DOTALL,
        ):
            for bullet in re.finditer(r'[-*]\s+((?:(?!\n[-*]).)+)', section.group(0), re.DOTALL):
                issue_text = bullet.group(1).strip()[:300]
                if len(issue_text) > 20:
                    issues.append(issue_text)

        # Fallback: any line with "Missing" or "should"
        for line in text.split("\n"):
            line = line.strip().lstrip("-*# ")
            if re.search(r'\b(?:Missing|should|must|need|require|lack)\b', line, re.IGNORECASE):
                if len(line) > 20 and line not in issues:
                    issues.append(line[:300])

        return issues[:10]  # Top 10

    def _normalize_issues(self, issues: list[str]) -> list[str]:
        """Normalize and deduplicate issue texts."""
        seen: set[str] = set()
        result: list[str] = []
        for i in issues:
            norm = re.sub(r'\s+', ' ', i.lower().strip())[:120]
            if norm not in seen:
                seen.add(norm)
                result.append(i)
        return result

    def _is_similar_issue(self, a: str, b: str) -> bool:
        """Check if two issue descriptions are semantically similar.

        Uses keyword overlap as a cheap proxy for semantic similarity.
        """
        # Extract key terms from each
        def key_terms(text: str) -> set[str]:
            words = re.findall(r'\b[a-z]{4,}\b', text.lower())
            # Filter stopwords
            stop = {"this", "that", "with", "from", "have", "been", "were", "they", "their", "about", "which", "would", "could", "should"}
            return {w for w in words if w not in stop}

        terms_a = key_terms(a)
        terms_b = key_terms(b)
        if not terms_a or not terms_b:
            return False

        overlap = len(terms_a & terms_b)
        union = len(terms_a | terms_b)
        return overlap / union > 0.3  # 30% keyword overlap

    # ------------------------------------------------------------------
    # Verdict extraction
    # ------------------------------------------------------------------

    def _extract_verdict(self, text: str) -> str:
        """Extract verdict from review text."""
        text_lower = text.lower()
        for verdict in ["accept", "weak accept", "borderline", "weak reject", "reject"]:
            if verdict in text_lower:
                return verdict
        return "pending"

    def _score_to_verdict(self, score: float) -> str:
        """Map numeric score to verdict."""
        if score >= 7.0:
            return "accept"
        if score >= 5.5:
            return "weak accept"
        if score >= 4.0:
            return "weak reject"
        return "reject"

    # ------------------------------------------------------------------
    # Conflict detection
    # ------------------------------------------------------------------

    def _detect_conflicts(self, result: SynthesisResult) -> list[dict[str, Any]]:
        """Detect conflicts between external and internal reviews."""
        conflicts: list[dict[str, Any]] = []

        # Conflict 1: Verdict mismatch > 1 level
        verdict_levels = {
            "accept": 5, "weak accept": 4, "borderline": 3,
            "weak reject": 2, "reject": 1, "pending": 0,
        }
        ext_level = verdict_levels.get(result.external_verdict, 0)
        int_level = verdict_levels.get(result.internal_consensus, 0)
        if abs(ext_level - int_level) >= 2:
            conflicts.append({
                "type": "verdict_mismatch",
                "description": (
                    f"External verdict ({result.external_verdict}) differs "
                    f"significantly from internal consensus ({result.internal_consensus})"
                ),
                "severity": "high",
                "recommendation": (
                    "Trust external review more for verdict-level decisions; "
                    "use internal reviews for detailed actionable feedback."
                ),
            })

        # Conflict 2: Score gap > 3 points
        if result.external_score > 0 and result.internal_avg_score > 0:
            gap = abs(result.external_score - result.internal_avg_score)
            if gap > 3:
                conflicts.append({
                    "type": "score_gap",
                    "description": (
                        f"External score ({result.external_score:.1f}) vs "
                        f"internal average ({result.internal_avg_score:.1f}) "
                        f"— gap of {gap:.1f} points"
                    ),
                    "severity": "medium",
                })

        return conflicts

    # ------------------------------------------------------------------
    # Roadmap
    # ------------------------------------------------------------------

    def _build_roadmap(self, result: SynthesisResult) -> list[dict[str, Any]]:
        """Build a prioritized revision roadmap."""
        roadmap: list[dict[str, Any]] = []

        # Priority 1: Common issues (both sources agree)
        for i, issue in enumerate(result.common_issues[:5]):
            roadmap.append({
                "priority": 1,
                "source": "common",
                "issue": issue[:200],
                "impact": "high",
                "note": "Flagged by both external AND internal reviewers — highest priority",
            })

        # Priority 2: External-only issues
        for i, issue in enumerate(result.external_only_issues[:5]):
            roadmap.append({
                "priority": 2,
                "source": "external",
                "issue": issue[:200],
                "impact": "high",
                "note": "External reviewer flagged this — must address for re-submission",
            })

        # Priority 3: Internal-only issues
        for i, issue in enumerate(result.internal_only_issues[:5]):
            roadmap.append({
                "priority": 3,
                "source": "internal",
                "issue": issue[:200],
                "impact": "medium",
                "note": "Internal reviewer flagged this — improve paper quality",
            })

        return roadmap

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def format_synthesis(self, result: SynthesisResult) -> str:
        """Format SynthesisResult as markdown."""
        lines = [
            f"# Review Synthesis — Round {result.round_num:03d}",
            f"",
            f"**Generated**: {result.generated_at}",
            f"",
            f"## Score Summary",
            f"",
            f"| Source | Reviewer | Score | Verdict |",
            f"|--------|----------|-------|---------|",
        ]
        for s in result.all_scores:
            lines.append(
                f"| {s.source.title()} | {s.name} | {s.score:.1f}/10 | {s.verdict} |"
            )
        lines.extend([
            "",
            f"**External Verdict**: {result.external_verdict.upper()}",
            f"**Internal Consensus**: {result.internal_consensus.upper()} (avg {result.internal_avg_score:.1f}/10)",
            "",
        ])

        # Common Issues
        if result.common_issues:
            lines.append("## 🔴 Common Issues (Both Sources Agree)")
            lines.append("")
            for i, issue in enumerate(result.common_issues, 1):
                lines.append(f"{i}. {issue}")
            lines.append("")

        # External-Only
        if result.external_only_issues:
            lines.append("## 🟡 External-Only Issues")
            lines.append("")
            for i, issue in enumerate(result.external_only_issues, 1):
                lines.append(f"{i}. {issue}")
            lines.append("")

        # Internal-Only
        if result.internal_only_issues:
            lines.append("## 🟢 Internal-Only Issues")
            lines.append("")
            for i, issue in enumerate(result.internal_only_issues, 1):
                lines.append(f"{i}. {issue}")
            lines.append("")

        # Conflicts
        if result.conflicts:
            lines.append("## ⚠️ Conflicts Between Sources")
            lines.append("")
            for c in result.conflicts:
                lines.append(f"- **[{c['severity'].upper()}] {c['type']}**: {c['description']}")
            lines.append("")

        # Roadmap
        if result.revision_roadmap:
            lines.append("## 📋 Revision Roadmap")
            lines.append("")
            for item in result.revision_roadmap:
                prio_emoji = {1: "🔴", 2: "🟡", 3: "🟢"}.get(item["priority"], "")
                lines.append(
                    f"{prio_emoji} **P{item['priority']} [{item['source'].upper()}]** "
                    f"{item['issue'][:150]}"
                )
                lines.append(f"   *{item['note']}*")
                lines.append("")

        return "\n".join(lines)

    def save(self, result: SynthesisResult) -> Path:
        """Save synthesis to review/round_NNN/synthesis.md."""
        rd = self.review_dir / f"round_{result.round_num:03d}"
        rd.mkdir(parents=True, exist_ok=True)
        path = rd / "synthesis.md"
        path.write_text(self.format_synthesis(result))
        return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="Synthesize internal and external reviews",
    )
    parser.add_argument("review_dir", help="Path to review directory")
    parser.add_argument("--round", "-r", type=int, default=0, help="Review round number")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    syn = ReviewSynthesizer(args.review_dir)
    result = syn.synthesize(args.round)

    if args.json:
        import dataclasses
        print(json.dumps(dataclasses.asdict(result), indent=2, ensure_ascii=False))
    else:
        print(syn.format_synthesis(result))

    path = syn.save(result)
    print(f"\nSynthesis saved to: {path}")


if __name__ == "__main__":
    main()
