#!/usr/bin/env python3
"""
SLAIResearch — Lean automated research system.
Claude Code is an execution TOOL of this project (not the controller).
The project orchestrates: literature → experiment → paper → review → iterate.

Usage:
    python slairesearch.py run "Your research topic"
    python slairesearch.py resume <topic_or_slug>
    python slairesearch.py status [<topic_or_slug>]
    python slairesearch.py list
"""

import sys
import os
import json
import re
import time
import subprocess
import logging
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

from config import (
    CLAUDE_CMD, CLAUDE_MODEL,
    PAPERREVIEW_EMAIL, PAPERREVIEW_VENUE,
    POLL_INITIAL_WAIT, POLL_INTERVAL, POLL_MAX_WAIT,
    MAX_ITERATIONS, TARGET_VERDICT,
    STAGE_REVIEW_ENABLED, STAGE_REVIEW_MAX_RETRIES,
    STAGE_REVIEW_MODE, STAGE_REVIEW_MODEL,
    LOCAL_EXECUTION_TIMEOUT, LOCAL_EXECUTION_MAX_RETRIES, FORCE_SCO,
    REVISION_ENGINE_ENABLED, REVISION_MAX_ROUNDS,
    REVISION_MIN_SECTION_SCORE, REVISION_CONVERGENCE_THRESHOLD,
    GROUNDING_PROTECTION_ENABLED,
    ACCELERATOR_PREFERENCE, NPU_ENABLED,
)
from state_manager import (
    StateManager, Stage, StageStatus, ResearchState, ReviewRecord,
    StageReviewRecord, StageState,
)
from paperreview_api import (
    submit_paper, poll_review, extract_verdict, review_to_markdown,
)
from sco_runner import (
    submit_job, wait_for_job, stream_logs, SCOConfig,
    run_experiment as sco_run_experiment, detect_gpu, needs_gpu_heuristic,
)
from stage_reviewer import StageReviewer, ReviewVerdict
from revision_engine import RevisionEngine, RevisionReport, apply_grounding_protection
from revision_protocol import RevisionProtocol, TODOList, should_reiterate_pipeline
from context_compressor import ContextCompressor, PipelineState as CompressorState
from review_synthesis import ReviewSynthesizer, SynthesisResult
from exceptions import CheckpointError
from progress import ProgressEmitter

try:
    from review_tools import detect_ai_artifacts, run_automated_checks, format_issues_for_llm
    _REVIEW_TOOLS_AVAILABLE = True
except ImportError:
    _REVIEW_TOOLS_AVAILABLE = False

logger = logging.getLogger("slairesearch")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)

# ── Stage output file mapping (where each stage writes its result) ──
# Used for resume: if a stage is marked "completed", we load its output from here.
_OUTPUT_FILE_MAP: dict[Stage, str] = {
    Stage.LITERATURE_SEARCH: "literature/literature_review.md",
    Stage.HYPOTHESIS_GENERATION: "hypothesis/hypothesis_output.json",
    Stage.BASELINE_FETCHING: "experiment/baseline_context.md",
    Stage.EXPERIMENT_DESIGN: "experiment/experiment_plan.md",
    Stage.EXPERIMENT_EXECUTION: "experiment/experiment_results.json",
    Stage.PAPER_WRITING: "paper",
}

# ── Processing stage order (for progress tracking & resume) ──
_PROCESSING_STAGES = [
    Stage.LITERATURE_SEARCH,
    Stage.HYPOTHESIS_GENERATION,
    Stage.BASELINE_FETCHING,
    Stage.EXPERIMENT_DESIGN,
    Stage.EXPERIMENT_EXECUTION,
    Stage.PAPER_WRITING,
]

# ── Retry configuration for _call_claude ──
_CLAUDE_MAX_RETRIES = 3
_CLAUDE_RETRY_BASE_DELAY = 10       # seconds
_CLAUDE_RETRY_BACKOFF_FACTOR = 2    # exponential multiplier
_CLAUDE_RETRY_MAX_DELAY = 120       # seconds cap
_CLAUDE_TIMEOUT = 600               # seconds per call


def _claude_subprocess_env() -> dict:
    """Return env dict for Claude CLI subprocess calls.

    Resolves API key / base URL from multiple sources (in priority order):
    1. ANTHROPIC_API_KEY / CLAUDE_API_KEY env vars
    2. PROJECT_ROOT/.claude/settings.json
    The result is injected into the child env so Claude never depends on
    cwd-based settings.json lookup.
    """
    from config import CLAUDE_BASE_URL
    env = os.environ.copy()

    api_key = (
        os.environ.get("ANTHROPIC_API_KEY", "")
        or os.environ.get("CLAUDE_API_KEY", "")
    )
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "") or CLAUDE_BASE_URL

    # Fallback: read from project .claude/settings.json
    if not api_key:
        settings_file = PROJECT_ROOT / ".claude" / "settings.json"
        if settings_file.exists():
            try:
                data = json.loads(settings_file.read_text())
                api_key = (data.get("env", {}) or {}).get("ANTHROPIC_API_KEY", "")
                base_url = (data.get("env", {}) or {}).get("ANTHROPIC_BASE_URL", base_url)
            except Exception:
                pass

    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key
    if base_url:
        env["ANTHROPIC_BASE_URL"] = base_url
    env["IS_SANDBOX"] = "1"
    return env


# ======================================================================
# Main CLI
# ======================================================================


def cmd_run(topic: str, force: bool = False) -> None:
    """Execute the full SLAIResearch pipeline for *topic*."""
    sm = StateManager(PROJECT_ROOT / "state")

    if sm.exists(topic):
        if force:
            import shutil
            slug = sm.slug_for(topic)
            shutil.rmtree(PROJECT_ROOT / "state" / slug, ignore_errors=True)
            shutil.rmtree(PROJECT_ROOT / "workspace" / _safe_dirname(topic), ignore_errors=True)
            print(f"Force restart: cleaned previous state for '{topic}'")
        else:
            print(f"Topic already exists. Use '--force' to restart, or 'resume' to continue.")
            return

    work_dir = PROJECT_ROOT / "workspace" / _safe_dirname(topic)
    state = sm.create(topic, work_dir=work_dir)
    state.max_iterations = MAX_ITERATIONS
    state.target_verdict = TARGET_VERDICT
    state.email = PAPERREVIEW_EMAIL
    state.venue = PAPERREVIEW_VENUE
    state.stage_review_enabled = STAGE_REVIEW_ENABLED
    state.stage_review_max_retries = STAGE_REVIEW_MAX_RETRIES
    state.stage_review_mode = STAGE_REVIEW_MODE
    state.stage_review_model = STAGE_REVIEW_MODEL or CLAUDE_MODEL
    sm.save(state)

    # Ensure Claude can find .claude/settings.json from work_dir (needed on Ascend)
    _ensure_claude_config(work_dir)

    # ── Mid-Entry Detection ──
    cc = ContextCompressor(work_dir)
    entry = cc.detect_entry_point()
    if entry.stage != "literature_search":
        print(f"\n  Auto-detected materials found. Entry point: {entry.stage}")
        print(f"  Reason: {entry.reasoning}")
        for name, found in entry.materials_found.items():
            print(f"    {'✓' if found else '✗'} {name}")

    print(f"\n{'='*60}")
    print(f"  SLAIResearch Pipeline")
    print(f"  Topic: {topic}")
    print(f"  Venue: {PAPERREVIEW_VENUE}  |  Max iterations: {MAX_ITERATIONS}")
    print(f"  Model: {CLAUDE_MODEL}")
    print(f"  Entry point: {entry.stage}")
    print(f"  Work dir: {work_dir}")
    print(f"{'='*60}\n")

    _run_pipeline(sm, state)


def cmd_resume(topic_or_slug: str) -> None:
    """Resume pipeline from last saved state with full crash recovery.

    Crash recovery steps:
    1. Load saved state
    2. Reset any stages stuck in "in_progress" (from hard crash / SIGKILL)
    3. If current_stage is "done", report and exit
    4. If current_stage is "failed", find the first failed stage, reset it to "pending"
    5. Run pipeline — completed stages are skipped automatically
    """
    sm = StateManager(PROJECT_ROOT / "state")
    if not sm.exists(topic_or_slug):
        print(f"No saved state found for '{topic_or_slug}'.")
        return

    state = sm.load(topic_or_slug)

    # ── Step 1: Crash recovery — reset stale "in_progress" stages ──
    state = sm.reset_stale_running_stages(state)

    # ── Step 2: Handle terminal states ──
    if state.stage == Stage.DONE.value:
        print(f"Pipeline already completed for '{state.topic}'.")
        print(f"  Iterations: {state.iteration}/{state.max_iterations}")
        return

    if state.stage == Stage.FAILED.value:
        # Find the first failed stage and reset it to "pending" for retry
        fixed = False
        for s in _PROCESSING_STAGES:
            st = state.stages.get(s.value)
            if isinstance(st, StageState) and st.status == StageStatus.FAILED.value:
                st.status = StageStatus.PENDING.value
                st.error = None
                state.stage = s.value
                fixed = True
                print(f"Recovering failed stage '{s.value}' → reset to pending")
                break
        if not fixed:
            # Check if there are any failed stages outside the core processing list
            for name, st in state.stages.items():
                if isinstance(st, StageState) and st.status == StageStatus.FAILED.value:
                    st.status = StageStatus.PENDING.value
                    st.error = None
                    state.stage = name
                    fixed = True
                    print(f"Recovering failed stage '{name}' → reset to pending")
                    break
        if not fixed:
            print("No failed stage found. Starting from the first pending stage.")
            state.stage = sm._first_pending_stage(state)
        sm.save(state)

    # Ensure Claude can find settings (needed on Ascend)
    _ensure_claude_config(Path(state.work_dir))

    # ── Step 3: Show what we're resuming ──
    completed = [
        k for k, v in state.stages.items()
        if isinstance(v, StageState) and v.status == "completed" and v.review_passed
    ]
    pending = [
        k for k, v in state.stages.items()
        if isinstance(v, StageState) and v.status == "pending"
    ]

    print(f"\n{'='*60}")
    print(f"  Resuming: {state.topic}")
    print(f"  Stage:    {state.stage}")
    print(f"  Iter:     {state.iteration}/{state.max_iterations}")
    if completed:
        print(f"  Completed stages: {', '.join(completed)}")
    if pending:
        print(f"  Pending stages:   {', '.join(pending)}")
    print(f"{'='*60}\n")

    _run_pipeline(sm, state)


