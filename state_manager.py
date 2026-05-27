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


@dataclass
class StageState:
    status: str = "pending"
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


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
    experiment_dir: str = ""
    paper_dir: str = ""
    review_dir: str = ""

    # Review records
    reviews: list[dict[str, Any]] = field(default_factory=list)

    # Email / Venue
    email: str = "250010008@slai.edu.cn"
    venue: str = "AAAI"


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
    # Use first 40 chars + hash suffix to keep paths manageable
    prefix = text.strip().lower().replace(" ", "_")[:40]
    suffix = hashlib.md5(text.encode()).hexdigest()[:6]
    return f"{prefix}_{suffix}"
