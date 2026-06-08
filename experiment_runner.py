"""
Experiment runner with preflight checks, resource scheduling, and debug memory.

This module wraps sco_runner.py with additional intelligence:
  1. Pre-flight validation before submission
  2. Resource-aware scheduling (CPU vs GPU heavy)
  3. Source-aware debugging (reads .py files, parses tracebacks)
  4. Persistent debug memory (learns from past fixes)

Usage as CLI:
  python experiment_runner.py preflight <experiment_dir>
  python experiment_runner.py schedule <experiment_dir> [--apply]
  python experiment_runner.py diagnose <workspace_dir> <log_file>
  python experiment_runner.py debug-context <workspace_dir> <log_file>
"""

from __future__ import annotations

import json
import os
import re
import sys
import traceback as tb_module
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Public API: preflight + schedule before execution
# ---------------------------------------------------------------------------


def run_preflight(experiment_dir: Path, project_slug: str = "") -> dict[str, Any]:
    """Run preflight checks and return JSON-serializable result.

    Returns:
        {"ok": bool, "report": str, "blocking_failures": int, "warnings": int}
    """
    from preflight import PreflightChecker

    checker = PreflightChecker(experiment_dir, project_slug)
    report = checker.run()

    return {
        "ok": report.ok,
        "report": report.format(),
        "blocking_failures": report._blocking_failures,
        "warnings": report._warnings,
    }


def run_schedule(experiment_dir: Path, apply: bool = False) -> dict[str, Any]:
    """Run resource scheduler and return recommendations.

    Returns:
        {"type": str, "jobs_per_gpu": int, "gpu_count": int, "env_vars": dict, "report": str}
    """
    from resource_scheduler import analyze_experiment, export_env_vars, print_recommendations
    import io

    profile = analyze_experiment(experiment_dir)

    # Capture the human-readable report
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    print_recommendations(profile, experiment_dir)
    sys.stdout = old_stdout

    if apply:
        from resource_scheduler import write_manifest
        write_manifest(experiment_dir.parent, profile)

    return {
        "type": profile.experiment_type,
        "confidence": profile.confidence,
        "cpu_score": profile.cpu_score,
        "gpu_score": profile.gpu_score,
        "jobs_per_gpu": profile.recommended_jobs_per_gpu,
        "gpu_count": profile.recommended_gpu_count,
        "batch_size_hint": profile.recommended_batch_size_hint,
        "env_vars": export_env_vars(profile),
        "report": buf.getvalue(),
    }


# ---------------------------------------------------------------------------
# Public API: enhanced diagnosis (reads source files + parses tracebacks)
# ---------------------------------------------------------------------------


def diagnose_failure(
    workspace_dir: Path,
    log_file: Path,
    project_slug: str = "",
    debug_round: int = 1,
) -> dict[str, Any]:
    """Diagnose an experiment failure with full context.

    Reads:
      - Error log (last 200 lines)
      - All .py files in experiment dir
      - Parsed traceback with file paths and line numbers
      - Relevant debug memory records

    Returns a structured dict ready for LLM prompt construction.
    """
    exp_dir = workspace_dir / "experiment"

    # 1. Read error log
    error_tail = ""
    error_key_lines = ""
    if log_file.exists():
        log_text = log_file.read_text(encoding="utf-8", errors="replace")
        lines = log_text.splitlines()
        error_tail = "\n".join(lines[-200:])
        error_key_lines = _extract_error_key_lines(error_tail)

    # 2. Parse traceback to find affected source files
    traceback_info = _parse_traceback(error_tail)

    # 3. Read affected source files (with context around error lines)
    source_contexts: list[dict[str, Any]] = []
    if traceback_info:
        for tb in traceback_info[:5]:  # top 5 traceback entries
            fpath = tb.get("file", "")
            lineno = tb.get("line", 0)
            if fpath and Path(fpath).exists():
                ctx = _read_file_context(Path(fpath), lineno, context_lines=20)
                if ctx:
                    source_contexts.append({
                        "file": fpath,
                        "line": lineno,
                        "function": tb.get("function", ""),
                        "code": tb.get("code", ""),
                        "context": ctx,
                    })

    # Also read all .py files (summary mode — first 5 lines + imports)
    all_py_files = _summarize_py_files(exp_dir)

    # 4. Retrieve debug memory
    debug_context = ""
    try:
        from debug_memory import get_debug_memory, classify_error
        store = get_debug_memory(workspace_dir)
        error_type = classify_error(error_tail)
        debug_context = store.format_context_for_prompt(
            error_tail,
            stage="experiment_execution",
            project_slug=project_slug,
            limit=3,
        )
    except Exception:
        error_type = "UnknownError"

    return {
        "error_type": error_type or "UnknownError",
        "error_tail": error_tail,
        "error_key_lines": error_key_lines,
        "traceback": traceback_info,
        "source_contexts": source_contexts,
        "all_py_files_summary": all_py_files,
        "debug_memory_context": debug_context,
    }