def cmd_status(topic_or_slug: str | None = None) -> None:
    sm = StateManager(PROJECT_ROOT / "state")
    if topic_or_slug:
        state = sm.load(topic_or_slug)
        _print_status(state)
    else:
        slugs = sm.list_topics()
        if not slugs:
            print("No research topics found.")
            return
        for slug in slugs:
            try:
                s = sm.load(slug)
                print(f"  {s.topic[:60]:60s}  stage={s.stage:20s}  iter={s.iteration}/{s.max_iterations}")
            except Exception:
                print(f"  {slug}  (error)")


def cmd_list() -> None:
    cmd_status(None)


# ======================================================================
# Pipeline orchestrator
# ======================================================================


def _run_pipeline(sm: StateManager, state: ResearchState) -> None:
    """Execute the full pipeline with per-stage review gates and progress tracking.

    Each stage: run → review → pass → next; fail → retry with feedback.
    After all stages, the paperreview.ai iteration loop handles external review.

    Resume-safe: completed + reviewed stages are skipped and their outputs
    are loaded from disk for downstream stages.
    """
    # ── Progress tracking ──
    progress = ProgressEmitter(Path(state.work_dir) / "progress.json")
    total_stages = len(_PROCESSING_STAGES)

    try:
        # ── Phase 1: Core research stages (with per-stage review) ──
        first_run_stages = [
            (Stage.LITERATURE_SEARCH, STAGE_HANDLERS[Stage.LITERATURE_SEARCH],
             f"{state.literature_dir}/literature_review.md"),
            (Stage.HYPOTHESIS_GENERATION, STAGE_HANDLERS[Stage.HYPOTHESIS_GENERATION],
             f"{state.hypothesis_dir}/hypothesis_output.json"),
            (Stage.BASELINE_FETCHING, STAGE_HANDLERS[Stage.BASELINE_FETCHING],
             f"{state.experiment_dir}/baseline_context.md"),
            (Stage.EXPERIMENT_DESIGN, STAGE_HANDLERS[Stage.EXPERIMENT_DESIGN],
             f"{state.experiment_dir}/experiment_plan.md"),
            (Stage.EXPERIMENT_EXECUTION, STAGE_HANDLERS[Stage.EXPERIMENT_EXECUTION], None),
            (Stage.PAPER_WRITING, STAGE_HANDLERS[Stage.PAPER_WRITING],
             f"{state.paper_dir}/*.tex"),
        ]

        for stage_idx, (stage, handler, output_glob) in enumerate(first_run_stages):
            st = state.stages.get(stage.value)

            # ── Skip already completed + reviewed stages ──
            if isinstance(st, StageState) and st.status == "completed" and st.review_passed:
                logger.info("Stage %s already completed + reviewed — skipping", stage.value)
                progress.stage_complete(
                    stage.value, total_stages, stage_idx,
                    message="(skipped — already completed)"
                )
                # Load the stage output so downstream stages can use it
                _load_stage_output_on_resume(state, stage)
                continue

            progress.stage_start(stage.value, total_stages, stage_idx)

            try:
                state = _run_stage_with_review(
                    sm, state, stage, handler, output_glob=output_glob,
                )
                progress.stage_complete(stage.value, total_stages, stage_idx)
            except Exception as exc:
                logger.exception("Stage %s failed irrecoverably: %s", stage.value, exc)
                progress.error(stage.value, str(exc))
                sm.fail_stage(state, stage, str(exc))
                progress.pipeline_complete(False, f"Failed at {stage.value}: {exc}")
                return

        # ── Grounding protection after paper writing ──
        try:
            gp_fixes = _run_grounding_protection(state)
            if gp_fixes > 0:
                print(f"  [green]✓[/green] Grounding protection: {gp_fixes} fix(es) applied")
                progress.substep(Stage.PAPER_WRITING.value, f"Grounding protection: {gp_fixes} fixes")
        except Exception as exc:
            logger.warning("Grounding protection failed: %s", exc)

        # ── Phase 2: PaperReview.ai iteration loop ──
        while state.iteration < state.max_iterations:
            submit_stage = Stage.SUBMIT_REVIEW if state.iteration == 0 else Stage.RESUBMIT
            state = sm.start_stage(state, submit_stage)

            try:
                state = _do_submit(sm, state)
            except Exception as exc:
                logger.exception("Submit failed: %s", exc)
                sm.fail_stage(state, submit_stage, str(exc))
                progress.error(submit_stage.value, str(exc))
                progress.pipeline_complete(False, f"Failed at submit: {exc}")
                return

            state = sm.start_stage(state, Stage.POLL_REVIEW)
            try:
                state, verdict = _do_poll(sm, state)
            except TimeoutError:
                tok = state.reviews[-1].get("token", "N/A") if state.reviews else "N/A"
                print(f"\n[!] Review timed out. Token: {tok}")
                print(f"[!] Check manually: https://paperreview.ai/review?token={tok}")
                progress.substep(Stage.POLL_REVIEW.value, f"Review timed out. Token: {tok}")
                sm.save(state)
                progress.pipeline_complete(False, "Review timed out")
                return
            except Exception as exc:
                logger.exception("Poll failed: %s", exc)
                sm.fail_stage(state, Stage.POLL_REVIEW, str(exc))
                progress.error(Stage.POLL_REVIEW.value, str(exc))
                progress.pipeline_complete(False, f"Failed at poll: {exc}")
                return

            if verdict in ("accept", "weak accept"):
                print(f"\n{'='*60}")
                print(f"  TARGET VERDICT REACHED: {verdict}")
                print(f"  Total iterations: {state.iteration + 1}")
                print(f"{'='*60}")
                state.stage = Stage.DONE.value
                sm.save(state)
                progress.pipeline_complete(True, verdict)
                return

            state.iteration += 1

            # ── Context Compression (mandatory between iterations) ──
            cc = ContextCompressor(state.work_dir)
            try:
                ext_issues = []
                int_issues = []
                review_dir = Path(state.review_dir)
                for rd in sorted(review_dir.rglob("round_*")):
                    ext = rd / "external.md"
                    if ext.exists():
                        ext_text = ext.read_text()
                        ext_issues = [l.strip("-* ") for l in ext_text.split("\n")
                                      if "missing" in l.lower() or "should" in l.lower()][:3]
            except Exception:
                pass

            if state.iteration >= 6:
                cc.compress_hard(
                    topic=state.topic,
                    iteration=state.iteration,
                    paper_path=f"{state.paper_dir}/paper.tex",
                )
                print(f"  [cyan]↻ Context compressed (Level 2: hard reset)[/cyan]")
            else:
                cc.compress_soft(
                    state.iteration,
                    verdict=verdict,
                    external_issues=ext_issues,
                    internal_issues=int_issues,
                )
                print(f"  [cyan]↻ Context compressed (Level 1: soft)[/cyan]")

            # Revise stage also gets its own review gate
            try:
                state = _run_stage_with_review(
                    sm, state, Stage.REVISE, _do_revise,
                    output_glob=f"{state.paper_dir}/*.tex",
                )
            except Exception as exc:
                logger.exception("Revise failed: %s", exc)
                sm.fail_stage(state, Stage.REVISE, str(exc))
                progress.error(Stage.REVISE.value, str(exc))
                progress.pipeline_complete(False, f"Failed at revise: {exc}")
                return

        print(f"\n[!] Reached max iterations ({state.max_iterations}) without target verdict.")
        state.stage = Stage.DONE.value
        sm.save(state)
        progress.pipeline_complete(True, "Max iterations reached")

    except Exception as exc:
        logger.exception("Pipeline crashed: %s", exc)
        try:
            progress.pipeline_complete(False, str(exc))
        except Exception:
            pass
        raise


# ======================================================================
# Stage-level review gate (per-stage approval)
# ======================================================================

def _get_reviewer(state: ResearchState) -> StageReviewer:
    """Build a StageReviewer from the current state configuration."""
    model = state.stage_review_model or STAGE_REVIEW_MODEL or CLAUDE_MODEL
    mode = state.stage_review_mode or STAGE_REVIEW_MODE
    max_retries = state.stage_review_max_retries or STAGE_REVIEW_MAX_RETRIES
    return StageReviewer(model=model, mode=mode, max_retries=max_retries)


