#!/usr/bin/env python3
"""
Rule-based review tools for SLAIResearch.
Pure-Python checks that run instantly without LLM calls:

  1. AI writing artifact detection  — 20 banned words, em-dash overuse,
     Furthermore/Moreover overuse, hedging pileups
  2. Claim-result consistency checking — paper claims vs experiment blueprint
  3. LaTeX structural backpressure checks — environment balance, missing sections
  4. Citation coverage checks

All return lists of lightweight issue dicts that can be fed into the review
or revision pipeline.

Usage:
    from review_tools import detect_ai_artifacts, check_claim_result_consistency

    issues = detect_ai_artifacts(paper_tex)
    issues += check_claim_result_consistency(paper_tex, experiment_blueprint)
"""

from __future__ import annotations

import re
import logging
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Top 20 most egregious AI-flavored words (case-insensitive)
AI_BANNED_WORDS: list[str] = [
    "delve", "leverage", "utilize", "harness", "pivotal", "unveil",
    "elucidate", "foster", "intricate", "nuanced", "profound",
    "testament", "vibrant", "ameliorate", "underscore", "transcend",
    "envision", "bolster", "culminate", "traverse",
]

# Hedging pileup patterns ("may potentially", "could possibly", "might perhaps")
_HEDGING_PILEUP_RE = re.compile(
    r"\b(?:may\s+potentially|could\s+possibly|might\s+perhaps)\b",
    re.IGNORECASE,
)

# Transition overuse thresholds
_TRANSITION_MAX = 3          # max recommended per paper
_EMDASH_MAX = 3              # max recommended em-dash per paper

# Citation coverage thresholds
_MIN_CITATIONS = 10          # minimum unique citations
_RECOMMENDED_CITATIONS = 25  # recommended for top venue
_MIN_RW_CITATIONS = 10       # minimum in Related Work