def build_debug_prompt(
    diagnosis: dict[str, Any],
    experiment_script: str,
    backend: str,
    debug_round: int,
    max_rounds: int = 20,
    round_history: str = "",
    protected_files: str = "",
) -> str:
    """Build a comprehensive debug prompt for the LLM.

    This prompt includes:
      - Error log
      - Parsed traceback with exact file:line locations
      - Source code context around error locations
      - Debug memory (relevant past fixes)
      - Full listing of Python files
      - Round history to avoid repeating fixes
    """
    lines = [
        "你是实验调试专家。实验执行失败了，请诊断并修复。",
        "",
        f"**后端**: {backend}",
        f"**修复轮次**: 第 {debug_round} 轮 / 最多 {max_rounds} 轮",
        f"**实验脚本**: {experiment_script}",
        "",
    ]

    # Error summary
    lines.append("## 错误日志 (最后 200 行)")
    lines.append("```")
    lines.append(diagnosis.get("error_tail", "(无日志)")[:8000])
    lines.append("```")
    lines.append("")

    # Key error lines
    key_lines = diagnosis.get("error_key_lines", "")
    if key_lines:
        lines.append("## 关键错误行")
        lines.append("```")
        lines.append(key_lines[:2000])
        lines.append("```")
        lines.append("")

    # Traceback
    tb = diagnosis.get("traceback", [])
    if tb:
        lines.append("## 解析的 Traceback (出错文件及行号)")
        for i, entry in enumerate(tb[:5], 1):
            lines.append(f"  {i}. {entry.get('file', '?')}:{entry.get('line', '?')} "
                         f"in {entry.get('function', '?')}")
            if entry.get("code"):
                lines.append(f"     {entry['code'].strip()}")
        lines.append("")

    # Source code context (the most important part!)
    source_ctxs = diagnosis.get("source_contexts", [])
    if source_ctxs:
        lines.append("## 出错位置的源代码上下文 (±20 行)")
        for ctx in source_ctxs[:3]:
            lines.append(f"### {ctx['file']}:{ctx['line']} in {ctx['function']}")
            lines.append("```python")
            lines.append(ctx.get("context", "(无法读取)")[:3000])
            lines.append("```")
            lines.append("")

    # All Python files summary
    py_summary = diagnosis.get("all_py_files_summary", "")
    if py_summary:
        lines.append("## 实验目录下的所有 Python 文件")
        lines.append("```")
        lines.append(py_summary[:3000])
        lines.append("```")
        lines.append("")

    # Debug memory
    debug_mem = diagnosis.get("debug_memory_context", "")
    if debug_mem:
        lines.append(debug_mem)
        lines.append("")

    # Round history
    if round_history:
        lines.append("## 之前轮次的修复历史 (避免重复)")
        lines.append(round_history)
        lines.append("")

    # Protected files
    if protected_files:
        lines.append("## 禁止修改的文件")
        lines.append(protected_files)
        lines.append("")

    # Task instructions
    lines.append("## 你的任务")
    lines.append("1. 仔细分析上面的错误日志和源代码，定位根因")
    lines.append("2. 参考「之前轮次的修复历史」和「DEBUG MEMORY」，避免重复无效修复")
    lines.append("3. 修改实验代码来修复问题")
    lines.append("4. 保存修改后的文件")
    lines.append("5. 报告 \"FIX_READY\" 表示已修复")
    lines.append("")
    lines.append("## 常见问题及修复")
    lines.append("- OOM / CUDA out of memory → 减小 batch_size, 减小模型, 加 gradient_accumulation")
    lines.append("- ModuleNotFoundError → 检查 import，确认包在容器镜像中")
    lines.append("- CUDA / driver 不兼容 → 调整 CUDA_VISIBLE_DEVICES 或 worker spec")
    lines.append("- 脚本超时 → 减小数据量或增加 checkpoint 续跑")
    lines.append("- 配额耗尽 → 等待或切换 quota_type")
    lines.append("- 容器启动失败 (零输出) → 检查 worker spec 格式")
    lines.append("- SyntaxError → 修正 Python 语法")
    lines.append("- FileNotFoundError → 修正路径或先下载数据")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API: save debug record after fix attempt
# ---------------------------------------------------------------------------