def _review_stage_output(
    sm: StateManager,
    state: ResearchState,
    stage: Stage,
    output_path: str,
) -> tuple[ResearchState, ReviewVerdict]:
    """Review a stage's output and record the verdict.

    Returns (updated_state, verdict).
    """
    reviewer = _get_reviewer(state)
    stage_output = ""
    if output_path:
        try:
            stage_output = Path(output_path).read_text()
        except FileNotFoundError:
            logger.warning("Stage output file not found for review: %s", output_path)

    # ── Automated checks for paper-related stages ──
    auto_check_text = ""
    if _REVIEW_TOOLS_AVAILABLE and stage in (Stage.PAPER_WRITING, Stage.REVISE):
        if stage_output and ".tex" in output_path:
            try:
                auto_issues = detect_ai_artifacts(stage_output)
                auto_check_text = format_issues_for_llm(auto_issues)
                if auto_issues:
                    logger.info(
                        "Automated checks found %d issues for %s",
                        len(auto_issues), stage.value,
                    )
            except Exception as exc:
                logger.warning("Automated checks failed for %s: %s", stage.value, exc)

    # Get previous feedback for retry context
    retry_context = sm.get_stage_review_feedback(state, stage)
    st = state.stages.get(stage.value)
    attempt = st.review_attempts if isinstance(st, StageState) else 0

    sm.start_stage_review(state, stage)
    # Inject automated check results into the review context
    extra = {"automated_checks": auto_check_text} if auto_check_text else None
    verdict = reviewer.review(
        stage_name=stage.value,
        stage_output=stage_output,
        topic=state.topic,
        retry_context=retry_context,
        attempt=attempt,
        extra_context=extra,
    )

    # Record the review
    record = StageReviewRecord(
        attempt=attempt + 1,
        stage=stage.value,
        score=verdict.score,
        passed=verdict.passed,
        feedback=verdict.feedback,
        strengths=verdict.strengths,
        weaknesses=verdict.weaknesses,
        critical_issues=verdict.critical_issues,
        suggestion=verdict.suggestion,
        reviewer_mode=verdict.reviewer_mode,
    )
    state = sm.record_stage_review(state, stage, record)

    if verdict.passed:
        logger.info(
            "Stage '%s' review PASSED (attempt %d, score %.1f)",
            stage.value, attempt + 1, verdict.score,
        )
        print(f"  [green]✓[/green] Stage review PASSED (score: {verdict.score:.1f}/10)")
    else:
        logger.warning(
            "Stage '%s' review FAILED (attempt %d, score %.1f)",
            stage.value, attempt + 1, verdict.score,
        )
        print(f"  [yellow]✗[/yellow] Stage review FAILED (score: {verdict.score:.1f}/10)")
        if verdict.critical_issues:
            for ci in verdict.critical_issues[:3]:
                print(f"    !! {ci}")

    return state, verdict


def _run_stage_with_review(
    sm: StateManager,
    state: ResearchState,
    stage: Stage,
    handler_fn,
    *,
    output_glob: str | None = None,  # glob pattern to find output file after stage
) -> ResearchState:
    """Run a pipeline stage with review gate.

    1. Execute the stage (with review feedback as context on retries).
    2. Review the stage output via LLM (or human).
    3. If review passes → proceed to next stage.
    4. If review fails → re-execute the stage with feedback (up to max retries).
    """
    max_retries = state.stage_review_max_retries or STAGE_REVIEW_MAX_RETRIES
    review_enabled = state.stage_review_enabled and STAGE_REVIEW_ENABLED

    for attempt in range(max_retries + 1):
        # --- Execute stage ---
        state = sm.start_stage(state, stage)

        # Build retry feedback from previous failed reviews
        retry_feedback = sm.get_stage_review_feedback(state, stage)
        if retry_feedback and attempt > 0:
            print(f"  [cyan]↻[/cyan] Retrying stage '{stage.value}' "
                  f"(attempt {attempt + 1}/{max_retries + 1}) with review feedback ...")

        try:
            state = handler_fn(sm, state, retry_feedback=retry_feedback)
        except Exception as exc:
            logger.exception("Stage '%s' execution failed: %s", stage.value, exc)
            sm.fail_stage(state, stage, str(exc))
            if attempt < max_retries:
                # Record a failed "review" to count toward retries
                record = StageReviewRecord(
                    attempt=attempt + 1,
                    stage=stage.value,
                    score=0,
                    passed=False,
                    feedback=f"Stage execution error: {exc}",
                    critical_issues=[str(exc)],
                )
                state = sm.record_stage_review(state, stage, record)
                continue
            raise  # Exhausted retries

        state = sm.complete_stage(state, stage)

        # --- Review stage output (if enabled) ---
        if not review_enabled:
            logger.info("Stage review disabled — skipping review for '%s'", stage.value)
            return state

        # Find the output file to review
        output_path = _find_stage_output(state, stage, output_glob)
        state, verdict = _review_stage_output(sm, state, stage, output_path)

        if verdict.passed:
            return state  # Stage approved!

        # Review failed — check retries
        if sm.stage_review_retries_exhausted(state, stage):
            logger.warning(
                "Stage '%s' exceeded max review retries (%d). Proceeding anyway.",
                stage.value, max_retries,
            )
            print(f"  [yellow]![/yellow] Max retries exhausted for '{stage.value}' — proceeding anyway")
            return state

    # Should only reach here if max retries loop ends naturally
    logger.warning("Stage '%s' exhausted all %d retries.", stage.value, max_retries)
    return state


def _find_stage_output(
    state: ResearchState, stage: Stage, output_glob: str | None
) -> str:
    """Find the output file to review for a given stage."""
    # Try explicit output path from stage metadata
    st = state.stages.get(stage.value)
    if isinstance(st, StageState) and st.meta:
        for key in ("output_file", "prompt_file"):
            if key in st.meta:
                cand = st.meta[key]
                # For claude -p output, the output is the _output.md variant
                out_cand = cand.replace("_prompt.md", "_output.md")
                if Path(out_cand).exists():
                    return out_cand
                if Path(cand).exists():
                    return cand

    # Fall back to glob pattern
    if output_glob:
        import glob as _glob
        matches = sorted(_glob.glob(output_glob))
        if matches:
            return matches[-1]  # Most recent

    # Stage-specific fallbacks
    fallbacks = {
        Stage.LITERATURE_SEARCH: Path(state.literature_dir) / "literature_review.md",
        Stage.EXPERIMENT_DESIGN: Path(state.experiment_dir) / "experiment_plan.md",
        Stage.PAPER_WRITING: Path(state.paper_dir),
        Stage.REVISE: Path(state.paper_dir),
    }
    fb = fallbacks.get(stage)
    if fb and fb.exists():
        if fb.is_dir():
            # For paper writing, find the main .tex or .pdf
            tex_files = sorted(fb.rglob("*.tex"))
            if tex_files:
                return str(tex_files[-1])
            pdf_files = sorted(fb.rglob("*.pdf"))
            if pdf_files:
                return str(pdf_files[-1])
        return str(fb)

    return ""


# ======================================================================
# Stage handlers
# ======================================================================


def _do_literature_search(sm: StateManager, state: ResearchState, retry_feedback: str = "") -> ResearchState:
    prompt = _load_prompt("literature_search.md",
        TOPIC=state.topic,
        OUTPUT_DIR=state.literature_dir,
    )
    logger.info("Calling Claude Code for literature search …")
    return _call_claude(sm, state, Stage.LITERATURE_SEARCH, prompt, retry_feedback)


def _do_hypothesis_generation(sm: StateManager, state: ResearchState, retry_feedback: str = "") -> ResearchState:
    """Run ReAct-based hypothesis generation using hypothesis_engine.py."""
    from hypothesis_engine import HypothesisEngine
    from config import HYPOTHESIS_MAX_REACT_ROUNDS, HYPOTHESIS_TOP_K_PDFS, HYPOTHESIS_MAX_PAPERS

    logger.info("Starting hypothesis generation (ReAct engine) ...")
    engine = HypothesisEngine(
        topic=state.topic,
        literature_dir=Path(state.literature_dir),
        work_dir=Path(state.hypothesis_dir),
        max_react_rounds=HYPOTHESIS_MAX_REACT_ROUNDS,
        top_k_pdfs=HYPOTHESIS_TOP_K_PDFS,
        max_papers_per_search=HYPOTHESIS_MAX_PAPERS,
    )

    # If retrying with feedback, inject it into the engine state
    if retry_feedback:
        react_state = engine.state
        react_state.add_facts([f"[Previous review feedback] {retry_feedback}"])
        # Mark previous hypotheses as needing revision
        (Path(state.hypothesis_dir) / "review_feedback.md").write_text(retry_feedback)

    result = engine.run()
    logger.info("Hypothesis generation complete. %d hypotheses, %d papers analyzed.",
                len(result.get("hypotheses", [])), result.get("total_papers_analyzed", 0))
    return sm.complete_stage(state, Stage.HYPOTHESIS_GENERATION, {
        "hypotheses_count": len(result.get("hypotheses", [])),
        "papers_analyzed": result.get("total_papers_analyzed", 0),
    })


def _do_baseline_fetching(sm: StateManager, state: ResearchState, retry_feedback: str = "") -> ResearchState:
    """Clone baseline GitHub repos for structural reference in experiment design."""
    from config import BASELINE_CLONING_ENABLED, BASELINE_MAX_REPOS

    if not BASELINE_CLONING_ENABLED:
        logger.info("Baseline cloning disabled — skipping")
        (Path(state.experiment_dir) / "baseline_context.md").write_text("")
        return sm.complete_stage(state, Stage.BASELINE_FETCHING, {"skipped": True})

    try:
        from literature_context import LiteratureContext
        lc = LiteratureContext(state.work_dir)
        landscape = lc._parse_literature()
        papers = landscape.papers if landscape else []
    except Exception as e:
        logger.warning("Could not parse literature for baselines: %s", e)
        papers = []

    # Extract baseline method names from hypothesis output
    method_names: list[str] = []
    hypo_file = Path(state.hypothesis_dir) / "hypothesis_output.json"
    if hypo_file.exists():
        try:
            import json
            hypo = json.loads(hypo_file.read_text())
            for h in hypo.get("hypotheses", []):
                if isinstance(h, dict):
                    # Primary fields
                    for key in ("baselines", "baseline_methods"):
                        val = h.get(key, [])
                        if isinstance(val, list):
                            method_names.extend([v for v in val if isinstance(v, str)])
                    # Fallback: extract from method_outline and key_references
                    if not method_names:
                        method_names.extend(
                            _extract_baselines_from_text(h.get("method_outline", ""))
                        )
                        method_names.extend(
                            _extract_baselines_from_text(h.get("rationale", ""))
                        )
                        refs = h.get("key_references", [])
                        if isinstance(refs, list):
                            for ref in refs:
                                if isinstance(ref, dict):
                                    method_names.extend(
                                        _extract_baselines_from_text(ref.get("title", ""))
                                    )
        except Exception:
            pass

    # Deduplicate while preserving order
    seen: set[str] = set()
    method_names = [n for n in method_names if n and not (n in seen or seen.add(n))]

    # Also try to extract from experiment plan if it exists (from literature context)
    if not method_names and papers:
        method_names = list(landscape.standard_baselines) if landscape else []

    if not method_names:
        logger.info("No baseline method names found — skipping baseline fetching")
        (Path(state.experiment_dir) / "baseline_context.md").write_text("")
        return sm.complete_stage(state, Stage.BASELINE_FETCHING, {
            "skipped": True,
            "reason": "no methods found (hypothesis output missing 'baselines' field; update prompt template to include it)",
        })

    logger.info("Fetching baseline repos for %d methods...", len(method_names))
    try:
        from baseline_finder import BaselineFinder
        bf = BaselineFinder(
            cache_dir=Path(state.work_dir) / ".." / ".shared" / "baselines",
            max_repos=BASELINE_MAX_REPOS,
        )
        contexts = bf.find_and_extract(papers=papers, method_names=method_names[:BASELINE_MAX_REPOS])
        prompt_block = bf.format_for_prompt(contexts)

        output_path = Path(state.experiment_dir) / "baseline_context.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(prompt_block)
        logger.info("Baseline context saved (%d chars, %d repos)", len(prompt_block), len(contexts))

        return sm.complete_stage(state, Stage.BASELINE_FETCHING, {
            "repos_found": len(contexts),
            "methods_searched": len(method_names[:BASELINE_MAX_REPOS]),
        })
    except Exception as e:
        logger.warning("Baseline fetching failed (non-fatal): %s", e)
        (Path(state.experiment_dir) / "baseline_context.md").write_text("")
        return sm.complete_stage(state, Stage.BASELINE_FETCHING, {
            "error": str(e)[:200], "repos_found": 0,
        })