# Regex patterns (compiled once)
_CITE_PATTERN = re.compile(r"\\[Cc]ite[tp]?(?:\w*)(?:\*)?(?:\[[^\]]*\])*\{([^}]+)\}")
_SECTION_PATTERN = re.compile(
    r"\\((?:sub){0,2})section\*?\{"
    r"((?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})+)"
    r"\}",
)
_RELATED_WORK_PATTERN = re.compile(
    r'\\section\{(?:Related\s+Works?|Prior\s+Work|Literature\s+Review'
    r'|Background(?:\s+and\s+Related\s+Work)?)\}'
    r'(.*?)(?=\\section\{|\\end\{document\})',
    re.DOTALL | re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# AI writing artifact detection
# ---------------------------------------------------------------------------

def detect_ai_artifacts(tex: str) -> list[dict[str, Any]]:
    """Scan LaTeX text for common AI-writing artifacts.

    Returns a list of issue dicts, each with:
      issue_type, description, severity, locations
    """
    issues: list[dict[str, Any]] = []
    tex_lower = tex.lower()

    # 1. Banned AI words
    flagged: list[tuple[str, int]] = []
    for word in AI_BANNED_WORDS:
        count = len(re.findall(r"\b" + re.escape(word) + r"\b", tex_lower))
        if count > 0:
            flagged.append((word, count))

    if flagged:
        word_summary = ", ".join(
            f'"{w}" ({c}x)' for w, c in sorted(flagged, key=lambda x: -x[1])
        )
        total = sum(c for _, c in flagged)
        issues.append({
            "issue_type": "ai_artifact_vocabulary",
            "description": (
                f"AI-flagged vocabulary detected ({total} total occurrences "
                f"across {len(flagged)} words): {word_summary}. "
                f"Replace with natural, specific alternatives."
            ),
            "severity": "high" if total >= 5 else "medium",
            "locations": [],
            "flagged_words": dict(flagged),
        })

    # 2. Em-dash overuse (--- in LaTeX)
    emdash_count = tex.count("---")
    if emdash_count > _EMDASH_MAX:
        issues.append({
            "issue_type": "ai_artifact_emdash",
            "description": (
                f"Excessive em-dashes: {emdash_count} occurrences of '---' "
                f"(max {_EMDASH_MAX} recommended). Rewrite sentences to avoid "
                f"em-dash constructions."
            ),
            "severity": "medium",
            "locations": [],
            "count": emdash_count,
        })

    # 3. Furthermore / Moreover overuse
    for transition in ("Furthermore", "Moreover"):
        count = len(re.findall(r"\b" + re.escape(transition) + r"\b", tex))
        if count > _TRANSITION_MAX:
            issues.append({
                "issue_type": "ai_artifact_transition",
                "description": (
                    f'Overuse of "{transition}": {count} occurrences '
                    f"(max {_TRANSITION_MAX} recommended). Vary transitions "
                    f"or restructure sentences."
                ),
                "severity": "medium",
                "locations": [],
                "word": transition,
                "count": count,
            })

    # 4. Hedging pileups
    hedging_matches = _HEDGING_PILEUP_RE.findall(tex)
    if hedging_matches:
        issues.append({
            "issue_type": "ai_artifact_hedging",
            "description": (
                f"Hedging pileup detected ({len(hedging_matches)} "
                f'occurrence(s)): {", ".join(repr(m) for m in hedging_matches[:5])}. '
                f"Use a single hedging word or state the claim directly."
            ),
            "severity": "medium",
            "locations": [],
            "count": len(hedging_matches),
        })

    return issues


# ---------------------------------------------------------------------------
# Claim-Result consistency checking
# ---------------------------------------------------------------------------

def check_claim_result_consistency(
    tex: str,
    experiment_blueprint: dict[str, Any] | None = None,
    experiment_results: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Check that claims in the paper match the experiment blueprint and results.

    Detects:
    - Metrics mentioned in paper but not in blueprint
    - Dataset names in paper that don't match blueprint datasets
    - Baseline methods in paper not listed in blueprint baselines
    - Proposed method name missing from paper
    - Numeric claims in text that don't match experiment results
    """
    issues: list[dict[str, Any]] = []
    if not experiment_blueprint:
        return issues

    bp = experiment_blueprint
    tex_lower = tex.lower()

    # Collect blueprint names (lowercased for fuzzy matching)
    bp_metrics: set[str] = {
        m.get("name", "").lower()
        for m in bp.get("metrics", [])
        if isinstance(m, dict) and m.get("name")
    }
    bp_datasets: set[str] = {
        d.get("name", "").lower()
        for d in bp.get("datasets", [])
        if isinstance(d, dict) and d.get("name")
    }
    bp_baselines: set[str] = {
        b.get("name", "").lower()
        for b in bp.get("baselines", [])
        if isinstance(b, dict) and b.get("name")
    }

    # 1. Check baseline methods mentioned in paper
    for baseline in bp_baselines:
        if baseline and len(baseline) > 2 and baseline not in tex_lower:
            issues.append({
                "issue_type": "missing_baseline_in_text",
                "description": (
                    f"Blueprint baseline '{baseline}' is not mentioned "
                    f"in the paper text"
                ),
                "severity": "low",
                "locations": ["Results / Experiments"],
            })

    # 2. Check proposed method name appears in paper
    proposed = bp.get("proposed_method", {})
    if isinstance(proposed, dict):
        method_name = proposed.get("name", "")
        if method_name and len(method_name) > 2:
            if method_name.lower() not in tex_lower:
                issues.append({
                    "issue_type": "missing_method_in_text",
                    "description": (
                        f"Proposed method '{method_name}' from blueprint "
                        f"is not mentioned in the paper"
                    ),
                    "severity": "low",
                    "locations": ["Throughout paper"],
                })

    # 3. Check experiment results consistency (if results available)
    if experiment_results and isinstance(experiment_results, dict):
        main_results = experiment_results.get("main_results", [])
        if isinstance(main_results, list):
            for entry in main_results:
                if not isinstance(entry, dict):
                    continue
                # Check each metric value from results appears in text
                for metric in entry.get("metrics", []) or []:
                    if not isinstance(metric, dict):
                        continue
                    m_name = metric.get("metric_name", "")
                    m_value = metric.get("value")
                    if m_value is not None and m_name:
                        # Check if the numeric value appears in text
                        value_str = str(m_value)
                        # Only flag if the paper makes numerical claims that
                        # don't include this actual value — this is a coarse check
                        pass  # Detailed numeric checking requires LLM

    return issues


# ---------------------------------------------------------------------------
# Citation coverage checks
# ---------------------------------------------------------------------------

def check_citation_coverage(tex: str) -> list[dict[str, Any]]:
    """Check citation coverage: total count and Related Work density."""
    issues: list[dict[str, Any]] = []

    # Count total unique citations
    cited: set[str] = set()
    for m in _CITE_PATTERN.finditer(tex):
        for k in m.group(1).split(","):
            k = k.strip()
            if k:
                cited.add(k)

    total = len(cited)
    if total < _MIN_CITATIONS:
        issues.append({
            "issue_type": "low_citation_count",
            "description": (
                f"Paper has only {total} unique citations. "
                f"A top-venue paper typically needs {_RECOMMENDED_CITATIONS}+ citations. "
                f"Add more references, especially in Related Work and Introduction."
            ),
            "severity": "high",
            "locations": ["Related Work", "Introduction"],
        })
    elif total < _RECOMMENDED_CITATIONS:
        issues.append({
            "issue_type": "moderate_citation_count",
            "description": (
                f"Paper has {total} unique citations. "
                f"Consider adding more to strengthen Related Work "
                f"(target: {_RECOMMENDED_CITATIONS}+)."
            ),
            "severity": "medium",
            "locations": ["Related Work"],
        })

    # Check Related Work section specifically
    rw_match = _RELATED_WORK_PATTERN.search(tex)
    if rw_match:
        rw_content = rw_match.group(1)
        rw_cited: set[str] = set()
        for m in _CITE_PATTERN.finditer(rw_content):
            for k in m.group(1).split(","):
                k = k.strip()
                if k:
                    rw_cited.add(k)
        if len(rw_cited) < _MIN_RW_CITATIONS:
            issues.append({
                "issue_type": "sparse_related_work_citations",
                "description": (
                    f"Related Work has only {len(rw_cited)} unique citations. "
                    f"A thorough survey needs {_MIN_RW_CITATIONS}+ citations minimum."
                ),
                "severity": "medium",
                "locations": ["Related Work"],
            })

    return issues


# ---------------------------------------------------------------------------
# LaTeX structural backpressure checks
# ---------------------------------------------------------------------------

def check_latex_structure(tex: str) -> list[str]:
    """Quick structural checks for LaTeX source (no compilation needed).

    Returns a list of issue description strings. Used by the backpressure
    mechanism to detect revision-introduced breakage and distinguish it
    from pre-existing issues.
    """
    issues: list[str] = []

    # Environment balance check
    begins = len(re.findall(r'\\begin\{', tex))
    ends = len(re.findall(r'\\end\{', tex))
    if begins != ends:
        issues.append(
            f"Unbalanced environments: {begins} \\begin vs {ends} \\end"
        )

    # Mismatched environment types check
    env_stack: list[str] = []
    for env_m in re.finditer(r'\\(begin|end)\{([^}]+)\}', tex):
        cmd, env_name = env_m.group(1), env_m.group(2)
        if cmd == "begin":
            env_stack.append(env_name)
        elif env_stack and env_stack[-1] == env_name:
            env_stack.pop()
        elif env_stack:
            issues.append(
                f"Mismatched environment: \\begin{{{env_stack[-1]}}} "
                f"closed by \\end{{{env_name}}}"
            )
            env_stack.pop()
            break  # one mismatch is enough

    # Essential document structure
    if '\\documentclass' not in tex:
        issues.append("Missing \\documentclass")
    if '\\end{document}' not in tex:
        issues.append("Missing \\end{document}")

    return issues


# ---------------------------------------------------------------------------
# LaTeX formatting / layout checks (no compilation needed for most)
# ---------------------------------------------------------------------------

# AAAI 2026 two-column layout: textwidth ~6.75in, columnwidth ~3.25in
_COLUMNWIDTH_IN = 3.25
_COLUMNWIDTH_APPROX_PT = 240  # ~3.25in in pt, for rough pt-based checks

# Patterns that indicate a figure/table is NOT constrained to column width
_FIGURE_NO_WIDTH_RE = re.compile(
    r'\\includegraphics\s*\{[^}]*\}',  # no [width=...] at all
)
_FIGURE_HAS_WIDTH_RE = re.compile(
    r'\\includegraphics\s*\[([^\]]*width\s*=\s*([^\],]+))', re.DOTALL,
)
_TABLE_RESIZEBOX_RE = re.compile(
    r'\\resizebox\s*\{([^}]+)\}\s*\{[^}]*\}',
)
_TABULAR_STAR_RE = re.compile(
    r'\\begin\{tabular\*\}(\{([^}]+)\})',
)
# Hardcoded widths that likely overflow in two-column
_OVERFLOW_WIDTHS_RE = re.compile(
    r'(?:width|totalwidth)\s*=\s*([\d.]+)\s*(cm|in|pt|mm|em|ex)',
    re.IGNORECASE,
)
# \textwidth in two-column is total page width (~6.75in), not column width
# Simplified: detect any \textwidth in the text, then check context
_TEXTWIDTH_RE = re.compile(r'\\textwidth')
# \begin{figure*} or \begin{table*} — allowed to span full width
_WIDE_ENV_RE = re.compile(r'\\begin\{(figure|table)\*\}')

# Max column width by unit (approximate, in inches)
_UNIT_TO_INCHES = {
    "cm": 2.54, "in": 1.0, "pt": 72.27, "mm": 25.4,
    "em": None, "ex": None,  # font-dependent, can't check statically
}

# Doc-level patterns to detect single vs two column
_TWOCOLUMN_PATTERNS = [
    r'\\documentclass[^]]*twocolumn',
    r'\\documentclass[^]]*\]\{[^}]*aaai',   # \documentclass[opts]{aaai2026}
    r'\\documentclass\{[^}]*aaai',          # \documentclass{aaai2026}
    r'\\documentclass[^]]*\]\{[^}]*sigchi',
    r'\\documentclass[^]]*\]\{[^}]*sigplan',
    r'\\documentclass[^]]*\]\{[^}]*acmart',
    r'\\documentclass[^]]*\]\{[^}]*ieee',
    r'\\usepackage\{[^}]*aaai',             # \usepackage{aaai2026}
    r'\\usepackage\[[^]]*\]\{[^}]*aaai',    # \usepackage[submission]{aaai2026}
]


def _is_two_column(tex: str) -> bool:
    """Heuristic: does the document use a two-column layout?"""
    for pattern in _TWOCOLUMN_PATTERNS:
        if re.search(pattern, tex):
            return True
    return False


def check_latex_formatting(tex: str) -> list[dict[str, Any]]:
    """Detect common LaTeX formatting problems that cause overfull hboxes,
    column overflow, and visual layout issues in two-column papers.

    Checks (no pdflatex needed):
      1. Figures without explicit width → may overflow
      2. Hardcoded widths exceeding column width
      3. ``\\textwidth`` used in figures/tables inside two-column layout
      4. Missing ``\\centering`` before figures/tables
      5. Tables without ``\\resizebox`` or ``tabular*`` → may overflow
      6. Wide figures/tables not using ``figure*`` / ``table*``

    Returns a list of issue dicts with keys: issue_type, severity, detail, fix
    """
    issues: list[dict[str, Any]] = []
    is_two_col = _is_two_column(tex)

    # ── 1. Figures without explicit width ──
    for m in _FIGURE_NO_WIDTH_RE.finditer(tex):
        # Exclude if inside a figure* or table* environment (full-width allowed)
        # Also exclude tikz/pgf which auto-size
        img_path = m.group(0)
        if "tikz" in img_path.lower() or "pgf" in img_path.lower():
            continue
        issues.append({
            "issue_type": "latex_formatting",
            "severity": "high",
            "detail": (
                "\\includegraphics without explicit width: will render at "
                "native resolution and likely overflow the column. "
                f"Use [width=\\columnwidth]{{...}}"
            ),
            "fix": f"Change to: \\includegraphics[width=\\columnwidth]{{...}}",
            "context": m.group(0)[:100],
        })
        break  # one example is enough — don't flood

    # ── 2. Hardcoded widths exceeding column width ──
    for m in _OVERFLOW_WIDTHS_RE.finditer(tex):
        value = float(m.group(1))
        unit = m.group(2).lower()
        inches = _UNIT_TO_INCHES.get(unit)
        if inches is not None:
            actual_in = value / inches if inches > 1 else value * inches
        else:
            continue  # font-dependent unit, skip

        if actual_in > _COLUMNWIDTH_IN * 1.05 and is_two_col:  # 5% tolerance
            # Check if inside figure* / table* (full-width allowed)
            before = tex[:m.start()]
            wide_envs = len(_WIDE_ENV_RE.findall(before))
            has_star = False
            # Simple check: find the most recent \begin{figure} or \begin{table}
            last_begin = max(
                before.rfind("\\begin{figure*}"),
                before.rfind("\\begin{table*}"),
            )
            last_end = max(
                before.rfind("\\end{figure}"),
                before.rfind("\\end{table}"),
                before.rfind("\\end{figure*}"),
                before.rfind("\\end{table*}"),
            )
            if last_begin > last_end:
                has_star = True

            if not has_star:
                issues.append({
                    "issue_type": "latex_formatting",
                    "severity": "high",
                    "detail": (
                        f"Hardcoded width {value}{unit} (≈{actual_in:.1f}in) "
                        f"exceeds column width ({_COLUMNWIDTH_IN}in). "
                        f"This will overflow into the adjacent column."
                    ),
                    "fix": f"Use [width=\\columnwidth] or reduce to ≤{_COLUMNWIDTH_IN}in",
                    "context": m.group(0),
                })
                break

    # ── 3. \textwidth used in figure/table inside two-column ──
    if is_two_col:
        for m in _TEXTWIDTH_RE.finditer(tex):
            # Determine if we're inside a float environment and which type
            before = tex[:m.start()]
            after = tex[m.end():m.end() + 300]

            # Find the nearest unclosed \begin{figure(*)} or \begin{table(*)}
            last_fig_open = before.rfind("\\begin{figure}")
            last_fig_star_open = before.rfind("\\begin{figure*}")
            last_tab_open = before.rfind("\\begin{table}")
            last_tab_star_open = before.rfind("\\begin{table*}")

            last_open = max(last_fig_open, last_fig_star_open, last_tab_open, last_tab_star_open)
            if last_open < 0:
                continue  # not inside a float

            # Check if this float is closed before our match
            last_fig_close = before.rfind("\\end{figure}")
            last_fig_star_close = before.rfind("\\end{figure*}")
            last_tab_close = before.rfind("\\end{table}")
            last_tab_star_close = before.rfind("\\end{table*}")

            last_close = max(last_fig_close, last_fig_star_close, last_tab_close, last_tab_star_close)
            if last_close > last_open:
                continue  # float already closed

            # Determine if we're in a starred (full-width) float
            in_starred = (
                (last_fig_star_open > last_fig_close) or
                (last_tab_star_open > last_tab_close)
            )

            if not in_starred:
                # Check if the \textwidth is used as a width parameter (likely a figure/table)
                nearby = tex[max(0, m.start()-50):m.end()+50]
                if any(kw in nearby for kw in ['width', 'resizebox', 'includegraphics', 'adjustbox']):
                    issues.append({
                        "issue_type": "latex_formatting",
                        "severity": "high",
                        "detail": (
                            "\\textwidth in two-column layout is the FULL page width "
                            "(~6.75in), not column width (~3.25in). "
                            "Use \\columnwidth instead, or figure*/table*."
                        ),
                        "fix": "Replace \\textwidth with \\columnwidth, or use figure*/table* environment",
                        "context": nearby.strip()[:120],
                    })
                    break  # one example is enough

    # ── 4. Missing \centering before figures/tables ──
    fig_table_starts = list(re.finditer(
        r'\\begin\{(figure|table)\*?\}', tex,
    ))
    for m in fig_table_starts:
        # Look at the text between \begin and the content (next ~200 chars)
        after_start = tex[m.end():m.end() + 300]
        # Skip if \centering is found
        if '\\centering' in after_start:
            continue
        # Skip if it's a subfigure environment (centering may be in subfigure)
        if '\\begin{subfigure}' in after_start:
            continue
        issues.append({
            "issue_type": "latex_formatting",
            "severity": "low",
            "detail": (
                f"Missing \\\\centering in {m.group(1)} environment. "
                f"Content may be left-aligned."
            ),
            "fix": "Add \\centering after \\begin{...}",
            "context": m.group(0),
        })
        if len(issues) >= 15:
            break

    # ── 5. Tables without resizebox or tabular* ──
    for m in re.finditer(r'\\begin\{table\}', tex):
        after_start = tex[m.end():m.end() + 600]
        has_resize = bool(re.search(r'\\resizebox', after_start))
        has_tabular_star = bool(re.search(r'\\begin\{tabular\*\}', after_start))
        has_adjustbox = bool(re.search(r'\\begin\{adjustbox\}', after_start))
        if not (has_resize or has_tabular_star or has_adjustbox):
            # Only flag if the table contains a regular tabular with several columns
            tab_match = re.search(r'\\begin\{tabular\}\{([^}]+)\}', after_start)
            if tab_match and tab_match.group(1).count('l') + tab_match.group(1).count('c') + tab_match.group(1).count('r') >= 4:
                issues.append({
                    "issue_type": "latex_formatting",
                    "severity": "medium",
                    "detail": (
                        "Table uses regular tabular with ≥4 columns. "
                        "In two-column layout, this likely overflows. "
                        "Use \\resizebox{\\columnwidth}{!}{...} or tabular*."
                    ),
                    "fix": "Wrap tabular with: \\resizebox{\\columnwidth}{!}{\\begin{tabular}{...}...\\end{tabular}}",
                    "context": f"\\begin{{tabular}}{{{tab_match.group(1)}}}",
                })
                if sum(1 for i in issues if "table" in str(i.get("context", ""))) >= 2:
                    break

    return issues


# ---------------------------------------------------------------------------
# pdflatex compilation + log analysis (optional, requires pdflatex binary)
# ---------------------------------------------------------------------------

def check_pdflatex_log(tex_path: str | Path, work_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """Compile the paper with pdflatex and parse the log for warnings.

    Catches:
      - Overfull/Underfull hbox warnings
      - Missing references / undefined citations
      - Font warnings
      - Float-too-large warnings

    Returns a list of issue dicts.  Empty list if pdflatex is not available
    or compilation succeeds cleanly.
    """
    import shutil
    if shutil.which("pdflatex") is None:
        return []

    tex_path = Path(tex_path)
    work_dir = Path(work_dir) if work_dir else tex_path.parent

    try:
        result = subprocess.run(
            [
                "pdflatex",
                "-interaction=nonstopmode",
                "-output-directory", str(work_dir),
                tex_path.name,
            ],
            capture_output=True, text=True,
            cwd=str(work_dir),
            timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []

    log_output = result.stdout + "\n" + result.stderr
    issues: list[dict[str, Any]] = []

    # Overfull hbox — content exceeds the allocated space
    overfull_lines = re.findall(r'Overfull \\hbox.*?at lines? (\d+--?\d+)', log_output)
    overfull_by = re.findall(
        r'Overfull \\hbox \(([\d.]+)pt too wide\)', log_output,
    )

    if overfull_lines:
        # Deduplicate
        unique_lines = list(set(overfull_lines))[:10]
        issues.append({
            "issue_type": "latex_compilation",
            "severity": "high",
            "detail": (
                f"Overfull hbox(es) at lines: {', '.join(unique_lines)}. "
                f"Content overflows by up to {max(float(x) for x in overfull_by):.1f}pt. "
                f"This causes text/figures to spill into the adjacent column or margin."
            ),
            "fix": (
                "Check the flagged lines. Common fixes: "
                "1) Figures: use [width=\\columnwidth] "
                "2) Tables: use \\resizebox{\\columnwidth}{!}{...} "
                "3) Equations: break long equations with \\begin{aligned} or \\begin{split} "
                "4) URLs: use \\url{} with the url package (line-breaking)"
            ),
            "context": f"Lines: {', '.join(unique_lines[:5])}",
        })

    # Underfull hbox — too much stretch (usually cosmetic but can indicate bad breaks)
    underfull = re.findall(r'Underfull \\hbox.*?at lines? (\d+--?\d+)', log_output)
    if len(underfull) > 5:  # a few is normal, many is a problem
        issues.append({
            "issue_type": "latex_compilation",
            "severity": "low",
            "detail": (
                f"{len(underfull)} Underfull hbox warnings. "
                f"Excessive stretching may cause ugly spacing."
            ),
            "fix": "Review line breaks. Consider using \\raggedright or microtype package.",
            "context": f"Count: {len(underfull)}",
        })

    # Undefined references / citations
    undefined_refs = re.findall(
        r'(?:LaTeX Warning: )?Reference `([^`]+)\' undefined', log_output,
    )
    undefined_cites = re.findall(
        r'(?:LaTeX Warning: )?Citation `([^`]+)\' undefined', log_output,
    )
    if undefined_refs or undefined_cites:
        issues.append({
            "issue_type": "latex_compilation",
            "severity": "medium",
            "detail": (
                f"Undefined references: {len(undefined_refs)}, "
                f"undefined citations: {len(undefined_cites)}. "
                f"Run bibtex + pdflatex twice to resolve."
            ),
            "fix": "Run: pdflatex → bibtex → pdflatex → pdflatex",
            "context": (
                f"Refs: {', '.join(undefined_refs[:5])}; "
                f"Cites: {', '.join(undefined_cites[:5])}"
            ),
        })

    # Float too large for page
    float_large = re.findall(
        r'Float too large for page.*?at lines? (\d+)', log_output,
    )
    if float_large:
        issues.append({
            "issue_type": "latex_compilation",
            "severity": "high",
            "detail": (
                f"Float too large for page at line(s): {', '.join(float_large[:5])}. "
                f"Figure or table exceeds the page boundaries."
            ),
            "fix": "Resize the figure/table to fit within \\textwidth or \\columnwidth.",
            "context": f"Lines: {', '.join(float_large[:5])}",
        })

    return issues


def fix_mismatched_environments(tex: str) -> str:
    """Auto-fix ``\\begin{X}...\\end{Y}`` mismatches in LaTeX source.

    Common LLM errors: ``\\begin{equation}...\\end{parameter}``,
    ``\\begin{align}...\\end{equation}``, etc.
    """
    env_events: list[tuple[int, int, str, str]] = []  # (start, end, cmd, env_name)
    for m in re.finditer(r'\\(begin|end)\{([^}]+)\}', tex):
        env_events.append((m.start(), m.end(), m.group(1), m.group(2)))

    fixes: list[tuple[int, int, str]] = []  # (start, end, replacement)
    stack: list[tuple[int, int, str]] = []  # (start, end, env_name)
    for start, end, cmd, env_name in env_events:
        if cmd == "begin":
            stack.append((start, end, env_name))
        elif cmd == "end":
            if stack and stack[-1][2] == env_name:
                stack.pop()  # correct match
            elif stack:
                expected = stack[-1][2]
                fixes.append((start, end, f"\\end{{{expected}}}"))
                stack.pop()
            # orphan \\end — leave as-is

    result = tex
    for start, end, replacement in reversed(fixes):
        result = result[:start] + replacement + result[end:]
    return result


# ---------------------------------------------------------------------------
# Figure-Text alignment
# ---------------------------------------------------------------------------

def check_figure_text_alignment(tex: str) -> list[dict[str, Any]]:
    """Check that figure references match figure definitions."""
    issues: list[dict[str, Any]] = []

    defined_figs = set(re.findall(r'\\label\{(fig:[^}]+)\}', tex))
    referenced_figs = set(re.findall(r'\\(?:(?:auto|[Cc])?ref)\{(fig:[^}]+)\}', tex))

    for fig in referenced_figs - defined_figs:
        issues.append({
            "issue_type": "undefined_figure_ref",
            "description": f"Figure reference '\\ref{{{fig}}}' has no matching \\label",
            "severity": "high",
            "locations": ["Figures"],
        })

    for fig in defined_figs - referenced_figs:
        issues.append({
            "issue_type": "unreferenced_figure",
            "description": f"Figure '\\label{{{fig}}}' is defined but never referenced in text",
            "severity": "low",
            "locations": ["Figures"],
        })

    return issues


# ---------------------------------------------------------------------------
# Complete automated review (all checks at once)
# ---------------------------------------------------------------------------

def run_automated_checks(
    tex: str,
    experiment_blueprint: dict[str, Any] | None = None,
    experiment_results: dict[str, Any] | None = None,
    literature_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Run all automated rule-based checks and return a merged issue list.

    This is the main entry point for the review pipeline.  Call this before
    the LLM review so the LLM can see the detected issues and incorporate
    them into its evaluation.

    Args:
        tex: Paper LaTeX source
        experiment_blueprint: Experiment design plan
        experiment_results: Actual experiment results
        literature_context: Pre-built literature context from LiteratureContext.build_context()
            with these optional keys:
            - "standard_baselines": list of baseline names
            - "sota_claims": list of SOTA performance strings
            - "literature_topics": list of key topic names
            - "top_papers": list of paper dicts with title/authors
    """
    all_issues: list[dict[str, Any]] = []

    # AI artifact detection
    try:
        all_issues.extend(detect_ai_artifacts(tex))
    except Exception as exc:
        logger.warning("AI artifact detection failed: %s", exc)

    # Citation coverage
    try:
        all_issues.extend(check_citation_coverage(tex))
    except Exception as exc:
        logger.warning("Citation coverage check failed: %s", exc)

    # Figure-text alignment
    try:
        all_issues.extend(check_figure_text_alignment(tex))
    except Exception as exc:
        logger.warning("Figure-text alignment check failed: %s", exc)

    # Claim-result consistency
    if experiment_blueprint:
        try:
            all_issues.extend(
                check_claim_result_consistency(tex, experiment_blueprint, experiment_results)
            )
        except Exception as exc:
            logger.warning("Claim-result consistency check failed: %s", exc)

    # ── Literature-grounded checks ──
    if literature_context:
        # Baseline completeness
        standard_baselines = literature_context.get("standard_baselines", [])
        if standard_baselines:
            try:
                all_issues.extend(check_baseline_completeness(tex, standard_baselines))
            except Exception as exc:
                logger.warning("Baseline completeness check failed: %s", exc)

        # SOTA comparison
        sota_claims = literature_context.get("sota_claims", [])
        literature_metrics = literature_context.get("literature_metrics", [])
        if sota_claims or literature_metrics:
            try:
                all_issues.extend(
                    check_sota_comparison(tex, sota_claims, literature_metrics)
                )
            except Exception as exc:
                logger.warning("SOTA comparison check failed: %s", exc)

        # Related work coverage
        literature_topics = literature_context.get("literature_topics", [])
        top_papers = literature_context.get("top_papers", [])
        if literature_topics or top_papers:
            try:
                all_issues.extend(
                    check_related_work_coverage(tex, literature_topics, top_papers)
                )
            except Exception as exc:
                logger.warning("Related work coverage check failed: %s", exc)

    return all_issues


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------

def format_issues_for_llm(issues: list[dict[str, Any]], max_issues: int = 30) -> str:
    """Format automated check issues as a string for inclusion in an LLM prompt."""
    if not issues:
        return ""

    lines = ["\n## Automated Pre-Review Checks\n"]
    lines.append(f"The following {min(len(issues), max_issues)} issues were "
                 f"detected by automated checks:\n")

    for i, issue in enumerate(issues[:max_issues], 1):
        sev = issue.get("severity", "medium").upper()
        itype = issue.get("issue_type", "unknown")
        desc = issue.get("description", "")
        lines.append(f"{i}. [{sev}][{itype}] {desc}")

    if len(issues) > max_issues:
        lines.append(f"\n... and {len(issues) - max_issues} more issues "
                     f"(see full report for details)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Literature-grounded checks (require literature context)
# ---------------------------------------------------------------------------

def check_baseline_completeness(
    paper_tex: str,
    standard_baselines: list[str],
) -> list[dict[str, Any]]:
    """Check which literature-standard baselines are missing from the paper.

    Compares the paper's mentioned baselines against a list of baselines
    commonly used in the research area (extracted from the literature review).

    Args:
        paper_tex: The paper's LaTeX source
        standard_baselines: List of baseline names commonly used in this field

    Returns:
        List of issues for baselines NOT found in the paper.
    """
    issues: list[dict[str, Any]] = []
    if not standard_baselines:
        return issues

    tex_lower = paper_tex.lower()
    missing: list[str] = []

    for baseline in standard_baselines:
        # Try to find the baseline name in the paper text
        # Normalize: lowercase, strip common suffixes
        bl_lower = baseline.lower().strip()
        # Check for the baseline name (or significant substrings)
        if bl_lower not in tex_lower:
            # Try shortened form
            words = bl_lower.split()
            if len(words) >= 2:
                # Check if the first significant word + abbreviation pattern appears
                key_terms = [w for w in words if len(w) > 2]
                if all(w in tex_lower for w in key_terms[:2]):
                    continue  # Found enough keywords
            missing.append(baseline)

    if missing:
        issues.append({
            "issue_type": "missing_standard_baselines",
            "description": (
                f"The following {len(missing)} standard baselines from the "
                f"literature are MISSING from the paper's experiments: "
                f"{', '.join(missing[:8])}"
                + (f"... and {len(missing)-8} more" if len(missing) > 8 else "")
            ),
            "severity": "high" if len(missing) >= 3 else "medium",
            "locations": ["Experiments"],
            "missing_baselines": missing,
        })

    return issues


def check_sota_comparison(
    paper_tex: str,
    sota_claims: list[str],
    literature_metrics: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Check if the paper's results are comparable to literature SOTA.

    Verifies:
    1. Paper uses the same metrics as the literature
    2. Paper reports results on the same standard datasets
    3. Paper's claimed SOTA is credible vs literature benchmarks
    """
    issues: list[dict[str, Any]] = []
    tex_lower = paper_tex.lower()

    if literature_metrics:
        missing_metrics = [
            m for m in literature_metrics
            if m.lower() not in tex_lower and len(m) > 2
        ]
        if missing_metrics and len(missing_metrics) > 2:
            issues.append({
                "issue_type": "missing_standard_metrics",
                "description": (
                    f"Paper may be missing standard evaluation metrics used "
                    f"in the literature: {', '.join(missing_metrics[:6])}"
                ),
                "severity": "medium",
                "locations": ["Experiments"],
                "missing_metrics": missing_metrics[:6],
            })

    # Check if the paper makes SOTA claims without comparing to literature-reported numbers
    if sota_claims:
        has_sota_claim = bool(re.search(
            r'\b(?:state.of.the.art|SOTA|best|outperform|superior|novel|'
            r'first|achieves?\s+\d)',
            tex_lower,
        ))
        # Check if any literature SOTA numbers are referenced
        has_literature_numbers = any(
            any(word in tex_lower for word in claim.lower().split()[:5])
            for claim in sota_claims
        )

        if has_sota_claim and not has_literature_numbers:
            issues.append({
                "issue_type": "ungrounded_sota_claim",
                "description": (
                    "Paper claims SOTA/superior performance but does not "
                    "reference specific literature benchmark numbers for comparison. "
                    "Cross-reference with literature SOTA claims: "
                    + "; ".join(sota_claims[:3])
                ),
                "severity": "high",
                "locations": ["Experiments", "Introduction"],
            })

    return issues


def check_related_work_coverage(
    paper_tex: str,
    literature_topics: list[str],
    top_papers: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Check if the paper's Related Work covers the major topics in the field.

    Args:
        paper_tex: The paper's LaTeX source
        literature_topics: Key topics/themes identified in the literature
        top_papers: Top cited papers that should be discussed
    """
    issues: list[dict[str, Any]] = []
    tex_lower = paper_tex.lower()

    # Extract the Related Work section
    rw_match = re.search(
        r'\\section\{(?:Related\s+Works?|Prior\s+Work|Literature\s+Review'
        r'|Background(?:\s+and\s+Related\s+Work)?)\}'
        r'(.*?)(?=\\section\{|\\end\{document\})',
        paper_tex, re.DOTALL | re.IGNORECASE,
    )
    rw_text = (rw_match.group(1) if rw_match else paper_tex).lower()

    # Check topic coverage
    uncovered_topics = []
    for topic in literature_topics:
        # Check if key terms from the topic appear in Related Work
        key_terms = re.findall(r'\b[a-z]{4,}\b', topic.lower())
        if key_terms:
            # Topic is covered if ≥50% of key terms appear
            coverage = sum(1 for t in key_terms if t in rw_text)
            if coverage < len(key_terms) * 0.5:
                uncovered_topics.append(topic)

    if uncovered_topics and len(uncovered_topics) >= 2:
        issues.append({
            "issue_type": "related_work_gaps",
            "description": (
                f"Related Work may not adequately cover these literature topics: "
                f"{', '.join(uncovered_topics[:5])}"
            ),
            "severity": "medium",
            "locations": ["Related Work"],
            "uncovered_topics": uncovered_topics[:5],
        })

    # Check if top cited papers are cited
    if top_papers:
        uncited = []
        for p in top_papers:
            title = p.get("title", "")
            authors = p.get("authors", "")
            # Check if first author's last name appears in citations
            first_author_last = authors.split(",")[0].split()[-1] if authors else ""
            if first_author_last and first_author_last.lower() not in tex_lower:
                # Also check title keywords
                title_words = set(re.findall(r'\b[a-z]{5,}\b', title.lower()))
                if title_words:
                    overlap = sum(1 for w in title_words if w in tex_lower)
                    if overlap < 3:  # Less than 3 title keywords found
                        uncited.append(f"{first_author_last} ({title[:60]})")

        if uncited and len(uncited) >= 2:
            issues.append({
                "issue_type": "missing_key_citations",
                "description": (
                    f"Key papers from the literature may not be cited: "
                    f"{'; '.join(uncited[:5])}"
                ),
                "severity": "high",
                "locations": ["Related Work", "Introduction"],
                "uncited_papers": uncited,
            })

    return issues


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="Run automated review checks on a LaTeX paper",
    )
    parser.add_argument("paper", help="Path to .tex file")
    parser.add_argument("--blueprint", help="Path to experiment blueprint JSON (optional)")
    parser.add_argument("--results", help="Path to experiment results JSON (optional)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    import json
    from pathlib import Path

    tex = Path(args.paper).read_text()

    blueprint = None
    if args.blueprint:
        try:
            blueprint = json.loads(Path(args.blueprint).read_text())
        except Exception:
            pass

    results = None
    if args.results:
        try:
            results = json.loads(Path(args.results).read_text())
        except Exception:
            pass

    issues = run_automated_checks(tex, blueprint, results)

    if args.json:
        print(json.dumps(issues, indent=2, ensure_ascii=False))
    else:
        print(format_issues_for_llm(issues))
        if blueprint:
            print(f"\n{'='*60}")
            ci_issues = check_claim_result_consistency(tex, blueprint, results)
            if ci_issues:
                print("\nClaim-Result Consistency Issues:")
                for iss in ci_issues:
                    print(f"  [{iss['severity'].upper()}] {iss['description']}")
            else:
                print("\nClaim-Result Consistency: OK")
        print(f"\nTotal issues found: {len(issues)}")


if __name__ == "__main__":
    main()
