#!/usr/bin/env python3
"""
Context Compressor for SLAIResearch — prevents Claude Code context overflow
across pipeline iterations.

Features ported from chen-research-skills pipeline-skill:
  1. Level 1 (Soft Compression) — iteration brief, light context pruning
  2. Level 2 (Hard Reset) — comprehensive Resume Packet for fresh sessions
  3. Context health monitoring — detect degradation signals
  4. Mid-entry auto-detection — determine pipeline entry point from existing materials

Usage:
    from context_compressor import ContextCompressor

    cc = ContextCompressor(workspace_dir="workspace/my_topic")
    cc.compress(iteration=3, level="soft")
    entry = cc.detect_entry_point()
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from typing import Any

logger = logging.getLogger("context_compressor")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PipelineState:
    """Serializable pipeline state for resume/compression."""
    topic: str = ""
    topic_slug: str = ""
    workspace: str = ""
    current_stage: str = "literature_search"
    iteration: int = 0
    max_iterations: int = 10
    stages_completed: dict[str, bool] = field(default_factory=dict)
    verdict_history: list[dict[str, Any]] = field(default_factory=list)
    experiment_runs: list[dict[str, Any]] = field(default_factory=list)
    context_compressions: list[dict[str, Any]] = field(default_factory=list)
    key_papers: list[str] = field(default_factory=list)
    key_results: dict[str, Any] = field(default_factory=dict)
    top_reviewer_issues: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


@dataclass
class EntryPointResult:
    """Result of mid-entry detection."""
    stage: str                     # which stage to start at
    confidence: str                # "high", "medium", "low"
    materials_found: dict[str, bool] = field(default_factory=dict)
    reasoning: str = ""


# ---------------------------------------------------------------------------
# Context Compressor
# ---------------------------------------------------------------------------

class ContextCompressor:
    """Manages context compression and pipeline state persistence."""

    def __init__(self, workspace_dir: str | Path):
        self.workspace = Path(workspace_dir)
        self.state_dir = self.workspace / "pipeline_state"
        self.state_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Mid-Entry Detection
    # ------------------------------------------------------------------

    def detect_entry_point(self) -> EntryPointResult:
        """Auto-detect where the pipeline should start based on available materials.

        NOTE: This is a material-based heuristic. The PRIMARY recovery mechanism
        is StateManager.reset_stale_running_stages() + cmd_resume(), which reads
        the authoritative state from state/<slug>/state.json.

        This method is useful when:
        - You only have a workspace directory (no state file)
        - You want to verify what materials exist on disk

        Inspection order:
        1. Check StateManager state (state/<slug>/state.json) — authoritative
        2. pipeline_state/state.json exists → resume from saved state
        3. review/round_*/ exists → start from REVISE with review feedback
        4. paper/paper.tex exists → start from PAPER_WRITING
        5. experiment/results exist → start from PAPER_WRITING
        6. experiment/plan exists → start from EXPERIMENT_EXECUTION
        7. hypothesis exists → start from EXPERIMENT_DESIGN
        8. literature exists → start from EXPERIMENT_DESIGN
        9. Nothing → start from LITERATURE_SEARCH
        """
        materials: dict[str, bool] = {
            "pipeline_state": (self.state_dir / "state.json").exists(),
            "literature": (self.workspace / "literature" / "literature_review.md").exists(),
            "hypothesis": (self.workspace / "hypothesis" / "hypothesis_output.json").exists(),
            "experiment_plan": (self.workspace / "experiment" / "experiment_plan.md").exists(),
            "experiment_results": (self.workspace / "experiment" / "experiment_results.json").exists(),
            "paper_tex": bool(list((self.workspace / "paper").rglob("*.tex"))),
            "paper_pdf": bool(list((self.workspace / "paper").rglob("*.pdf"))),
            "reviews": bool(list((self.workspace / "review").rglob("round_*"))),
        }

        # Priority-ordered detection
        if materials["pipeline_state"]:
            try:
                state = self.load_state()
                stage = state.current_stage
                return EntryPointResult(
                    stage=stage,
                    confidence="high",
                    materials_found=materials,
                    reasoning=f"Resuming from saved pipeline state (iteration {state.iteration})",
                )
            except Exception:
                pass

        if materials["reviews"]:
            # Check if there's a recent review with feedback
            review_rounds = sorted((self.workspace / "review").rglob("round_*"))
            if review_rounds:
                return EntryPointResult(
                    stage="revise",
                    confidence="high",
                    materials_found=materials,
                    reasoning=f"Found {len(review_rounds)} review rounds → start at REVISE",
                )

        if materials["paper_tex"] and not materials["reviews"]:
            return EntryPointResult(
                stage="submit_review",
                confidence="high",
                materials_found=materials,
                reasoning="Paper draft exists but no reviews → start at SUBMIT_REVIEW",
            )

        if materials["experiment_results"]:
            return EntryPointResult(
                stage="paper_writing",
                confidence="high",
                materials_found=materials,
                reasoning="Experiment results available → start at PAPER_WRITING",
            )

        if materials["experiment_plan"]:
            return EntryPointResult(
                stage="experiment_execution",
                confidence="high",
                materials_found=materials,
                reasoning="Experiment plan exists → start at EXPERIMENT_EXECUTION",
            )

        if materials["hypothesis"]:
            return EntryPointResult(
                stage="experiment_design",
                confidence="high",
                materials_found=materials,
                reasoning="Hypothesis generated → start at EXPERIMENT_DESIGN",
            )

        if materials["literature"]:
            return EntryPointResult(
                stage="experiment_design",
                confidence="medium",
                materials_found=materials,
                reasoning="Literature exists → start at EXPERIMENT_DESIGN",
            )

        return EntryPointResult(
            stage="literature_search",
            confidence="high",
            materials_found=materials,
            reasoning="No materials found — start from scratch",
        )

    # ------------------------------------------------------------------
    # Level 1: Soft Compression
    # ------------------------------------------------------------------

    def compress_soft(
        self,
        iteration: int,
        verdict: str = "",
        external_issues: list[str] | None = None,
        internal_issues: list[str] | None = None,
        key_changes: list[str] | None = None,
        experiment_status: str = "",
        key_metrics: dict[str, Any] | None = None,
        file_paths: list[str] | None = None,
    ) -> Path:
        """Generate a compact Iteration Brief (Level 1 compression).

        This is the lightweight option — preserves essential context while
        discarding verbose conversation history.
        """
        external_issues = external_issues or []
        internal_issues = internal_issues or []
        key_changes = key_changes or []
        key_metrics = key_metrics or {}
        file_paths = file_paths or []

        lines = [
            f"# Iteration {iteration} Summary",
            f"",
            f"**Verdict**: {verdict or 'pending'}",
            f"**Timestamp**: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            f"",
        ]

        if external_issues:
            lines.append("## Top External Reviewer Issues")
            for i, issue in enumerate(external_issues, 1):
                lines.append(f"{i}. {issue}")
            lines.append("")

        if internal_issues:
            lines.append("## Top Internal Reviewer Issues")
            for i, issue in enumerate(internal_issues, 1):
                lines.append(f"{i}. {issue}")
            lines.append("")

        if key_changes:
            lines.append("## Key Changes Made")
            for c in key_changes:
                lines.append(f"- {c}")
            lines.append("")

        if experiment_status:
            lines.append(f"## Experiment Status: {experiment_status}")
            if key_metrics:
                for k, v in list(key_metrics.items())[:10]:
                    lines.append(f"- {k}: {v}")
            lines.append("")

        if file_paths:
            lines.append("## Key Files")
            for fp in file_paths:
                lines.append(f"- `{fp}`")
            lines.append("")

        brief = "\n".join(lines)
        brief_path = self.state_dir / f"iteration_{iteration}_brief.md"
        brief_path.write_text(brief)

        # Update compression log
        self._log_compression(iteration, "soft", str(brief_path))

        logger.info("Soft compression: iteration %d brief → %s", iteration, brief_path)
        return brief_path

    # ------------------------------------------------------------------
    # Level 2: Hard Reset (Resume Packet)
    # ------------------------------------------------------------------

    def compress_hard(
        self,
        topic: str = "",
        iteration: int = 0,
        pipeline_state: PipelineState | None = None,
        paper_path: str | None = None,
        literature_summary: str = "",
        experiment_summary: str = "",
        review_history: list[dict[str, Any]] | None = None,
        next_priorities: list[str] | None = None,
    ) -> Path:
        """Generate a comprehensive Resume Packet (Level 2 compression).

        This is for hard resets — start a fresh Claude Code session with
        this packet as the initial prompt. Contains everything needed to
        continue the pipeline without reading full history.
        """
        review_history = review_history or []
        next_priorities = next_priorities or []

        lines = [
            "# Pipeline Resume Packet",
            f"",
            f"**Topic**: {topic}",
            f"**Iteration**: {iteration}",
            f"**Generated**: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            f"",
        ]

        # Research Summary
        lines.append("## Research Summary")
        lines.append(f"Topic: {topic}")
        if literature_summary:
            lines.append(f"Literature: {literature_summary[:1000]}")
        else:
            lines.append("Literature: Check workspace/literature/literature_review.md")
        lines.append("")

        # Experiment Status
        lines.append("## Experiment Status")
        if experiment_summary:
            lines.append(experiment_summary[:1000])
        else:
            lines.append("Experiment results: Check workspace/experiment/")
        lines.append("")

        # Paper Status
        lines.append("## Paper Status")
        if paper_path:
            lines.append(f"Current draft: `{paper_path}`")
        else:
            tex_files = list((self.workspace / "paper").rglob("*.tex"))
            if tex_files:
                lines.append(f"Current draft: `{tex_files[-1]}`")
            else:
                lines.append("No paper draft found")
        lines.append("")

        # Review History
        if review_history:
            lines.append("## Review History")
            lines.append("| Iteration | External Verdict | Internal Avg Score | Top Issue |")
            lines.append("|-----------|-----------------|-------------------|-----------|")
            for rh in review_history:
                lines.append(
                    f"| {rh.get('iteration', '?')} "
                    f"| {rh.get('external_verdict', '?')} "
                    f"| {rh.get('internal_avg', '?')} "
                    f"| {rh.get('top_issue', '?')[:60]} |"
                )
            lines.append("")

        # Next Priorities
        if next_priorities:
            lines.append("## Next Iteration Priorities")
            for i, p in enumerate(next_priorities, 1):
                lines.append(f"{i}. {p}")
            lines.append("")

        # File Map
        lines.append("## File Map")
        for dir_name, desc in [
            ("literature", "Literature review, references, papers"),
            ("experiment", "Experiment code, scripts, results"),
            ("paper", "Paper LaTeX, figures, compiled PDF"),
            ("review", "Review rounds (external + internal)"),
            ("pipeline_state", "Pipeline state, iteration briefs"),
        ]:
            d = self.workspace / dir_name
            if d.exists():
                lines.append(f"- **{desc}**: `{d}/`")
            else:
                lines.append(f"- **{desc}**: (not created yet)")
        lines.append("")

        resume = "\n".join(lines)
        resume_path = self.state_dir / "resume_packet.md"
        resume_path.write_text(resume)

        # Update compression log
        self._log_compression(iteration, "hard", str(resume_path))

        logger.info("Hard compression: resume packet → %s", resume_path)
        return resume_path

    # ------------------------------------------------------------------
    # State Management
    # ------------------------------------------------------------------

    def save_state(self, state: PipelineState) -> Path:
        """Save pipeline state to JSON."""
        state.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        path = self.state_dir / "state.json"
        path.write_text(json.dumps(asdict(state), indent=2, ensure_ascii=False))
        return path

    def load_state(self) -> PipelineState:
        """Load pipeline state from JSON."""
        path = self.state_dir / "state.json"
        if not path.exists():
            raise FileNotFoundError(f"No pipeline state at {path}")
        data = json.loads(path.read_text())
        return PipelineState(**data)

    def _log_compression(self, iteration: int, level: str, path: str) -> None:
        """Log a compression event."""
        log_path = self.state_dir / "compression_log.json"
        log: list[dict] = []
        if log_path.exists():
            log = json.loads(log_path.read_text())
        log.append({
            "iteration": iteration,
            "level": level,
            "path": path,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        log_path.write_text(json.dumps(log, indent=2))

    # ------------------------------------------------------------------
    # Progress Dashboard
    # ------------------------------------------------------------------

    def render_dashboard(self, state: PipelineState) -> str:
        """Render an ASCII progress dashboard for the pipeline."""
        stages = [
            "literature_search", "hypothesis_generation",
            "experiment_design", "experiment_execution",
            "paper_writing", "submit_review",
        ]

        lines = [
            "╔══════════════════════════════════════════════════════╗",
            f"║  Pipeline Status — {state.topic[:45]:45s} ║",
            "╠══════════════════════════════════════════════════════╣",
            f"║  Iteration: {state.iteration}/{state.max_iterations:<40}║",
            "╠══════════════════════════════════════════════════════╣",
            "║                                                       ║",
        ]

        for stage in stages:
            done = state.stages_completed.get(stage, False)
            mark = "✓" if done else " "
            arrow = "←" if stage == state.current_stage else " "
            lines.append(
                f"║  [{mark}] {stage:30s} [{arrow}]                    ║"
            )

        lines.extend([
            "║                                                       ║",
            "╠══════════════════════════════════════════════════════╣",
        ])

        if state.verdict_history:
            lines.append("║  Verdict History:                                     ║")
            for vh in state.verdict_history[-5:]:
                lines.append(
                    f"║    Round: {vh.get('iteration', '?'):2d} → "
                    f"{vh.get('external_verdict', 'pending'):12s} "
                    f"(int: {vh.get('internal_avg', '?')})          ║"
                )

        lines.append("╚══════════════════════════════════════════════════════╝")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="Context compression and pipeline state management",
    )
    sub = parser.add_subparsers(dest="command")

    # detect
    p = sub.add_parser("detect", help="Detect pipeline entry point")
    p.add_argument("workspace", help="Path to workspace")

    # compress
    p = sub.add_parser("compress", help="Compress context")
    p.add_argument("workspace", help="Path to workspace")
    p.add_argument("--level", choices=["soft", "hard"], default="soft")
    p.add_argument("--iteration", type=int, default=1)

    # dashboard
    p = sub.add_parser("dashboard", help="Show progress dashboard")
    p.add_argument("workspace", help="Path to workspace")

    args = parser.parse_args()

    if args.command == "detect":
        cc = ContextCompressor(args.workspace)
        entry = cc.detect_entry_point()
        print(f"Entry point: {entry.stage} (confidence: {entry.confidence})")
        print(f"Reason: {entry.reasoning}")
        print(f"Materials found:")
        for k, v in entry.materials_found.items():
            print(f"  {'✓' if v else '✗'} {k}")

    elif args.command == "compress":
        cc = ContextCompressor(args.workspace)
        if args.level == "soft":
            path = cc.compress_soft(args.iteration, verdict="pending")
        else:
            path = cc.compress_hard(topic="Unknown", iteration=args.iteration)
        print(f"Compression saved to: {path}")

    elif args.command == "dashboard":
        cc = ContextCompressor(args.workspace)
        try:
            state = cc.load_state()
        except FileNotFoundError:
            state = PipelineState(topic="Unknown", workspace=args.workspace)
        print(cc.render_dashboard(state))


if __name__ == "__main__":
    main()