def _do_experiment_design(sm: StateManager, state: ResearchState, retry_feedback: str = "") -> ResearchState:
    """Prompt-driven experiment phase — Claude Code autonomously handles design
    through execution via the experiment scientist system prompt.

    This replaces the old hardcoded experiment pipeline (preflight → schedule →
    local/SCO dispatch → debug loop).  Claude Code now owns the full experiment
    lifecycle: environment check → code writing → local/SCO execution → debug
    → evaluation → report.
    """
    from config import (
        EXPERIMENT_SYSTEM_PROMPT, EXPERIMENT_TASK_TEMPLATE,
        SCO_WORKSPACE, SCO_AEC2, SCO_IMAGE, SCO_STORAGE_MOUNT,
        SCO_WORKER_SPEC_MAP, MAX_COMPUTE_BUDGET_GPU_HOURS,
        EXPERIMENT_MAX_DEBUG_ROUNDS, EXPERIMENT_CLAUDE_TIMEOUT,
        LOCAL_EXECUTION_TIMEOUT, LOCAL_EXECUTION_MAX_RETRIES,
        DOWNLOAD_CACHE_DIR,
        ACCELERATOR_PREFERENCE, NPU_ENABLED,
    )

    # Detect NPU availability for context injection
    npu_context = ""
    try:
        from accelerator import detect_accelerator, check_sco_npu_compatibility
        accel_info = detect_accelerator()
        if accel_info.npu_count > 0:
            npu_context = (
                f"\n\n## NPU 环境信息\n"
                f"- NPU 设备数: {accel_info.npu_count}\n"
                f"- 加速器类型: {accel_info.accelerator_type}\n"
                f"- 使用 NPU 时，请使用 device-agnostic 代码 (torch.device)，避免直接调用 torch.cuda\n"
                f"- torch_npu 包需要与 CANN 版本匹配\n"
            )
            if not accel_info.supports_cuda_api:
                sco_compat = check_sco_npu_compatibility()
                npu_context += (
                    f"- SCO 云 GPU 兼容性: {sco_compat['compatible']}\n"
                    f"- {sco_compat['recommendation']}\n"
                )
            logger.info("NPU environment detected — injecting context into experiment prompt")
    except ImportError:
        logger.info("accelerator module not available — skipping NPU context injection")
    except Exception as exc:
        logger.warning("NPU detection failed (non-fatal): %s", exc)

    # Read inputs
    lit = _read_or(state.literature_dir, "literature_review.md")
    hyp = _read_or(state.hypothesis_dir, "hypothesis_output.json")

    # Baseline context (if available)
    baseline_ctx = ""
    baseline_path = Path(state.experiment_dir) / "baseline_context.md"
    if baseline_path.exists():
        baseline_ctx = baseline_path.read_text()

    # Load previous execution trace for resume context
    trace_context = _load_execution_trace(state)

    # Render the task template with variable substitution
    task_prompt = _render_template_file(
        EXPERIMENT_TASK_TEMPLATE,
        HYPOTHESIS_FILE=str(Path(state.hypothesis_dir) / "hypothesis_output.json"),
        LITERATURE_FILE=str(Path(state.literature_dir) / "literature_review.md"),
        OUTPUT_DIR=state.experiment_dir,
        BASELINE_SECTION=(
            f"- **基线参考**: {baseline_path}\n{baseline_ctx[:5000]}"
            if baseline_ctx
            else "*(无基线代码参考 — 根据文献描述自行设计基线)*"
        ),
        SCO_WORKSPACE=SCO_WORKSPACE,
        SCO_AEC2=SCO_AEC2,
        SCO_IMAGE=SCO_IMAGE,
        SCO_STORAGE_MOUNT=SCO_STORAGE_MOUNT,
        SCO_WORKER_SPEC_1GPU=SCO_WORKER_SPEC_MAP.get(1, "n6ls.iu.i40.1.8c128g"),
        SCO_WORKER_SPEC_2GPU=SCO_WORKER_SPEC_MAP.get(2, "n6ls.iu.i40.2.16c256g"),
        SCO_WORKER_SPEC_4GPU=SCO_WORKER_SPEC_MAP.get(4, "n6ls.iu.i40.4.32c512g"),
        MAX_GPU_HOURS=str(MAX_COMPUTE_BUDGET_GPU_HOURS),
        MAX_DEBUG_ROUNDS=str(EXPERIMENT_MAX_DEBUG_ROUNDS),
        LOCAL_TIMEOUT=str(LOCAL_EXECUTION_TIMEOUT),
        LOCAL_MAX_RETRIES=str(LOCAL_EXECUTION_MAX_RETRIES),
        MODEL_CACHE_DIR=str(Path(DOWNLOAD_CACHE_DIR) / "models"),
    )

    # Inject NPU environment context
    if npu_context:
        task_prompt = task_prompt + npu_context

    # Inject previous execution trace as context for resume
    if trace_context:
        task_prompt = task_prompt + trace_context
        logger.info("Injected execution trace (%d chars) for resume context",
                    len(trace_context))

    # Ensure output directory exists
    os.makedirs(state.experiment_dir, exist_ok=True)

    logger.info("Launching experiment scientist (Claude Code + system prompt) …")
    return _call_claude_with_system_prompt(
        sm, state, Stage.EXPERIMENT_DESIGN,
        task_prompt=task_prompt,
        system_prompt_path=EXPERIMENT_SYSTEM_PROMPT,
        retry_feedback=retry_feedback,
        timeout=EXPERIMENT_CLAUDE_TIMEOUT,
    )


def _do_experiment_execution(sm: StateManager, state: ResearchState, retry_feedback: str = "") -> ResearchState:
    """Execution-only phase — used when experiment_design completed but execution
    was not done (e.g. in a pipeline resume scenario).

    With prompt-driven experiments the design phase already covers execution.
    This stage now checks for existing results and falls back to the design
    path if execution is needed.
    """
    exp_dir = Path(state.experiment_dir)
    results = exp_dir / "experiment_results.json"

    if results.exists():
        logger.info("Experiment results already exist → %s", results)
        return sm.complete_stage(state, Stage.EXPERIMENT_EXECUTION, {
            "results_file": str(results),
            "note": "Results from previous experiment_design session",
        })

    # Results missing — re-run the full experiment scientist
    logger.info("No experiment results found, re-running experiment scientist …")
    return _do_experiment_design(sm, state, retry_feedback)


def _diagnose_experiment_failure(result, state) -> str:
    """Extract diagnostic info from a failed experiment result."""
    parts = [f"Experiment failed (backend={result.backend})"]

    if result.error_summary:
        parts.append(f"Error: {result.error_summary}")

    # Try to read SCO logs for more detail
    log_path = result.log_path
    if log_path and Path(log_path).exists():
        try:
            log_content = Path(log_path).read_text()
            # Extract last 50 lines (most relevant for error)
            tail_lines = log_content.strip().split("\n")[-50:]
            error_lines = [l for l in tail_lines
                          if any(kw in l.lower() for kw in
                                 ["error", "fail", "exception", "traceback",
                                  "killed", "oom", "cuda", "npu", "segfault", "abort"])]
            if error_lines:
                parts.append("Log errors (last 50 lines):")
                parts.extend(f"  | {l}" for l in error_lines[-15:])  # Max 15 error lines
            else:
                # No obvious errors — include the last 10 lines anyway
                parts.append("Log tail (last 10 lines):")
                parts.extend(f"  | {l}" for l in tail_lines[-10:])
        except Exception:
            pass

    # Include retry hint
    parts.append(
        "HINT: The experiment script will be re-run. If the same error persists, "
        "check run_experiment.sh for: dependency installation, GPU memory, "
        "data paths, and hardcoded assumptions."
    )

    return "\n".join(parts)


