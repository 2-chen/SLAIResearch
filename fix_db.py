"""Known error-to-fix mapping for automatic experiment debug loops.

Maps error signatures (regex patterns) to concrete shell-script fixes.
Used by sco_runner.run_with_debug_loop() to auto-diagnose and fix
failed experiments without human intervention.

Add new FixAction entries to _builtin_fixes() when you discover a
recurring error pattern and its reliable fix.
"""

import re
import logging
from pathlib import Path
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class FixAction:
    """A known error pattern and its remediation.

    Attributes:
        name: Unique identifier (e.g. "opencv-typing-bug").
        error_patterns: Compiled regexes to match against error log text.
        fix_description: Human-readable for logging and debug records.
        fix_commands: Shell commands to inject into run_experiment.sh.
        insertion_point: Where to inject — one of:
            "after_phase0_deps"  — after the last pip install in Phase 0
            "before_phase0"      — at start of Phase 0
            "after_phase0"       — just before Phase 1 header
    """

    name: str
    error_patterns: list[re.Pattern]
    fix_description: str
    fix_commands: list[str]
    insertion_point: str = "after_phase0_deps"
    match_confidence: float = 0.95

    def __hash__(self) -> int:
        return hash(self.name)


# Sentinel comment injected alongside fix commands to guarantee idempotency.
_SENTINEL_PREFIX = "# [fix_db:"
_SENTINEL_SUFFIX = "]"


def _sentinel(name: str) -> str:
    return f"{_SENTINEL_PREFIX}{name}{_SENTINEL_SUFFIX}"


class FixDatabase:
    """Collection of known error patterns and their fixes."""

    def __init__(self) -> None:
        self._fixes: list[FixAction] = self._builtin_fixes()

    @staticmethod
    def _builtin_fixes() -> list[FixAction]:
        return [
            FixAction(
                name="opencv-typing-bug",
                error_patterns=[
                    re.compile(
                        r"AttributeError: module 'cv2\.dnn' has no attribute 'DictValue'",
                        re.IGNORECASE,
                    ),
                    re.compile(
                        r"AttributeError.*cv2.*typing",
                        re.IGNORECASE,
                    ),
                ],
                fix_description=(
                    "Remove cv2/typing stubs (incompatible with opencv-python-headless 4.11)"
                ),
                fix_commands=[
                    "rm -rf /usr/local/lib/python3.10/dist-packages/cv2/typing 2>/dev/null || true",
                    "rm -rf /usr/local/lib/python3.11/dist-packages/cv2/typing 2>/dev/null || true",
                    "rm -rf /usr/local/lib/python3.10/dist-packages/cv2/misc 2>/dev/null || true",
                    "rm -rf /usr/local/lib/python3.11/dist-packages/cv2/misc 2>/dev/null || true",
                ],
                insertion_point="after_phase0_deps",
                match_confidence=0.95,
            ),
        ]

    def match_fix(self, error_text: str) -> FixAction | None:
        """Return the best-matching FixAction for error_text, or None."""
        best: tuple[float, FixAction] | None = None

        for fix in self._fixes:
            matched = sum(1 for p in fix.error_patterns if p.search(error_text))
            if matched == 0:
                continue
            score = fix.match_confidence * (matched / len(fix.error_patterns))
            if best is None or score > best[0]:
                best = (score, fix)

        if best is not None:
            logger.info(
                "fix_db matched '%s' (score=%.2f, patterns=%d/%d)",
                best[1].name, best[0],
                sum(1 for p in best[1].error_patterns if p.search(error_text)),
                len(best[1].error_patterns),
            )
            return best[1]
        return None

    def register_fix(self, fix: FixAction) -> None:
        """Register a runtime-discovered fix."""
        self._fixes.append(fix)
        logger.info("Registered fix '%s'", fix.name)

    @staticmethod
    def apply_fix_to_script(script_path: Path, fix: FixAction) -> bool:
        """Inject fix commands into a run_experiment.sh script.

        Idempotent: checks for a sentinel comment before injecting.
        Returns True if the script was modified, False if the fix was
        already present or injection failed.
        """
        sentinel = _sentinel(fix.name)

        try:
            content = script_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("Cannot read %s: %s", script_path, e)
            return False

        if sentinel in content:
            logger.info("Fix '%s' already present in %s — skipping", fix.name, script_path.name)
            return False

        lines = content.splitlines(keepends=True)
        insertion_idx = _find_insertion_point(lines, fix.insertion_point)

        if insertion_idx is None:
            logger.warning(
                "Could not find insertion point '%s' in %s — appending fix at end",
                fix.insertion_point, script_path.name,
            )
            insertion_idx = len(lines)

        # Build injection block
        block = [f"{sentinel}\n"]
        for cmd in fix.fix_commands:
            block.append(f"{cmd}\n")
        block.append("\n")

        new_lines = lines[:insertion_idx] + block + lines[insertion_idx:]

        try:
            script_path.write_text("".join(new_lines), encoding="utf-8")
        except Exception as e:
            logger.warning("Cannot write %s: %s", script_path, e)
            return False

        logger.info(
            "Applied fix '%s' to %s (inserted %d lines at position %d)",
            fix.name, script_path.name, len(block), insertion_idx,
        )
        return True


