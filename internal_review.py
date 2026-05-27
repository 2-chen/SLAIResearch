#!/usr/bin/env python3
"""
Internal multi-perspective paper review system.
Runs 5 reviewer personas in parallel, each evaluating the paper from
a different angle. Runs alongside paperreview.ai to use waiting time.

Usage:
    python internal_review.py paper.pdf -o review/
"""

import sys
import subprocess
import json
import time
import logging
import textwrap
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import CLAUDE_CMD, CLAUDE_MODEL

logging.basicConfig(level=logging.INFO, format="%(asctime)s [review] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("internal_review")

# ---------------------------------------------------------------------------
# Reviewer Personas
# ---------------------------------------------------------------------------

REVIEWER_POOL = [
    {
        "name": "Methodology Expert",
        "role": "方法论与算法专家",
        "focus": "method, algorithm design, theoretical justification, notation",
        "prompt": textwrap.dedent("""\
            You are a senior reviewer specializing in methodology and algorithm design.
            Evaluate the paper's METHOD section focusing on:

            1. **Technical Soundness**: Is the proposed method mathematically/theoretically sound?
            2. **Novelty**: What is genuinely new vs. incremental? Be specific.
            3. **Clarity of Description**: Can a competent researcher reproduce the method from the description?
            4. **Design Justification**: Are design choices justified (not arbitrary)?
            5. **Notation**: Is mathematical notation consistent, well-defined, and standard?

            For each issue found, use format:
            [PROBLEM] specific issue → [IMPACT] why it matters → [FIX] concrete suggestion

            Output a structured review section:
            ## Methodology Review
            ### Strengths (3-5)
            ### Weaknesses (3-5)
            ### Detailed Issues (with PROBLEM→IMPACT→FIX format)
            ### Score (1-10)
            ### Recommendation (Accept / Weak Accept / Borderline / Reject)
        """).strip(),
    },
    {
        "name": "Experiments Reviewer",
        "role": "实验评估专家",
        "focus": "experiments, baselines, metrics, statistical rigor, ablation",
        "prompt": textwrap.dedent("""\
            You are a senior reviewer specializing in experimental evaluation.
            Evaluate the paper's EXPERIMENTS section focusing on:

            1. **Dataset Selection**: Are the datasets standard and appropriate?
            2. **Baseline Comparison**: Are all relevant baselines included? Are they fairly tuned?
            3. **Metrics**: Are the evaluation metrics standard and comprehensive?
            4. **Statistical Rigor**: Error bars, significance tests, multiple seeds?
            5. **Ablation Studies**: Are key components ablated to show their contribution?
            6. **Result Interpretation**: Are claimed improvements actually significant?

            For each issue found, use format:
            [PROBLEM] specific issue → [IMPACT] why it matters → [FIX] concrete suggestion

            Output a structured review section:
            ## Experimental Review
            ### Strengths (3-5)
            ### Weaknesses (3-5)
            ### Missing Baselines / Metrics
            ### Statistical Issues
            ### Detailed Issues (with PROBLEM→IMPACT→FIX format)
            ### Score (1-10)
            ### Recommendation (Accept / Weak Accept / Borderline / Reject)
        """).strip(),
    },
    {
        "name": "Clarity & Writing Reviewer",
        "role": "写作与表达专家",
        "focus": "writing quality, structure, clarity, flow, presentation",
        "prompt": textwrap.dedent("""\
            You are a senior reviewer specializing in academic writing quality.
            Evaluate the paper's WRITING focusing on:

            1. **Structure**: Is the paper well-organized (Abstract→Intro→Related Work→Method→Experiments→Conclusion)?
            2. **Clarity**: Is every claim clear and unambiguous?
            3. **Flow**: Do sections connect logically?
            4. **Conciseness**: Is there redundant or verbose text?
            5. **Title & Abstract**: Do they accurately reflect the contribution?
            6. **Figures & Tables**: Are they well-designed, properly labeled, and informative?
            7. **AI Writing Detection**: Any signs of LLM-generated text (hedging pileups, "delve", "leverage", "furthermore" overuse)?

            For each issue found, use format:
            [PROBLEM] specific issue → [IMPACT] why it matters → [FIX] concrete suggestion

            Output a structured review section:
            ## Writing & Presentation Review
            ### Strengths (3-5)
            ### Weaknesses (3-5)
            ### Structural Issues
            ### Language & Clarity Issues
            ### Figure/Table Issues
            ### AI Writing Flags (if any)
            ### Score (1-10)
            ### Recommendation
        """).strip(),
    },
    {
        "name": "Related Work Reviewer",
        "role": "文献覆盖度专家",
        "focus": "related work coverage, citation completeness, positioning",
        "prompt": textwrap.dedent("""\
            You are a senior reviewer specializing in literature coverage.
            Evaluate the paper's RELATED WORK and CITATIONS focusing on:

            1. **Coverage**: Are all relevant lines of work cited?
            2. **Currency**: Are recent papers (last 2-3 years) adequately covered?
            3. **Positioning**: Does the paper clearly explain how it differs from prior work?
            4. **Missing Citations**: What important papers are NOT cited but should be?
            5. **Over-citation**: Are there citations that don't actually support the claim?
            6. **Reference Quality**: Are the cited papers from reputable venues?

            For each issue found, use format:
            [PROBLEM] specific issue → [IMPACT] why it matters → [FIX] concrete suggestion

            Output a structured review section:
            ## Literature Coverage Review
            ### Strengths
            ### Weaknesses
            ### Missing Citations (be specific: author, year, title)
            ### Positioning Issues
            ### Score (1-10)
            ### Recommendation
        """).strip(),
    },
    {
        "name": "Devils Advocate",
        "role": "魔鬼辩护人",
        "focus": "fundamental flaws, overclaims, hidden assumptions, alternative explanations",
        "prompt": textwrap.dedent("""\
            You are a skeptical reviewer (Devil's Advocate). Your job is to find EVERY possible flaw,
            overclaim, hidden assumption, or alternative explanation.

            Attack the paper from every angle:

            1. **Overclaims**: Does the paper claim more than the evidence supports?
            2. **Hidden Assumptions**: What unstated assumptions does the method rely on?
            3. **Alternative Explanations**: Could the results be explained by something other than the proposed method?
            4. **Reproducibility**: Would another team get the same results?
            5. **Generalizability**: Would this work on different datasets/domains?
            6. **Fair Comparison**: Are baselines unfairly handicapped?
            7. **Cherry Picking**: Are the best results selectively reported?
            8. **Data Leakage**: Any sign of train/test contamination?

            Be harsh but fair. If the paper is genuinely good, say so — but only after trying hard to find flaws.

            Output a structured review section:
            ## Critical Review (Devil's Advocate)
            ### Potential Overclaims
            ### Hidden Assumptions
            ### Alternative Explanations for Results
            ### Reproducibility Concerns
            ### Generalizability Concerns
            ### Fairness of Comparison
            ### Score (1-10)
            ### Overall Verdict (be honest: is this paper fundamentally sound?)
        """).strip(),
    },
]


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def review_paper(paper_path: str, output_dir: str, model: str = CLAUDE_MODEL,
                 parallel: bool = True, reviewers: list[dict] | None = None) -> dict:
    """
    Run all reviewers against *paper_path*.
    Returns a dict with merged review data.
    """
    if reviewers is None:
        reviewers = REVIEWER_POOL

    paper_path = Path(paper_path).resolve()
    if not paper_path.exists():
        raise FileNotFoundError(f"Paper not found: {paper_path}")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Internal review starting — %d reviewers on %s", len(reviewers), paper_path.name)

    # Read paper content for inline review prompts
    paper_text = _read_paper(paper_path)

    results = {}
    if parallel:
        with ThreadPoolExecutor(max_workers=len(reviewers)) as executor:
            futures = {
                executor.submit(_run_reviewer, r, paper_text, paper_path, output_dir): r["name"]
                for r in reviewers
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    results[name] = future.result(timeout=600)
                    logger.info("✓ %s completed (score: %s)", name, results[name].get("score", "?"))
                except Exception as e:
                    logger.error("✗ %s failed: %s", name, e)
                    results[name] = {"error": str(e)}
    else:
        for r in reviewers:
            try:
                results[r["name"]] = _run_reviewer(r, paper_text, paper_path, output_dir)
                logger.info("✓ %s completed (score: %s)", r["name"], results[r["name"]].get("score", "?"))
            except Exception as e:
                logger.error("✗ %s failed: %s", r["name"], e)
                results[r["name"]] = {"error": str(e)}

    # Merge into final review document
    merged = _merge_reviews(results, reviewers)
    return merged


def _run_reviewer(reviewer: dict, paper_text: str, paper_path: Path, output_dir: Path) -> dict:
    """Run a single reviewer via claude -p."""
    prompt = f"""{reviewer['prompt']}

Below is the paper to review. Please provide your detailed evaluation.

PAPER:
{paper_text[:20000]}

Output ONLY your structured review section (no preamble, no meta-commentary)."""

    # Write paper excerpt for reference
    ref_file = output_dir / f"paper_excerpt_{reviewer['name'].replace(' ', '_').lower()}.txt"
    ref_file.write_text(paper_text[:20000])

    cmd = [
        CLAUDE_CMD, "-p",
        "--model", CLAUDE_MODEL,
        "--output-format", "text",
        "--max-budget-usd", "0.50",
        prompt,
    ]

    logger.info("  Reviewing: %s ...", reviewer["name"])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

    output = result.stdout or ""
    if result.returncode != 0 and not output:
        raise RuntimeError(f"claude -p failed: {result.stderr[:300]}")

    # Save individual review
    review_file = output_dir / f"internal_review_{reviewer['name'].replace(' ', '_').lower()}.md"
    review_file.write_text(f"# Internal Review: {reviewer['name']}\n\n{output}")

    # Try to extract score
    score = _extract_score(output)
    return {"reviewer": reviewer["name"], "output": output, "score": score, "file": str(review_file)}


def _extract_score(text: str) -> str:
    """Extract score from reviewer output."""
    import re
    # Look for "Score: X" or "Score (1-10): X" patterns
    for pattern in [r"Score.*?(\d+(?:\.\d+)?)", r"score.*?(\d+(?:\.\d+)?)"]:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(1)
    return "?"


def _merge_reviews(results: dict, reviewer_list: list[dict]) -> dict:
    """Merge individual reviews into a comprehensive document."""
    scores = []
    sections = []
    for name, data in results.items():
        sections.append(f"\n## {name}\n\n")
        if "error" in data:
            sections.append(f"*Reviewer failed: {data['error']}*\n")
        else:
            sections.append(data.get("output", "*No output*"))
            sections.append(f"\n\n*Score: {data.get('score', '?')} / 10*")
            if data.get("score", "?").replace(".", "").isdigit():
                scores.append(float(data["score"]))
        sections.append("\n\n---\n")

    avg_score = sum(scores) / len(scores) if scores else 0
    verdict = "accept" if avg_score >= 7 else ("weak accept" if avg_score >= 5.5 else "revise")

    header = f"""# Internal Multi-Perspective Review

**Date**: {datetime.now().strftime('%Y-%m-%d %H:%M')}
**Reviewers**: {len(reviewer_list)} ({', '.join(r['name'] for r in reviewer_list)})
**Average Score**: {avg_score:.1f} / 10
**Consensus**: {verdict.upper()}

---

"""
    return {"sections": "\n".join(sections), "header": header, "avg_score": avg_score,
            "verdict": verdict, "scores": scores, "individual": results}


def save_review(merged: dict, output_dir: str | Path) -> Path:
    """Save the merged review to a markdown file."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Find next available iteration number
    existing = list(output_dir.glob("internal_review_iter*.md"))
    iter_num = len(existing)

    review_path = output_dir / f"internal_review_iter{iter_num:02d}.md"
    full_text = merged["header"] + merged["sections"]
    review_path.write_text(full_text)

    # Also save summary
    summary_path = output_dir / f"internal_review_iter{iter_num:02d}_summary.md"
    summary = f"""# Internal Review Summary (Iteration {iter_num})
**Average Score**: {merged['avg_score']:.1f} / 10
**Consensus**: {merged['verdict'].upper()}

## Individual Scores
"""
    for name, data in merged.get("individual", {}).items():
        summary += f"- **{name}**: {data.get('score', '?')} / 10\n"

    summary_path.write_text(summary)

    logger.info("Review saved: %s", review_path)
    return review_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_paper(paper_path: Path) -> str:
    """Read paper content from PDF or tex file."""
    if paper_path.suffix == ".pdf":
        # Try pdftotext
        try:
            result = subprocess.run(
                ["pdftotext", "-layout", str(paper_path), "-"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        # Fallback: just note we can't read PDFs
        logger.warning("Cannot extract text from PDF (pdftotext not available)")
        return f"[PDF file: {paper_path.name} — please review the PDF directly]"
    else:
        return paper_path.read_text()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Internal multi-perspective paper review")
    parser.add_argument("paper", help="Path to paper PDF or .tex file")
    parser.add_argument("-o", "--output", default="review", help="Output directory")
    parser.add_argument("--sequential", action="store_true", help="Run reviewers sequentially")
    parser.add_argument("--skip", nargs="*", help="Reviewers to skip (name prefix)")
    parser.add_argument("--model", default=CLAUDE_MODEL, help="Model for reviewers")
    args = parser.parse_args()

    # Filter reviewers (don't mutate module-level list)
    reviewer_list = REVIEWER_POOL
    if args.skip:
        reviewer_list = [r for r in REVIEWER_POOL
                         if not any(r["name"].lower().startswith(s.lower()) for s in args.skip)]

    print(f"\nRunning {len(reviewer_list)} reviewers on {args.paper}\n")
    start = time.time()

    merged = review_paper(args.paper, args.output, model=args.model,
                          parallel=not args.sequential, reviewers=reviewer_list)
    save_review(merged, args.output)

    elapsed = time.time() - start
    print(f"\nInternal review complete in {elapsed:.0f}s")
    print(f"Average score: {merged['avg_score']:.1f}/10 → {merged['verdict'].upper()}")
    print(f"Review saved to: {args.output}/")


if __name__ == "__main__":
    main()