def save_debug_record(
    workspace_dir: Path,
    project_slug: str,
    error_log: str,
    root_cause: str,
    fix_summary: str,
    files_modified: list[str],
    success: bool,
    fix_round: int,
    total_rounds: int,
    backend: str = "sco",
    job_id: str = "",
) -> None:
    """Save a debug record to persistent memory."""
    try:
        from debug_memory import (
            DebugRecord, DebugMemoryStore, classify_error, compute_signature,
        )
        store = DebugMemoryStore(Path(workspace_dir))
        record = DebugRecord(
            error_signature=compute_signature(error_log, "experiment_execution"),
            project_slug=project_slug,
            stage="experiment_execution",
            error_type=classify_error(error_log),
            error_message=error_log[:500],
            error_key_lines=_extract_error_key_lines(error_log).splitlines(),
            root_cause=root_cause,
            fix_summary=fix_summary,
            files_modified=files_modified,
            fix_round=fix_round,
            total_rounds_attempted=total_rounds,
            success=success,
            job_id=job_id,
            backend=backend,
        )
        store.save(record)
    except Exception:
        pass  # debug memory is best-effort, never block the pipeline


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_error_key_lines(log_text: str) -> str:
    """Extract key error lines from log output."""
    pattern = re.compile(
        r"error|fail|exception|traceback|killed|oom|cuda|abort|segfault|"
        r"module.*not found|no module|import error|command not found|"
        r"no such file|permission denied|cannot find|could not find|"
        r"timed out|connection refused|quota.*exhaust|forbid",
        re.IGNORECASE,
    )
    key_lines = []
    for line in log_text.splitlines():
        if pattern.search(line):
            key_lines.append(line.strip()[:200])
    return "\n".join(key_lines[-30:])


def _parse_traceback(log_text: str) -> list[dict[str, Any]]:
    """Parse Python traceback from log output.

    Returns list of {file, line, function, code} dicts.
    """
    # Match: File "path", line N, in function_name
    #          code_line
    tb_pattern = re.compile(
        r'File\s+"([^"]+)",\s*line\s+(\d+),\s*in\s+(\S+)\s*\n\s*(.*)',
    )
    entries: list[dict[str, Any]] = []
    for m in tb_pattern.finditer(log_text):
        entries.append({
            "file": m.group(1),
            "line": int(m.group(2)),
            "function": m.group(3),
            "code": m.group(4).strip(),
        })
    return entries


def _read_file_context(filepath: Path, error_lineno: int, context_lines: int = 20) -> str:
    """Read context around an error line from a source file."""
    try:
        all_lines = filepath.read_text(encoding="utf-8").splitlines()
    except Exception:
        return ""

    start = max(0, error_lineno - context_lines - 1)
    end = min(len(all_lines), error_lineno + context_lines)
    out_lines = []
    for i in range(start, end):
        marker = ">>>" if i == error_lineno - 1 else "   "
        out_lines.append(f"{marker} {i+1:4d}: {all_lines[i]}")
    return "\n".join(out_lines)


