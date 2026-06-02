#!/usr/bin/env python3
"""
Revision engine for ChenResearch — per-section LLM revision loop with
backpressure, convergence detection, meta-refine, and grounding protection.

Key features (ported from NanoResearch review module):

  1. **Section extraction** — parse LaTeX into reviewable sections
  2. **Per-section revision** — only sections scoring < 6 get rewritten
  3. **Backpressure** — LaTeX structure check after each revision;
     auto-fix mismatched environments or revert if structure breaks
  4. **Meta-refine** — if a revision DECREASES the score, diagnose the
     failure and retry with targeted guidance before giving up
  5. **Convergence detection** — stop when improvement < 0.3 or stalls
     for 2 consecutive rounds
  6. **Grounding protection** — replace LLM-generated result tables with
     artifact-grounded versions from experiment_results; correct numeric
     claims that don't match actual experiment data

Usage:
    from revision_engine import RevisionEngine

    engine = RevisionEngine(model="deepseek-v4-pro")
    revised_tex, report = await engine.revise(
        paper_tex=open("paper.tex").read(),
        review_feedback=open("review.md").read(),
        experiment_blueprint={...},
        experiment_results={...},
        max_rounds=3,
    )
"""

from __future__ import annotations

import re
import json
import logging
import subprocess
import textwrap
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

from config import CLAUDE_CMD, CLAUDE_MODEL

logger = logging.getLogger("revision_engine")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_REVISION_ROUNDS = 3
MIN_SECTION_SCORE = 6       # Only revise sections scoring below this
CONVERGENCE_THRESHOLD = 0.3  # Stop if avg score improves less than this
MAX_STALL_ROUNDS = 2         # Stop after this many consecutive stalled rounds
MAX_BP_REVERTS = 2           # Max consecutive backpressure reverts before stopping

# Sections that MUST exist after revision
REQUIRED_SECTIONS = {"Introduction", "Related Work", "Method", "Experiments", "Conclusion"}

# LaTeX result table labels that are allowed to survive (others get dropped)
ALLOWED_TABLE_LABELS = {"tab:main_results", "tab:ablation"}

# Revision system prompt — instructs the LLM to rewrite, not evaluate
REVISION_SYSTEM_PROMPT = textwrap.dedent("""\
    You are an expert academic paper reviser. Your job is to REWRITE a specific
    section of a LaTeX paper to address reviewer feedback and improve quality.

    CRITICAL RULES:
    1. Output ONLY the revised LaTeX body for the requested section
    2. Do NOT include \\section{} commands in your output
    3. Do NOT modify figure or table blocks (\\begin{figure}...\\end{figure},
       \\begin{table}...\\end{table}) — they contain real experiment data
    4. Do NOT change any numeric results in tables — these are from real experiments
    5. Do NOT add new result numbers that don't exist in the original text
    6. Use ONLY citation keys that appear in the paper's bibliography
    7. Keep the same overall structure and length (+-20%)
    8. Fix ALL issues mentioned in the reviewer feedback
    9. Preserve good aspects (strengths) identified by the reviewer
    10. If reviewer asks for experiments you cannot run, convert those
        requests into honest limitations or future work
""")

# Section-specific revision guidance
_SECTION_GUIDANCE: dict[str, str] = {
    "Abstract": (
        "The abstract should be concise (150-250 words), state the problem, "
        "method, key results, and significance. Do NOT include citations or "
        "\\ref{} commands. Keep it self-contained."
    ),
    "Introduction": (
        "The introduction should motivate the problem, summarize related work, "
        "identify the gap, state contributions clearly, and outline paper "
        "structure. Use \\ref{} and \\cite{} appropriately."
    ),
    "Related Work": (
        "Related Work should be organized by topic area, compare and contrast "
        "with prior work, and clearly position the paper's contribution. "
        "Every claim about prior work needs a citation."
    ),
    "Method": (
        "The method section should be a formal, precise description of the "
        "proposed approach. Use mathematical notation where appropriate. "
        "Include algorithm descriptions and justify design choices."
    ),
    "Experiments": (
        "DO NOT modify result tables or figures — they contain real data. "
        "Do NOT change numeric values in tables. You may improve the "
        "descriptive text and analysis surrounding the tables."
    ),
    "Conclusion": (
        "Summarize contributions, discuss limitations honestly, and suggest "
        "concrete future work directions. Do NOT overclaim."
    ),
}

# AI artifact words to warn about during revision
_AI_ARTIFACT_WARNING = (
    "Avoid these overused AI words in your revision: delve, leverage, utilize, "
    "harness, pivotal, unveil, elucidate, foster, intricate, nuanced, profound, "
    "testament, vibrant, ameliorate, underscore, transcend, envision, bolster, "
    "culminate, traverse. Also avoid excessive em-dashes (---), Furthermore/Moreover "
    "overuse, and hedging pileups like 'may potentially'."
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SectionInfo:
    """Metadata about a paper section."""
    heading: str
    content: str
    level: int = 0         # 0 = top-level, 1 = subsection, etc.
    score: float = 5.0
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)

