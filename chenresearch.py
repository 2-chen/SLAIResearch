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
import subprocess
import logging
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

from config import (
    CLAUDE_CMD, CLAUDE_MODEL,
    PAPERREVIEW_EMAIL, PAPERREVIEW_VENUE,
    POLL_INITIAL_WAIT, POLL_INTERVAL, POLL_MAX_WAIT,
    MAX_ITERATIONS, TARGET_VERDICT,
)
from state_manager import StateManager, Stage, ResearchState, ReviewRecord
from paperreview_api import (
    submit_paper, poll_review, extract_verdict, review_to_markdown,
)
from sco_runner import submit_job, wait_for_job, stream_logs, SCOConfig

logger = logging.getLogger("chenresearch")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)


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
    sm.save(state)

    print(f"\n{'='*60}")
    print(f"  ChenResearch Pipeline")
    print(f"  Topic: {topic}")
    print(f"  Venue: {PAPERREVIEW_VENUE}  |  Max iterations: {MAX_ITERATIONS}")
    print(f"  Model: {CLAUDE_MODEL}")
    print(f"  Work dir: {work_dir}")
    print(f"{'='*60}\n")

    _run_pipeline(sm, state)


def cmd_resume(topic_or_slug: str) -> None:
    """Resume pipeline from last saved state."""
    sm = StateManager(PROJECT_ROOT / "state")
    if not sm.exists(topic_or_slug):
        print(f"No saved state found for '{topic_or_slug}'.")
        return
    state = sm.load(topic_or_slug)
    print(f"Resuming '{state.topic}' from stage '{state.stage}' (iteration {state.iteration})")
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
    first_run_stages = [
        Stage.LITERATURE_SEARCH,
        Stage.EXPERIMENT_DESIGN,
        Stage.EXPERIMENT_EXECUTION,
        Stage.PAPER_WRITING,
    ]

    for stage in first_run_stages:
        st = state.stages.get(stage.value)
        if st and getattr(st, "status", None) == "completed":
            logger.info("Stage %s already completed — skipping", stage.value)
            continue

        state = sm.start_stage(state, stage)
        try:
            state = STAGE_HANDLERS[stage](sm, state)
        except Exception as exc:
            logger.exception("Stage %s failed: %s", stage.value, exc)
            sm.fail_stage(state, stage, str(exc))
            return

    # Iteration loop
    while state.iteration < state.max_iterations:
        submit_stage = Stage.SUBMIT_REVIEW if state.iteration == 0 else Stage.RESUBMIT
        state = sm.start_stage(state, submit_stage)

        try:
            state = _do_submit(sm, state)
        except Exception as exc:
            logger.exception("Submit failed: %s", exc)
            sm.fail_stage(state, submit_stage, str(exc))
            return

        state = sm.start_stage(state, Stage.POLL_REVIEW)
        try:
            state, verdict = _do_poll(sm, state)
        except TimeoutError:
            tok = state.reviews[-1].get("token", "N/A") if state.reviews else "N/A"
            print(f"\n[!] Review timed out. Token: {tok}")
            print(f"[!] Check manually: https://paperreview.ai/review?token={tok}")
            sm.save(state)
            return
        except Exception as exc:
            logger.exception("Poll failed: %s", exc)
            sm.fail_stage(state, Stage.POLL_REVIEW, str(exc))
            return

        if verdict in ("accept", "weak accept"):
            print(f"\n{'='*60}")
            print(f"  TARGET VERDICT REACHED: {verdict}")
            print(f"  Total iterations: {state.iteration + 1}")
            print(f"{'='*60}")
            state.stage = Stage.DONE.value
            sm.save(state)
            return

        state.iteration += 1
        state = sm.start_stage(state, Stage.REVISE)
        try:
            state = _do_revise(sm, state)
        except Exception as exc:
            logger.exception("Revise failed: %s", exc)
            sm.fail_stage(state, Stage.REVISE, str(exc))
            return

    print(f"\n[!] Reached max iterations ({state.max_iterations}) without target verdict.")
    state.stage = Stage.DONE.value
    sm.save(state)


# ======================================================================
# Stage handlers
# ======================================================================


def _do_literature_search(sm: StateManager, state: ResearchState) -> ResearchState:
    prompt = _load_prompt("literature_search.md",
        TOPIC=state.topic,
        OUTPUT_DIR=state.literature_dir,
    )
    logger.info("Calling Claude Code for literature search …")
    return _call_claude(sm, state, Stage.LITERATURE_SEARCH, prompt)


