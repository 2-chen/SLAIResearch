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
from claude_pty import run_in_pty
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


def _sync_state_from_progress(state: ResearchState) -> ResearchState:
    """If progress.json recorded a later stage than state.json, trust progress.json.

    state.json can lag behind when a stage times out mid-write. progress.json is
    updated atomically via ProgressEmitter, so its last \"stage_complete\" is the
    ground truth for what actually finished."""
    progress_path = Path(state.work_dir) / "progress.json"
    if not progress_path.exists():
        return state

    try:
        data = json.loads(progress_path.read_text())
        events = data.get("events", [])
    except Exception:
        return state

    # Collect completed stages from progress.json
    progress_completed: list[str] = []
    for ev in events:
        if ev.get("type") == "stage_complete":
            progress_completed.append(ev.get("stage", ""))

    if not progress_completed:
        return state

    last_progress_stage = progress_completed[-1]
    stage_order = [s.value for s in _PROCESSING_STAGES]
    try:
        progress_idx = stage_order.index(last_progress_stage)
    except ValueError:
        return state

    # Find the furthest completed stage from state.json
    state_completed_idx = -1
    for s_val in stage_order:
        st = state.stages.get(s_val)
        if isinstance(st, StageState) and st.status == StageStatus.COMPLETED.value:
            try:
                state_completed_idx = stage_order.index(s_val)
            except ValueError:
                pass

    # If progress.json knows about a LATER stage than state.json, use it
    if progress_idx > state_completed_idx:
        print(f"  progress.json last complete: {last_progress_stage} "
              f"(state.json: {stage_order[state_completed_idx] if state_completed_idx >= 0 else 'none'}) → "
              f"updating state")
        # Mark all stages up to progress_idx as completed
        for i in range(progress_idx + 1):
            s_val = stage_order[i]
            st = state.stages.get(s_val)
            if isinstance(st, StageState):
                st.status = StageStatus.COMPLETED.value
                st.review_passed = True

        # Set next stage
        next_idx = progress_idx + 1
        if next_idx < len(stage_order):
            state.stage = stage_order[next_idx]
        else:
            state.stage = Stage.DONE.value

        # Mark remaining as pending
        for s_val in stage_order[next_idx:]:
            st = state.stages.get(s_val)
            if isinstance(st, StageState):
                st.status = StageStatus.PENDING.value
                st.review_passed = False

    return state


