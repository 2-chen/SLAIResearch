"""
Persistent debug memory for SLAIResearch experiments.

Stores debug records per project so that future debug sessions can learn from
past fixes. Records are saved to workspace/<project>/debug_memory/records.jsonl.

Each record captures:
  - Error signature (hashed from error type + key lines)
  - Root cause classification
  - Fix applied (file changes)
  - Success/failure outcome
  - Timestamps and attempt count

Retrieval finds similar past errors by:
  1. Exact error signature match
  2. Fuzzy keyword overlap
  3. Same project / cross-project
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class DebugRecord:
    """A single debug attempt record."""

    # Identity
    error_signature: str          # SHA-256 of normalized error message
    project_slug: str
    stage: str                    # experiment_execution, etc.

    # Error info
    error_type: str               # OOM, ModuleNotFoundError, SyntaxError, CUDA, etc.
    error_message: str            # First 500 chars of error
    error_key_lines: list[str] = field(default_factory=list)

    # Fix info
    root_cause: str = ""          # Human-readable root cause
    fix_summary: str = ""         # One-line summary of what was changed
    files_modified: list[str] = field(default_factory=list)
    fix_round: int = 0            # Which debug round succeeded (1-indexed)
    total_rounds_attempted: int = 0

    # Outcome
    success: bool = False
    job_id: str = ""
    backend: str = ""             # local or sco

    # Metadata
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DebugRecord:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Error signature computation
# ---------------------------------------------------------------------------

_ERROR_CLASSIFY_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("OOM", re.compile(r"out of memory|OOM|CUDA error.*out of memory|cannot allocate memory", re.I)),
    ("NPU_OOM", re.compile(r"NPU out of memory|ACL.*memory|Ascend.*memory.*exceed|npu.*OOM", re.I)),
    ("ModuleNotFoundError", re.compile(r"ModuleNotFoundError|ImportError|No module named", re.I)),
    ("SyntaxError", re.compile(r"SyntaxError|invalid syntax", re.I)),
    ("CUDA_ERROR", re.compile(r"CUDA error|cudaError|CUDNN_STATUS", re.I)),
    ("NPU_ERROR", re.compile(r"NPU error|npuError|ACL_ERROR|Ascend error|aclRet|ge::", re.I)),
    ("FileNotFoundError", re.compile(r"FileNotFoundError|No such file|cannot find", re.I)),
    ("PermissionError", re.compile(r"Permission denied|PermissionError|not permitted", re.I)),
    ("TimeoutError", re.compile(r"timed out|TimeoutError|time limit", re.I)),
    ("KeyError", re.compile(r"KeyError", re.I)),
    ("TypeError", re.compile(r"TypeError", re.I)),
    ("ValueError", re.compile(r"ValueError", re.I)),
    ("OpenCVTypingBug", re.compile(r"cv2\.dnn.*DictValue|cv2/typing.*AttributeError", re.I)),
    ("AttributeError", re.compile(r"AttributeError|has no attribute", re.I)),
    ("RuntimeError", re.compile(r"RuntimeError", re.I)),
    ("ConnectionError", re.compile(r"Connection.*refused|ConnectionError|NetworkError|Failed to connect", re.I)),
    ("QuotaExhausted", re.compile(r"quota.*exhausted|quota.*limit|insufficient.*quota|member_default|forbid", re.I)),
    ("DiskFull", re.compile(r"No space left|disk full|ENOSPC", re.I)),
    ("Segfault", re.compile(r"segfault|segmentation fault|SIGSEGV|core dumped", re.I)),
    ("NPU_DriverError", re.compile(r"drvDevice|drvAscend|soc version|driver.*npu|npu.*driver", re.I)),
]


def classify_error(error_text: str) -> str:
    """Classify error text into a category."""
    for category, pattern in _ERROR_CLASSIFY_PATTERNS:
        if pattern.search(error_text):
            return category
    return "UnknownError"


def compute_signature(error_text: str, stage: str = "") -> str:
    """Compute a stable error signature from error text.

    Normalizes paths, numbers, and timestamps before hashing so that the same
    error type produces the same signature across different runs.
    """
    normalized = error_text.lower()
    # Normalize paths
    normalized = re.sub(r"/[\w/.-]+/[\w.-]+\.py", "<path>/<file>.py", normalized)
    # Normalize numbers
    normalized = re.sub(r"\b\d+(?:\.\d+)?\b", "<N>", normalized)
    # Normalize timestamps
    normalized = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", "<timestamp>", normalized)
    # Normalize memory addresses
    normalized = re.sub(r"0x[0-9a-f]+", "<addr>", normalized)
    # Normalize job IDs
    normalized = re.sub(r"\b[a-z]+-[a-z0-9]+\b", "<jobid>", normalized)

    # Take first 1000 chars to bound length
    content = f"{stage}:{normalized[:1000]}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Memory store
# ---------------------------------------------------------------------------


class DebugMemoryStore:
    """Persistent store for debug records.

    Records are appended to a JSON-lines file.  Reads are cached in memory.
    """

    def __init__(self, workspace_dir: Path):
        self.workspace_dir = Path(workspace_dir)
        self.store_dir = self.workspace_dir / "debug_memory"
        self.store_path = self.store_dir / "records.jsonl"
        self._records: list[DebugRecord] = []
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self.store_dir.mkdir(parents=True, exist_ok=True)
        if self.store_path.exists():
            text = self.store_path.read_text(encoding="utf-8")
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    self._records.append(DebugRecord.from_dict(json.loads(line)))
                except (json.JSONDecodeError, TypeError):
                    continue
        self._loaded = True

    def save(self, record: DebugRecord) -> None:
        """Append a debug record to the store."""
        self._ensure_loaded()
        self._records.append(record)
        with open(self.store_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")

    def find_similar(
        self,
        error_text: str,
        stage: str = "",
        project_slug: str | None = None,
        limit: int = 5,
    ) -> list[DebugRecord]:
        """Find past debug records similar to the given error.

        Search order:
          1. Exact signature match (same project first)
          2. Same error type (same project first)
          3. Keyword overlap

        Returns up to `limit` records, sorted by recency (newest first).
        """
        self._ensure_loaded()
        error_type = classify_error(error_text)
        signature = compute_signature(error_text, stage)

        scored: list[tuple[float, DebugRecord]] = []

        for rec in self._records:
            score = 0.0
            # Exact signature match
            if rec.error_signature == signature:
                score += 100.0
            # Same error type
            if rec.error_type == error_type:
                score += 30.0
            # Same project
            if project_slug and rec.project_slug == project_slug:
                score += 20.0
            # Same stage
            if stage and rec.stage == stage:
                score += 10.0
            # Successful fix bonus
            if rec.success:
                score += 5.0

            if score > 0:
                scored.append((score, rec))

        scored.sort(key=lambda x: (-x[0], x[1].timestamp), reverse=True)
        return [r for _, r in scored[:limit]]

    def get_recent(self, project_slug: str | None = None, limit: int = 10) -> list[DebugRecord]:
        """Get most recent debug records, optionally filtered by project."""
        self._ensure_loaded()
        records = self._records
        if project_slug:
            records = [r for r in records if r.project_slug == project_slug]
        records.sort(key=lambda r: r.timestamp, reverse=True)
        return records[:limit]

    def stats(self, project_slug: str | None = None) -> dict[str, Any]:
        """Return statistics about debug records."""
        self._ensure_loaded()
        records = self._records
        if project_slug:
            records = [r for r in records if r.project_slug == project_slug]

        if not records:
            return {"total": 0}

        error_types: dict[str, int] = {}
        success_count = 0
        for r in records:
            error_types[r.error_type] = error_types.get(r.error_type, 0) + 1
            if r.success:
                success_count += 1

        return {
            "total": len(records),
            "successful": success_count,
            "failed": len(records) - success_count,
            "success_rate": round(success_count / len(records), 2) if records else 0,
            "top_error_types": sorted(error_types.items(), key=lambda x: -x[1])[:5],
            "avg_fix_rounds": round(
                sum(r.total_rounds_attempted for r in records) / len(records), 1
            ),
        }

    def format_context_for_prompt(
        self,
        error_text: str,
        stage: str = "",
        project_slug: str | None = None,
        limit: int = 3,
    ) -> str:
        """Format relevant past debug records as prompt context.

        Returns a string suitable for inclusion in a debug LLM prompt, or
        empty string if no relevant records found.
        """
        similar = self.find_similar(error_text, stage, project_slug, limit)
        if not similar:
            return ""

        lines = [
            "",
            "=== DEBUG MEMORY: 相关历史修复记录 ===",
            f"从 {len(self._records)} 条历史记录中找到 {len(similar)} 条相关记录:",
            "",
        ]
        for i, rec in enumerate(similar, 1):
            outcome = "成功" if rec.success else "未成功"
            lines.append(f"  [{i}] {rec.error_type} | {outcome} | {rec.timestamp[:10]}")
            if rec.root_cause:
                lines.append(f"      根因: {rec.root_cause}")
            if rec.fix_summary:
                lines.append(f"      修复: {rec.fix_summary}")
            if rec.files_modified:
                lines.append(f"      修改文件: {', '.join(rec.files_modified[:5])}")
            if rec.fix_round > 0:
                lines.append(f"      第 {rec.fix_round}/{rec.total_rounds_attempted} 轮修复成功")
            lines.append("")

        lines.append("请参考以上历史记录，避免重复无效修复。")
        lines.append("=== DEBUG MEMORY END ===")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Factory: create store for a project
# ---------------------------------------------------------------------------

def get_debug_memory(workspace_dir: Path | str) -> DebugMemoryStore:
    """Get the debug memory store for a project workspace."""
    return DebugMemoryStore(Path(workspace_dir))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="Debug memory management for SLAIResearch"
    )
    sub = parser.add_subparsers(dest="cmd")

    # stats
    sub.add_parser("stats", help="Show debug memory statistics").add_argument(
        "workspace", type=str, help="Path to workspace directory"
    )

    # list
    list_p = sub.add_parser("list", help="List recent debug records")
    list_p.add_argument("workspace", type=str)
    list_p.add_argument("--limit", type=int, default=10)

    # search
    search_p = sub.add_parser("search", help="Search similar past errors")
    search_p.add_argument("workspace", type=str)
    search_p.add_argument("error_text", type=str, help="Error text to search for")
    search_p.add_argument("--stage", type=str, default="experiment_execution")
    search_p.add_argument("--limit", type=int, default=5)

    args = parser.parse_args()

    if args.cmd == "stats":
        store = get_debug_memory(args.workspace)
        slug = Path(args.workspace).name
        stats = store.stats(slug)
        print(json.dumps(stats, indent=2, ensure_ascii=False))

    elif args.cmd == "list":
        store = get_debug_memory(args.workspace)
        slug = Path(args.workspace).name
        for rec in store.get_recent(slug, args.limit):
            outcome = "OK" if rec.success else "FAIL"
            print(f"[{rec.timestamp[:10]}] {rec.error_type:20s} {outcome:4s}  "
                  f"round={rec.fix_round}/{rec.total_rounds_attempted}  {rec.fix_summary[:80]}")

    elif args.cmd == "search":
        store = get_debug_memory(args.workspace)
        slug = Path(args.workspace).name
        similar = store.find_similar(args.error_text, args.stage, slug, args.limit)
        if not similar:
            print("No similar records found.")
        for rec in similar:
            outcome = "OK" if rec.success else "FAIL"
            print(f"[{rec.timestamp[:10]}] {rec.error_type:20s} {outcome:4s}  "
                  f"{rec.root_cause[:80] if rec.root_cause else '(no root cause)'}")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
