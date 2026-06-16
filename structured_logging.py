"""
Structured JSON-lines logging for SLAIResearch pipeline.

Every log line is a standalone JSON object with fields:
  timestamp, stage, level, message, metrics, error_type, session_id, source_file

Logs are written to workspace/<project>/logs/structured.jsonl and aggregated
to state/<slug>/pipeline_log.jsonl.
"""

import json
import logging
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------

class JSONLinesFormatter(logging.Formatter):
    """Emit each log record as a single JSON line."""

    def __init__(self, session_id: str = "", stage: str = ""):
        super().__init__()
        self.session_id = session_id
        self.stage = stage

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        if self.session_id:
            payload["session_id"] = self.session_id
        if self.stage:
            payload["stage"] = self.stage
        if hasattr(record, "metrics") and record.metrics:
            payload["metrics"] = record.metrics
        if hasattr(record, "error_type") and record.error_type:
            payload["error_type"] = record.error_type
        if hasattr(record, "source_file") and record.source_file:
            payload["source_file"] = record.source_file
        if record.exc_info and record.exc_info[0]:
            payload["exception"] = traceback.format_exception_only(
                record.exc_info[0], record.exc_info[1]
            )[-1].strip()
        return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Logger factory
# ---------------------------------------------------------------------------

_loggers: dict[str, logging.Logger] = {}
_logger_lock = threading.Lock()


def get_logger(
    name: str = "slairesearch",
    log_path: str | None = None,
    session_id: str = "",
    stage: str = "",
    also_console: bool = True,
) -> logging.Logger:
    """Get or create a structured logger.

    Args:
        name: Logger name (used for caching).
        log_path: Path to JSON-lines log file. If None, no file output.
        session_id: Current SLAIResearch session ID.
        stage: Current pipeline stage.
        also_console: If True, also emit human-readable lines to stderr.

    Returns:
        Configured logger instance.
    """
    cache_key = f"{name}:{log_path or ''}"
    with _logger_lock:
        if cache_key in _loggers:
            return _loggers[cache_key]

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    formatter = JSONLinesFormatter(session_id=session_id, stage=stage)

    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(formatter)
        logger.addHandler(fh)

    if also_console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(logging.INFO)
        ch.setFormatter(
            logging.Formatter(
                "[%(asctime)s] %(levelname)-5s %(message)s", datefmt="%H:%M:%S"
            )
        )
        logger.addHandler(ch)

    _loggers[cache_key] = logger
    return logger


# ---------------------------------------------------------------------------
# Metrics helper
# ---------------------------------------------------------------------------

class MetricsRecord:
    """Accumulate metrics during a pipeline stage and attach to log records."""

    def __init__(self):
        self._data: dict[str, Any] = {}
        self._start = time.monotonic()

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def inc(self, key: str, delta: int = 1) -> None:
        self._data[key] = self._data.get(key, 0) + delta

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._start

    def to_dict(self) -> dict[str, Any]:
        d = dict(self._data)
        d["elapsed_seconds"] = round(self.elapsed_seconds(), 1)
        return d


# ---------------------------------------------------------------------------
# Convenience: create stage-scoped logger from state manager context
# ---------------------------------------------------------------------------

def stage_logger(
    workspace_dir: Path,
    slug: str,
    stage: str,
    session_id: str = "",
) -> logging.Logger:
    """Create a structured logger scoped to a pipeline stage.

    Writes to both:
      - workspace/<project>/logs/structured.jsonl  (per-project)
      - state/<slug>/pipeline_log.jsonl            (per-session)
    """
    ws_log = workspace_dir / "logs" / "structured.jsonl"
    state_log = Path("state") / slug / "pipeline_log.jsonl"
    # Merge both into one handler by using the workspace path
    # and also appending to the state path on each emit.
    logger = get_logger(
        name=f"stage.{slug}.{stage}",
        log_path=str(ws_log),
        session_id=session_id,
        stage=stage,
    )
    # Patch: also write to state log
    if state_log.parent.exists() or True:
        state_log.parent.mkdir(parents=True, exist_ok=True)

        class DualWriter:
            def __init__(self, primary: str, secondary: str):
                self.primary = primary
                self.secondary = secondary

            def write(self, line: str) -> int:
                with open(self.primary, "a") as f:
                    f.write(line)
                with open(self.secondary, "a") as f:
                    f.write(line)
                return len(line)

            def flush(self) -> None:
                pass

        for h in logger.handlers:
            if isinstance(h, logging.FileHandler):
                h.stream = DualWriter(str(ws_log), str(state_log))
                break

    return logger