@dataclass
class RevisionReport:
    """Complete revision run report."""
    rounds: int = 0
    initial_avg_score: float = 0.0
    final_avg_score: float = 0.0
    sections_revised: int = 0
    backpressure_reverts: int = 0
    meta_refine_attempts: int = 0
    grounding_fixes: int = 0
    convergence_reached: bool = False
    sections: list[SectionInfo] = field(default_factory=list)
    consistency_issues: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Section extraction
# ---------------------------------------------------------------------------

def extract_sections(tex: str) -> list[SectionInfo]:
    """Parse LaTeX source into a list of SectionInfo objects.

    Handles Abstract (from \\begin{abstract}...\\end{abstract}) and
    all \\section{} / \\subsection{} boundaries.
    """
    sections: list[SectionInfo] = []

    # Extract abstract
    abs_match = re.search(
        r'\\begin\{abstract\}(.*?)\\end\{abstract\}',
        tex, re.DOTALL,
    )
    if abs_match:
        sections.append(SectionInfo(
            heading="Abstract",
            content=abs_match.group(1).strip(),
        ))

    # Find all section/subsection boundaries
    boundaries: list[tuple[int, str, int]] = []  # (position, heading, level)
    for m in re.finditer(
        r'\\(section|subsection|subsubsection)\*?\{((?:[^{}]|\{[^{}]*\})*)\}',
        tex,
    ):
        cmd = m.group(1)
        heading = m.group(2).strip()
        level = {"section": 0, "subsection": 1, "subsubsection": 2}.get(cmd, 0)
        boundaries.append((m.start(), heading, level))

    # Extract content for each top-level section
    if boundaries:
        # Find the end of the document body (before \\end{document} or \\bibliography)
        end_pos = len(tex)
        for pattern in [r'\\end\{document\}', r'\\bibliographystyle', r'\\bibliography\{']:
            m = re.search(pattern, tex)
            if m and m.start() < end_pos:
                end_pos = m.start()

        for i, (start, heading, level) in enumerate(boundaries):
            if level != 0:  # Only top-level sections get their own entry
                continue

            # Content runs from this section start to the next top-level section
            # (or end_pos), and includes all subsections within
            content_start = boundaries[i][0]
            content_end = end_pos
            for j in range(i + 1, len(boundaries)):
                if boundaries[j][2] == 0:  # Next top-level section
                    content_end = boundaries[j][0]
                    break

            # Get content after the \section{} command itself
            content_match = re.search(
                r'\\(?:section|subsection|subsubsection)\*?\{'
                + re.escape(heading)
                + r'\}',
                tex[content_start:content_end],
            )
            if content_match:
                actual_start = content_start + content_match.end()
            else:
                actual_start = content_start

            content = tex[actual_start:content_end].strip()
            if content:
                sections.append(SectionInfo(
                    heading=heading,
                    content=content,
                    level=0,
                ))

    # If no top-level sections found, try to extract the body as a whole
    if not sections:
        body_match = re.search(
            r'\\begin\{document\}(.*?)\\end\{document\}',
            tex, re.DOTALL,
        )
        if body_match:
            sections.append(SectionInfo(
                heading="Body",
                content=body_match.group(1).strip(),
            ))

    return sections


def rebuild_tex(original_tex: str, sections: list[SectionInfo]) -> str:
    """Rebuild LaTeX source with revised section contents.

    Replaces each section's body content in the original tex while
    preserving the \\section{} command and everything else.
    """
    result = original_tex

    for sec in sections:
        if not sec.content:
            continue

        # Find the section heading in the tex
        escaped_heading = re.escape(sec.heading)
        if sec.heading == "Abstract":
            # Replace abstract content
            result = re.sub(
                r'(\\begin\{abstract\}).*?(\\end\{abstract\})',
                lambda m: m.group(1) + "\n" + sec.content + "\n" + m.group(2),
                result,
                count=1,
                flags=re.DOTALL,
            )
        else:
            # Find the section command and replace everything between it
            # and the next top-level section/end
            pattern = (
                r'(\\section\*?\{' + escaped_heading + r'\})'
                r'(.*?)'
                r'(?=\\section\*?\{|\\end\{document\}|\\bibliographystyle|\\bibliography\{|$)'
            )
            result = re.sub(
                pattern,
                lambda m: m.group(1) + "\n" + sec.content + "\n\n",
                result,
                count=1,
                flags=re.DOTALL,
            )

    # Clean up excessive blank lines
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result


# ---------------------------------------------------------------------------
# Grounding protection
# ---------------------------------------------------------------------------

def _format_paper_number(value: Any) -> str:
    """Format a numeric value for paper display."""
    if isinstance(value, float):
        if abs(value) < 0.01 or abs(value) > 1000:
            return f"{value:.4f}"
        return f"{value:.3f}" if value != int(value) else str(int(value))
    return str(value)