def _try_fix_experiment_script(script: Path, feedback: str, state) -> bool:
    """Use LLM to attempt a fix of the experiment script based on failure feedback.
    Returns True if the script was modified."""
    if not script.exists():
        return False

    try:
        original = script.read_text()
    except Exception:
        return False

    prompt = f"""You are debugging a failed experiment script. Review the failure feedback
and fix the shell script. Only make minimal, targeted fixes — do NOT rewrite the whole script.

## Experiment script: {script}
```bash
{original}
```

## Failure feedback from previous run:
{feedback}

## Instructions:
1. Identify the most likely cause of failure from the feedback
2. Make the MINIMAL fix to address it (e.g., fix a broken pip install, add missing dependency,
   reduce batch size for OOM, fix a path, increase timeout)
3. If you can't determine the cause, add diagnostic echo statements instead
4. Output ONLY the fixed script in a ```bash block

```bash
<fixed script here>
```"""

    logger.info("Calling LLM to auto-fix experiment script: %s", script)
    try:
        cmd = [CLAUDE_CMD, "-p", "--output-format", "text", "--model", CLAUDE_MODEL,
               "--max-turns", "5"]
        result = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                timeout=300, cwd=str(state.work_dir),
                                env=_claude_subprocess_env())
        output = result.stdout or ""

        # Extract the fixed bash script
        m = re.search(r'```bash\s*\n(.*?)```', output, re.DOTALL)
        if m:
            fixed_script = m.group(1).strip()
            if fixed_script and fixed_script != original.strip():
                # Backup original
                backup = script.with_suffix(".sh.bak")
                script.rename(backup)
                logger.info("Original script backed up to: %s", backup)
                # Write fixed version
                script.write_text(fixed_script + "\n")
                script.chmod(0o755)
                logger.info("Experiment script auto-fixed: %s", script)
                return True
            else:
                logger.info("LLM returned no meaningful changes to experiment script")
        else:
            logger.warning("Could not extract fixed bash script from LLM output")
    except Exception as e:
        logger.warning("Auto-fix experiment script failed: %s", e)

    return False


def _extract_latex_from_output(output_text: str) -> str | None:
    """Try to extract LaTeX source from Claude's paper writing output.

    Handles the case where Claude generates TeX but the Write tool fails
    (e.g. root environment without pre-approved permissions)."""
    # Pattern 1: ```latex ... ``` fenced block (most common)
    m = re.search(r'```(?:latex|tex)\s*\n(.*?)```', output_text, re.DOTALL)
    if m:
        return m.group(1).strip()

    # Pattern 2: \documentclass ... \end{document}
    m = re.search(
        r'(\\documentclass\[.*?\]\{.*?\}.*?\\end\{document\})',
        output_text, re.DOTALL,
    )
    if m:
        return m.group(1).strip()

    # Pattern 3: large ``` block containing \documentclass
    for m in re.finditer(r'```\s*\n(.*?)```', output_text, re.DOTALL):
        block = m.group(1)
        if r'\documentclass' in block and r'\begin{document}' in block:
            return block.strip()

    return None


def _compile_paper_pdf(paper_dir: Path, claude_output: str = "") -> bool:
    """Compile paper.tex to PDF.  If .tex is missing, attempts to extract
    LaTeX from *claude_output* (the paper_writing output.md text) first.

    Returns True if PDF exists after compilation."""
    existing = sorted(paper_dir.rglob("*.pdf"))
    if existing:
        logger.info("PDF already exists: %s", existing[0].name)
        return True

    tex = paper_dir / "paper.tex"
    if not tex.exists():
        # Check if Claude wrote .tex elsewhere
        tex_candidates = sorted(paper_dir.rglob("*.tex"))
        if tex_candidates:
            tex = tex_candidates[0]
        elif claude_output:
            latex = _extract_latex_from_output(claude_output)
            if latex:
                tex.write_text(latex)
                logger.info("Extracted LaTeX from Claude output → %s", tex)
    if not tex.exists():
        logger.warning("No .tex file found in %s — cannot compile PDF", paper_dir)
        return False

    # Copy AAAI template files
    for fname in ("aaai2026.sty", "aaai2026.bst"):
        src = PROJECT_ROOT / "templates" / fname
        if src.exists():
            import shutil
            shutil.copy2(src, paper_dir / fname)

    logger.info("Compiling %s → PDF …", tex.name)
    for _ in range(2):
        r = subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", tex.name],
            capture_output=True, text=True, timeout=120, cwd=str(paper_dir),
        )
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "")[-500:]
            logger.warning("pdflatex failed: %s", tail)

    pdf = paper_dir / "paper.pdf"
    if pdf.exists():
        logger.info("PDF compiled: %s", pdf)
        return True
    logger.warning("PDF compilation failed — no paper.pdf produced")
    return False


def _do_paper_writing(sm: StateManager, state: ResearchState, retry_feedback: str = "") -> ResearchState:
    exp = _read_or(state.experiment_dir, "experiment_report.md")
    lit = _read_or(state.literature_dir, "literature_review.md")
    prompt = _load_prompt("paper_writing.md",
        TOPIC=state.topic,
        LITERATURE_REVIEW=lit[:15000],
        EXPERIMENT_REPORT=exp[:30000],
        OUTPUT_DIR=state.paper_dir,
        VENUE=state.venue,
    )
    logger.info("Calling Claude Code for paper writing …")
    state = _call_claude(sm, state, Stage.PAPER_WRITING, prompt, retry_feedback)

    # Auto-compile TeX → PDF.  If .tex is missing (e.g. Write tool permission
    # denied on root), try extracting LaTeX from Claude's stdout output.
    output_text = _read_or(state.work_dir, "paper_writing_output.md")
    _compile_paper_pdf(Path(state.paper_dir), claude_output=output_text)

    return state


def _format_revision_report(report: RevisionReport) -> str:
    """Format a RevisionReport as a Markdown string."""
    lines = [
        f"# Revision Report",
        f"",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Rounds | {report.rounds} |",
        f"| Sections revised | {report.sections_revised} |",
        f"| Initial avg score | {report.initial_avg_score:.1f} |",
        f"| Final avg score | {report.final_avg_score:.1f} |",
        f"| Backpressure reverts | {report.backpressure_reverts} |",
        f"| Meta-refine attempts | {report.meta_refine_attempts} |",
        f"| Grounding fixes | {report.grounding_fixes} |",
        f"| Convergence reached | {'Yes' if report.convergence_reached else 'No'} |",
        f"",
        f"## Section Scores",
        f"",
    ]
    for sec in report.sections:
        flag = " ⚠️" if sec.score < REVISION_MIN_SECTION_SCORE else ""
        lines.append(f"- **{sec.heading}**: {sec.score:.1f}{flag}")
    if report.consistency_issues:
        lines.append(f"\n## Consistency Issues\n")
        for issue in report.consistency_issues[:10]:
            lines.append(f"- {issue}")
    return "\n".join(lines)


def _run_grounding_protection(state: ResearchState) -> int:
    """Apply grounding protection to the current paper tex.

    Returns the number of fixes applied.
    """
    if not GROUNDING_PROTECTION_ENABLED:
        return 0

    tex_files = sorted(Path(state.paper_dir).rglob("*.tex"))
    if not tex_files:
        logger.warning("No .tex files found for grounding protection")
        return 0

    paper_path = tex_files[-1]
    paper_tex = paper_path.read_text()

    # Load experiment results
    experiment_results = None
    experiment_blueprint = None
    results_path = Path(state.experiment_dir) / "experiment_results.json"
    if results_path.exists():
        try:
            experiment_results = json.loads(results_path.read_text())
        except Exception:
            pass
    bp_path = Path(state.experiment_dir) / "experiment_plan.json"
    if bp_path.exists():
        try:
            experiment_blueprint = json.loads(bp_path.read_text())
        except Exception:
            pass

    if experiment_results:
        protected_tex, fix_count = apply_grounding_protection(
            paper_tex, experiment_results, experiment_blueprint,
        )
        if fix_count > 0:
            # Backup original
            backup_path = paper_path.with_suffix(".pre_grounding.tex")
            backup_path.write_text(paper_tex)
            # Write protected version
            paper_path.write_text(protected_tex)
            logger.info("Grounding protection: %d fixes applied to %s", fix_count, paper_path)
            return fix_count

    return 0


def _do_submit(sm: StateManager, state: ResearchState) -> ResearchState:
    pdf_files = sorted(Path(state.paper_dir).rglob("*.pdf"))
    if not pdf_files:
        raise FileNotFoundError(f"No PDF found in {state.paper_dir}")

    pdf_path = pdf_files[-1]
    logger.info("Uploading %s to paperreview.ai …", pdf_path.name)
    token = submit_paper(str(pdf_path), email=state.email, venue=state.venue)

    record = ReviewRecord(iteration=state.iteration, token=token, submitted_at=str(pdf_path))
    state = sm.add_review(state, record)
    return sm.complete_stage(state, Stage.SUBMIT_REVIEW, {"token": token})


def _do_poll(sm: StateManager, state: ResearchState) -> tuple[ResearchState, str]:
    token = state.reviews[-1]["token"]
    print(f"\n  Iteration {state.iteration} — waiting for review …")
    print(f"  Token: {token}")
    print(f"  URL:   https://paperreview.ai/review?token={token}\n")

    try:
        review_data = poll_review(token, initial_wait=POLL_INITIAL_WAIT,
                                  interval=POLL_INTERVAL, max_wait=POLL_MAX_WAIT)
    except TimeoutError:
        sm.save(state)
        raise

    review_md = review_to_markdown(review_data)
    md_path = Path(state.review_dir) / f"review_iter{state.iteration:02d}.md"
    md_path.write_text(review_md)

    verdict = extract_verdict(review_data)
    state.reviews[-1]["verdict"] = verdict
    state.reviews[-1]["review_md_path"] = str(md_path)
    state = sm.complete_stage(state, Stage.POLL_REVIEW, {"verdict": verdict})
    sm.save(state)

    print(f"  Verdict: {verdict}  |  Review saved: {md_path}")

    # ── Review Synthesis (cross-source comparison) ──
    try:
        syn = ReviewSynthesizer(state.review_dir)
        result = syn.synthesize(state.iteration)
        syn.save(result)
        print(f"  Review synthesis saved (common: {len(result.common_issues)}, "
              f"ext-only: {len(result.external_only_issues)}, "
              f"int-only: {len(result.internal_only_issues)})")
    except Exception as exc:
        logger.warning("Review synthesis failed: %s", exc)

    # ── TODO-driven revision protocol ──
    try:
        rp = RevisionProtocol(state.work_dir)
        todo = rp.extract_todo_from_reviews(round_num=state.iteration)
        rp.save_todo(todo, state.iteration)
        print(f"  TODO extracted: {len(todo.items)} items "
              f"({len(todo.critical)} critical, {len(todo.major)} major)")
    except Exception as exc:
        logger.warning("TODO extraction failed: %s", exc)

    return state, verdict