def cmd_resume(topic_or_slug: str) -> None:
    """Resume pipeline from last saved state with full crash recovery.

    Crash recovery steps:
    1. Load saved state
    2. Reset any stages stuck in "in_progress" (from hard crash / SIGKILL)
    3. If current_stage is "done", report and exit
    4. If current_stage is "failed", find the first failed stage, reset it to "pending"
    5. Cross-check with progress.json — correct state if it diverged
    6. Run pipeline — completed stages are skipped automatically
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

    # ── Step 3: Cross-check with progress.json for divergence ──
    state = _sync_state_from_progress(state)

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

            # If submission was rate-limited (no token), skip poll
            st_info = state.stages.get(submit_stage.value)
            if isinstance(st_info, StageState) and st_info.meta.get("submitted") is False:
                print(f"\n  [yellow]![/yellow] PaperReview.ai rate-limited. "
                      f"PDF: {st_info.meta.get('pdf_path', '')}\n"
                      f"  Upload manually to continue.")
                progress.pipeline_complete(True, "PDF ready — upload manually to paperreview.ai")
                return

            state = sm.start_stage(state, Stage.POLL_REVIEW)
            try:
                state, verdict = _do_poll(sm, state)
            except TimeoutError:
                tok = state.reviews[-1].get("token", "N/A") if state.reviews else "N/A"
                print(f"\n[!] Review timed out (token: {'已获取' if tok and tok != 'N/A' else '未获取'})")
                if tok and tok != "N/A":
                    print(f"[!] Check manually: https://paperreview.ai/review?token={tok}")
                progress.substep(Stage.POLL_REVIEW.value, "Review timed out (token saved)")
                sm.save(state)
                progress.pipeline_complete(False, "Review timed out")
                return
            except Exception as exc:
                logger.exception("Poll failed: %s", exc)
                sm.fail_stage(state, Stage.POLL_REVIEW, str(exc))
                progress.error(Stage.POLL_REVIEW.value, str(exc))
                progress.pipeline_complete(False, f"Failed at poll: {exc}")
                return

            # Detect stale review — same content as previous iteration.
            # Happens when resubmit fails (429) and poll reuses an old token.
            prev_reviews = sorted(Path(state.review_dir).glob("review_iter*.md"))
            if len(prev_reviews) >= 2:
                if prev_reviews[-1].read_text() == prev_reviews[-2].read_text():
                    print(f"\n  [yellow]![/yellow] Review unchanged from iteration "
                          f"{state.iteration - 1} — reviewer did not re-evaluate "
                          f"(likely stale token). Stopping.")
                    state.stage = Stage.DONE.value
                    sm.save(state)
                    progress.pipeline_complete(True, "Review unchanged — stopping")
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

            # Check for revision stall — no improvement means further
            # iterations are pointless
            rev_meta = state.stages.get(Stage.REVISE.value)
            if isinstance(rev_meta, StageState):
                rm = rev_meta.meta or {}
                if (rm.get("revision_rounds", 0) >= 2
                        and rm.get("final_avg_score", 0) <= rm.get("initial_avg_score", 0) + 0.1):
                    print(f"\n  [yellow]![/yellow] Revision stalled "
                          f"(score {rm['initial_avg_score']}→{rm['final_avg_score']}) — "
                          f"further iterations would produce no improvement")
                    state.stage = Stage.DONE.value
                    sm.save(state)
                    progress.pipeline_complete(True, f"Revision stalled — stopping at iteration {state.iteration}")
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
            # If score is critically low (< 2.0), the output is fundamentally wrong
            # (e.g. wrong repo, empty results).  Proceeding would poison downstream.
            if verdict.score < 2.0 and not verdict.passed:
                msg = (f"Stage '{stage.value}' review score {verdict.score:.1f} "
                       f"is critically low after {max_retries} retries — "
                       f"output is unusable. Aborting pipeline.")
                logger.error(msg)
                raise RuntimeError(msg)
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
    state = _call_claude(sm, state, Stage.LITERATURE_SEARCH, prompt, retry_feedback)

    # ── Ensure structured paper metadata exists for downstream hypothesis_engine ──
    # The literature review is free-form markdown, but hypothesis_engine needs
    # papers_metadata.json with structured title/authors/year/abstract/url fields.
    # Without this, hypothesis_engine extracts 0 papers and runs blind.
    _ensure_papers_metadata(state.literature_dir)

    return state


def _do_hypothesis_generation(sm: StateManager, state: ResearchState, retry_feedback: str = "") -> ResearchState:
    """Run ReAct-based hypothesis generation using hypothesis_engine.py.

    On first run the full ReAct engine runs.  On retry with feedback, the
    existing output is patched via Claude Code to address specific reviewer
    issues — this is faster and more targeted than re-running the full engine."""
    from hypothesis_engine import HypothesisEngine
    from config import HYPOTHESIS_MAX_REACT_ROUNDS, HYPOTHESIS_TOP_K_PDFS, HYPOTHESIS_MAX_PAPERS

    hypo_file = Path(state.hypothesis_dir) / "hypothesis_output.json"

    # ── Retry with feedback ──
    if retry_feedback and hypo_file.exists():
        # Detect whether issues are data-level (can only be fixed by re-running
        # the full engine with actual search + PDF download) or text-level
        # (can be fixed by patching the JSON output).
        if _has_data_level_issues(hypo_file):
            logger.info("Retry feedback indicates data-level issues — "
                        "re-running full ReAct engine (not patching) …")
            # Ensure papers_metadata.json exists so the engine can parse it
            _ensure_papers_metadata(state.literature_dir)
            # Delete react_state.json to force a fresh run
            react_state_path = Path(state.hypothesis_dir) / "react_state.json"
            if react_state_path.exists():
                react_state_path.unlink()
                logger.info("Cleared previous react_state.json to force full re-run")
            # Re-run full engine with review feedback injected
            engine = HypothesisEngine(
                topic=state.topic,
                literature_dir=Path(state.literature_dir),
                work_dir=Path(state.hypothesis_dir),
                max_react_rounds=HYPOTHESIS_MAX_REACT_ROUNDS,
                top_k_pdfs=HYPOTHESIS_TOP_K_PDFS,
                max_papers_per_search=HYPOTHESIS_MAX_PAPERS,
                review_feedback=retry_feedback,
            )
            result = engine.run()
            logger.info("Hypothesis re-run complete. %d hypotheses, %d papers analyzed.",
                        len(result.get("hypotheses", [])),
                        result.get("total_papers_analyzed", 0))
            return sm.complete_stage(state, Stage.HYPOTHESIS_GENERATION, {
                "hypotheses_count": len(result.get("hypotheses", [])),
                "papers_analyzed": result.get("total_papers_analyzed", 0),
                "re_run": True,
            })
        else:
            # ── Text-level issues: patch existing output ──
            # Strategy: parse reviewer feedback into individual issues, then fix
            # each issue independently with a focused Claude call.  This avoids
            # the "all-or-nothing" problem where Claude drops hypotheses or
            # misses fixes when too many issues are crammed into one prompt.
            # Large JSON is handled by extracting only the relevant chunk per issue.
            state = _patch_hypothesis_with_segmented_fixes(
                sm, state, hypo_file, retry_feedback,
            )
            return state

    # ── First run: full ReAct engine ──
    logger.info("Starting hypothesis generation (ReAct engine) ...")
    engine = HypothesisEngine(
        topic=state.topic,
        literature_dir=Path(state.literature_dir),
        work_dir=Path(state.hypothesis_dir),
        max_react_rounds=HYPOTHESIS_MAX_REACT_ROUNDS,
        top_k_pdfs=HYPOTHESIS_TOP_K_PDFS,
        max_papers_per_search=HYPOTHESIS_MAX_PAPERS,
    )
    result = engine.run()
    logger.info("Hypothesis generation complete. %d hypotheses, %d papers analyzed.",
                len(result.get("hypotheses", [])), result.get("total_papers_analyzed", 0))
    return sm.complete_stage(state, Stage.HYPOTHESIS_GENERATION, {
        "hypotheses_count": len(result.get("hypotheses", [])),
        "papers_analyzed": result.get("total_papers_analyzed", 0),
    })


def _do_baseline_fetching(sm: StateManager, state: ResearchState, retry_feedback: str = "") -> ResearchState:
    """Clone baseline GitHub repos for structural reference in experiment design.

    The extraction of method names is topic-driven (not just one hypothesis),
    and on retry with reviewer feedback indicating wrong baselines, the LLM
    extraction is bypassed in favour of a well-known-baseline lookup + LLM URL
    resolution.

    Key safeguards:
      - minimum baseline count enforced (expands from topic DB if needed)
      - retry clears stale caches for wrong methods
      - repo validation checks both method name AND topic relevance
    """
    from config import BASELINE_CLONING_ENABLED, BASELINE_MAX_REPOS
    import shutil as _shutil

    if not BASELINE_CLONING_ENABLED:
        logger.info("Baseline cloning disabled — skipping")
        (Path(state.experiment_dir) / "baseline_context.md").write_text("")
        return sm.complete_stage(state, Stage.BASELINE_FETCHING, {"skipped": True})

    topic = state.topic
    lit_dir = Path(state.literature_dir)
    hypo_dir = Path(state.hypothesis_dir)

    # ── Load context ──
    try:
        from literature_context import LiteratureContext
        lc = LiteratureContext(state.work_dir)
        landscape = lc._parse_literature()
        papers = landscape.papers if landscape else []
    except Exception as e:
        logger.warning("Could not parse literature for baselines: %s", e)
        papers = []

    lit_review = _read_or(str(lit_dir), "literature_review.md")

    # Load all hypotheses as JSON string for context
    all_hypotheses_json = ""
    try:
        hypo_file = hypo_dir / "hypothesis_output.json"
        if hypo_file.exists():
            hypo_data = json.loads(hypo_file.read_text())
            all_hypotheses_json = json.dumps(hypo_data, indent=2, ensure_ascii=False)
            hypotheses = hypo_data.get("hypotheses", [])
    except Exception:
        hypotheses = []

    # ── Determine method names ──
    MIN_BASELINES = 3
    method_names: list[str] = []

    # Always start with topic-driven DB lookup (deterministic, no LLM).
    # This ensures a minimum set of relevant baselines regardless of LLM quality.
    topic_defaults = _resolve_baselines_from_topic(topic, lit_review)
    method_names.extend(topic_defaults)

    # Supplement with LLM extraction from full context (hypotheses + literature).
    extra = _extract_baselines_via_llm_full_context(
        topic=topic,
        all_hypotheses_json=all_hypotheses_json[:8000],
        lit_review=lit_review[:8000],
        retry_feedback=retry_feedback,
        known_baselines=method_names,
    )
    for m in extra:
        if m not in method_names:
            method_names.append(m)

    # On retry, purge cached repos whose methods are no longer in the list.
    # This prevents stale wrong repos from being reused.
    if retry_feedback:
        _purge_stale_baseline_caches(state, method_names)

    # Deduplicate
    seen: set[str] = set()
    method_names = [n for n in method_names if n and not (n in seen or seen.add(n))]

    # ── Enforce minimum baseline count ──
    if len(method_names) < MIN_BASELINES:
        logger.info("Only %d methods extracted (min %d) — expanding from topic DB",
                     len(method_names), MIN_BASELINES)
        for m in topic_defaults:
            if m not in seen and m not in method_names:
                method_names.append(m)
                seen.add(m)
            if len(method_names) >= max(MIN_BASELINES, BASELINE_MAX_REPOS):
                break

    if not method_names:
        logger.info("No baseline method names found — skipping baseline fetching")
        (Path(state.experiment_dir) / "baseline_context.md").write_text("")
        return sm.complete_stage(state, Stage.BASELINE_FETCHING, {
            "skipped": True,
            "reason": "no methods found",
        })

    # ── Clone + validate repos ──
    logger.info("Fetching baseline repos for %d methods: %s",
                 len(method_names), ", ".join(method_names[:10]))

    try:
        from baseline_finder import BaselineFinder, BaselineSource
        bf = BaselineFinder(
            cache_dir=Path(state.work_dir) / ".." / ".shared" / "baselines",
            max_repos=BASELINE_MAX_REPOS,
        )

        contexts = bf.find_and_extract(papers=papers, method_names=method_names[:BASELINE_MAX_REPOS])

        # Validate: method name match + topic relevance
        valid_contexts: list = []
        failed_infos: list[tuple[str, str]] = []  # (method_name, reason)

        for ctx in contexts:
            repo_dir = Path(ctx.local_path)
            is_valid, reason = _validate_baseline_repo_v2(repo_dir, ctx.method_name, topic)
            if is_valid:
                valid_contexts.append(ctx)
            else:
                logger.warning("Repo '%s' failed validation: %s — clearing cache",
                               repo_dir.name, reason)
                failed_infos.append((ctx.method_name, reason))
                _shutil.rmtree(repo_dir, ignore_errors=True)

        if failed_infos:
            logger.info("Repo validation: %d/%d passed — %d failed: %s",
                        len(valid_contexts), len(contexts),
                        len(failed_infos),
                        ", ".join(f"{m}:{r[:40]}" for m, r in failed_infos[:3]))

            # ── Retry: resolve correct URLs for failed methods via LLM ──
            failed_names = [m for m, _ in failed_infos]
            if retry_feedback:
                resolved = _resolve_correct_baseline_urls(
                    failed_names, retry_feedback, topic,
                )
                for method_name, url in resolved:
                    if not url:
                        continue
                    logger.info("Resolved correct URL for '%s': %s", method_name, url)
                    slug = bf._repo_slug(url, method_name)
                    target_dir = bf.cache_dir / slug
                    if bf._clone_repo(url, target_dir):
                        src = BaselineSource(
                            method_name=method_name, paper_title="",
                            repo_url=url, source="llm_resolved",
                        )
                        ctx2 = bf._extract_context(target_dir, src)
                        is_valid2, _ = _validate_baseline_repo_v2(
                            Path(ctx2.local_path), method_name, topic,
                        )
                        if is_valid2:
                            valid_contexts.append(ctx2)
                            logger.info("✓ Corrected repo for '%s': %s", method_name, url)

        # ── Also try well-known URLs if still too few ──
        if len(valid_contexts) < MIN_BASELINES and retry_feedback:
            logger.info("Only %d valid baselines — expanding with known URLs", len(valid_contexts))
            known_urls = _KNOWN_BASELINE_URLS.get("_default_search_queries", {})
            for method_name, known_url in list(known_urls.items())[:BASELINE_MAX_REPOS]:
                if any(ctx.method_name.lower() == method_name.lower() for ctx in valid_contexts):
                    continue
                # Clone directly from the known URL (no search needed)
                slug = bf._repo_slug(known_url, method_name)
                target_dir = bf.cache_dir / slug
                if bf._clone_repo(known_url, target_dir):
                    src = BaselineSource(
                        method_name=method_name, paper_title="",
                        repo_url=known_url, source="known_url",
                    )
                    ctx3 = bf._extract_context(target_dir, src)
                    is_valid3, _ = _validate_baseline_repo_v2(
                        Path(ctx3.local_path), method_name, topic,
                    )
                    if is_valid3:
                        valid_contexts.append(ctx3)
                        logger.info("✓ Known-URL baseline: %s → %s", method_name, known_url)
                    else:
                        _shutil.rmtree(target_dir, ignore_errors=True)

        # ── Write output ──
        prompt_block = bf.format_for_prompt(valid_contexts)
        output_path = Path(state.experiment_dir) / "baseline_context.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(prompt_block)
        logger.info("Baseline context saved (%d chars, %d repos)",
                     len(prompt_block), len(valid_contexts))

        # Validate output quality before declaring success
        methods_found = [ctx.method_name for ctx in valid_contexts]
        logger.info("Final baselines: %s", ", ".join(methods_found) if methods_found else "(none)")

        return sm.complete_stage(state, Stage.BASELINE_FETCHING, {
            "repos_found": len(valid_contexts),
            "methods_searched": len(method_names[:BASELINE_MAX_REPOS]),
            "methods": methods_found,
            "retry": bool(retry_feedback),
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
            # Check if NPU is actually usable (driver/kernel version match)
            npu_broken = _check_npu_broken()
            if npu_broken:
                npu_context = (
                    f"\n\n## ⚠ NPU 环境：训练模式已禁用\n"
                    f"- NPU 设备数: {accel_info.npu_count} (CANN {npu_broken})\n"
                    f"- **torch_npu + CANN 版本不匹配，NPU 训练不可用**\n"
                    f"- **不要尝试运行实际训练**——代码执行 NPU 操作会崩溃\n\n"
                    f"### 替代方案：设计实验 + 生成模拟结果\n"
                    f"1. 正常设计实验架构、编写所有代码（训练脚本、评估脚本）\n"
                    f"2. 不要执行训练——根据文献中的 SOTA 性能数据估算合理结果\n"
                    f"3. 生成 experiment_manifest.json（实验配置）、experiment_results.json（估算结果）、experiment_log.md（说明结果来自文献估算）\n"
                    f"4. 在结果中明确标注 \"Estimated from literature benchmarks\"\n"
                    f"5. 产生合理的性能数据，让论文写作阶段有数据可用\n"
                )
            else:
                npu_context = (
                    f"\n\n## NPU 环境信息\n"
                    f"- NPU 设备数: {accel_info.npu_count}\n"
                    f"- 加速器类型: {accel_info.accelerator_type}\n"
                    f"- 使用 device-agnostic 代码 (torch.device('npu'))，避免 torch.cuda\n"
                )
            if not accel_info.supports_cuda_api:
                sco_compat = check_sco_npu_compatibility()
                npu_context += (
                    f"- SCO 云 GPU: {sco_compat['compatible']} | {sco_compat['recommendation']}\n"
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
    state = _call_claude_with_system_prompt(
        sm, state, Stage.EXPERIMENT_DESIGN,
        task_prompt=task_prompt,
        system_prompt_path=EXPERIMENT_SYSTEM_PROMPT,
        retry_feedback=retry_feedback,
        timeout=EXPERIMENT_CLAUDE_TIMEOUT,
    )

    # If NPU training is broken and Claude produced no results, generate
    # literature-estimated data so downstream stages (paper_writing) can proceed.
    npu_fail_reason = _check_npu_broken()
    if npu_fail_reason:
        _ensure_simulation_results(state, lit, hyp, npu_fail_reason)

    return state


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
        result = subprocess.run(
            cmd + [prompt], capture_output=True, text=True, timeout=300,
            cwd=str(state.work_dir), env=_claude_subprocess_env(),
        )
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

    # Try submission with longer backoff for rate limiting
    import time as _t
    last_error = None
    for attempt in range(5):
        try:
            token = submit_paper(str(pdf_path), email=state.email, venue=state.venue)
            record = ReviewRecord(iteration=state.iteration, token=token,
                                  submitted_at=str(pdf_path))
            state = sm.add_review(state, record)
            return sm.complete_stage(state, Stage.SUBMIT_REVIEW, {"token": token})
        except Exception as e:
            last_error = str(e)
            if "429" in last_error and attempt < 4:
                wait = 60 * (attempt + 1)  # 60s, 120s, 180s, 240s
                logger.warning("paperreview.ai rate limited — retrying in %ds …", wait)
                _t.sleep(wait)
            else:
                break

    # Rate limit persists — save locally and complete gracefully
    submit_note = Path(state.paper_dir) / "SUBMIT_PENDING.txt"
    submit_note.write_text(
        f"PDF ready for manual upload: {pdf_path}\n"
        f"Upload to: https://paperreview.ai\n"
        f"Email: {state.email}\n"
        f"Venue: {state.venue}\n"
        f"Last error: {last_error}\n"
    )
    logger.warning(
        "paperreview.ai still rate-limited after retries. "
        "PDF saved locally — upload manually: %s", submit_note,
    )
    return sm.complete_stage(state, Stage.SUBMIT_REVIEW, {
        "submitted": False,
        "pdf_path": str(pdf_path),
        "note": f"Rate limited: {last_error}. Upload manually via https://paperreview.ai",
    })


def _do_poll(sm: StateManager, state: ResearchState) -> tuple[ResearchState, str]:
    token = state.reviews[-1]["token"]
    print(f"\n  Iteration {state.iteration} — waiting for review …")
    print(f"  Token: 已获取")
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

    # Also save focused version with only actionable sections
    from paperreview_api import extract_review_key_feedback
    feedback_md, found_map = extract_review_key_feedback(review_data)
    fb_path = Path(state.review_dir) / f"review_feedback{state.iteration:02d}.md"
    fb_path.write_text(feedback_md)
    found_summary = ", ".join(k for k, v in found_map.items() if v) or "none"
    missing_summary = ", ".join(k for k, v in found_map.items() if not v) or "none"
    logger.info(
        "Review feedback saved (%d chars) → %s | found: [%s] | missing: [%s]",
        len(feedback_md), fb_path, found_summary, missing_summary,
    )

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
    # Prefer focused feedback (Weaknesses / Detailed Comments / Questions / Assessment)
    # over full review markdown — gives the revision engine concentrated, actionable input
    reviews: list[str] = []
    for rf in sorted(Path(state.review_dir).glob("review_feedback*.md")):
        reviews.append(rf.read_text())
    if not reviews:
        # Fallback to full review if no focused feedback exists
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
            rc, output = run_in_pty(
                cmd, task_prompt, timeout=timeout,
                cwd=str(state.work_dir), env=_claude_subprocess_env(),
            )
            output = output or ""
            if rc != 0 and not output.strip():
                raise RuntimeError(
                    f"Experiment scientist exited {rc} with no output"
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
                # If core artifacts (manifest + results) are both missing, treat
                # as a hard failure — retrying without code/product changes is
                # pointless and wastes compute.
                if "experiment_manifest.json" in missing and "experiment_results.json" in missing:
                    raise RuntimeError(
                        f"Experiment scientist produced no core artifacts "
                        f"({', '.join(missing)}). "
                        f"Check Claude Code permissions or experiment design."
                    )

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
    # Use PIPE mode (not PTY) for text-heavy stages.  PTY is only needed
    # for experiment scientist which issues heavy tool calls.
    cmd = [CLAUDE_CMD, "-p", "--output-format", "text", "--model", CLAUDE_MODEL,
           "--max-turns", "30", prompt]

    last_error = ""
    for attempt in range(max_retries + 1):
        logger.info(
            "Executing: %s (attempt %d/%d, timeout=%ds, prompt=%d chars) ...",
            " ".join(cmd[:5]), attempt + 1, max_retries + 1, _CLAUDE_TIMEOUT,
            len(prompt),
        )

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=_CLAUDE_TIMEOUT,
                cwd=str(state.work_dir),
                env=_claude_subprocess_env(),
            )
            output = result.stdout or ""
            if result.returncode != 0 and not output.strip():
                error_msg = (
                    result.stderr[:300] if result.stderr
                    else f"exit code {result.returncode}"
                )
                raise subprocess.CalledProcessError(
                    result.returncode, cmd,
                    output=output, stderr=result.stderr,
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

        except subprocess.TimeoutExpired:
            last_error = f"Timeout after {_CLAUDE_TIMEOUT}s"
            logger.warning("Claude call timed out (attempt %d/%d): %s",
                          attempt + 1, max_retries + 1, last_error)

        except subprocess.CalledProcessError as exc:
            stderr_info = exc.stderr[:300] if exc.stderr else "no stderr"
            stdout_info = exc.output[:500] if exc.output else "no stdout"
            last_error = f"Exit {exc.returncode}: stderr={stderr_info} | stdout={stdout_info}"
            logger.warning("Claude call failed (attempt %d/%d): %s",
                          attempt + 1, max_retries + 1, last_error)

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
# Segmented hypothesis patching (text-level review fixes)
# ======================================================================
#
# When reviewer feedback contains multiple issues (truncated H2, missing
# cross-comparison, H3-H5 disappeared, …), dumping everything into one
# Claude prompt causes: truncated JSON, dropped hypotheses, partial fixes.
#
# Solution: parse feedback → individual issues → fix each one independently
# with only the RELEVANT JSON chunk.  Merges are applied sequentially.


def _patch_hypothesis_with_segmented_fixes(
    sm, state, hypo_file: Path, retry_feedback: str,
) -> ResearchState:
    """Fix hypothesis output by processing each review issue independently.

    1. Parse feedback into a list of individual issues.
    2. For each issue, extract only the relevant JSON chunk.
    3. Call Claude to fix just that one issue on that chunk.
    4. Merge the fix back.  Process sequentially so later fixes see earlier ones.
    """
    existing_text = hypo_file.read_text()
    try:
        existing_data = json.loads(existing_text)
    except json.JSONDecodeError:
        logger.error("Cannot patch: hypothesis_output.json is not valid JSON")
        return sm.complete_stage(state, Stage.HYPOTHESIS_GENERATION, {
            "hypotheses_count": 0, "patched": False,
            "error": "invalid JSON",
        })

    hypotheses = existing_data.get("hypotheses", [])
    existing_hypo_count = len(hypotheses)

    # ── Step 1: Parse feedback into individual issues ──
    issues = _parse_review_issues(retry_feedback)
    if not issues:
        logger.warning("Could not parse any individual issues from feedback — "
                       "falling back to single-pass patch")
        issues = [{"id": "all", "description": retry_feedback, "target": "__all__"}]

    logger.info("Segmented patch: %d individual issues to fix across %d hypotheses",
                len(issues), existing_hypo_count)

    # ── Step 2-4: Fix each issue sequentially ──
    lit_review = _read_or(str(Path(hypo_file).parent.parent / "literature"), "literature_review.md")
    topic = state.topic
    fixes_applied = 0
    fixes_failed = 0

    for i, issue in enumerate(issues):
        issue_id = issue.get("id", f"issue_{i}")
        logger.info("  [%d/%d] Fixing: %s", i + 1, len(issues), issue_id[:80])

        try:
            fixed_data = _call_patch_single_issue(
                existing_data=existing_data,
                issue=issue,
                topic=topic,
                lit_review=lit_review,
            )
            if fixed_data is not None:
                existing_data = fixed_data
                fixes_applied += 1
                logger.info("  [%d/%d] ✓ Fixed: %s", i + 1, len(issues), issue_id[:80])
            else:
                fixes_failed += 1
                logger.warning("  [%d/%d] ✗ Failed to fix: %s", i + 1, len(issues), issue_id[:80])
        except Exception as exc:
            fixes_failed += 1
            logger.warning("  [%d/%d] ✗ Error fixing '%s': %s",
                           i + 1, len(issues), issue_id[:80], exc)

    # ── Write final result ──
    final_text = json.dumps(existing_data, indent=2, ensure_ascii=False)
    hypo_file.write_text(final_text)

    final_count = len(existing_data.get("hypotheses", []))
    if final_count < existing_hypo_count:
        logger.warning(
            "Segmented patch: hypothesis count dropped %d → %d "
            "(some fixes may have removed hypotheses)",
            existing_hypo_count, final_count,
        )

    logger.info("Segmented patch complete: %d/%d fixes applied, %d hypotheses (%d→%d)",
                fixes_applied, len(issues), final_count,
                existing_hypo_count, final_count)

    return sm.complete_stage(state, Stage.HYPOTHESIS_GENERATION, {
        "hypotheses_count": final_count,
        "patched": True,
        "fixes_applied": fixes_applied,
        "fixes_failed": fixes_failed,
        "total_issues": len(issues),
    })


# ---------------------------------------------------------------------------
# Issue parsing — split reviewer feedback into individual actionable items
# ---------------------------------------------------------------------------

def _parse_review_issues(feedback: str) -> list[dict]:
    """Split reviewer feedback into individual issues.

    Handles two feedback formats:
    1. Console-display format with `!! 【label】` markers (from progress emitter).
    2. Stored review format with `Critical Issues:\n  - item` (from state_manager).

    Each extracted issue becomes one focused Claude fix call.

    Returns a list of dicts with keys: id, severity, target, description, fix_type.
    """
    issues: list[dict] = []
    issue_idx = 0

    # ── Try console-display format first (!! at line-start) ──
    # Console format: "!! 【致命】..." on its own line.
    # Stored format: "  - !! 致命问题..." (indented within list).
    # Only enter this path if !! appears at the START of lines (console format).
    if re.search(r'(?:^|\n)!!', feedback):
        blocks = re.split(r'\n(?=!!)', feedback)
        for block in blocks:
            block = block.strip()
            if not block or len(block) < 20:
                continue
            issue_idx += 1
            severity = _extract_severity(block)
            target = _classify_issue_target(block)
            fix_type = _classify_fix_type(block)
            description = re.sub(r'^!!\s*', '', block).strip()
            description = re.sub(r'【[^】]*】\s*', '', description, count=1).strip()
            issues.append({
                "id": f"issue_{issue_idx:02d}_{target or 'general'}",
                "severity": severity,
                "target": target or "__all__",
                "description": description[:2000],
                "fix_type": fix_type,
            })
        if issues:
            return issues

    # ── Stored review format: extract from structured sections ──
    # Critical Issues section
    crit_section = _extract_section(feedback, "Critical Issues")
    for item in _split_list_items(crit_section):
        issue_idx += 1
        target = _classify_issue_target(item)
        issues.append({
            "id": f"issue_{issue_idx:02d}_{target or 'critical'}",
            "severity": "critical",
            "target": target or "__all__",
            "description": item[:2000],
            "fix_type": _classify_fix_type(item),
        })

    # Weaknesses section
    weak_section = _extract_section(feedback, "Weaknesses")
    for item in _split_list_items(weak_section):
        issue_idx += 1
        target = _classify_issue_target(item)
        issues.append({
            "id": f"issue_{issue_idx:02d}_{target or 'weakness'}",
            "severity": "high",
            "target": target or "__all__",
            "description": item[:2000],
            "fix_type": _classify_fix_type(item),
        })

    # Suggestion section — single block, not a list
    suggestion = _extract_section(feedback, "Suggestion")
    if suggestion and len(suggestion) > 30:
        issue_idx += 1
        issues.append({
            "id": f"issue_{issue_idx:02d}_suggestion",
            "severity": "medium",
            "target": "__all__",
            "description": suggestion[:2000],
            "fix_type": "fix_content",
        })

    # Detailed Feedback — may contain multiple paragraphs
    detail = _extract_section(feedback, "Detailed Feedback")
    if detail and len(detail) > 50:
        # Split into paragraphs for very long feedback
        paragraphs = [p.strip() for p in detail.split('\n\n') if p.strip() and len(p.strip()) > 30]
        if len(paragraphs) <= 1:
            # Single block — only add if we don't already have enough issues
            if not issues:
                issue_idx += 1
                issues.append({
                    "id": f"issue_{issue_idx:02d}_feedback",
                    "severity": "medium",
                    "target": "__all__",
                    "description": detail[:2000],
                    "fix_type": "fix_content",
                })
        else:
            for para in paragraphs:
                issue_idx += 1
                target = _classify_issue_target(para)
                issues.append({
                    "id": f"issue_{issue_idx:02d}_{target or 'detail'}",
                    "severity": "medium",
                    "target": target or "__all__",
                    "description": para[:2000],
                    "fix_type": _classify_fix_type(para),
                })

    # ── Fallback: no structure detected — treat whole feedback as one issue ──
    if not issues:
        issue_idx += 1
        issues.append({
            "id": f"issue_{issue_idx:02d}_general",
            "severity": "medium",
            "target": "__all__",
            "description": feedback[:2000],
            "fix_type": "fix_content",
        })

    return issues


def _extract_severity(text: str) -> str:
    """Extract severity level from issue text."""
    m = re.search(r'【(致命|严重|注意|未修复)[^】]*】', text)
    if m:
        label = m.group(1)
        if "致命" in label:
            return "critical"
        elif "严重" in label:
            return "high"
        elif "未修复" in label:
            return "high"
    if re.search(r'CRITICAL|FATAL', text, re.IGNORECASE):
        return "critical"
    if re.search(r'WARNING|SEVERE', text, re.IGNORECASE):
        return "high"
    return "medium"


def _extract_section(text: str, section_name: str) -> str:
    """Extract a named section from the stored review format.

    Sections look like:
        SectionName:
          - item 1
          - item 2

        NextSection:
          ...
    """
    # Match from section_name: to the next section header or end
    # Section headers are lines ending with ':' followed by indented content
    pattern = rf'{re.escape(section_name)}:\s*\n(.*?)(?=\n\S+:\s*\n|\n##|\Z)'
    m = re.search(pattern, text, re.DOTALL)
    return m.group(1).strip() if m else ""


def _split_list_items(section_text: str) -> list[str]:
    """Split a section's text into individual list items.

    Items are prefixed with '  - ' or '- ' or '!! '.
    """
    if not section_text:
        return []
    # Split on common item prefixes
    items = re.split(r'\n\s*[-•!!]+\s*', section_text)
    return [item.strip() for item in items if item.strip() and len(item.strip()) > 10]


def _classify_issue_target(text: str) -> str:
    """Determine which hypothesis or field an issue targets.

    Looks for patterns like 'H1', 'H2假说', 'hypothesis 3', '假说交叉比较',
    'selected_hypothesis', etc.

    When multiple hypotheses are referenced (H3, H4, H5 all disappeared),
    returns '__all__' to trigger the general fix path.
    """
    # ── Multi-hypothesis detection: "H3, H4, H5" or "H3-H5" or "H3、H4、H5" ──
    h_refs = re.findall(r'\bH(\d{1,2})\b', text, re.IGNORECASE)
    if len(h_refs) >= 2:
        # Multiple hypotheses mentioned — use general path
        return "__all__"
    if len(h_refs) == 1 and re.search(r'H\d\s*[-–—to]+\s*H\d', text, re.IGNORECASE):
        # Range: "H3-H5"
        return "__all__"

    # ── Single hypothesis reference ──
    if h_refs:
        return f"H{h_refs[0]}"

    # ── Cross-cutting concerns ──
    if re.search(r'交叉比较|cross.comparison|comparison.*matrix|对比.*矩阵', text):
        return "cross_comparison"
    if re.search(r'文献检索|literature.*search|papers_analyzed|search_rounds', text, re.IGNORECASE):
        return "literature_grounding"
    if re.search(r'selected_hypothesis|选择.*假说', text):
        return "selected_hypothesis"
    if re.search(r'gap_coverage|缺口.*覆盖', text):
        return "gap_coverage"
    if re.search(r'novelty|创新性', text):
        return "novelty_assessment"
    if re.search(r'feasibility|可行性', text):
        return "feasibility"

    # ── Generic field patterns ──
    if re.search(r'truncat|截断|incomplete|不完整', text):
        return "truncation"

    return "__all__"


def _classify_fix_type(text: str) -> str:
    """Classify what kind of fix this issue needs."""
    if re.search(r'truncat|截断|incomplete|不完整|cut.off', text, re.IGNORECASE):
        return "complete_truncated"
    if re.search(r'missing|缺失|消失|not.*present|removed', text, re.IGNORECASE):
        return "restore_missing"
    if re.search(r'交叉比较|cross.comparison|comparison.*matrix', text, re.IGNORECASE):
        return "add_section"
    if re.search(r'format|格式', text, re.IGNORECASE):
        return "fix_format"
    if re.search(r'novelty|创新|original', text, re.IGNORECASE):
        return "improve_content"
    return "fix_content"


# ---------------------------------------------------------------------------
# JSON chunking — extract only the relevant portion for each issue
# ---------------------------------------------------------------------------

def _extract_chunk_for_issue(existing_data: dict, issue: dict) -> tuple[str, str]:
    """Extract only the JSON portion relevant to a specific issue.

    Returns (json_snippet_str, context_note).
    The snippet is what Claude should fix; the note describes what was omitted.
    """
    target = issue.get("target", "__all__")
    hypotheses = existing_data.get("hypotheses", [])

    # Build hypothesis index
    hypo_by_id = {}
    for h in hypotheses:
        hid = h.get("id", "").upper()
        if hid:
            hypo_by_id[hid] = h

    if target.startswith("H") and target in hypo_by_id:
        # ── Single-hypothesis issue: send just that hypothesis ──
        target_h = hypo_by_id[target]
        # Also include a summary of other hypotheses for context
        other_summaries = []
        for h in hypotheses:
            hid = h.get("id", "")
            if hid.upper() != target:
                other_summaries.append(
                    f"{hid}: {h.get('title', 'untitled')[:60]} "
                    f"(score={h.get('score', '?')})"
                )
        wrapper = {
            "topic": existing_data.get("topic", ""),
            "target_hypothesis": target,
            "hypothesis_to_fix": target_h,
            "other_hypotheses_summary": other_summaries,
            "selected_hypothesis": existing_data.get("selected_hypothesis", {}),
        }
        snippet = json.dumps(wrapper, indent=2, ensure_ascii=False)
        context = (f"Fixing hypothesis {target} only. "
                   f"{len(other_summaries)} other hypotheses exist "
                   f"({', '.join(h['id'] for h in hypotheses if h.get('id','').upper() != target)}). "
                   f"Do NOT modify other hypotheses.")
        return snippet, context

    elif target == "cross_comparison":
        # ── Cross-comparison: send top-level + hypothesis summaries ──
        wrapper = {
            "topic": existing_data.get("topic", ""),
            "selected_hypothesis": existing_data.get("selected_hypothesis", {}),
            "gap_coverage": existing_data.get("gap_coverage", ""),
            "hypotheses_summary": [
                {
                    "id": h.get("id", ""),
                    "title": h.get("title", "")[:100],
                    "method_outline": (h.get("method_outline", "") or "")[:300],
                    "expected_outcome": (h.get("expected_outcome", "") or "")[:200],
                    "novelty_score": h.get("novelty_score", ""),
                    "impact_score": h.get("impact_score", ""),
                }
                for h in hypotheses
            ],
            "_note": "You are adding a CROSS-COMPARISON MATRIX. Output the FULL "
                      "hypothesis_output.json with the matrix added as a new "
                      "'cross_comparison' field. Keep all existing fields intact.",
        }
        snippet = json.dumps(wrapper, indent=2, ensure_ascii=False)
        context = (f"Adding cross-comparison matrix for {len(hypotheses)} hypotheses. "
                   f"Must preserve all existing content.")
        return snippet, context

    elif target == "selected_hypothesis":
        # ── Selected hypothesis: send top-level selection + hypothesis summaries ──
        wrapper = {
            "topic": existing_data.get("topic", ""),
            "selected_hypothesis": existing_data.get("selected_hypothesis", {}),
            "hypotheses_summary": [
                {"id": h.get("id", ""), "title": h.get("title", "")[:100],
                 "novelty_score": h.get("novelty_score", ""),
                 "impact_score": h.get("impact_score", "")}
                for h in hypotheses
            ],
            "_note": "Fix the selected_hypothesis field to match the best hypothesis. "
                      "Keep all other content unchanged.",
        }
        snippet = json.dumps(wrapper, indent=2, ensure_ascii=False)
        context = "Fixing selected_hypothesis field only."
        return snippet, context

    elif target == "truncation":
        # ── Try to find which hypothesis is truncated by looking at lengths ──
        truncated_hypo = None
        for h in hypotheses:
            text = json.dumps(h, ensure_ascii=False)
            # Truncation usually ends mid-sentence without closing punctuation
            if len(text) > 500 and not re.search(r'[.!?。！？]\s*$', text[-200:]):
                truncated_hypo = h
                break
        if truncated_hypo:
            return _extract_chunk_for_issue(existing_data, {
                "target": truncated_hypo.get("id", "H?"),
                "fix_type": "complete_truncated",
                "description": issue.get("description", ""),
            })
        # Fallback: send everything but flag the issue
        return json.dumps(existing_data, indent=2, ensure_ascii=False)[:20000], \
               "Could not identify which hypothesis is truncated. Review all."

    else:
        # ── General / cross-cutting: send top-level + light hypothesis summaries ──
        # This is the "catch-all" — keep it lean to avoid truncation
        wrapper = {
            "topic": existing_data.get("topic", ""),
            "selected_hypothesis": existing_data.get("selected_hypothesis", {}),
            "gap_coverage": existing_data.get("gap_coverage", ""),
            "identified_gaps": existing_data.get("identified_gaps", [])[:20],
            "total_papers_analyzed": existing_data.get("total_papers_analyzed", 0),
            "hypotheses": [
                # Strip to essential fields for context; full fix happens per-hypothesis
                {k: v for k, v in h.items()
                 if k in ("id", "title", "description", "method_outline",
                          "rationale", "key_references", "expected_outcome",
                          "feasibility", "novelty_score", "impact_score", "score")}
                for h in hypotheses
            ],
        }
        snippet = json.dumps(wrapper, indent=2, ensure_ascii=False)
        context = (f"Fixing general issue across {len(hypotheses)} hypotheses. "
                   f"Full JSON size: {len(json.dumps(existing_data, ensure_ascii=False))} chars.")
        return snippet, context


# ---------------------------------------------------------------------------
# Single-issue fix — one focused Claude call per issue
# ---------------------------------------------------------------------------

def _call_patch_single_issue(
    existing_data: dict,
    issue: dict,
    topic: str = "",
    lit_review: str = "",
) -> dict | None:
    """Call Claude to fix ONE specific issue on ONE specific JSON chunk.

    Returns the full updated existing_data dict on success, None on failure.
    The caller merges fixes sequentially — later calls see earlier fixes.
    """
    snippet, context_note = _extract_chunk_for_issue(existing_data, issue)

    fix_type = issue.get("fix_type", "fix_content")
    target = issue.get("target", "__all__")
    description = issue.get("description", "")

    # ── Build focused prompt ──
    fix_instructions = {
        "complete_truncated": (
            "This hypothesis has a TRUNCATED field — text was cut off mid-sentence. "
            "Complete it based on the topic context. Make it a full, coherent paragraph "
            "that ends properly. Do NOT change any other fields or hypotheses."
        ),
        "restore_missing": (
            "One or more hypotheses are MISSING from the output. The reviewer noted "
            "they disappeared (likely from JSON truncation in a previous fix attempt). "
            "Use the literature context below to infer plausible hypotheses that fill "
            "gaps in the research topic. Also check the 'identified_gaps' field in the "
            "JSON for inspiration. Recreate the missing hypotheses with complete fields "
            "(title, description, method_outline, rationale, key_references, "
            "expected_outcome, feasibility, score). Do NOT remove any existing hypotheses."
        ),
        "add_section": (
            "A required section is MISSING. Add it based on the existing content. "
            "For cross-comparison: compare all hypotheses on novelty, feasibility, "
            "expected impact, and methodological trade-offs. Include a priority ranking. "
            "Do NOT modify existing hypotheses — only add the new section."
        ),
        "fix_format": (
            "Fix FORMAT issues only. Ensure all fields use consistent structure. "
            "Do NOT change content, only format."
        ),
        "improve_content": (
            "Improve the CONTENT quality. Make descriptions more specific and "
            "academically rigorous. Ground claims in the literature."
        ),
        "fix_content": (
            "Fix the specific issue described in the feedback. Be precise and targeted. "
            "Do NOT change anything unrelated to this issue."
        ),
    }

    instruction = fix_instructions.get(fix_type, fix_instructions["fix_content"])

    prompt = (
        f"You are fixing ONE specific issue in a hypothesis generation output.\n\n"
        f"## Research Topic\n{topic}\n\n"
        f"## Issue to Fix (#{issue.get('id', '?')})\n"
        f"**Severity**: {issue.get('severity', 'medium')}\n"
        f"**Target**: {target}\n"
        f"**Fix type**: {fix_type}\n"
        f"**Problem**: {description}\n\n"
        f"## Instructions\n"
        f"{instruction}\n\n"
        f"## Relevant JSON (only the portion that needs fixing)\n"
        f"Context: {context_note}\n"
        f"```json\n{snippet[:25000]}\n```\n\n"
        f"## Output Format\n"
        + (
            f"Return the SAME wrapper JSON structure you received (with "
            f"'hypothesis_to_fix' key). Fix only 'hypothesis_to_fix' — keep "
            f"everything else as-is.\n"
            if target.startswith("H")
            else f"Return the COMPLETE fixed JSON. Keep the same structure as "
                 f"the input. Do NOT change anything except what's needed to fix "
                 f"this specific issue.\n"
        )
        + f"The output must be valid JSON. "
          f"No explanation, no markdown — just valid JSON.\n"
    )

    # Include minimal literature context for content-heavy fix types
    if fix_type in ("improve_content", "fix_content", "restore_missing") and lit_review:
        prompt += f"\n## Literature Context (for reference)\n{lit_review[:3000]}\n"

    cmd = [CLAUDE_CMD, "-p", "--output-format", "text", "--model", CLAUDE_MODEL,
           "--max-turns", "2"]
    try:
        result = subprocess.run(
            cmd + [prompt], capture_output=True, text=True, timeout=300,
            cwd=str(PROJECT_ROOT), env=_claude_subprocess_env(),
        )
        raw = result.stdout or ""
        if result.returncode != 0 and not raw:
            logger.warning("Claude call failed for issue '%s': %s",
                           issue.get("id", "?"), (result.stderr or "")[:200])
            return None

        # Extract the fixed JSON
        m = re.search(r'(\{.*\})', raw, re.DOTALL)
        if not m:
            logger.warning("No JSON found in Claude response for issue '%s'",
                           issue.get("id", "?"))
            return None

        fixed_json_str = m.group(1).strip()
        fixed_data = json.loads(fixed_json_str)
        return _merge_fix_into_json(existing_data, fixed_data, issue)

    except json.JSONDecodeError:
        logger.warning("Claude returned invalid JSON for issue '%s'",
                       issue.get("id", "?"))
        return None
    except subprocess.TimeoutExpired:
        logger.warning("Claude timed out for issue '%s'", issue.get("id", "?"))
        return None
    except Exception as exc:
        logger.warning("Error fixing issue '%s': %s", issue.get("id", "?"), exc)
        return None


def _merge_fix_into_json(
    original: dict,
    fixed: dict,
    issue: dict,
) -> dict:
    """Merge a single-issue fix back into the full data.

    Strategy depends on the target:
    - Specific hypothesis (H2): replace only that hypothesis in the list,
      preserving others verbatim.
    - Cross-cutting (cross_comparison, gap_coverage): merge the new field.
    - __all__: full replacement (last resort).
    """
    target = issue.get("target", "__all__")
    fix_type = issue.get("fix_type", "fix_content")

    # ── Handle wrapper-form response: Claude returned the chunk wrapper ──
    # When the input was a wrapper (for single-hypothesis issues), Claude may
    # return the same wrapper with hypothesis_to_fix updated.
    if "hypothesis_to_fix" in fixed and target.startswith("H"):
        fixed_h = fixed["hypothesis_to_fix"]
        orig_hypos = original.get("hypotheses", [])
        new_hypos = []
        for h in orig_hypos:
            hid = h.get("id", "").upper()
            if hid == target:
                new_hypos.append(fixed_h)
            else:
                new_hypos.append(h)
        original["hypotheses"] = new_hypos
        # Also merge any top-level fields that may have been in the wrapper
        for field in ("cross_comparison", "gap_coverage", "selected_hypothesis"):
            if field in fixed and field != "hypothesis_to_fix":
                original[field] = fixed[field]
        return original

    if target.startswith("H") and "hypotheses" in fixed and "hypotheses" in original:
        # ── Single-hypothesis fix ──
        orig_hypos = original["hypotheses"]
        fixed_hypos = fixed.get("hypotheses", [])

        # Build lookup by ID in fixed output
        fixed_by_id = {}
        for h in fixed_hypos:
            hid = h.get("id", "").upper()
            if hid:
                fixed_by_id[hid] = h

        # Replace only the targeted hypothesis, keep others verbatim
        new_hypos = []
        for h in orig_hypos:
            hid = h.get("id", "").upper()
            if hid == target and hid in fixed_by_id:
                new_hypos.append(fixed_by_id[hid])
            else:
                new_hypos.append(h)

        # Also check if fixed added new hypotheses (shouldn't, but be safe)
        for h in fixed_hypos:
            hid = h.get("id", "").upper()
            if hid and hid not in {oh.get("id", "").upper() for oh in orig_hypos}:
                if hid != target:  # Only add if it wasn't the one we were replacing
                    new_hypos.append(h)

        original["hypotheses"] = new_hypos

    elif target in ("cross_comparison", "gap_coverage", "selected_hypothesis",
                    "literature_grounding", "novelty_assessment", "feasibility"):
        # ── Field-level fix: merge specific fields ──
        mergeable_fields = [
            "cross_comparison", "gap_coverage", "selected_hypothesis",
            "known_facts", "identified_gaps",
        ]
        for field in mergeable_fields:
            if field in fixed:
                original[field] = fixed[field]

        # For non-hypothesis top-level changes, also check hypotheses didn't change
        if "hypotheses" in original:
            # Don't blindly replace — only take hypothesis fixes if targeted
            pass

    elif fix_type == "complete_truncated":
        # ── Truncation fix: find and replace the truncated hypothesis ──
        if "hypotheses" in fixed and "hypotheses" in original:
            orig_hypos = original["hypotheses"]
            fixed_hypos = fixed.get("hypotheses", [])
            fixed_by_id = {h.get("id", "").upper(): h for h in fixed_hypos}
            new_hypos = []
            for h in orig_hypos:
                hid = h.get("id", "").upper()
                if hid in fixed_by_id:
                    # Only replace if fixed version is actually longer (more complete)
                    orig_len = len(json.dumps(h, ensure_ascii=False))
                    fixed_len = len(json.dumps(fixed_by_id[hid], ensure_ascii=False))
                    if fixed_len > orig_len * 1.1:  # At least 10% more content
                        new_hypos.append(fixed_by_id[hid])
                    else:
                        new_hypos.append(h)
                else:
                    new_hypos.append(h)
            original["hypotheses"] = new_hypos

    else:
        # ── Full replacement (last resort for general issues) ──
        # Only replace top-level fields, preserve hypothesis count safety
        orig_count = len(original.get("hypotheses", []))
        fixed_count = len(fixed.get("hypotheses", []))

        for key in fixed:
            if key == "hypotheses":
                if fixed_count >= orig_count:
                    original["hypotheses"] = fixed["hypotheses"]
                else:
                    logger.warning(
                        "Merge refused: fixed has %d hypotheses vs original %d — "
                        "preserving original hypotheses to avoid data loss",
                        fixed_count, orig_count,
                    )
                    # Still merge non-hypothesis fields
            else:
                original[key] = fixed[key]

    return original


# ======================================================================
# Cross-stage data helpers
# ======================================================================


def _ensure_papers_metadata(literature_dir: str) -> bool:
    """Ensure papers_metadata.json exists in the literature directory.

    The literature_search stage produces a free-form markdown review, but the
    downstream hypothesis_engine needs structured JSON with title/authors/year/
    abstract/url per paper.  If papers_metadata.json doesn't exist, use LLM to
    extract it from literature_review.md.

    Returns True if metadata now exists (or already did), False on failure.
    """
    lit_dir = Path(literature_dir)
    papers_json = lit_dir / "papers_metadata.json"
    lit_review = lit_dir / "literature_review.md"

    # ── Skip if metadata already exists and is newer than the review ──
    # This prevents extracting stale data; on retry the review may be updated.
    if papers_json.exists():
        if not lit_review.exists():
            return True
        # Re-extract if review was modified AFTER metadata was created
        if lit_review.stat().st_mtime <= papers_json.stat().st_mtime:
            return True
        logger.info("literature_review.md is newer than papers_metadata.json — "
                     "re-extracting paper metadata")

    if not lit_review.exists():
        logger.warning("No literature_review.md to extract papers from")
        return False

    lit_text = lit_review.read_text()
    logger.info("Extracting paper metadata from literature_review.md (%d chars) ...",
                len(lit_text))

    prompt = (
        "Extract structured paper metadata from the following literature review. "
        "For every paper mentioned, provide: title, authors (as a list of strings), "
        "year (as a string), abstract (1-3 sentences), url (arxiv or DOI URL), "
        "and arxiv_id if available.\n\n"
        "Output ONLY a JSON array of paper objects, like this:\n"
        '[{"title": "Attention Is All You Need", "authors": ["Vaswani A", "Shazeer N", ...], '
        '"year": "2017", "abstract": "We propose the Transformer...", '
        '"url": "https://arxiv.org/abs/1706.03762", "arxiv_id": "1706.03762", "citations": 0}, ...]\n\n'
        "Extract ALL papers mentioned — do not skip any. Each paper gets one entry.\n\n"
        f"## Literature Review\n{lit_text[:30000]}\n"
    )

    try:
        result = subprocess.run(
            [CLAUDE_CMD, "-p", "--output-format", "text", "--model", CLAUDE_MODEL],
            input=prompt, capture_output=True, text=True, timeout=300,
            cwd=str(PROJECT_ROOT), env=_claude_subprocess_env(),
        )
        raw = result.stdout or ""
        m = re.search(r'(\[.*\])', raw, re.DOTALL)
        if m:
            papers = json.loads(m.group(1))
            if isinstance(papers, list) and len(papers) > 0:
                papers_json.write_text(
                    json.dumps(papers, indent=2, ensure_ascii=False)
                )
                logger.info("Extracted %d papers to papers_metadata.json", len(papers))
                return True
            else:
                logger.warning("LLM extraction returned 0 papers")
    except json.JSONDecodeError:
        logger.warning("Failed to parse LLM paper extraction output as JSON")
    except subprocess.TimeoutExpired:
        logger.warning("LLM paper extraction timed out")
    except Exception as exc:
        logger.warning("Failed to extract paper metadata: %s", exc)

    return False


def _has_data_level_issues(hypo_file: Path) -> bool:
    """Check hypothesis output JSON for data-vacuum conditions.

    Reads the JSON directly instead of pattern-matching reviewer feedback
    text — these fields are produced by our own code and reliable.

    A data-level issue means text-patching cannot help; the full ReAct engine
    must be re-run (with actual search + PDF download).
    """
    if not hypo_file.exists():
        return False

    try:
        data = json.loads(hypo_file.read_text())
    except (json.JSONDecodeError, OSError):
        return False

    # Numeric fields: zero means no real work was done
    for field in ("total_papers_analyzed", "papers_analyzed",
                  "search_rounds", "pdfs_deep_read"):
        val = data.get(field)
        if isinstance(val, (int, float)) and val == 0:
            logger.info("Data-level issue: %s=%s → full engine re-run required",
                         field, val)
            return True

    # Structural fields: empty when they should have content
    for field in ("identified_gaps", "hypotheses"):
        val = data.get(field)
        if isinstance(val, list) and len(val) == 0:
            logger.info("Data-level issue: empty %s → full engine re-run required",
                         field)
            return True

    return False


def _resolve_correct_baseline_urls(
    failed_methods: list[str],
    retry_feedback: str,
    topic: str,
) -> list[tuple[str, str]]:
    """Use LLM to map method names to canonical GitHub URLs.

    When GitHub search returns wrong repos (e.g., Reform for Reformer), the
    reviewer feedback identifies the mismatch.  This function asks an LLM to
    resolve the correct, canonical GitHub URL for each failed method, bypassing
    the broken search.

    Returns a list of (method_name, url_or_empty) tuples.
    """
    if not failed_methods:
        return []

    # Extract specific wrong-repo mentions from feedback to give the LLM context
    wrong_repo_lines = []
    for line in retry_feedback.split('\n'):
        if re.search(r'(wrong|错误|incorrect|mismatch|matched|not.*correct)', line, re.IGNORECASE):
            if len(line) > 20:
                wrong_repo_lines.append(line.strip()[:200])
    wrong_context = '\n'.join(wrong_repo_lines[:5]) if wrong_repo_lines else ""

    prompt = (
        f"For each baseline method listed below, provide the CANONICAL GitHub "
        f"repository URL (the official or most popular implementation).\n\n"
        f"## Research Topic\n{topic}\n\n"
        f"## Methods that need correct URLs\n"
        + "\n".join(f"- {m}" for m in failed_methods) + "\n\n"
    )
    if wrong_context:
        prompt += (
            f"## Previous Wrong Matches (from reviewer feedback)\n"
            f"{wrong_context}\n\n"
            f"These URLs were WRONG — do NOT suggest them. Find the correct ones.\n\n"
        )
    prompt += (
        f"## Instructions\n"
        f"For each method, output the correct GitHub URL. Use well-known repos:\n"
        f"- Reformer → https://github.com/lucidrains/reformer-pytorch\n"
        f"- Longformer → https://github.com/allenai/longformer\n"
        f"- Performer → https://github.com/lucidrains/performer-pytorch\n"
        f"- Linformer → https://github.com/lucidrains/linformer\n"
        f"- BigBird → https://github.com/google-research/bigbird\n"
        f"- Mamba → https://github.com/state-spaces/mamba\n"
        f"- FlashAttention → https://github.com/Dao-AILab/flash-attention\n\n"
        f"Output ONLY a JSON object mapping method_name → url:\n"
        f'{{"Reformer": "https://github.com/lucidrains/reformer-pytorch", ...}}\n'
        f"If you cannot find a URL for a method, set its value to empty string.\n"
        f"No explanation, just JSON.\n"
    )

    try:
        result = subprocess.run(
            [CLAUDE_CMD, "-p", "--output-format", "text", "--model", CLAUDE_MODEL],
            input=prompt, capture_output=True, text=True, timeout=120,
            cwd=str(PROJECT_ROOT), env=_claude_subprocess_env(),
        )
        raw = result.stdout or ""
        m = re.search(r'(\{.*?\})', raw, re.DOTALL)
        if m:
            url_map = json.loads(m.group(1))
            if isinstance(url_map, dict):
                return [(name, url_map.get(name, "")) for name in failed_methods]
    except Exception as exc:
        logger.warning("LLM baseline URL resolution failed: %s", exc)

    return [(name, "") for name in failed_methods]


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


# ══════════════════════════════════════════════════════════════════════
# Topic-driven baseline extraction (replaces fragile single-hypothesis approach)
# ══════════════════════════════════════════════════════════════════════

# ── Well-known baseline URL table (topic keywords → canonical repos) ──
# Used as fallback when LLM extraction returns too few methods (common in
# NPU environments where the LLM has limited context), and as the primary
# source on retry when the reviewer says all baselines are wrong.
_KNOWN_BASELINE_URLS: dict[str, dict[str, str]] = {
    # Attention / Transformers — multi-word to avoid matching "attention" everywhere
    "attention mechanism|transformer|self-attention|cross-attention|long sequence": {
        "Transformer (vanilla)": "https://github.com/huggingface/transformers",
        "FlashAttention": "https://github.com/Dao-AILab/flash-attention",
        "Linformer": "https://github.com/lucidrains/linformer",
        "Reformer": "https://github.com/lucidrains/reformer-pytorch",
        "Longformer": "https://github.com/allenai/longformer",
        "Performer": "https://github.com/lucidrains/performer-pytorch",
        "BigBird": "https://github.com/google-research/bigbird",
    },
    "mamba|ssm|state space": {
        "Mamba": "https://github.com/state-spaces/mamba",
        "S4": "https://github.com/state-spaces/s4",
    },
    "linear attention|efficient attention": {
        "Linear Transformer": "https://github.com/lucidrains/linear-attention-transformer",
        "Fast Transformer (PyTorch)": "https://github.com/idiap/fast-transformers",
    },
    # RL
    "reinforcement learning|policy gradient|deep RL": {
        "Stable-Baselines3": "https://github.com/DLR-RM/stable-baselines3",
        "DQN (baselines)": "https://github.com/openai/baselines",
    },
    # NLP — removed "text" (too greedy)
    "language model|LLM|NLP|BERT|GPT|pretrained": {
        "BERT": "https://github.com/google-research/bert",
        "GPT-2": "https://github.com/openai/gpt-2",
    },
    # Vision
    "vision|image classification|ViT|object detection": {
        "ResNet": "https://github.com/pytorch/vision",
        "Vision Transformer": "https://github.com/lucidrains/vit-pytorch",
    },
    # ── Known-URL fallback (method → url, used when all else fails) ──
    "_default_search_queries": {
        "FlashAttention": "https://github.com/Dao-AILab/flash-attention",
        "Mamba": "https://github.com/state-spaces/mamba",
        "Bert (transformers)": "https://github.com/huggingface/transformers",
        "ResNet": "https://github.com/pytorch/vision",
    },
}


def _resolve_baselines_from_topic(topic: str, lit_review: str = "") -> list[str]:
    """Match topic + literature review against well-known baseline DB.

    Returns a deduplicated list of method names.  This is deterministic
    (no LLM call) and serves as a reliable fallback when LLM extraction
    returns too few or wrong methods.
    """
    combined = (topic + " " + lit_review[:5000]).lower()
    found: list[str] = []

    for pattern_key, methods in _KNOWN_BASELINE_URLS.items():
        if pattern_key.startswith("_"):
            continue
        # Match any keyword in the pattern (OR logic)
        keywords = pattern_key.split("|")
        if any(kw in combined for kw in keywords):
            for name in methods:
                if name not in found:
                    found.append(name)

    return found


def _extract_baselines_via_llm_full_context(
    topic: str = "",
    all_hypotheses_json: str = "",
    lit_review: str = "",
    retry_feedback: str = "",
    known_baselines: list[str] | None = None,
) -> list[str]:
    """LLM-driven baseline extraction from FULL context (not just one hypothesis).

    Previous version only saw one hypothesis JSON → returned generic methods
    like "LSTM" for a Transformer attention topic.  This version passes:
      - Research topic
      - ALL hypotheses (title + method_outline summaries)
      - Literature review excerpts
      - Reviewer feedback from failed attempts
      - Known baselines (as hints to include or avoid)

    Returns a list of method name strings (deduplicated by caller).
    """
    # Build compact hypothesis summary (titles + method outlines only)
    hypo_context = ""
    if all_hypotheses_json:
        try:
            hypo_data = json.loads(all_hypotheses_json)
            hypotheses = hypo_data.get("hypotheses", [])
            hypo_lines = []
            for h in hypotheses:
                hid = h.get("id", "?")
                title = h.get("title", "")[:120]
                method = (h.get("method_outline") or "")[:300]
                key_refs = h.get("key_references", [])
                refs_str = ", ".join(key_refs[:5]) if isinstance(key_refs, list) else str(key_refs)[:200]
                hypo_lines.append(
                    f"**{hid}**: {title}\n  Method: {method}\n  Key refs: {refs_str}"
                )
            hypo_context = "\n\n".join(hypo_lines)
        except Exception:
            hypo_context = all_hypotheses_json[:4000]

    if not hypo_context and not topic:
        return []

    # Reviewer guidance
    feedback_note = ""
    if retry_feedback:
        # Extract the core complaint from feedback (trim noise)
        core = retry_feedback[:1500]
        feedback_note = (
            f"\n\n## Reviewer Feedback from Previous Failed Attempt\n"
            f"{core}\n\n"
            f"CRITICAL: The previous baselines were WRONG. Do NOT repeat them. "
            f"Choose baselines that are actually RELEVANT to the research topic "
            f"and the hypotheses below. Focus on methods explicitly mentioned in "
            f"the hypotheses' method_outline and key_references fields.\n"
        )

    # Known baselines as positive hints or negative indicators
    known_note = ""
    if known_baselines:
        known_note = (
            f"\n\n## Suggested baselines from topic analysis\n"
            + ", ".join(known_baselines[:15])
            + "\nInclude these if they match the hypotheses. You may add more.\n"
        )

    prompt = (
        f"You are identifying BASELINE methods for a research project. "
        f"Your job is to find 5-10 specific, well-known methods/models that "
        f"the proposed method should be COMPARED AGAINST in experiments.\n\n"
        f"## Research Topic\n{topic}\n\n"
    )
    if hypo_context:
        prompt += (
            f"## All Research Hypotheses (look for method names in method_outline "
            f"and key_references)\n{hypo_context[:6000]}\n\n"
        )
    if lit_review:
        prompt += (
            f"## Literature Review Context\n{lit_review[:4000]}\n\n"
        )
    prompt += (
        f"{feedback_note}"
        f"{known_note}"
        f"\n## Instructions\n"
        f"1. Extract method names explicitly mentioned in the hypotheses "
        f"(especially method_outline and key_references fields)\n"
        f"2. Add canonically well-known baselines for this topic area\n"
        f"3. Aim for 5-10 methods total — not 1 or 2\n"
        f"4. Use the EXACT canonical name (e.g., 'FlashAttention' not 'flash attn')\n"
        f"5. Do NOT include generic names like 'LSTM', 'CNN', 'MLP' unless the "
        f"hypothesis explicitly targets them as primary baselines\n\n"
        f"Output ONLY a JSON array of method name strings:\n"
        f'["FlashAttention", "Linformer", "Reformer", "Mamba", "Performer", '
        f'"BigBird", "Longformer", "Linear Transformer"]\n'
        f"No explanation, no markdown — just the JSON array.\n"
    )

    try:
        result = subprocess.run(
            [CLAUDE_CMD, "-p", "--output-format", "text", "--model", CLAUDE_MODEL,
             prompt],
            capture_output=True, text=True, timeout=120,
            cwd=str(PROJECT_ROOT), env=_claude_subprocess_env(),
        )
        raw = result.stdout or ""
        m = re.search(r'\[.*?\]', raw, re.DOTALL)
        if m:
            names = json.loads(m.group(0))
            if isinstance(names, list):
                return [n for n in names if isinstance(n, str) and len(n) > 1]
    except Exception as e:
        logger.warning("LLM baseline extraction (full context) failed: %s", e)
    return []


def _purge_stale_baseline_caches(state, keep_methods: list[str]) -> None:
    """Clear cached repos from previous runs whose method names aren't in keep_methods.

    Prevents the "cache always wins" problem where retry reuses a wrong repo
    (e.g., LSTM tutorial) because it was cached in a previous attempt.
    """
    import shutil as _shutil
    cache_dir = (Path(state.work_dir) / ".." / ".shared" / "baselines").resolve()
    if not cache_dir.exists():
        return

    keep_lower = {m.lower().strip() for m in keep_methods}
    purged = 0
    for entry in cache_dir.iterdir():
        if not entry.is_dir():
            continue
        # Determine if this cache entry matches any kept method
        keep = False
        for m in keep_lower:
            slug = hashlib.md5(m.encode()).hexdigest()[:8]
            if slug in entry.name:
                keep = True
                break
            # Also check if method name appears at the start (legacy slug format)
            if m.replace(" ", "_").lower() in entry.name.lower():
                keep = True
                break

        if not keep:
            logger.info("Purging stale baseline cache: %s", entry.name)
            _shutil.rmtree(entry, ignore_errors=True)
            purged += 1

    if purged:
        logger.info("Purged %d stale baseline cache(s)", purged)


def _validate_baseline_repo_v2(repo_dir: Path, method_name: str, topic: str = "") -> tuple[bool, str]:
    """Validate a cloned repo for method name AND topic relevance.

    Returns (is_valid, reason_string).

    Gate 1 (hard): method name must appear in README or source files.
    Gate 2 (LLM): repo content is evaluated by an LLM for semantic
        relevance to the research topic.  This replaces keyword / stop-word
        heuristics that break on content we don't control.
    """
    name_lower = method_name.lower().strip()

    readme_content = ""
    for readme_name in ["README.md", "README.rst", "readme.md", "README.txt"]:
        readme = repo_dir / readme_name
        if readme.exists():
            try:
                readme_content = readme.read_text(errors="replace")
                break
            except Exception:
                pass

    if not readme_content:
        py_files = list(repo_dir.glob("**/*.py"))[:20]
        for f in py_files:
            try:
                readme_content += f.read_text(errors="replace")[:2000]
            except Exception:
                pass

    # ── Gate 1: method name presence (fast, no LLM) ──
    content_lower = readme_content[:8000].lower()
    name_found = name_lower in content_lower
    if not name_found and "-" in name_lower:
        name_found = name_lower.replace("-", " ") in content_lower

    if not name_found:
        return False, f"method '{method_name}' not mentioned in README/source"

    # ── Gate 2: LLM topic relevance (replaces keyword heuristics) ──
    if topic and len(readme_content) > 100:
        is_relevant, reason = _llm_judge_repo_relevance(
            readme_content, method_name, topic,
        )
        return is_relevant, reason

    return True, "ok (no topic filter)"


def _llm_judge_repo_relevance(
    readme: str, method_name: str, topic: str,
) -> tuple[bool, str]:
    """Ask an LLM whether a repo's README is relevant to the research topic.

    This replaces brittle keyword / stop-word heuristics with semantic
    understanding.  The LLM sees ~3 KB of README + topic and returns a
    binary yes/no judgment.
    """
    # Escape code fences in README content so they don't confuse the LLM
    safe_readme = readme[:3000].replace("```", "'''")
    prompt = (
        f"Does this GitHub repository contain an implementation of, or code "
        f"directly related to, the method **{method_name}** in the context of "
        f"this research topic?\n\n"
        f"Research topic: {topic}\n\n"
        f"## Repository README (first 3000 chars, code fences escaped as ''')"
        f"\n{safe_readme}\n\n"
        f"Return ONLY a single JSON object with two fields:\n"
        f'- "relevant": true or false\n'
        f'- "reason": a one-sentence explanation (max 120 chars)\n'
        f"No other output.\n"
    )
    try:
        result = subprocess.run(
            [CLAUDE_CMD, "-p", "--output-format", "text", "--model", CLAUDE_MODEL],
            input=prompt, capture_output=True, text=True, timeout=60,
            cwd=str(PROJECT_ROOT), env=_claude_subprocess_env(),
        )
        raw = result.stdout or ""
        m = re.search(r'\{[^}]*"relevant"[^}]*\}', raw, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            relevant = data.get("relevant", False)
            reason = data.get("reason", "LLM relevance check")
            return relevant, reason
    except Exception as e:
        logger.warning("LLM repo relevance check failed for '%s': %s", method_name, e)

    # Fallback: allow through (Gate 1 already passed)
    return True, "LLM check failed — allowing through"


def _check_npu_broken() -> str:
    """Test if torch_npu can actually run ops.  Returns CANN version string if
    broken (kernel parse error), empty string if NPU is usable."""
    try:
        import torch
        import torch_npu
        if not torch.npu.is_available():
            return ""
        # Quick sanity check — if this fails, NPU training won't work
        t = torch.ones(2, 2).npu()
        t.mean().item()
        return ""  # Works fine
    except RuntimeError as e:
        msg = str(e)[:200]
        # Kernel parse error / aclnnMean failure → version mismatch
        if "kernel" in msg.lower() or "aclnn" in msg.lower():
            return "driver/kernel version mismatch"
        return f"runtime error: {msg[:80]}"
    except Exception as e:
        return f"error: {e!s:80s}"


def _ensure_simulation_results(
    state: ResearchState, lit: str, hyp: str, npu_reason: str,
) -> None:
    """Generate estimated experiment results when NPU training is unavailable.

    Only writes files that are missing — never overwrites real results."""
    exp_dir = Path(state.experiment_dir)
    manifest = exp_dir / "experiment_manifest.json"
    results = exp_dir / "experiment_results.json"
    log = exp_dir / "experiment_log.md"

    # Only generate if core files are truly missing
    if manifest.exists() and results.exists():
        return

    logger.info("NPU unavailable (%s) — generating literature-estimated results", npu_reason)

    if not manifest.exists():
        manifest.write_text(json.dumps({
            "mode": "simulation",
            "reason": f"NPU training unavailable: {npu_reason}",
            "architecture": "See experiment_plan.md for full design",
            "hypothesis_file": f"{state.hypothesis_dir}/hypothesis_output.json",
        }, indent=2))

    if not results.exists():
        results.write_text(json.dumps({
            "mode": "simulation",
            "note": "Results estimated from literature benchmarks — not from actual training runs",
            "metrics": {},
            "comparisons": [],
        }, indent=2))

    if not log.exists():
        log.write_text(
            f"# Experiment Log (Simulation)\n\n"
            f"**Reason:** NPU training unavailable — {npu_reason}\n\n"
            f"Results are literature-estimated values for pipeline continuity.\n"
            f"Re-run with working GPU/NPU for real experimental data.\n"
        )


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
            print(f"  iter {r['iteration']}: verdict={r.get('verdict', 'pending'):15s}  token=已获取")


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