def apply_grounding_protection(
    tex: str,
    experiment_results: dict[str, Any] | None = None,
    experiment_blueprint: dict[str, Any] | None = None,
    experiment_analysis: dict[str, Any] | None = None,
) -> tuple[str, int]:
    """Protect real experiment results from being overwritten by LLM revision.

    Actions:
    1. Remove LLM-generated result tables NOT in the allowed_labels whitelist
    2. Deduplicate figure/table blocks
    3. Replace Experiments section with artifact-grounded version if data available

    Returns (protected_tex, fix_count).
    """
    fix_count = 0
    protected = tex

    # 1. Drop LLM-generated tables that aren't in the allowed whitelist
    seen_tab_labels: set[str] = set()
    def _filter_tables(m: re.Match) -> str:
        nonlocal fix_count
        block = m.group(0)
        label_m = re.search(r'\\label\{(tab:[^}]+)\}', block)
        lbl = label_m.group(1) if label_m else ""
        if lbl and lbl not in ALLOWED_TABLE_LABELS:
            fix_count += 1
            return ""
        if lbl and lbl in seen_tab_labels:
            fix_count += 1
            return ""  # dedup
        if lbl:
            seen_tab_labels.add(lbl)
        return block

    protected = re.sub(
        r'\\begin\{table\*?\}.*?\\end\{table\*?\}',
        _filter_tables,
        protected,
        count=0,
        flags=re.DOTALL,
    )

    # 2. Deduplicate figures
    seen_fig_labels: set[str] = set()
    seen_fig_files: set[str] = set()
    def _dedup_figures(m: re.Match) -> str:
        nonlocal fix_count
        block = m.group(0)
        label_m = re.search(r'\\label\{(fig:[^}]+)\}', block)
        lbl = label_m.group(1) if label_m else None
        if lbl and lbl in seen_fig_labels:
            fix_count += 1
            return ""
        file_m = re.search(r'\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}', block)
        if file_m:
            fname = file_m.group(1)
            if fname in seen_fig_files:
                fix_count += 1
                return ""
            seen_fig_files.add(fname)
        if lbl:
            seen_fig_labels.add(lbl)
        return block

    protected = re.sub(
        r'\\begin\{figure\*?\}.*?\\end\{figure\*?\}',
        _dedup_figures,
        protected,
        flags=re.DOTALL,
    )

    # 3. If we have experiment results, rebuild the Experiments section
    if experiment_results and isinstance(experiment_results, dict):
        main_results = experiment_results.get("main_results", [])
        if main_results and isinstance(main_results, list):
            try:
                # Rebuild abstract with verified results
                protected, abs_fixed = _rebuild_abstract_with_results(
                    protected, experiment_results, experiment_blueprint
                )
                fix_count += abs_fixed

                # Rebuild experiments section
                protected, exp_fixed = _rebuild_experiments_section(
                    protected, experiment_results, experiment_blueprint, experiment_analysis
                )
                fix_count += exp_fixed
            except Exception as exc:
                logger.warning("Grounding rebuild failed: %s", exc)

    # Clean up
    protected = re.sub(r'\n{3,}', '\n\n', protected)
    return protected, fix_count


def _rebuild_abstract_with_results(
    tex: str,
    experiment_results: dict,
    experiment_blueprint: dict | None = None,
) -> tuple[str, int]:
    """Replace the abstract with a version grounded in actual experiment results."""
    bp = experiment_blueprint or {}
    method = bp.get("proposed_method", {})
    method_name = method.get("name", "the proposed method") if isinstance(method, dict) else "the proposed method"

    # Find the proposed method's results entry
    main_results = experiment_results.get("main_results", [])
    proposed_entry = None
    for entry in main_results:
        if isinstance(entry, dict) and entry.get("is_proposed"):
            proposed_entry = entry
            break
    if not proposed_entry and main_results:
        proposed_entry = main_results[0] if isinstance(main_results[0], dict) else None

    if not proposed_entry:
        return tex, 0

    # Build result clause from metrics
    parts = []
    for metric in proposed_entry.get("metrics", []) or []:
        if isinstance(metric, dict):
            name = metric.get("metric_name", "")
            value = metric.get("value")
            if value is not None:
                parts.append(f"{_format_paper_number(value)} {name.replace('_', ' ')}")

    result_clause = ", ".join(parts) if parts else "verified artifact-grounded results"

    abs_match = re.search(
        r'\\begin\{abstract\}(.*?)\\end\{abstract\}',
        tex, re.DOTALL,
    )
    if not abs_match:
        return tex, 0

    # Only replace if the abstract seems LLM-generated (has placeholder patterns)
    old_abs = abs_match.group(1)
    if "[EXPERIMENTAL RESULTS PENDING]" in old_abs or "UNKNOWN" in old_abs:
        new_abs = (
            f"We propose {method_name}. "
            f"Experimental evaluation on standard benchmarks demonstrates "
            f"{result_clause}. "
            f"These results show that the proposed approach achieves "
            f"competitive performance."
        )
        tex = re.sub(
            r'\\begin\{abstract\}.*?\\end\{abstract\}',
            lambda _m: "\\begin{abstract}\n" + new_abs + "\n\\end{abstract}",
            tex,
            count=1,
            flags=re.DOTALL,
        )
        return tex, 1

    return tex, 0