def _do_revise(sm: StateManager, state: ResearchState, retry_feedback: str = "") -> ResearchState:
    """Revise paper using the RevisionEngine with per-section loop and grounding protection."""
    reviews = []
    for rf in sorted(Path(state.review_dir).glob("review_iter*.md")):
        reviews.append(rf.read_text())
    combined = "\n\n---\n\n".join(reviews[-3:])

    # Find the paper tex
    tex_files = sorted(Path(state.paper_dir).rglob("*.tex"))
    if not tex_files:
        raise FileNotFoundError(f"No .tex file found in {state.paper_dir}")
    paper_path = tex_files[-1]
    paper_tex = paper_path.read_text()

    # Load experiment blueprint and results if available
    experiment_blueprint = None
    experiment_results = None
    bp_path = Path(state.experiment_dir) / "experiment_plan.json"
    if bp_path.exists():
        try:
            experiment_blueprint = json.loads(bp_path.read_text())
        except Exception:
            pass
    results_path = Path(state.experiment_dir) / "experiment_results.json"
    if results_path.exists():
        try:
            experiment_results = json.loads(results_path.read_text())
        except Exception:
            pass

    if REVISION_ENGINE_ENABLED:
        logger.info("Using RevisionEngine for paper revision (iteration %d) …", state.iteration)

        engine = RevisionEngine(
            model=CLAUDE_MODEL,
            max_rounds=REVISION_MAX_ROUNDS,
            min_score=REVISION_MIN_SECTION_SCORE,
            convergence_threshold=REVISION_CONVERGENCE_THRESHOLD,
            checkpoint_path=f"state/{state.topic_slug}/revision_engine_ckpt.json",
        )
        revised_tex, report = engine.revise(
            paper_tex=paper_tex,
            review_feedback=combined,
            experiment_blueprint=experiment_blueprint,
            experiment_results=experiment_results,
        )

        # Save revised paper
        revised_path = Path(state.paper_dir) / "paper.tex"
        revised_path.write_text(revised_tex)
        backup_path = Path(state.paper_dir) / f"paper_revised_iter{state.iteration}.tex"
        backup_path.write_text(revised_tex)

        # Save revision report
        report_path = Path(state.review_dir) / f"revision_report_iter{state.iteration:02d}.md"
        report_md = _format_revision_report(report)
        report_path.write_text(report_md)

        logger.info(
            "Revision complete: %d rounds, score %.1f→%.1f, %d sections revised",
            report.rounds, report.initial_avg_score, report.final_avg_score,
            report.sections_revised,
        )
        return sm.complete_stage(state, Stage.REVISE, {
            "revision_rounds": report.rounds,
            "initial_avg_score": report.initial_avg_score,
            "final_avg_score": report.final_avg_score,
            "sections_revised": report.sections_revised,
            "backpressure_reverts": report.backpressure_reverts,
            "grounding_fixes": report.grounding_fixes,
            "revised_tex_path": str(revised_path),
            "revision_report": str(report_path),
        })

    # Fallback: traditional Claude Code-based revision
    prompt = _load_prompt("paper_revision.md",
        TOPIC=state.topic,
        REVIEWS=combined,
        OUTPUT_DIR=state.paper_dir,
        ITERATION=str(state.iteration),
    )
    logger.info("Calling Claude Code for paper revision (iteration %d) …", state.iteration)
    return _call_claude(sm, state, Stage.REVISE, prompt, retry_feedback)


STAGE_HANDLERS = {
    Stage.LITERATURE_SEARCH: _do_literature_search,
    Stage.HYPOTHESIS_GENERATION: _do_hypothesis_generation,
    Stage.BASELINE_FETCHING: _do_baseline_fetching,
    Stage.EXPERIMENT_DESIGN: _do_experiment_design,
    Stage.EXPERIMENT_EXECUTION: _do_experiment_execution,
    Stage.PAPER_WRITING: _do_paper_writing,
}


# ======================================================================
# Resume helpers
# ======================================================================


def _load_stage_output_on_resume(state: ResearchState, stage: Stage) -> None:
    """Verify that a completed stage's output file exists on disk.

    Called when skipping already-completed stages during resume.
    Logs a warning if the expected output is missing (may have been deleted).
    """
    rel_path = _OUTPUT_FILE_MAP.get(stage)
    if not rel_path:
        return
    full_path = Path(state.work_dir) / rel_path
    if stage == Stage.PAPER_WRITING:
        # Paper writing output is a directory with .tex files
        if full_path.is_dir() and list(full_path.rglob("*.tex")):
            logger.info("Paper output verified: %s", full_path)
        else:
            logger.warning(
                "Stage '%s' marked completed but no .tex files found in %s",
                stage.value, full_path,
            )
    elif full_path.exists():
        logger.info("Stage '%s' output verified: %s", stage.value, full_path)
    else:
        logger.warning(
            "Stage '%s' marked completed but expected output not found: %s",
            stage.value, full_path,
        )


# ======================================================================
# Claude Code tool integration
# ======================================================================

# ---------------------------------------------------------------------------
# Execution trace — preserve Claude Code session history for resume
# ---------------------------------------------------------------------------

_TRACE_FILENAME = ".trace.jsonl"


def _save_execution_trace(state: ResearchState, stage: Stage, output: str,
                          meta: dict | None = None) -> Path:
    """Append a structured trace record after a Claude Code session completes.

    The trace is a JSON-lines file that accumulates every session's key
    information: what was asked, what was done, what artifacts were created,
    and any errors encountered.  On resume this trace is injected as context
    so Claude Code "remembers" the full execution history.
    """
    import hashlib

    trace_file = Path(state.experiment_dir) / _TRACE_FILENAME

    # Extract key decisions and actions from output (first 3000 chars)
    output_summary = output[:3000] if output else "(empty output)"

    # Try to extract a one-line summary from the output
    summary_line = ""
    for line in output_summary.split("\n"):
        line = line.strip()
        if len(line) > 10 and not line.startswith("#") and not line.startswith("```"):
            summary_line = line[:200]
            break

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stage": stage.value,
        "iteration": state.iteration,
        "task_hash": hashlib.md5(
            (state.topic_slug + stage.value + str(state.iteration)).encode()
        ).hexdigest()[:8],
        "summary": summary_line,
        "output_preview": output_summary,
        "artifacts": meta.get("artifacts_present", {}) if meta else {},
        "missing_artifacts": meta.get("missing_artifacts", []) if meta else [],
        "error": meta.get("error", "") if meta else "",
    }

    # Append as JSON line
    with trace_file.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    logger.info("Execution trace saved → %s (%d records)",
                trace_file, _count_trace_records(trace_file))
    return trace_file


def _load_execution_trace(state: ResearchState) -> str:
    """Load previous execution traces and format them as context for Claude Code.

    Returns a markdown string summarizing all previous experiment sessions,
    or empty string if no trace exists yet.
    """
    trace_file = Path(state.experiment_dir) / _TRACE_FILENAME
    if not trace_file.exists():
        return ""

    records = []
    with trace_file.open() as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    if not records:
        return ""

    lines = [
        "",
        "---",
        "# 执行轨迹记忆 (Execution Trace Memory)",
        "",
        f"以下是之前 {len(records)} 次实验会话的执行轨迹。你可以在其中了解之前的决策、",
        "已完成的步骤、遇到的错误和修复、以及当前实验状态。",
        "",
        "| # | 时间 | 阶段 | 摘要 | 产物 |",
        "|---|------|------|------|------|",
    ]

    for i, r in enumerate(records, 1):
        ts = r.get("timestamp", "?")[:19]
        stage = r.get("stage", "?")
        summary = r.get("summary", "")[:100]
        artifacts = r.get("artifacts", {})
        art_str = ", ".join(
            k for k, v in artifacts.items() if v
        ) or "(none)"
        lines.append(f"| {i} | {ts} | {stage} | {summary} | {art_str} |")

    lines.append("")
    lines.append("## 最近一次会话详情")
    lines.append("")
    last = records[-1]
    lines.append(f"- **时间**: {last.get('timestamp', '?')}")
    lines.append(f"- **阶段**: {last.get('stage', '?')}")
    lines.append(f"- **迭代**: {last.get('iteration', '?')}")
    lines.append(f"- **产物**: {json.dumps(last.get('artifacts', {}))}")
    if last.get("missing_artifacts"):
        lines.append(f"- **缺失产物**: {', '.join(last['missing_artifacts'])}")
    if last.get("error"):
        lines.append(f"- **错误**: {last['error']}")

    lines.append("")
    lines.append("## 最近一次会话输出摘要")
    lines.append("")
    lines.append(last.get("output_preview", "(empty)")[:2000])
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("请基于以上执行轨迹继续工作。避免重复已完成的步骤，优先处理缺失的产物。")

    return "\n".join(lines)


def _count_trace_records(trace_file: Path) -> int:
    """Count the number of trace records (for logging)."""
    if not trace_file.exists():
        return 0
    return sum(1 for _ in trace_file.open() if _.strip())


