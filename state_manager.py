"""
JSON-file state manager for the ChenResearch pipeline.
Each research topic gets its own state file under state/<topic_slug>/state.json.
Thread-safe enough for a single-process orchestrator.
"""

import json
import hashlib
import logging
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class Stage(str, Enum):
    LITERATURE_SEARCH = "literature_search"
    HYPOTHESIS_GENERATION = "hypothesis_generation"
    EXPERIMENT_DESIGN = "experiment_design"
    EXPERIMENT_EXECUTION = "experiment_execution"
    PAPER_WRITING = "paper_writing"
    SUBMIT_REVIEW = "submit_review"
    POLL_REVIEW = "poll_review"
    REVISE = "revise"
    RESUBMIT = "resubmit"
    DONE = "done"
    FAILED = "failed"


class StageStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    REVIEWING = "reviewing"       # stage output is being reviewed
    REVIEW_FAILED = "review_failed"  # review rejected, needs retry


@dataclass
class StageReviewRecord:
    """Record of a single stage-level review attempt."""
    attempt: int
    stage: str
    score: float = 0.0
    passed: bool = False
    feedback: str = ""
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    critical_issues: list[str] = field(default_factory=list)
    suggestion: str = ""
    reviewer_mode: str = "llm"  # "llm" or "human"
    reviewed_at: str = ""


@dataclass
class StageState:
    status: str = "pending"
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    # Stage-level review tracking
    review_attempts: int = 0           # how many times reviewed for this stage
    review_history: list[dict[str, Any]] = field(default_factory=list)  # list of StageReviewRecord as dicts
    review_passed: bool = False        # whether the most recent review passed


@dataclass
class ReviewRecord:
    iteration: int
    token: str
    verdict: str = ""
    review_md_path: str = ""
    submitted_at: str = ""
    reviewed_at: str = ""


@dataclass
class ResearchState:
    topic: str
    topic_slug: str = ""
    stage: str = Stage.LITERATURE_SEARCH.value
    iteration: int = 0
    max_iterations: int = 10
    target_verdict: str = "weak accept"
    created_at: str = ""
    updated_at: str = ""

    # Pipeline stage states
    stages: dict[str, StageState] = field(default_factory=dict)

    # Working directories
    work_dir: str = ""
    literature_dir: str = ""
    hypothesis_dir: str = ""
    experiment_dir: str = ""
    paper_dir: str = ""
    review_dir: str = ""

    # Review records
    reviews: list[dict[str, Any]] = field(default_factory=list)

    # Email / Venue
    email: str = "250010008@slai.edu.cn"
    venue: str = "AAAI"

    # Stage-level review configuration
    stage_review_enabled: bool = True
    stage_review_max_retries: int = 10     # max retries per stage
    stage_review_mode: str = "llm"         # "llm" or "human"
    stage_review_model: str = ""           # model for review (empty = use default)