def _summarize_py_files(experiment_dir: Path) -> str:
    """Generate a summary of all Python files in the experiment directory."""
    if not experiment_dir.exists():
        return "(experiment directory not found)"

    py_files = sorted(experiment_dir.rglob("*.py"))
    if not py_files:
        return "(no Python files found)"

    lines_out = []
    for pf in py_files[:30]:  # limit to 30 files
        try:
            content = pf.read_text(encoding="utf-8")
            line_count = len(content.splitlines())
            # Extract imports
            imports = []
            for line in content.splitlines()[:30]:
                stripped = line.strip()
                if stripped.startswith("import ") or stripped.startswith("from "):
                    imports.append(stripped[:100])
            relpath = pf.relative_to(experiment_dir.parent)
            lines_out.append(f"{relpath} ({line_count} lines)")
            for imp in imports[:5]:
                lines_out.append(f"    {imp}")
        except Exception:
            lines_out.append(f"{pf.name} (unreadable)")

    return "\n".join(lines_out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="Experiment runner with preflight, scheduling, and debug intelligence"
    )
    sub = parser.add_subparsers(dest="cmd")

    # preflight
    pf = sub.add_parser("preflight", help="Run pre-flight checks")
    pf.add_argument("experiment_dir", type=str)
    pf.add_argument("--json", action="store_true", dest="json_out")

    # schedule
    sch = sub.add_parser("schedule", help="Analyze resource requirements")
    sch.add_argument("experiment_dir", type=str)
    sch.add_argument("--apply", action="store_true")
    sch.add_argument("--env", action="store_true", dest="env_only")

    # diagnose
    diag = sub.add_parser("diagnose", help="Diagnose experiment failure")
    diag.add_argument("workspace_dir", type=str)
    diag.add_argument("log_file", type=str)
    diag.add_argument("--slug", type=str, default="")
    diag.add_argument("--round", type=int, default=1, dest="debug_round")
    diag.add_argument("--json", action="store_true", dest="json_out")

    # build-prompt
    bp = sub.add_parser("build-prompt", help="Build debug prompt for LLM")
    bp.add_argument("workspace_dir", type=str)
    bp.add_argument("log_file", type=str)
    bp.add_argument("--script", type=str, default="")
    bp.add_argument("--backend", type=str, default="sco")
    bp.add_argument("--round", type=int, default=1, dest="debug_round")
    bp.add_argument("--max-rounds", type=int, default=20)
    bp.add_argument("--history", type=str, default="")
    bp.add_argument("--protected", type=str, default="")

    # save-record
    sr = sub.add_parser("save-record", help="Save debug record to memory")
    sr.add_argument("workspace_dir", type=str)
    sr.add_argument("--slug", type=str, default="")
    sr.add_argument("--error-log", type=str, default="")
    sr.add_argument("--root-cause", type=str, default="")
    sr.add_argument("--fix-summary", type=str, default="")
    sr.add_argument("--files-modified", type=str, default="")
    sr.add_argument("--success", type=str, default="false")
    sr.add_argument("--fix-round", type=int, default=1)
    sr.add_argument("--total-rounds", type=int, default=1)
    sr.add_argument("--backend", type=str, default="sco")
    sr.add_argument("--job-id", type=str, default="")

    # stats
    st = sub.add_parser("stats", help="Show debug memory statistics")
    st.add_argument("workspace_dir", type=str)

    args = parser.parse_args()

    if args.cmd == "preflight":
        result = run_preflight(Path(args.experiment_dir))
        if args.json_out:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(result["report"])
        sys.exit(0 if result["ok"] else 1)

    elif args.cmd == "schedule":
        if args.env_only:
            from resource_scheduler import analyze_experiment, export_env_vars
            profile = analyze_experiment(Path(args.experiment_dir))
            for k, v in export_env_vars(profile).items():
                print(f"export {k}={v}")
        else:
            result = run_schedule(Path(args.experiment_dir), apply=args.apply)
            print(result["report"])

    elif args.cmd == "diagnose":
        result = diagnose_failure(
            Path(args.workspace_dir),
            Path(args.log_file),
            project_slug=args.slug,
            debug_round=args.debug_round,
        )
        if args.json_out:
            # Strip large fields for JSON output
            slim = {k: v for k, v in result.items()
                    if k not in ("error_tail", "all_py_files_summary")}
            print(json.dumps(slim, indent=2, ensure_ascii=False))
        else:
            print(f"Error type: {result['error_type']}")
            print(f"Traceback entries: {len(result['traceback'])}")
            print(f"Source contexts: {len(result['source_contexts'])}")
            if result["source_contexts"]:
                for ctx in result["source_contexts"][:3]:
                    print(f"\n--- {ctx['file']}:{ctx['line']} in {ctx['function']} ---")
                    print(ctx["context"][:2000])
            if result["debug_memory_context"]:
                print(result["debug_memory_context"])

    elif args.cmd == "build-prompt":
        workspace_dir = Path(args.workspace_dir)
        log_file = Path(args.log_file)
        script = args.script or str(workspace_dir / "experiment" / "run_experiment.sh")
        diagnosis = diagnose_failure(
            workspace_dir, log_file,
            project_slug=workspace_dir.name,
            debug_round=args.debug_round,
        )
        prompt = build_debug_prompt(
            diagnosis,
            experiment_script=script,
            backend=args.backend,
            debug_round=args.debug_round,
            max_rounds=args.max_rounds,
            round_history=args.history,
            protected_files=args.protected,
        )
        print(prompt)

    elif args.cmd == "save-record":
        save_debug_record(
            workspace_dir=Path(args.workspace_dir),
            project_slug=args.slug,
            error_log=args.error_log,
            root_cause=args.root_cause,
            fix_summary=args.fix_summary,
            files_modified=[f.strip() for f in args.files_modified.split(",") if f.strip()],
            success=args.success.lower() == "true",
            fix_round=args.fix_round,
            total_rounds=args.total_rounds,
            backend=args.backend,
            job_id=args.job_id,
        )
        print("Debug record saved.")

    elif args.cmd == "stats":
        from debug_memory import get_debug_memory
        store = get_debug_memory(Path(args.workspace_dir))
        slug = Path(args.workspace_dir).name
        stats = store.stats(slug)
        print(json.dumps(stats, indent=2, ensure_ascii=False))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