def _call_claude_with_system_prompt(
    sm: StateManager,
    state: ResearchState,
    stage: Stage,
    task_prompt: str,
    system_prompt_path: str,
    retry_feedback: str = "",
    max_retries: int = _CLAUDE_MAX_RETRIES,
    timeout: int | None = None,
) -> ResearchState:
    """Invoke Claude Code with a system prompt for autonomous experiment execution.

    This is the prompt-driven experiment path.  Claude Code runs with a
    comprehensive system prompt that tells it HOW to be an experiment
    scientist; the task prompt tells it WHAT to do this session.

    Args:
        sm: StateManager instance.
        state: Current research state.
        stage: Pipeline stage being executed.
        task_prompt: The per-task prompt (rendered template).
        system_prompt_path: Path to the system prompt markdown file.
        retry_feedback: Optional review feedback from a failed prior attempt.
        max_retries: Max retry attempts for transient failures.
        timeout: Override timeout in seconds (defaults to EXPERIMENT_CLAUDE_TIMEOUT).
    """
    if timeout is None:
        from config import EXPERIMENT_CLAUDE_TIMEOUT

        timeout = EXPERIMENT_CLAUDE_TIMEOUT

    if retry_feedback:
        feedback_block = f"""

---
# IMPORTANT: Previous Review Feedback

The previous attempt at this stage was reviewed and did NOT pass. You MUST
address ALL of the following issues in this revision:

{retry_feedback}

Please explicitly acknowledge how you've addressed each issue above.
---
"""
        task_prompt = task_prompt + feedback_block

    # Write task prompt to file (avoids shell argument length limits)
    prompt_file = Path(state.work_dir) / f"{stage.value}_task.md"
    prompt_file.write_text(task_prompt)

    output_file = Path(state.work_dir) / f"{stage.value}_output.md"

    # Build command: claude reads task from stdin, loads system prompt from file
    cmd = [
        CLAUDE_CMD, "-p",
        "--output-format", "text",
        "--model", CLAUDE_MODEL,
        "--system-prompt", system_prompt_path,
        "--max-turns", "100",  # Allow many turns for autonomous work
    ]

    last_error = ""
    for attempt in range(max_retries + 1):
        logger.info(
            "Executing experiment scientist (attempt %d/%d, timeout=%ds)...",
            attempt + 1, max_retries + 1, timeout,
        )

        try:
            result = subprocess.run(
                cmd,
                input=task_prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(state.work_dir),
                env=_claude_subprocess_env(),
            )
            output = result.stdout or ""
            if result.returncode != 0:
                error_msg = (
                    result.stderr[:300] if result.stderr
                    else f"exit code {result.returncode}"
                )
                raise subprocess.CalledProcessError(
                    result.returncode, cmd,
                    output=output, stderr=result.stderr,
                )

            output_file.write_text(output)
            logger.info(
                "Experiment scientist output → %s (%d chars)",
                output_file, len(output),
            )

            # Verify expected artifacts exist
            exp_dir = Path(state.experiment_dir)
            manifest = exp_dir / "experiment_manifest.json"
            results = exp_dir / "experiment_results.json"
            log_file = exp_dir / "experiment_log.md"

            missing = []
            if not manifest.exists():
                missing.append("experiment_manifest.json")
            if not results.exists():
                missing.append("experiment_results.json")
            if not log_file.exists():
                missing.append("experiment_log.md")

            meta = {
                "prompt_file": str(prompt_file),
                "output_file": str(output_file),
                "system_prompt": system_prompt_path,
                "artifacts_present": {
                    "manifest": manifest.exists(),
                    "results": results.exists(),
                    "log": log_file.exists(),
                },
            }

            if missing:
                logger.warning(
                    "Experiment scientist completed but missing: %s",
                    ", ".join(missing),
                )
                meta["missing_artifacts"] = missing

            # Save execution trace for future resume
            _save_execution_trace(state, stage, output, meta)

            return sm.complete_stage(state, stage, meta)

        except FileNotFoundError:
            logger.warning(
                "`%s` CLI not found. Task saved to %s — run manually.",
                CLAUDE_CMD, prompt_file,
            )
            raise

        except subprocess.TimeoutExpired as exc:
            last_error = f"Timeout after {timeout}s"
            logger.warning(
                "Experiment scientist timed out (attempt %d/%d)",
                attempt + 1, max_retries + 1,
            )

        except subprocess.CalledProcessError as exc:
            stderr_info = exc.stderr[:300] if exc.stderr else "no stderr"
            stdout_info = exc.output[:500] if exc.output else "no stdout"
            last_error = f"Exit {exc.returncode}: stderr={stderr_info} | stdout={stdout_info}"
            logger.warning(
                "Experiment scientist failed (attempt %d/%d): %s",
                attempt + 1, max_retries + 1, last_error,
            )

        except Exception as exc:
            last_error = str(exc)
            logger.warning(
                "Experiment scientist error (attempt %d/%d): %s",
                attempt + 1, max_retries + 1, exc,
            )

        if attempt < max_retries:
            wait = min(30 * (2 ** attempt), 300)
            logger.info("Retrying in %ds ...", wait)
            time.sleep(wait)

    raise RuntimeError(
        f"Experiment scientist failed after {max_retries + 1} attempts: "
        f"{last_error}"
    )


def _check_claude_ready() -> None:
    """Startup check: actually run `claude -p` with a tiny prompt (30s timeout).

    This is more reliable than an HTTP ping — it validates the full stack:
    CLI binary → config → auth → network → API → response.  On failure,
    prints diagnostics and exits so the pipeline never enters the 600s hang.
    """
    settings_path = Path(PROJECT_ROOT) / ".claude" / "settings.json"

    # ── Check 1: API key is configured ──
    import json as _json
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if settings_path.exists():
        try:
            s = _json.loads(settings_path.read_text())
            base_url = base_url or (s.get("env", {}) or {}).get("ANTHROPIC_BASE_URL", "")
            api_key = api_key or (s.get("env", {}) or {}).get("ANTHROPIC_API_KEY", "")
        except Exception:
            pass

    if not api_key or api_key == "your-api-key-here":
        print(f"\n  ✗ Claude API Key 未配置")
        print(f"  配置文件: {settings_path}")
        print(f"  设置: export ANTHROPIC_API_KEY='sk-...'")
        sys.exit(1)

    # ── Check 2: Run `claude -p "ping"` with 30s timeout ──
    print(f"  [check] 测试 Claude CLI 连通性 (base_url={base_url})...", flush=True)
    try:
        result = subprocess.run(
            [CLAUDE_CMD, "-p", "--output-format", "text",
             "--model", CLAUDE_MODEL, "--max-turns", "1"],
            input="say hello in one word, no explanation",
            capture_output=True, text=True, timeout=30,
            cwd=str(PROJECT_ROOT), env=_claude_subprocess_env(),
        )
        if result.returncode == 0 and result.stdout.strip():
            preview = result.stdout.strip()[:80]
            logger.info("Claude CLI test OK: %s", preview)
            return
        # Non-zero exit or empty output
        stderr_tail = (result.stderr or "")[-300:]
        print(f"\n  ✗ Claude CLI 测试失败 (exit={result.returncode})")
        print(f"  stderr: {stderr_tail}")
        _print_claude_diagnostics(base_url, api_key, settings_path)

    except subprocess.TimeoutExpired as exc:
        stderr_tail = (exc.stderr or b"").decode(errors="replace")[-300:] if exc.stderr else ""
        print(f"\n  ✗ Claude CLI 测试超时 (30s)")
        if stderr_tail:
            print(f"  stderr: {stderr_tail}")
        _print_claude_diagnostics(base_url, api_key, settings_path)

    except FileNotFoundError:
        print(f"\n  ✗ '{CLAUDE_CMD}' 命令未找到")
        sys.exit(1)


def _print_claude_diagnostics(base_url: str, api_key: str, settings_path: Path) -> None:
    """Print diagnostic information when claude CLI fails."""
    print(f"\n  请检查以下项目:")
    print(f"    1. 网络: curl -sS --max-time 5 {base_url}/v1/models")
    print(f"    2. 代理: env | grep -i proxy")
    print(f"    3. 配置: cat {settings_path}")
    print(f"    4. NPU 环境网络隔离 → 在有网络的机器上运行或配置代理")
    print(f"    5. 手动测试: echo 'hi' | claude -p --model {CLAUDE_MODEL} --max-turns 1")
    sys.exit(1)


def _call_claude(sm: StateManager, state: ResearchState, stage: Stage,
                 prompt: str, retry_feedback: str = "",
                 max_retries: int = _CLAUDE_MAX_RETRIES) -> ResearchState:
    """
    Invoke Claude Code as an EXECUTION TOOL of this project.

    Includes retry with exponential backoff for transient failures
    (network errors, timeouts, rate limits).

    If retry_feedback is provided, it is appended to the prompt.
    """
    if retry_feedback:
        feedback_block = f"""

---
# IMPORTANT: Previous Review Feedback

The previous attempt at this stage was reviewed and did NOT pass. You MUST
address ALL of the following issues in this revision:

{retry_feedback}

Please explicitly acknowledge how you've addressed each issue above.
---
"""
        prompt = prompt + feedback_block

    prompt_file = Path(state.work_dir) / f"{stage.value}_prompt.md"
    prompt_file.write_text(prompt)

    output_file = Path(state.work_dir) / f"{stage.value}_output.md"

    # Large prompts go via stdin — avoids OS argv limits and CLI arg-parsing issues.
    # --max-turns: prevents infinite agent loops during tool-heavy stages.
    cmd = [CLAUDE_CMD, "-p", "--output-format", "text", "--model", CLAUDE_MODEL,
           "--max-turns", "30"]

    last_error = ""
    for attempt in range(max_retries + 1):
        logger.info(
            "Executing: %s (attempt %d/%d, timeout=%ds, prompt=%d chars) ...",
            " ".join(cmd[:5]), attempt + 1, max_retries + 1, _CLAUDE_TIMEOUT,
            len(prompt),
        )

        try:
            result = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True,
                timeout=_CLAUDE_TIMEOUT,
                cwd=str(state.work_dir),
                env=_claude_subprocess_env(),
            )
            output = result.stdout or ""
            if result.returncode != 0:
                error_msg = result.stderr[:300] if result.stderr else f"exit code {result.returncode}"
                raise subprocess.CalledProcessError(
                    result.returncode, cmd, output=output, stderr=result.stderr,
                )

            output_file.write_text(output)
            logger.info("Claude output → %s (%d chars)", output_file, len(output))
            return sm.complete_stage(state, stage, {
                "prompt_file": str(prompt_file),
                "output_file": str(output_file),
            })

        except FileNotFoundError:
            logger.warning(
                "`%s` CLI not found. Prompt saved to %s — run manually.",
                CLAUDE_CMD, prompt_file,
            )
            raise

        except subprocess.TimeoutExpired as exc:
            partial_stderr = (exc.stderr or b"").decode(errors="replace")[-500:] if exc.stderr else ""
            last_error = f"Timeout after {_CLAUDE_TIMEOUT}s"
            if partial_stderr:
                last_error += f" | stderr: {partial_stderr[:200]}"
            logger.warning("Claude call timed out (attempt %d/%d): %s",
                          attempt + 1, max_retries + 1,
                          partial_stderr[:200] if partial_stderr else "(no stderr)")

        except subprocess.CalledProcessError as exc:
            stderr_info = exc.stderr[:300] if exc.stderr else "no stderr"
            stdout_info = exc.output[:500] if exc.output else "no stdout"
            last_error = f"Exit {exc.returncode}: stderr={stderr_info} | stdout={stdout_info}"
            logger.warning("Claude call failed (attempt %d/%d): %s", attempt + 1, max_retries + 1, last_error)

        except Exception as exc:
            last_error = str(exc)
            logger.warning("Claude call error (attempt %d/%d): %s", attempt + 1, max_retries + 1, exc)

        # ── Retry with backoff ──
        if attempt < max_retries:
            delay = min(
                _CLAUDE_RETRY_BASE_DELAY * (_CLAUDE_RETRY_BACKOFF_FACTOR ** attempt),
                _CLAUDE_RETRY_MAX_DELAY,
            )
            logger.info("Retrying in %.0fs ...", delay)
            time.sleep(delay)
            sm.increment_retry(state, stage)
            state = sm.load(state.topic_slug)
        else:
            raise RuntimeError(
                f"Claude call for stage '{stage.value}' failed after "
                f"{max_retries + 1} attempts: {last_error}"
            )

    raise RuntimeError(f"Unexpected: all retries exhausted for stage '{stage.value}'")