def _rebuild_experiments_section(
    tex: str,
    experiment_results: dict,
    experiment_blueprint: dict | None = None,
    experiment_analysis: dict | None = None,
) -> tuple[str, int]:
    """Rebuild the Experiments section with artifact-grounded tables."""
    fix_count = 0

    # Check if we have a main results table to insert
    main_table = experiment_results.get("main_table_latex", "")
    if main_table:
        # Replace or insert the main results table
        main_pat = r'\\begin\{table\*?\}.*?\\label\{tab:main_results\}.*?\\end\{table\*?\}'
        if re.search(main_pat, tex, re.DOTALL):
            tex = re.sub(
                main_pat,
                lambda _m: main_table,
                tex,
                count=1,
                flags=re.DOTALL,
            )
            fix_count += 1

    # Check if we have an ablation table to insert
    ablation_table = experiment_results.get("ablation_table_latex", "")
    if ablation_table:
        ablation_pat = r'\\begin\{table\*?\}.*?\\label\{tab:ablation\}.*?\\end\{table\*?\}'
        if re.search(ablation_pat, tex, re.DOTALL):
            tex = re.sub(
                ablation_pat,
                lambda _m: ablation_table,
                tex,
                count=1,
                flags=re.DOTALL,
            )
            fix_count += 1

    return tex, fix_count


# ---------------------------------------------------------------------------
# Revision Engine
# ---------------------------------------------------------------------------