def _do_experiment_design(sm: StateManager, state: ResearchState) -> ResearchState:
    lit = _read_or(state.literature_dir, "literature_review.md")
    prompt = _load_prompt("experiment_design.md",
        TOPIC=state.topic,
        LITERATURE_REVIEW=lit[:30000],
        OUTPUT_DIR=state.experiment_dir,
    )
    logger.info("Calling Claude Code for experiment design …")
    return _call_claude(sm, state, Stage.EXPERIMENT_DESIGN, prompt)


def _do_experiment_execution(sm: StateManager, state: ResearchState) -> ResearchState:
    script = Path(state.experiment_dir) / "run_experiment.sh"
    if not script.exists():
        raise FileNotFoundError(f"Experiment script not found: {script}")

    logger.info("Submitting experiment to SCO cluster …")
    job = submit_job(
        script_path=script,
        job_name=f"ar-{state.topic_slug}",
        extra_env={"CHENRESEARCH": "1"},
    )

    logger.info("Waiting for SCO job %s …", job.job_id)
    try:
        job = wait_for_job(job.job_id, poll_interval=120, max_wait=43200)
        stream_logs(job.job_id, Path(state.experiment_dir) / "sco_logs.txt")
        logger.info("Experiment finished: %s", job.status)
    except TimeoutError:
        logger.warning("Experiment still running — resume later")
        sm.save(state)
        return state

    return sm.complete_stage(state, Stage.EXPERIMENT_EXECUTION, {
        "job_id": job.job_id, "job_status": job.status,
    })


def _do_paper_writing(sm: StateManager, state: ResearchState) -> ResearchState:
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
    return _call_claude(sm, state, Stage.PAPER_WRITING, prompt)


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
    return state, verdict


def _do_revise(sm: StateManager, state: ResearchState) -> ResearchState:
    reviews = []
    for rf in sorted(Path(state.review_dir).glob("review_iter*.md")):
        reviews.append(rf.read_text())
    combined = "\n\n---\n\n".join(reviews[-3:])

    prompt = _load_prompt("paper_revision.md",
        TOPIC=state.topic,
        REVIEWS=combined,
        OUTPUT_DIR=state.paper_dir,
        ITERATION=str(state.iteration),
    )
    logger.info("Calling Claude Code for paper revision (iteration %d) …", state.iteration)
    return _call_claude(sm, state, Stage.REVISE, prompt)


STAGE_HANDLERS = {
    Stage.LITERATURE_SEARCH: _do_literature_search,
    Stage.EXPERIMENT_DESIGN: _do_experiment_design,
    Stage.EXPERIMENT_EXECUTION: _do_experiment_execution,
    Stage.PAPER_WRITING: _do_paper_writing,
}


# ======================================================================
# Claude Code tool integration
# ======================================================================


def _call_claude(sm: StateManager, state: ResearchState, stage: Stage, prompt: str) -> ResearchState:
    """
    Invoke Claude Code as an EXECUTION TOOL of this project.
    Claude Code runs headless (`claude -p`) to perform the assigned task,
    then returns.  The project orchestrator remains in control.
    """
    prompt_file = Path(state.work_dir) / f"{stage.value}_prompt.md"
    prompt_file.write_text(prompt)

    output_file = Path(state.work_dir) / f"{stage.value}_output.md"

    cmd = [CLAUDE_CMD, "-p", "--output-format", "text", "--model", CLAUDE_MODEL, prompt]

    logger.info("Executing: %s ...", " ".join(cmd[:4]))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                                cwd=str(state.work_dir))
        output = result.stdout or ""
        if result.returncode != 0:
            logger.warning("claude -p exit=%d stderr=%s", result.returncode, result.stderr[:300])
        output_file.write_text(output)
        logger.info("Claude output → %s (%d chars)", output_file, len(output))
    except FileNotFoundError:
        logger.warning(
            "`%s` CLI not found. Prompt saved to %s — run manually.",
            CLAUDE_CMD, prompt_file,
        )
        raise

    return sm.complete_stage(state, stage, {
        "prompt_file": str(prompt_file),
        "output_file": str(output_file),
    })


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
    return text.strip().replace(" ", "_")[:50]


def _print_status(state: ResearchState) -> None:
    print(f"\nTopic:        {state.topic}")
    print(f"Stage:        {state.stage}")
    print(f"Iteration:    {state.iteration}/{state.max_iterations}")
    print(f"Work dir:     {state.work_dir}")
    print(f"\nStage details:")
    for name, st in state.stages.items():
        marker = "←" if name == state.stage else " "
        s = st if isinstance(st, dict) else {"status": getattr(st, "status", "?")}
        print(f"  [{marker}] {name:25s}  {s.get('status', '?'):12s}")
    if state.reviews:
        print(f"\nReview history:")
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
