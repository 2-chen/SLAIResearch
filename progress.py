"""
Progress emitter for the SLAIResearch pipeline.
Writes real-time progress events to workspace/progress.json with atomic writes.
Maintains a rolling window of up to 50 events.
"""

import json
import os
import tempfile
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

MAX_EVENTS = 50


class ProgressEmitter:
    """Writes pipeline progress events to a JSON file with atomic writes."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._events: list[dict[str, Any]] = []
        self._start_time = datetime.now(timezone.utc)

    def _emit(self, event: dict) -> None:
        event.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        elapsed = (datetime.now(timezone.utc) - self._start_time).total_seconds()
        event.setdefault("elapsed_s", round(elapsed, 1))
        self._events.append(event)
        if len(self._events) > MAX_EVENTS:
            self._events = self._events[-MAX_EVENTS:]
        self._write()

    def _write(self) -> None:
        data = {
            "events": self._events,
            "last_update": datetime.now(timezone.utc).isoformat(),
        }
        content = json.dumps(data, indent=2, ensure_ascii=False)
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._path.parent), suffix=".tmp"
            )
            try:
                os.write(fd, content.encode("utf-8"))
            finally:
                os.close(fd)
            os.replace(tmp_path, self._path)
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            # Fallback to direct write
            self._path.write_text(content, encoding="utf-8")

    # ── event helpers ──

    def stage_start(
        self, stage: str, total_stages: int, stage_index: int, message: str = ""
    ) -> None:
        self._emit({
            "type": "stage_start",
            "stage": stage,
            "total_stages": total_stages,
            "stage_index": stage_index,
            "message": message,
        })

    def stage_complete(
        self, stage: str, total_stages: int, stage_index: int, message: str = ""
    ) -> None:
        self._emit({
            "type": "stage_complete",
            "stage": stage,
            "total_stages": total_stages,
            "stage_index": stage_index,
            "message": message,
        })

    def substep(self, stage: str, message: str) -> None:
        self._emit({
            "type": "substep",
            "stage": stage,
            "message": message,
        })

    def error(self, stage: str, message: str) -> None:
        self._emit({
            "type": "error",
            "stage": stage,
            "message": message,
        })

    def pipeline_complete(self, success: bool, message: str = "") -> None:
        self._emit({
            "type": "pipeline_complete",
            "success": success,
            "message": message,
        })
