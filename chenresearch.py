#!/usr/bin/env python3
"""
ChenResearch — Lean automated research system.
Claude Code is an execution TOOL of this project (not the controller).
The project orchestrates: literature → experiment → paper → review → iterate.

Usage:
    python chenresearch.py run "Your research topic"
    python chenresearch.py resume <topic_or_slug>
    python chenresearch.py status [<topic_or_slug>]
    python chenresearch.py list
"""

import sys
import json
import re
import time
import subprocess
import logging
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

logger = logging.getLogger("chenresearch")
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
    Stage.EXPERIMENT_DESIGN: "experiment/experiment_plan.md",
    Stage.EXPERIMENT_EXECUTION: "experiment/experiment_results.json",
    Stage.PAPER_WRITING: "paper",
}

# ── Processing stage order (for progress tracking & resume) ──
_PROCESSING_STAGES = [
    Stage.LITERATURE_SEARCH,
    Stage.HYPOTHESIS_GENERATION,
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


# ======================================================================
# Main CLI
# ======================================================================


def cmd_run(topic: str, force: bool = False) -> None:
    """Execute the full ChenResearch pipeline for *topic*."""
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

    # ── Mid-Entry Detection ──
    cc = ContextCompressor(work_dir)
    entry = cc.detect_entry_point()
    if entry.stage != "literature_search":
        print(f"\n  Auto-detected materials found. Entry point: {entry.stage}")
        print(f"  Reason: {entry.reasoning}")
        for name, found in entry.materials_found.items():
            print(f"    {'✓' if found else '✗'} {name}")

    print(f"\n{'='*60}")
    print(f"  ChenResearch Pipeline")
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


def _do_experiment_design(sm: StateManager, state: ResearchState, retry_feedback: str = "") -> ResearchState:
    lit = _read_or(state.literature_dir, "literature_review.md")
    prompt = _load_prompt("experiment_design.md",
        TOPIC=state.topic,
        LITERATURE_REVIEW=lit[:30000],
        OUTPUT_DIR=state.experiment_dir,
    )
    logger.info("Calling Claude Code for experiment design …")
    return _call_claude(sm, state, Stage.EXPERIMENT_DESIGN, prompt, retry_feedback)


def _do_experiment_execution(sm: StateManager, state: ResearchState, retry_feedback: str = "") -> ResearchState:
    script = Path(state.experiment_dir) / "run_experiment.sh"
    if not script.exists():
        raise FileNotFoundError(f"Experiment script not found: {script}")

    # --- If retrying with feedback, try to fix the experiment script first ---
    if retry_feedback:
        logger.info("Retrying experiment with review feedback — attempting auto-fix...")
        _try_fix_experiment_script(script, retry_feedback, state)

    gpu_info = detect_gpu()
    has_gpu = gpu_info["available"]
    logger.info("Local GPU: %s (count=%d)", has_gpu, gpu_info["count"])

    if FORCE_SCO:
        logger.info("FORCE_SCO=true — skipping local, going straight to SCO")

    result = sco_run_experiment(
        script_path=script,
        job_name=f"cr-{state.topic_slug}",
        local_timeout=LOCAL_EXECUTION_TIMEOUT,
        max_local_retries=LOCAL_EXECUTION_MAX_RETRIES,
        force_sco=FORCE_SCO,
    )

    logger.info("Experiment result: backend=%s success=%s attempts=%d",
                result.backend, result.success, result.attempts)

    if not result.success:
        # Diagnose the failure before raising
        error_detail = _diagnose_experiment_failure(result, state)
        logger.error("Experiment failed: %s", error_detail)
        raise RuntimeError(error_detail)

    return sm.complete_stage(state, Stage.EXPERIMENT_EXECUTION, {
        "backend": result.backend,
        "job_id": result.job_id,
        "job_status": "SUCCEEDED" if result.success else "FAILED",
        "log_path": result.log_path,
        "attempts": result.attempts,
    })


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
                                  "killed", "oom", "cuda", "segfault", "abort"])]
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
        cmd = [CLAUDE_CMD, "-p", "--model", CLAUDE_MODEL, "--output-format", "text", prompt]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
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
    logger.info("Calling Claude Code for paper writing + LaTeX compilation …")
    return _call_claude(sm, state, Stage.PAPER_WRITING, prompt, retry_feedback)


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

    cmd = [CLAUDE_CMD, "-p", "--output-format", "text", "--model", CLAUDE_MODEL, prompt]

    last_error = ""
    for attempt in range(max_retries + 1):
        logger.info(
            "Executing: %s (attempt %d/%d) ...",
            " ".join(cmd[:4]), attempt + 1, max_retries + 1,
        )

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=_CLAUDE_TIMEOUT,
                cwd=str(state.work_dir),
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
            last_error = f"Timeout after {_CLAUDE_TIMEOUT}s"
            logger.warning("Claude call timed out (attempt %d/%d)", attempt + 1, max_retries + 1)

        except subprocess.CalledProcessError as exc:
            last_error = f"Exit {exc.returncode}: {exc.stderr[:200] if exc.stderr else 'no stderr'}"
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
            print("Usage: python chenresearch.py run [--force] \"<topic>\"")
            sys.exit(1)
        force = args[0] == "--force"
        topic = args[1] if force else args[0]
        cmd_run(topic, force=force)
    elif cmd == "resume":
        if not args:
            print("Usage: python chenresearch.py resume <topic_or_slug>")
            sys.exit(1)
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