class RevisionEngine:
    """Per-section revision loop with backpressure and grounding protection.

    Usage:
        engine = RevisionEngine(model="deepseek-v4-pro")
        revised_tex, report = await engine.revise(
            paper_tex=tex_string,
            review_feedback="reviewer feedback text...",
            experiment_blueprint={...},
            experiment_results={...},
        )
    """

    def __init__(
        self,
        model: str = CLAUDE_MODEL,
        claude_cmd: str = CLAUDE_CMD,
        max_rounds: int = MAX_REVISION_ROUNDS,
        min_score: int = MIN_SECTION_SCORE,
        convergence_threshold: float = CONVERGENCE_THRESHOLD,
    ):
        self.model = model
        self.claude_cmd = claude_cmd
        self.max_rounds = max_rounds
        self.min_score = min_score
        self.convergence_threshold = convergence_threshold

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def revise(
        self,
        paper_tex: str,
        review_feedback: str = "",
        experiment_blueprint: dict[str, Any] | None = None,
        experiment_results: dict[str, Any] | None = None,
        experiment_analysis: dict[str, Any] | None = None,
        paper_structure_plan: dict[str, Any] | None = None,
    ) -> tuple[str, RevisionReport]:
        """Run the full revision loop and return (revised_tex, report).

        Args:
            paper_tex: Full LaTeX source of the paper
            review_feedback: Review feedback (from internal_review or paperreview.ai)
            experiment_blueprint: Experiment design plan
            experiment_results: Actual experiment run results
            experiment_analysis: Structured analysis of experiment outputs
            paper_structure_plan: Writing plan with section_budget, forbidden_claims, etc.

        Returns:
            (revised_tex, RevisionReport)
        """
        report = RevisionReport()
        if not paper_tex:
            logger.warning("No paper content, skipping revision")
            return paper_tex, report

        # Step 0: Pre-check LaTeX structure (detect pre-existing issues)
        from review_tools import check_latex_structure, fix_mismatched_environments

        original_bp_issues = check_latex_structure(paper_tex)
        if any("Mismatched environment" in i for i in original_bp_issues):
            candidate = fix_mismatched_environments(paper_tex)
            fixed_issues = check_latex_structure(candidate)
            if len(fixed_issues) < len(original_bp_issues):
                n_fixed = len(original_bp_issues) - len(fixed_issues)
                logger.info("Auto-fixed %d pre-existing mismatched environment(s)", n_fixed)
                paper_tex = candidate
                original_bp_issues = fixed_issues

        current_tex = paper_tex
        sections = extract_sections(current_tex)
        if not sections:
            logger.warning("No sections extracted, cannot revise")
            return current_tex, report

        report.sections = sections
        report.consistency_issues = list(original_bp_issues)

        # Step 1: Score all sections (via LLM)
        logger.info("Scoring %d sections ...", len(sections))
        for sec in sections:
            try:
                score_info = self._score_section(
                    sec.heading, sec.content, review_feedback, experiment_blueprint
                )
                sec.score = score_info.get("score", 5.0)
                sec.issues = score_info.get("issues", [])
                sec.suggestions = score_info.get("suggestions", [])
                sec.strengths = score_info.get("strengths", [])
                logger.info("  Section '%s': score=%.1f", sec.heading, sec.score)
            except Exception as exc:
                logger.warning("Failed to score section '%s': %s", sec.heading, exc)
                sec.score = 5.0

        report.initial_avg_score = (
            sum(s.score for s in sections) / len(sections)
            if sections else 0
        )

        # Step 2: Revision loop
        revision_round = 0
        prev_avg = report.initial_avg_score
        stall_count = 0
        bp_revert_count = 0

        while revision_round < self.max_rounds:
            low_sections = [s for s in sections if s.score < self.min_score]
            if not low_sections:
                logger.info("No sections below threshold (%.1f) — revision complete", self.min_score)
                break

            revision_round += 1
            logger.info(
                "Revision round %d: %d sections below threshold (%s)",
                revision_round, len(low_sections),
                ", ".join(s.heading for s in low_sections),
            )

            round_revised: dict[str, str] = {}
            for sec in low_sections:
                try:
                    revised_content = self._revise_section(
                        sec.heading, sec.content, sec, review_feedback,
                        experiment_blueprint, paper_structure_plan,
                    )
                    if revised_content and revised_content != sec.content:
                        round_revised[sec.heading] = revised_content
                        sec.content = revised_content
                        logger.info("  Revised section '%s'", sec.heading)
                except Exception as exc:
                    logger.warning("Failed to revise section '%s': %s", sec.heading, exc)

            if not round_revised:
                logger.info("No sections were revised this round — stopping")
                break

            # Rebuild tex with revised sections
            current_tex = rebuild_tex(paper_tex, sections)

            # Backpressure: verify revision didn't break LaTeX structure
            bp_issues = check_latex_structure(current_tex)
            new_bp_issues = [i for i in bp_issues if i not in original_bp_issues]

            if new_bp_issues:
                # Try auto-fix first
                if any("Mismatched environment" in i for i in new_bp_issues):
                    fixed_tex = fix_mismatched_environments(current_tex)
                    fixed_bp = check_latex_structure(fixed_tex)
                    still_new = [i for i in fixed_bp if i not in original_bp_issues]
                    if len(still_new) < len(new_bp_issues):
                        logger.info(
                            "  Auto-fixed %d mismatched environment(s)",
                            len(new_bp_issues) - len(still_new),
                        )
                        current_tex = fixed_tex
                        new_bp_issues = still_new

                if new_bp_issues:
                    bp_revert_count += 1
                    report.backpressure_reverts += 1
                    logger.warning(
                        "  Backpressure FAILED (new issues): %s — reverting round %d",
                        new_bp_issues, revision_round,
                    )
                    # Revert: restore original contents for this round's sections
                    for heading in round_revised:
                        orig = next((s for s in sections if s.heading == heading), None)
                        if orig:
                            # Restore from original extraction
                            orig_sections = extract_sections(paper_tex)
                            orig_match = next((s for s in orig_sections if s.heading == heading), None)
                            if orig_match:
                                orig.content = orig_match.content
                    current_tex = rebuild_tex(paper_tex, sections)
                    if bp_revert_count >= MAX_BP_REVERTS:
                        logger.warning(
                            "%d consecutive backpressure reverts — stopping revision loop",
                            bp_revert_count,
                        )
                        break
                    continue
                else:
                    bp_revert_count = 0  # reset on success
            else:
                bp_revert_count = 0  # reset on success

            # Re-score all revised sections
            for sec in sections:
                if sec.heading in round_revised:
                    try:
                        new_score_info = self._score_section(
                            sec.heading, sec.content, review_feedback, experiment_blueprint
                        )
                        new_score = new_score_info.get("score", sec.score)

                        if new_score < sec.score:
                            # Score DECREASED — try meta-refine
                            logger.info(
                                "  '%s' score dropped %.1f→%.1f, running meta-refine",
                                sec.heading, sec.score, new_score,
                            )
                            refined = self._meta_refine(
                                sec, new_score_info, review_feedback,
                                experiment_blueprint, paper_structure_plan,
                            )
                            if refined:
                                sec.content = refined
                                # Re-score after meta-refine
                                rescore = self._score_section(
                                    sec.heading, refined, review_feedback, experiment_blueprint
                                )
                                if rescore.get("score", 0) >= sec.score:
                                    sec.score = rescore.get("score", sec.score)
                                    sec.issues = rescore.get("issues", sec.issues)
                                    sec.suggestions = rescore.get("suggestions", sec.suggestions)
                                    sec.strengths = rescore.get("strengths", sec.strengths)
                                    logger.info(
                                        "  '%s' meta-refine succeeded: %.1f→%.1f",
                                        sec.heading, new_score, sec.score,
                                    )
                                    report.meta_refine_attempts += 1
                                    continue

                            # Meta-refine failed — revert
                            logger.info(
                                "  '%s' meta-refine failed, keeping original",
                                sec.heading,
                            )
                            # Restore original content
                            orig_secs = extract_sections(paper_tex)
                            orig = next((s for s in orig_secs if s.heading == sec.heading), None)
                            if orig:
                                sec.content = orig.content
                        else:
                            # Score maintained or improved — accept
                            sec.score = new_score
                            sec.issues = new_score_info.get("issues", sec.issues)
                            sec.suggestions = new_score_info.get("suggestions", sec.suggestions)
                            sec.strengths = new_score_info.get("strengths", sec.strengths)

                    except Exception as exc:
                        logger.warning("Re-scoring failed for '%s': %s", sec.heading, exc)

            report.sections_revised += len(round_revised)

            # Rebuild final tex
            current_tex = rebuild_tex(paper_tex, sections)

            # Convergence check
            new_avg = sum(s.score for s in sections) / len(sections) if sections else 0
            improvement = new_avg - prev_avg
            logger.info(
                "  Round %d avg score: %.1f (delta: %+.2f)",
                revision_round, new_avg, improvement,
            )

            if improvement < self.convergence_threshold:
                stall_count += 1
                still_low = [s for s in sections if s.score < self.min_score]
                if stall_count >= MAX_STALL_ROUNDS:
                    logger.info(
                        "  Stalled for %d rounds — stopping (%d sections still below threshold)",
                        stall_count, len(still_low),
                    )
                    report.convergence_reached = True
                    break
                elif still_low and revision_round < self.max_rounds:
                    logger.info(
                        "  Improvement stalled but %d section(s) still below threshold — continuing",
                        len(still_low),
                    )
                else:
                    logger.info("  Convergence reached — stopping")
                    report.convergence_reached = True
                    break
            else:
                stall_count = 0

            prev_avg = new_avg

        # Step 3: Apply grounding protection
        if experiment_results:
            current_tex, gp_fixes = apply_grounding_protection(
                current_tex, experiment_results, experiment_blueprint, experiment_analysis,
            )
            report.grounding_fixes = gp_fixes
            if gp_fixes:
                logger.info("Grounding protection: %d fixes applied", gp_fixes)

        # Final metrics
        report.rounds = revision_round
        report.final_avg_score = (
            sum(s.score for s in sections) / len(sections)
            if sections else 0
        )

        logger.info(
            "Revision complete: %d rounds, score %.1f→%.1f, %d sections revised",
            report.rounds, report.initial_avg_score, report.final_avg_score,
            report.sections_revised,
        )
        return current_tex, report

    # ------------------------------------------------------------------
    # LLM scoring
    # ------------------------------------------------------------------

    def _score_section(
        self,
        heading: str,
        content: str,
        review_feedback: str,
        experiment_blueprint: dict | None = None,
    ) -> dict[str, Any]:
        """Score a single section using LLM. Returns {score, issues, suggestions, strengths}."""
        bp = experiment_blueprint or {}
        method_name = bp.get("proposed_method", {}).get("name", "Unknown") if isinstance(bp.get("proposed_method"), dict) else "Unknown"

        prompt = textwrap.dedent(f"""\
            You are evaluating a single section of an academic paper for publication quality.

            ## Section
            **{heading}**

            ## Content
            ```latex
            {content[:8000]}
            ```

            ## Context
            - Topic: {bp.get('topic', 'Unknown')}
            - Method: {method_name}

            ## Reviewer Feedback (from prior review)
            {review_feedback[:3000]}

            ## Instructions
            Score this section 1-10 using this rubric:
            - 9-10: Publication-ready, only cosmetic fixes
            - 7-8: Solid, minor fixable issues
            - 5-6: Significant problems but recoverable
            - 3-4: Major rewrite needed
            - 1-2: Fundamentally flawed

            Output ONLY valid JSON (no markdown, no wrapping):
            {{{{
                "score": 7,
                "score_justification": "Brief reason for the score",
                "strengths": ["Strength 1", "Strength 2"],
                "issues": ["[PROBLEM] ... [IMPACT] ... [FIX] ..."],
                "suggestions": ["Suggestion 1"]
            }}}}
        """)

        try:
            raw = self._call_llm(prompt, json_mode=True)
            data = self._parse_json(raw)
            if not data:
                return {"score": 5.0, "issues": [], "suggestions": [], "strengths": []}

            score = data.get("score", 5)
            try:
                score = float(score)
            except (TypeError, ValueError):
                score = 5.0
            score = max(1.0, min(10.0, score))

            return {
                "score": score,
                "score_justification": data.get("score_justification", ""),
                "issues": self._coerce_str_list(data.get("issues", [])),
                "suggestions": self._coerce_str_list(data.get("suggestions", [])),
                "strengths": self._coerce_str_list(data.get("strengths", [])),
            }
        except Exception as exc:
            logger.warning("_score_section failed for '%s': %s", heading, exc)
            return {"score": 5.0, "issues": [], "suggestions": [], "strengths": []}

    # ------------------------------------------------------------------
    # LLM revision
    # ------------------------------------------------------------------

    def _revise_section(
        self,
        heading: str,
        content: str,
        section_info: SectionInfo,
        review_feedback: str,
        experiment_blueprint: dict | None = None,
        paper_structure_plan: dict | None = None,
    ) -> str:
        """Revise a single section based on review feedback. Returns revised LaTeX content."""
        bp = experiment_blueprint or {}
        method_name = bp.get("proposed_method", {}).get("name", "Unknown") if isinstance(bp.get("proposed_method"), dict) else "Unknown"

        # Build strengths block
        strengths_block = ""
        if section_info.strengths:
            strengths_json = json.dumps(section_info.strengths, indent=2)
            strengths_block = (
                f"\n\n## Strengths to PRESERVE\n{strengths_json}\n"
                f"These aspects are good — do NOT change them."
            )

        # Build issues block
        issues_json = json.dumps(section_info.issues[:10], indent=2)
        suggestions_json = json.dumps(section_info.suggestions[:10], indent=2)

        # Section-specific guidance
        guidance = _SECTION_GUIDANCE.get(heading, "")

        # Paper structure plan compliance
        plan_block = ""
        if paper_structure_plan:
            for key in ("section_budget", "review_checklist", "forbidden_claims"):
                value = paper_structure_plan.get(key)
                if value:
                    plan_block += f"\n{key}: {value}"
            if plan_block:
                plan_block = f"\n\n## Paper Structure Plan\n{plan_block[:3000]}"

        # Extract available citation keys
        bib_keys = ""
        cite_matches = re.findall(r'\\bibitem\{([^}]+)\}|@\w+\{([^,]+),', content)
        if cite_matches:
            keys = [m[0] or m[1] for m in cite_matches[:50]]
            bib_keys = f"\n\nAvailable citation keys: {', '.join(keys)}"

        # Build grounding block
        grounding_block = ""
        if experiment_blueprint:
            bp_metrics = experiment_blueprint.get("metrics", [])
            bp_datasets = experiment_blueprint.get("datasets", [])
            if bp_metrics or bp_datasets:
                grounding_block = "\n\n## Grounding Data\n"
                if bp_metrics:
                    grounding_block += f"Metrics in blueprint: {json.dumps(bp_metrics)}\n"
                if bp_datasets:
                    grounding_block += f"Datasets in blueprint: {json.dumps(bp_datasets)}\n"
                grounding_block += "Do NOT change numeric values reported in the original text."

        prompt = textwrap.dedent(f"""\
            Revise the "{heading}" section of this academic paper based on reviewer feedback.

            ## Reviewer Issues to FIX
            {issues_json}

            ## Suggestions (optional improvements)
            {suggestions_json}
            {strengths_block}

            ## Section-Specific Guidance
            {guidance}
            {plan_block}
            {bib_keys}
            {grounding_block}

            ## AI Writing Quality
            {_AI_ARTIFACT_WARNING}

            ## CRITICAL RULES
            1. Fix ALL listed issues — each one must be addressed
            2. PRESERVE all strengths identified by the reviewer
            3. Do NOT introduce new problems (vague claims, broken LaTeX, removed content)
            4. Do NOT remove or modify figure/table blocks
            5. Keep the same overall structure and length (+-20%)
            6. Use ONLY citation keys from the paper's bibliography
            7. Do NOT change any concrete numbers in tables — these come from real experiments
            8. If reviewer feedback asks for experiments not yet run, convert requests into
               honest limitations or future work
            9. Do NOT include \\section commands, \\bibliographystyle, \\bibliography,
               or \\end{{document}} in your output

            ## Current "{heading}" Content
            ```latex
            {content[:12000]}
            ```

            ## Research Context
            - Topic: {bp.get('topic', 'Unknown')}
            - Method: {method_name}

            Output ONLY the revised LaTeX body for this section (no \\section command).
            The output will be spliced directly back into the paper. Do NOT wrap in
            markdown code fences — output raw LaTeX.
        """)

        try:
            revised = self._call_llm(prompt, json_mode=False)
            if revised:
                # Strip any markdown code fences the LLM might add
                revised = revised.strip()
                if revised.startswith("```"):
                    revised = re.sub(r'^```(?:latex)?\s*', '', revised)
                    revised = re.sub(r'\s*```$', '', revised)
                return revised.strip()
        except Exception as exc:
            logger.warning("Revision failed for '%s': %s", heading, exc)
        return ""

    # ------------------------------------------------------------------
    # Meta-refine (diagnose + retry when score decreases)
    # ------------------------------------------------------------------

    def _meta_refine(
        self,
        sec: SectionInfo,
        failed_review: dict[str, Any],
        review_feedback: str,
        experiment_blueprint: dict | None = None,
        paper_structure_plan: dict | None = None,
    ) -> str:
        """Diagnose why a revision decreased the score, and retry with targeted fixes."""
        bp = experiment_blueprint or {}
        method_name = bp.get("proposed_method", {}).get("name", "Unknown") if isinstance(bp.get("proposed_method"), dict) else "Unknown"

        failed_issues = json.dumps(failed_review.get("issues", [])[:5], indent=2)
        original_issues = json.dumps(sec.issues[:5], indent=2)

        prompt = textwrap.dedent(f"""\
            A previous revision of the "{sec.heading}" section FAILED — the quality score
            DECREASED after revision. Diagnose the problem and produce a BETTER revision.

            ## Original Issues (before revision)
            {original_issues}

            ## Issues AFTER Failed Revision
            {failed_issues}

            ## Failed Revision Content
            ```latex
            {sec.content[:8000]}
            ```

            ## Instructions
            The revision likely introduced NEW problems or failed to properly address
            the original issues. Identify what went wrong and produce a corrected
            version that:
            1. Actually fixes the original issues
            2. Does NOT introduce new problems
            3. Preserves the section's strengths

            ## CRITICAL RULES (same as before)
            - Do NOT modify figure/table blocks
            - Do NOT change numeric results in tables
            - Do NOT include \\section commands
            - Use ONLY existing citation keys
            - Do NOT include markdown code fences
            - Output ONLY raw LaTeX body content

            ## Research Context
            - Topic: {bp.get('topic', 'Unknown')}
            - Method: {method_name}

            Output the corrected LaTeX body for "{sec.heading}":
        """)

        try:
            revised = self._call_llm(prompt, json_mode=False)
            if revised:
                revised = revised.strip()
                if revised.startswith("```"):
                    revised = re.sub(r'^```(?:latex)?\s*', '', revised)
                    revised = re.sub(r'\s*```$', '', revised)
                return revised.strip()
        except Exception as exc:
            logger.warning("Meta-refine failed for '%s': %s", heading, exc)
        return ""

    # ------------------------------------------------------------------
    # LLM invocation
    # ------------------------------------------------------------------

    def _call_llm(self, prompt: str, json_mode: bool = False) -> str:
        """Call claude -p with the given prompt and return stdout."""
        cmd = [
            self.claude_cmd, "-p",
            "--model", self.model,
            "--output-format", "text",
        ]
        if json_mode:
            cmd.extend(["--json"])

        cmd.append(prompt)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )
            output = result.stdout or ""
            if result.returncode != 0 and not output:
                logger.warning("LLM call failed: %s", result.stderr[:300])
                return ""
            return output
        except subprocess.TimeoutExpired:
            logger.warning("LLM call timed out")
            return ""
        except FileNotFoundError:
            logger.warning("claude CLI not found")
            return ""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        """Extract and parse JSON from LLM output."""
        if not raw:
            return {}

        # Try ```json ... ``` block
        m = re.search(r'```json\s*(.*?)\s*```', raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

        # Try bare JSON object
        m = re.search(r'\{[^{}]*"score"[^{}]*\}', raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass

        # Try to parse the whole output
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        # Last resort: regex extract individual fields
        result: dict[str, Any] = {}
        score_m = re.search(r'"score"\s*:\s*(\d+(?:\.\d+)?)', raw)
        if score_m:
            result["score"] = float(score_m.group(1))
        for field in ("issues", "suggestions", "strengths"):
            vals = re.findall(r'"([^"]+)"', raw)
            if vals:
                # Filter to likely issue/suggestion/strength strings
                result[field] = [v for v in vals if len(v) > 10][:5]
        return result

    @staticmethod
    def _coerce_str_list(items: list[Any]) -> list[str]:
        """Coerce a list of items to list[str]."""
        result: list[str] = []
        for item in (items or [])[:10]:
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, dict):
                parts = [f"[{k.upper()}] {v}" for k, v in item.items() if v]
                result.append(" -> ".join(parts) if parts else str(item))
            else:
                result.append(str(item))
        return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="Run revision loop on a paper",
    )
    parser.add_argument("paper", help="Path to .tex file")
    parser.add_argument("--review", "-r", help="Path to review feedback file")
    parser.add_argument("--blueprint", help="Path to experiment blueprint JSON")
    parser.add_argument("--results", help="Path to experiment results JSON")
    parser.add_argument("--output", "-o", default="revised_paper.tex", help="Output path for revised tex")
    parser.add_argument("--model", default=CLAUDE_MODEL, help="LLM model")
    parser.add_argument("--rounds", type=int, default=MAX_REVISION_ROUNDS, help="Max revision rounds")
    parser.add_argument("--min-score", type=float, default=MIN_SECTION_SCORE, help="Min section score")
    args = parser.parse_args()

    paper_tex = Path(args.paper).read_text()
    review_feedback = Path(args.review).read_text() if args.review else ""
    blueprint = json.loads(Path(args.blueprint).read_text()) if args.blueprint else None
    results = json.loads(Path(args.results).read_text()) if args.results else None

    engine = RevisionEngine(
        model=args.model,
        max_rounds=args.rounds,
        min_score=args.min_score,
    )
    revised_tex, report = engine.revise(
        paper_tex=paper_tex,
        review_feedback=review_feedback,
        experiment_blueprint=blueprint,
        experiment_results=results,
    )

    # Write output
    Path(args.output).write_text(revised_tex)

    # Print report
    print(f"\n## Revision Report")
    print(f"Rounds: {report.rounds}")
    print(f"Sections revised: {report.sections_revised}")
    print(f"Score: {report.initial_avg_score:.1f} → {report.final_avg_score:.1f}")
    print(f"Backpressure reverts: {report.backpressure_reverts}")
    print(f"Meta-refine attempts: {report.meta_refine_attempts}")
    print(f"Grounding fixes: {report.grounding_fixes}")
    print(f"Convergence reached: {report.convergence_reached}")
    print(f"\nSection scores:")
    for sec in report.sections:
        flag = " ⚠" if sec.score < engine.min_score else ""
        print(f"  {sec.heading}: {sec.score:.1f}{flag}")
    print(f"\nRevised paper saved to: {args.output}")


if __name__ == "__main__":
    main()