class StateManager:
    """Load / save / create ResearchState backed by a JSON file."""

    def __init__(self, state_dir: str | Path = "state"):
        self._base = Path(state_dir)

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    def create(self, topic: str, work_dir: str | Path = ".") -> ResearchState:
        """Create a fresh state for *topic*.  Raises if state already exists."""
        slug = _slugify(topic)
        state_path = self._path(slug)
        if state_path.exists():
            raise FileExistsError(f"State already exists for '{topic}': {state_path}")

        work_path = Path(work_dir).resolve()
        now = _now()

        state = ResearchState(
            topic=topic,
            topic_slug=slug,
            created_at=now,
            updated_at=now,
            work_dir=str(work_path),
            literature_dir=str(work_path / "literature"),
            hypothesis_dir=str(work_path / "hypothesis"),
            experiment_dir=str(work_path / "experiment"),
            paper_dir=str(work_path / "paper"),
            review_dir=str(work_path / "review"),
            stages={
                st.value: StageState() for st in Stage
            },
        )
        self._save(slug, state)
        # Create subdirectories
        for d in (
            state.literature_dir,
            state.hypothesis_dir,
            state.experiment_dir,
            state.paper_dir,
            state.review_dir,
        ):
            Path(d).mkdir(parents=True, exist_ok=True)
        logger.info("State created: %s", state_path)
        return state

    def load(self, topic_or_slug: str) -> ResearchState:
        slug = _slugify(topic_or_slug) if " " in topic_or_slug else topic_or_slug
        data = json.loads(self._path(slug).read_text())
        # Rebuild StageState objects (they deserialize as plain dicts)
        if "stages" in data:
            data["stages"] = {
                k: StageState(**v) if isinstance(v, dict) else v
                for k, v in data["stages"].items()
            }
        return ResearchState(**data)

    def save(self, state: ResearchState) -> None:
        state.updated_at = _now()
        self._save(state.topic_slug, state)

    def exists(self, topic_or_slug: str) -> bool:
        slug = _slugify(topic_or_slug) if " " in topic_or_slug else topic_or_slug
        return self._path(slug).exists()

    def slug_for(self, topic: str) -> str:
        """Return the slug for *topic* without creating state."""
        return _slugify(topic)

    def list_topics(self) -> list[str]:
        """Return slugs of all saved research topics."""
        if not self._base.exists():
            return []
        return [
            p.parent.name
            for p in self._base.rglob("state.json")
        ]

    # ------------------------------------------------------------------
    # stage helpers
    # ------------------------------------------------------------------

    def start_stage(self, state: ResearchState, stage: Stage) -> ResearchState:
        state.stage = stage.value
        st = state.stages.setdefault(stage.value, StageState())
        st.status = StageStatus.IN_PROGRESS.value
        st.started_at = _now()
        self.save(state)
        return state

    def complete_stage(
        self, state: ResearchState, stage: Stage, meta: dict | None = None
    ) -> ResearchState:
        st = state.stages.setdefault(stage.value, StageState())
        st.status = StageStatus.COMPLETED.value
        st.completed_at = _now()
        if meta:
            st.meta.update(meta)
        self.save(state)
        return state

    def fail_stage(
        self, state: ResearchState, stage: Stage, error: str
    ) -> ResearchState:
        st = state.stages.setdefault(stage.value, StageState())
        st.status = StageStatus.FAILED.value
        st.error = error
        st.completed_at = _now()
        state.stage = Stage.FAILED.value
        self.save(state)
        return state

    def add_review(self, state: ResearchState, record: ReviewRecord) -> ResearchState:
        state.iteration = record.iteration
        state.reviews.append(asdict(record))
        self.save(state)
        return state

    # ------------------------------------------------------------------
    # stage-level review helpers
    # ------------------------------------------------------------------

    def start_stage_review(self, state: ResearchState, stage: Stage) -> ResearchState:
        """Mark a stage as being reviewed."""
        st = state.stages.setdefault(stage.value, StageState())
        st.status = StageStatus.REVIEWING.value
        self.save(state)
        return state

    def record_stage_review(
        self,
        state: ResearchState,
        stage: Stage,
        review_record: StageReviewRecord,
    ) -> ResearchState:
        """Record a stage review verdict."""
        st = state.stages.setdefault(stage.value, StageState())
        st.review_attempts += 1
        st.review_passed = review_record.passed
        st.review_history.append({
            "attempt": review_record.attempt,
            "score": review_record.score,
            "passed": review_record.passed,
            "feedback": review_record.feedback,
            "strengths": review_record.strengths,
            "weaknesses": review_record.weaknesses,
            "critical_issues": review_record.critical_issues,
            "suggestion": review_record.suggestion,
            "reviewer_mode": review_record.reviewer_mode,
            "reviewed_at": review_record.reviewed_at or _now(),
        })
        if not review_record.passed:
            st.status = StageStatus.REVIEW_FAILED.value
        self.save(state)
        return state

    def get_stage_review_feedback(
        self, state: ResearchState, stage: Stage
    ) -> str:
        """Get accumulated review feedback for a stage (for retry context)."""
        st = state.stages.get(stage.value)
        if not st or not isinstance(st, StageState):
            return ""
        if not st.review_history:
            return ""

        parts = []
        for i, r in enumerate(st.review_history):
            if not r.get("passed", False):
                parts.append(f"## Review Round {i+1} (Score: {r.get('score', '?')}/10)")
                if r.get("critical_issues"):
                    parts.append("\nCritical Issues:")
                    for c in r["critical_issues"]:
                        parts.append(f"  - {c}")
                if r.get("weaknesses"):
                    parts.append("\nWeaknesses:")
                    for w in r["weaknesses"]:
                        parts.append(f"  - {w}")
                if r.get("suggestion"):
                    parts.append(f"\nSuggestion: {r['suggestion']}")
                if r.get("feedback"):
                    parts.append(f"\nDetailed Feedback: {r['feedback']}")
                parts.append("")
        return "\n".join(parts)

    def stage_review_retries_exhausted(
        self, state: ResearchState, stage: Stage
    ) -> bool:
        """Check if max review retries have been exhausted for a stage."""
        st = state.stages.get(stage.value)
        if not st or not isinstance(st, StageState):
            return False
        failed_reviews = sum(
            1 for r in st.review_history if not r.get("passed", False)
        )
        return failed_reviews >= state.stage_review_max_retries

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _path(self, slug: str) -> Path:
        return self._base / slug / "state.json"

    def _save(self, slug: str, state: ResearchState) -> None:
        p = self._path(slug)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(state), indent=2, ensure_ascii=False))


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slugify(text: str) -> str:
    """Short deterministic slug for a topic string."""
    import re
    # Use first 40 chars + hash suffix to keep paths manageable
    prefix = text.strip().lower().replace(" ", "_")[:40]
    # Strip characters not allowed in SCO job names and safe filenames
    prefix = re.sub(r'[^a-z0-9_-]', '', prefix)
    # Strip leading/trailing underscores and hyphens (keeps slug clean)
    prefix = prefix.strip('_-')
    suffix = hashlib.md5(text.encode()).hexdigest()[:6]
    return f"{prefix}_{suffix}"