# ---- Insertion-point locators ----

_PHASE0_DONE_PATTERNS = [
    re.compile(r'''echo\s+["']\[Phase 0\] Done\.["']'''),
    re.compile(r'''echo\s+\[Phase 0\]\s+Done\.'''),
]

_PIP_INSTALL_PATTERN = re.compile(r'^\s*pip\s+install\b')
_PHASE0_HEADER = re.compile(r'#\s*-+\s*Phase\s+0\b')
_PHASE1_HEADER = re.compile(r'#\s*-+\s*Phase\s+1\b')


def _find_insertion_point(lines: list[str], point: str) -> int | None:
    """Locate the insertion index for fix commands based on *point*.

    Returns a 0-based line index (insert BEFORE this line), or None.
    """
    if point == "after_phase0_deps":
        return _after_phase0_deps(lines)
    if point == "before_phase0":
        return _before_phase0(lines)
    if point == "after_phase0":
        return _after_phase0(lines)
    return None


def _after_phase0_deps(lines: list[str]) -> int | None:
    """Find insertion point after the last pip install in Phase 0.

    Priority:
      1. Line matching 'echo "[Phase 0] Done."' — insert BEFORE it
      2. Last line matching 'pip install' in Phase 0 section
      3. Line just before Phase 1 header
    """
    # Strategy 1: echo "[Phase 0] Done."
    for i, line in enumerate(lines):
        for pat in _PHASE0_DONE_PATTERNS:
            if pat.search(line):
                return i  # Insert before this line

    # Strategy 2: Find Phase 0 boundaries, then the last pip install within
    phase0_start = None
    phase0_end = None
    for i, line in enumerate(lines):
        if phase0_start is None and _PHASE0_HEADER.search(line):
            phase0_start = i
        if phase0_start is not None and phase0_end is None and _PHASE1_HEADER.search(line):
            phase0_end = i
            break

    if phase0_start is not None:
        search_end = phase0_end if phase0_end is not None else len(lines)
        last_pip = None
        for i in range(phase0_start, search_end):
            if _PIP_INSTALL_PATTERN.search(lines[i]):
                last_pip = i
        if last_pip is not None:
            return last_pip + 1  # Insert after the last pip install

    return None


def _before_phase0(lines: list[str]) -> int | None:
    """Find insertion point at the start of Phase 0."""
    for i, line in enumerate(lines):
        if _PHASE0_HEADER.search(line):
            return i + 1  # Insert after the header comment
    return None


def _after_phase0(lines: list[str]) -> int | None:
    """Find insertion point just before Phase 1."""
    for i, line in enumerate(lines):
        if _PHASE1_HEADER.search(line):
            return i  # Insert before Phase 1
    return None