# ======================================================================
# Utilities
# ======================================================================


def _load_prompt(name: str, **kwargs: str) -> str:
    """Read a prompt template from prompts/ and substitute placeholders."""
    text = (PROJECT_ROOT / "prompts" / name).read_text()
    for key, val in kwargs.items():
        text = text.replace("{{" + key + "}}", val)
    return text


def _read_or(dir_path: str, filename: str) -> str:
    p = Path(dir_path) / filename
    return p.read_text() if p.exists() else ""


def _render_template_file(template_path: str, **kwargs: str) -> str:
    """Read a template file and substitute ``${VAR}``-style placeholders.

    Unlike ``_load_prompt`` which uses ``str.format(**kwargs)``, this uses
    simple ``${VAR}`` substitution to match shell-style templates.
    """
    tp = Path(template_path)
    if not tp.exists():
        logger.error("Template file not found: %s", template_path)
        return ""
    template = tp.read_text()
    logger.info("Rendering template: %s (%d chars, %d placeholders)",
                template_path, len(template), len(kwargs))
    for key, value in kwargs.items():
        template = template.replace("${" + key + "}", value)
    logger.info("Rendered template: %d chars", len(template))
    return template


def _extract_baselines_from_text(text: str) -> list[str]:
    """Extract potential baseline method names from free-text fields.

    Looks for patterns like:
      - "outperforms X, Y, Z"
      - "compared to X and Y"
      - "baseline X / baseline method X"
      - Capitalized acronyms (BERT, ResNet, DQN, etc.)
    Returns a list of candidate baseline names.
    """
    import re
    names: list[str] = []

    if not text:
        return names

    # Pattern 1: "compared to / outperforms / against X, Y, Z"
    for pat in [
        r'(?:outperforms?|beats?|surpasses?|vs\.?|versus|against|compared\s*to)\s+([A-Z][\w\s,()-]+?)(?:\.|,|\s+by|\s+in|\s+on|\s+with|\s+and\s+[a-z]|\s*$)',
        r'(?:baselines?|baseline\s*methods?)[:\s]+([A-Z][\w\s,()-]+?)(?:\.|,|\s*$)',
        r'(?:such\s+as|like|e\.g\.|including)\s+([A-Z][\w\s,()-]+?)(?:\.|,|\s+and\s+[a-z]|\s*$)',
    ]:
        for m in re.finditer(pat, text, re.IGNORECASE):
            segment = m.group(1).strip()
            # Split on commas and "and"
            for part in re.split(r',\s*|\s+and\s+', segment):
                part = part.strip().rstrip(')')
                if len(part) >= 2 and len(part) <= 60 and not part.lower().startswith(('the ', 'our ', 'this ')):
                    names.append(part)

    # Pattern 2: Known baseline acronyms/names
    acronym_pat = re.compile(
        r'\b(?:ResNet\d*|ViT|BERT|GPT\d*|LLaMA\d*|DQN|PPO|A2C|SAC|TD3|DDPG|'
        r'Transformer|UNet|Diffusion|CLIP|DiT|MoE|MoD|LoRA|RAG|GRPO|'
        r'AdamW?|SGD|CNN|RNN|LSTM|GRU|GAN|VAE|WGAN|StyleGAN)\b'
    )
    for m in acronym_pat.finditer(text):
        if m.group() not in names:
            names.append(m.group())

    return names


def _contains_cjk(text: str) -> bool:
    """Return True if text contains Chinese/CJK characters."""
    import re
    return bool(re.search(r'[一-鿿㐀-䶿豈-﫿]', text))


def _ensure_claude_config(work_dir: Path) -> None:
    """Ensure Claude can find API config from work_dir.

    Creates a minimal .claude/settings.json in work_dir if one doesn't exist,
    pulling API key / base URL from the current environment. Claude Code reads
    env.ANTHROPIC_API_KEY and env.ANTHROPIC_BASE_URL from settings.json."""
    dst = work_dir / ".claude"
    dst.mkdir(exist_ok=True)
    settings_path = dst / "settings.json"

    # Load existing settings if present (preserve user modifications)
    existing: dict = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text())
        except Exception:
            pass

    # Resolve API key (env → project settings → existing)
    api_key = (
        os.environ.get("ANTHROPIC_API_KEY", "")
        or os.environ.get("CLAUDE_API_KEY", "")
    )
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "") or os.environ.get(
        "CLAUDE_BASE_URL", "https://api.deepseek.com/anthropic"
    )
    if not api_key:
        project_settings = PROJECT_ROOT / ".claude" / "settings.json"
        if project_settings.exists():
            try:
                data = json.loads(project_settings.read_text())
                api_key = (data.get("env", {}) or {}).get("ANTHROPIC_API_KEY", "")
                base_url = (data.get("env", {}) or {}).get("ANTHROPIC_BASE_URL", base_url)
            except Exception:
                pass
    if not api_key:
        api_key = (existing.get("env", {}) or {}).get("ANTHROPIC_API_KEY", "")
        base_url = (existing.get("env", {}) or {}).get("ANTHROPIC_BASE_URL", base_url) or base_url

    # Merge: preserve existing config, ensure critical keys are present
    env = existing.get("env", {}) if existing.get("env") else {}
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key
    if base_url:
        env["ANTHROPIC_BASE_URL"] = base_url

    permissions = existing.get("permissions", {}) if existing.get("permissions") else {}
    perms_allow: list = permissions.get("allow", [])
    required_perms = [
        "WebSearch(*)", "WebFetch(*)", "Bash(*)", "Read(*)",
        "Write(*)", "Edit(*)", "NotebookEdit(*)", "Task(*)",
        "Agent(*)", "Skill(*)", "Search(*)", "Grep(*)", "Glob(*)", "List(*)",
    ]
    for p in required_perms:
        if p not in perms_allow:
            perms_allow.append(p)

    settings = {
        "env": env,
        "permissions": {"allow": perms_allow, "deny": permissions.get("deny", [])},
    }
    settings_path.write_text(json.dumps(settings, indent=2))
    logger.info("Ensured .claude/settings.json in %s (permissions=%d)", work_dir, len(perms_allow))


def _safe_dirname(text: str) -> str:
    import re
    name = text.strip().replace(" ", "_")[:50]
    # Strip characters unsafe for filenames and SCO job names
    name = re.sub(r'[^a-zA-Z0-9._-]', '', name)
    return name.strip('_-')


def _print_status(state: ResearchState) -> None:
    import textwrap
    print(f"\nTopic:        {state.topic}")
    print(f"Stage:        {state.stage}")
    print(f"Iteration:    {state.iteration}/{state.max_iterations}")
    print(f"Work dir:     {state.work_dir}")
    print(f"Stage review: {'ON' if state.stage_review_enabled else 'OFF'} "
          f"(mode={state.stage_review_mode}, max_retries={state.stage_review_max_retries})")
    print(f"\nStage details:")
    for name, st in state.stages.items():
        marker = "←" if name == state.stage else " "
        if isinstance(st, StageState):
            status = st.status
            review_info = ""
            if st.review_attempts > 0:
                review_info = f"  reviews: {st.review_attempts} ({'✓' if st.review_passed else '✗'})"
            print(f"  [{marker}] {name:25s}  {status:14s}{review_info}")
        elif isinstance(st, dict):
            print(f"  [{marker}] {name:25s}  {st.get('status', '?'):12s}")
    if state.reviews:
        print(f"\nPaperReview.ai history:")
        for r in state.reviews:
            print(f"  iter {r['iteration']}: verdict={r.get('verdict', 'pending'):15s}  token={r['token'][:30]}...")


# ======================================================================
# Entry point
# ======================================================================


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    args = sys.argv[2:]

    if cmd == "run":
        if not args:
            print("Usage: python slairesearch.py run [--force] \"<topic>\"")
            sys.exit(1)
        os.environ.setdefault("IS_SANDBOX", "1")
        _check_claude_ready()
        force = args[0] == "--force"
        topic = args[1] if force else args[0]
        if _contains_cjk(topic):
            print(f"错误：研究主题不支持中文，请使用英文输入。", file=sys.stderr)
            print(f"当前输入: {topic}", file=sys.stderr)
            sys.exit(1)
        cmd_run(topic, force=force)
    elif cmd == "resume":
        if not args:
            print("Usage: python slairesearch.py resume <topic_or_slug>")
            sys.exit(1)
        os.environ.setdefault("IS_SANDBOX", "1")
        _check_claude_ready()
        cmd_resume(args[0])
    elif cmd == "status":
        cmd_status(args[0] if args else None)
    elif cmd == "list":
        cmd_list()
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
