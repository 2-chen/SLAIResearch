"""
SCO CLI wrapper + local execution for SLAIResearch experiment execution.
Imports defaults from config.py; everything overridable via env vars.

Execution strategy (local-first):
  1. Detect local GPU. If available → run locally with retries.
  2. If no local GPU:
     a. Heuristic check: does the experiment NEED a GPU?
     b. If GPU is needed → fall back to SCO cloud.
     c. If not GPU-dependent → run locally anyway.
  3. Local failure after max retries → SCO fallback (if GPU-relevant).
"""

import subprocess
import time
import json
import logging
import shutil
import os
import re
import sys
import threading
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

from config import (
    SCO_WORKSPACE, SCO_AEC2, SCO_IMAGE,
    SCO_WORKER_SPEC, SCO_STORAGE_MOUNT, SCO_WORKER_NODES,
    SCO_QUOTA_TYPE, SCO_PRIORITY,
    SCO_WORKER_SPEC_MAP, DEFAULT_GPU_COUNT,
    MAX_COMPUTE_BUDGET_GPU_HOURS,
    DOWNLOAD_CACHE_DIR, DOWNLOAD_WHEELS_DIR, DOWNLOAD_DATASETS_DIR,
)
from fix_db import FixDatabase

_PROJECT_ROOT = Path(__file__).resolve().parent

logger = logging.getLogger(__name__)

JOB_STATE_TERMINAL = {"SUCCEEDED", "FAILED", "STOPPED", "CANCELLED", "SUSPENDED"}

# ---------------------------------------------------------------------------
# GPU detection
# ---------------------------------------------------------------------------

def detect_gpu() -> dict:
    """Detect locally available GPUs. Returns dict with count, names, memory."""
    info: dict[str, Any] = {"available": False, "count": 0, "devices": []}

    # Try nvidia-smi
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            info["available"] = True
            for line in result.stdout.strip().split("\n"):
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 2:
                    info["devices"].append({"name": parts[0], "memory": parts[1]})
            info["count"] = len(info["devices"])
            logger.info("GPU detected: %d device(s) — %s", info["count"],
                         ", ".join(d["name"] for d in info["devices"]))
            return info
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback: check CUDA_VISIBLE_DEVICES
    cvd = os.environ.get("CUDA_VISIBLE_DEVICES", "")
    if cvd and cvd.strip():
        logger.info("CUDA_VISIBLE_DEVICES=%s (no nvidia-smi, but env set)", cvd)
        info["available"] = True
        info["count"] = len([x for x in cvd.split(",") if x.strip()])
        return info

    # Fallback: check for torch with CUDA
    try:
        result = subprocess.run(
            [sys.executable, "-c",
             "import torch; print(torch.cuda.device_count())"],
            capture_output=True, text=True, timeout=15,
        )
        count = int(result.stdout.strip())
        if count > 0:
            info["available"] = True
            info["count"] = count
            logger.info("GPU detected via torch: %d device(s)", count)
            return info
    except Exception:
        pass

    logger.info("No local GPU detected")
    return info


def needs_gpu_heuristic(script_path: Path, experiment_dir: Path | None = None) -> bool:
    """Heuristic: does this experiment script likely need a GPU?

    If experiment_dir is given, checks experiment_manifest.json first.
    Falls back to scanning the script for GPU-related keywords.
    """
    # Check manifest first if available
    exp_dir = experiment_dir or script_path.parent
    manifest = exp_dir / "experiment_manifest.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text())
            gpu_count = int(data.get("gpu_count", 0))
            if gpu_count > 0:
                logger.info("Manifest declares %d GPU(s) -> experiment needs GPU", gpu_count)
                return True
        except Exception:
            pass

    gpu_keywords = [
        r'\.cuda\(', r'\.to\(.*device', r'device.*cuda',
        r'torch\.cuda', r'CUDA', r'gpu',
        r'GPUS', r'n_gpu', r'num_gpus',
        r'DataParallel', r'DistributedDataParallel',
        r'deepspeed', r'FSDP', r'flash_attn',
        r'bf16', r'fp16.*training', r'amp',
    ]
    try:
        text = script_path.read_text()
        # Also read .py files in same directory
        for py_file in sorted(script_path.parent.glob("*.py")):
            try:
                text += "\n" + py_file.read_text()
            except Exception:
                pass

        for kw in gpu_keywords:
            if re.search(kw, text, re.IGNORECASE):
                logger.info("Heuristic: experiment likely needs GPU (matched '%s')", kw)
                return True
    except Exception:
        pass

    logger.info("Heuristic: experiment appears CPU-compatible")
    return False


# ---------------------------------------------------------------------------
# GPU requirement parsing + worker spec resolution
# ---------------------------------------------------------------------------


def parse_gpu_requirement(experiment_dir: str | Path) -> int | None:
    """Parse GPU requirement from experiment_manifest.json.

    Returns GPU count (>= 1) if manifest exists and is valid,
    or None if no manifest is found (caller should use default).
    """
    manifest_path = Path(experiment_dir) / "experiment_manifest.json"
    if not manifest_path.exists():
        logger.warning("=" * 60)
        logger.warning("MISSING experiment_manifest.json at %s", manifest_path)
        logger.warning("Falling back to DEFAULT_GPU_COUNT=%d. This may UNDERUTILIZE SCO resources.", DEFAULT_GPU_COUNT)
        logger.warning("Fix: create experiment_manifest.json with {\"gpu_count\": 4}")
        logger.warning("=" * 60)
        return None

    try:
        data = json.loads(manifest_path.read_text())
        raw = data.get("gpu_count", None)
        if raw is None:
            logger.warning("experiment_manifest.json missing 'gpu_count' key")
            return None
        gpu_count = int(raw)
        if gpu_count < 1:
            logger.warning("Invalid gpu_count=%d in manifest, clamping to 1", gpu_count)
            gpu_count = 1
        logger.info("GPU requirement from manifest: %d GPU(s)", gpu_count)
        return gpu_count
    except (json.JSONDecodeError, ValueError, OSError) as e:
        logger.warning("Failed to parse %s: %s", manifest_path, e)
        return None


def _gpu_count_from_spec(spec: str) -> int:
    """Extract the GPU count encoded in a worker spec string.

    Spec format: {machine}.{type}.{variant}.{gpus}.{cpu}c{ram}g
    Example: n6ls.iu.i40.2.16c256g → 2
    """
    m = re.search(r'\.(\d+)\.\d+c\d+g$', spec)
    if m:
        return int(m.group(1))
    logger.warning("Cannot parse GPU count from spec: %s — assuming 1", spec)
    return 1


# Valid spec format: {machine}.{type}.{variant}.{gpus}.{cpu}c{ram}g
_VALID_SPEC_RE = re.compile(r'\.(\d+)\.\d+c\d+g$')


def _validate_worker_spec(spec: str, source: str = "") -> None:
    """Validate worker spec format. Raises RuntimeError on invalid format.

    Valid format: {machine}.{type}.{variant}.{gpus}.{cpu}c{ram}g
    Example: n6ls.iu.i40.1.8c128g

    Truncated specs (e.g. n6ls.iu.i40.1 — missing .XcYg suffix) cause
    silent SCO container failures with zero output.  This validator catches
    them before submission so the error message is clear and actionable.
    """
    if not _VALID_SPEC_RE.search(spec):
        label = f" ({source})" if source else ""
        raise RuntimeError(
            f"Invalid SCO worker spec format{label}: '{spec}'. "
            f"Expected format: {{machine}}.{{type}}.{{variant}}.{{gpus}}.{{cpu}}c{{ram}}g. "
            f"Examples: n6ls.iu.i40.1.8c128g, n6ls.iu.i40.2.16c256g, n6ls.iu.i40.4.32c512g. "
            f"Check SCO_WORKER_SPEC or SCO_WORKER_SPEC_MAP in config.py."
        )


def resolve_worker_spec(gpu_count: int) -> str:
    """Map a GPU count to the best matching SCO worker spec.

    Strategy:
      1. Exact match in SCO_WORKER_SPEC_MAP.
      2. Smallest spec with >= gpu_count GPUs.
      3. Largest available spec if request exceeds all.
      4. Fallback to SCO_WORKER_SPEC.

    The returned spec is validated to ensure correct format.
    """
    resolved: str | None = None

    if not SCO_WORKER_SPEC_MAP:
        resolved = SCO_WORKER_SPEC
    elif gpu_count in SCO_WORKER_SPEC_MAP:
        spec = SCO_WORKER_SPEC_MAP[gpu_count]
        spec_gpus = _gpu_count_from_spec(spec)
        if spec_gpus <= gpu_count:
            logger.info("Resolved %d GPU(s) -> exact match: %s (%d GPUs in spec)",
                        gpu_count, spec, spec_gpus)
            resolved = spec
        else:
            logger.warning("Exact-match spec %s encodes %d GPUs > requested %d — will search alternatives",
                           spec, spec_gpus, gpu_count)

    if resolved is None:
        available = sorted(SCO_WORKER_SPEC_MAP.keys())
        for count in available:
            if count >= gpu_count:
                spec = SCO_WORKER_SPEC_MAP[count]
                spec_gpus = _gpu_count_from_spec(spec)
                if spec_gpus <= gpu_count:
                    logger.info("Resolved %d GPU(s) -> next available slot %d: %s (%d GPUs in spec)",
                                gpu_count, count, spec, spec_gpus)
                    resolved = spec
                    break

    if resolved is None:
        available = sorted(SCO_WORKER_SPEC_MAP.keys())
        for count in reversed(available):
            spec = SCO_WORKER_SPEC_MAP[count]
            spec_gpus = _gpu_count_from_spec(spec)
            if spec_gpus <= gpu_count:
                logger.info("Resolved %d GPU(s) -> fallback slot %d: %s (%d GPUs in spec)",
                            gpu_count, count, spec, spec_gpus)
                resolved = spec
                break

    if resolved is None:
        resolved = SCO_WORKER_SPEC_MAP.get(1, SCO_WORKER_SPEC)
        logger.warning("No spec fits %d GPU(s), using fallback: %s", gpu_count, resolved)

    _validate_worker_spec(resolved, f"resolve_worker_spec(gpu_count={gpu_count})")
    return resolved


def _auto_detect_config(script_path: str | Path) -> tuple[dict[str, str], "SCOConfig"]:
    """Auto-detect GPU requirements from experiment manifest.

    Reads experiment_manifest.json in the script's parent directory.
    Returns (extra_env, sco_config) — always non-None.
    extra_env includes GPU_COUNT. sco_config has the resolved worker_spec.
    """
    exp_dir = Path(script_path).parent
    gpu_count = parse_gpu_requirement(exp_dir) or DEFAULT_GPU_COUNT
    resolved_spec = resolve_worker_spec(gpu_count)
    # Resolved spec may encode more GPUs than requested (e.g. 1 → 2
    # because SCO has no 1-GPU machine type).  Propagate the actual count
    # so the experiment script parallelizes correctly.
    actual_gpu_count = _gpu_count_from_spec(resolved_spec)
    extra_env = {
        "GPU_COUNT": str(actual_gpu_count),
        "CHENRESEARCH": "1",
    }
    sco_config = SCOConfig(worker_spec=resolved_spec)
    logger.info("Auto-detected config: manifest_gpu=%d actual_gpu=%d worker_spec=%s",
                gpu_count, actual_gpu_count, resolved_spec)
    return extra_env, sco_config


# ---------------------------------------------------------------------------
# Pre-submission checks (GPU utilization + compute budget)
# ---------------------------------------------------------------------------

# Inline GPU-related keyword lists for heuristic checks.  These are used
# both by needs_gpu_heuristic() and check_gpu_utilization().
_GPU_TRAINING_KEYWORDS = [
    r'nn\.DataParallel\(', r'DistributedDataParallel',
    r'torch\.cuda\.device_count\(\)',
    r'multigpu', r'multi_gpu', r'world_size', r'local_rank',
    r'torchrun', r'torch\.distributed', r'deepspeed', r'FSDP',
]
_GPU_INFERENCE_KEYWORDS = [
    r'multiprocessing.*spawn', r'multiprocessing.*Pool',
    r'CUDA_VISIBLE_DEVICES', r'device.*cuda:\d+',
    r'gpu_id', r'per_gpu',
]


def check_gpu_utilization(experiment_dir: str | Path, gpu_count: int) -> dict:
    """Static analysis: does this experiment actually use multiple GPUs?

    Scans Python source and shell scripts under *experiment_dir* for
    multi-GPU patterns (DataParallel, DDP, multiprocessing, background
    processes with wait).  Returns a dict with:
        score:   "good" | "partial" | "poor"
        issues:  list of concrete problems found
    """
    exp_dir = Path(experiment_dir)
    issues: list[str] = []

    if gpu_count <= 1:
        return {"score": "good", "issues": []}

    # ── Collect all source text ──
    py_text = ""
    sh_text = ""
    for f in sorted(exp_dir.rglob("*.py")):
        try:
            py_text += "\n" + f.read_text()
        except Exception:
            pass
    for f in sorted(exp_dir.glob("*.sh")):
        try:
            sh_text += "\n" + f.read_text()
        except Exception:
            pass

    # ── Check Python patterns ──
    has_training_parallel = any(
        re.search(kw, py_text, re.IGNORECASE) for kw in _GPU_TRAINING_KEYWORDS
    )
    has_inference_parallel = any(
        re.search(kw, py_text, re.IGNORECASE) for kw in _GPU_INFERENCE_KEYWORDS
    )

    # ── Check shell patterns ──
    has_bg_processes = bool(
        re.search(r'&\s*$', sh_text, re.MULTILINE) and 'wait' in sh_text
    )
    # Multiple CUDA_VISIBLE_DEVICES assignments (not just a single export at the top)
    has_multi_gpu_shell = len(re.findall(r'CUDA_VISIBLE_DEVICES=', sh_text)) >= 2
    # Script reads $GPU_COUNT / ${GPU_COUNT} to dynamically adapt to available GPUs
    reads_gpu_count_env = bool(re.search(r'\$\{?GPU_COUNT\}?', sh_text))

    # ── Detect serial training anti-patterns ──
    # Sequential for-loop over sigma/baseline training without parallel
    has_serial_training = False
    serial_train_patterns = [
        (r'for\s+sigma\s+in', 'serial sigma training loop'),
        (r'for\s+.*\s+in\s+.*baseline', 'serial baseline loop'),
    ]
    for pat, desc in serial_train_patterns:
        if re.search(pat, py_text, re.IGNORECASE):
            if not has_training_parallel and not has_bg_processes:
                has_serial_training = True
                issues.append(
                    f"Training appears to use a {desc} — all baselines will "
                    f"run sequentially on a single GPU. With {gpu_count} GPUs, "
                    f"each baseline should be dispatched to a different GPU in parallel."
                )
                break  # one is enough

    # ── Score ──
    # Priority: serial training without parallelism is always poor utilization
    if has_serial_training:
        score = "poor"
    elif has_training_parallel:
        score = "good"
    elif reads_gpu_count_env and has_bg_processes and has_multi_gpu_shell:
        # Shell-level multi-GPU orchestration: script reads GPU_COUNT,
        # sets CUDA_VISIBLE_DEVICES per process, and runs jobs in parallel.
        # This is a fully valid multi-GPU pattern (e.g. run_experiment.sh).
        score = "good"
    elif has_inference_parallel and has_bg_processes:
        score = "partial"
    elif has_inference_parallel or has_bg_processes or has_multi_gpu_shell:
        score = "partial"
        if not reads_gpu_count_env and gpu_count >= 2:
            issues.append(
                f"Shell uses multi-GPU patterns but does NOT read \$GPU_COUNT. "
                f"GPU_COUNT={gpu_count} will be ignored — experiment may use only 1 GPU."
            )
    else:
        score = "poor"
        if not issues:
            issues.append(
                f"No multi-GPU patterns detected (DataParallel, DDP, multiprocessing, "
                f"background jobs) but gpu_count={gpu_count}. "
                f"Experiment will use only 1 of {gpu_count} allocated GPUs."
            )

    return {"score": score, "issues": issues}


def check_compute_budget(experiment_dir: str | Path, gpu_count: int) -> dict:
    """Check whether estimated GPU-hours exceed the per-task budget.

    Reads *estimated_runtime_hours* (or *estimated_duration_minutes*) from
    experiment_manifest.json.  If the estimate is missing the check is
    skipped (warning, not error).

    Returns:
        ok:                  bool
        estimated_gpu_hours: float | None
        max_allowed:         int
        message:             human-readable result
    """
    exp_dir = Path(experiment_dir)
    manifest = exp_dir / "experiment_manifest.json"
    estimated_hours: float | None = None

    if manifest.exists():
        try:
            data = json.loads(manifest.read_text())
            estimated_hours = data.get("estimated_runtime_hours")
            if estimated_hours is None:
                minutes = data.get("estimated_duration_minutes")
                if minutes is not None:
                    estimated_hours = float(minutes) / 60.0
        except Exception:
            pass

    if estimated_hours is None:
        return {
            "ok": True,
            "estimated_gpu_hours": None,
            "max_allowed": MAX_COMPUTE_BUDGET_GPU_HOURS,
            "message": (
                "No estimated_runtime_hours in experiment_manifest.json — "
                "skipping compute budget check"
            ),
        }

    gpu_hours = gpu_count * float(estimated_hours)

    if gpu_hours > MAX_COMPUTE_BUDGET_GPU_HOURS:
        return {
            "ok": False,
            "estimated_gpu_hours": gpu_hours,
            "max_allowed": MAX_COMPUTE_BUDGET_GPU_HOURS,
            "message": (
                f"Estimated {gpu_hours:.1f} GPU-hours exceeds "
                f"max {MAX_COMPUTE_BUDGET_GPU_HOURS} GPU-hours "
                f"({gpu_count} GPUs × {estimated_hours:.1f}h). "
                f"Reduce experiment scope or add parallelism."
            ),
        }

    return {
        "ok": True,
        "estimated_gpu_hours": gpu_hours,
        "max_allowed": MAX_COMPUTE_BUDGET_GPU_HOURS,
        "message": (
            f"Budget OK: {gpu_hours:.1f} / {MAX_COMPUTE_BUDGET_GPU_HOURS} GPU-hours "
            f"({gpu_count} GPUs × {estimated_hours:.1f}h)"
        ),
    }


def _pre_submit_check(experiment_dir: str | Path, gpu_count: int) -> None:
    """Run all pre-submission checks.  Raises RuntimeError on hard failures,
    logs warnings for soft issues."""
    exp_dir = Path(experiment_dir)

    # 1. GPU utilization check
    util = check_gpu_utilization(exp_dir, gpu_count)
    if util["score"] == "poor":
        if gpu_count >= 3:
            # Hard block: 3+ GPUs requested but zero multi-GPU awareness detected.
            # This would waste expensive GPU resources with no benefit.
            msg = (
                f"GPU utilization check FAILED: {gpu_count} GPUs requested but no "
                f"multi-GPU patterns detected (no DataParallel/DDP, no GPU_COUNT "
                f"reading, no CUDA_VISIBLE_DEVICES distribution). "
                f"Experiment will only use 1 of {gpu_count} GPUs. "
                f"Fix: either set gpu_count=1 in experiment_manifest.json, or add "
                f"GPU_COUNT-based parallelism to run_experiment.sh."
            )
            logger.error("=" * 60)
            logger.error(msg)
            for issue in util["issues"]:
                logger.error("  ✗ %s", issue)
            logger.error("=" * 60)
            raise RuntimeError(msg)
        logger.warning("=" * 60)
        logger.warning("GPU UTILIZATION CHECK: POOR")
        for issue in util["issues"]:
            logger.warning("  ✗ %s", issue)
        logger.warning("Experiment will UNDERUTILIZE %d allocated GPU(s).", gpu_count)
        logger.warning("=" * 60)
    elif util["score"] == "partial":
        logger.warning("GPU UTILIZATION CHECK: PARTIAL")
        for issue in util["issues"]:
            logger.warning("  ⚠ %s", issue)

    # 2. Compute budget check (hard — block if exceeded)
    budget = check_compute_budget(exp_dir, gpu_count)
    if not budget["ok"]:
        logger.error("=" * 60)
        logger.error("COMPUTE BUDGET EXCEEDED")
        logger.error(budget["message"])
        logger.error("Reduce estimated_runtime_hours or split into smaller tasks.")
        logger.error("Set SLAIRESEARCH_MAX_GPU_HOURS to override (not recommended).")
        logger.error("=" * 60)
        raise RuntimeError(budget["message"])
    if budget["estimated_gpu_hours"] is not None:
        logger.info("Compute budget: %s", budget["message"])
    else:
        logger.warning("Compute budget: %s", budget["message"])


# ---------------------------------------------------------------------------
# Local execution
# ---------------------------------------------------------------------------

@dataclass
class LocalResult:
    success: bool
    exit_code: int
    log_path: Path
    stdout: str = ""
    stderr: str = ""
    attempts: int = 0
    error_summary: str = ""


def run_local_experiment(
    script_path: Path,
    work_dir: Path | None = None,
    log_dir: Path | None = None,
    timeout: int = 7200,
    max_retries: int = 3,
    extra_env: dict[str, str] | None = None,
) -> LocalResult:
    """Run an experiment script locally with retry on failure.

    Returns LocalResult with success status and log paths.
    On failure, the caller can inspect logs and optionally fall back to SCO.
    """
    script_path = Path(script_path).resolve()
    if work_dir is None:
        work_dir = script_path.parent
    work_dir = Path(work_dir)
    log_dir = Path(log_dir) if log_dir else work_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    # Ensure local execution marker
    env["SLAIRESEARCH_LOCAL"] = "1"

    for attempt in range(1, max_retries + 1):
        log_path = log_dir / f"local_run_{attempt:02d}.log"
        logger.info("Local execution attempt %d/%d → %s", attempt, max_retries, log_path)

        try:
            result = subprocess.run(
                ["bash", str(script_path)],
                cwd=str(work_dir),
                capture_output=True, text=True,
                timeout=timeout,
                env=env,
            )
            # Write logs
            with open(log_path, "w") as f:
                f.write(f"=== STDOUT (attempt {attempt}/{max_retries}) ===\n")
                f.write(result.stdout)
                f.write(f"\n=== STDERR ===\n")
                f.write(result.stderr)
                f.write(f"\n=== EXIT CODE: {result.returncode} ===\n")

            if result.returncode == 0:
                logger.info("Local experiment SUCCEEDED (attempt %d)", attempt)
                return LocalResult(
                    success=True, exit_code=0, log_path=log_path,
                    stdout=result.stdout, stderr=result.stderr, attempts=attempt,
                )

            # Non-zero exit → analyze and maybe retry
            error_tail = (result.stderr or result.stdout)[-2000:]
            logger.warning("Local experiment FAILED (attempt %d, exit=%d)", attempt, result.returncode)

            if attempt < max_retries:
                logger.info("Retrying local execution in 10s ...")
                time.sleep(10)

        except subprocess.TimeoutExpired:
            logger.warning("Local experiment TIMED OUT after %ds (attempt %d)", timeout, attempt)
            with open(log_path, "w") as f:
                f.write(f"TIMEOUT after {timeout}s (attempt {attempt})\n")
            if attempt >= max_retries:
                return LocalResult(
                    success=False, exit_code=-1, log_path=log_path,
                    error_summary=f"Timeout after {timeout}s × {max_retries} attempts",
                    attempts=attempt,
                )
            time.sleep(10)

        except Exception as e:
            logger.error("Local execution exception (attempt %d): %s", attempt, e)
            with open(log_path, "w") as f:
                f.write(f"EXCEPTION: {e}\n")
            if attempt >= max_retries:
                return LocalResult(
                    success=False, exit_code=-2, log_path=log_path,
                    error_summary=str(e), attempts=attempt,
                )
            time.sleep(10)

    # Exhausted retries
    last_log = log_dir / f"local_run_{max_retries:02d}.log"
    return LocalResult(
        success=False, exit_code=-3, log_path=last_log,
        error_summary=f"Failed after {max_retries} local attempts",
        attempts=max_retries,
    )


# ---------------------------------------------------------------------------
# Unified experiment runner (local-first)
# ---------------------------------------------------------------------------

@dataclass
class ExperimentResult:
    """Unified result from either local or SCO execution."""
    backend: str                     # "local" or "sco"
    success: bool
    job_id: str = ""                 # SCO job_id (empty if local)
    log_path: str = ""               # path to main log file
    attempts: int = 0
    error_summary: str = ""


def run_experiment(
    script_path: str | Path,
    job_name: str = "slairesearch",
    work_dir: str | Path | None = None,
    log_dir: str | Path | None = None,
    local_timeout: int = 7200,
    max_local_retries: int = 20,
    extra_env: dict[str, str] | None = None,
    sco_config: "SCOConfig | None" = None,
    force_sco: bool = False,
    force_local: bool = False,
) -> ExperimentResult:
    """Run an experiment with local-first strategy.

    Decision flow:
      1. If force_local → run locally unconditionally.
      2. If force_sco → go straight to SCO.
      3. Detect local GPU.
      4. If GPU available → run locally with retries.
      5. If no GPU:
         a. Heuristic: does experiment NEED a GPU?
         b. If needs GPU → SCO fallback.
         c. If CPU-compatible → run locally.
      6. If local fails after max retries and experiment needs GPU → SCO fallback.

    Returns ExperimentResult with backend and success status.
    """
    script_path = Path(script_path).resolve()

    # Auto-detect GPU config from experiment manifest if not provided
    if sco_config is None or extra_env is None:
        auto_env, auto_cfg = _auto_detect_config(script_path)
        if extra_env is None:
            extra_env = auto_env
        else:
            extra_env = {**auto_env, **extra_env}
        if sco_config is None:
            sco_config = auto_cfg
    if work_dir is None:
        work_dir = script_path.parent
    work_dir = Path(work_dir)
    log_dir = Path(log_dir) if log_dir else work_dir / "logs"

    if force_local:
        logger.info("Local execution forced — skipping SCO")
        local_result = run_local_experiment(
            script_path, work_dir, log_dir,
            timeout=local_timeout,
            max_retries=max_local_retries,
            extra_env=extra_env,
        )
        return ExperimentResult(
            backend="local",
            success=local_result.success,
            log_path=str(local_result.log_path),
            attempts=local_result.attempts,
            error_summary=local_result.error_summary if not local_result.success else "",
        )

    if force_sco:
        logger.info("SCO forced — skipping local execution")
        return _run_sco_path(script_path, job_name, log_dir, extra_env, sco_config)

    # ── Step 1: GPU detection ──
    gpu_info = detect_gpu()
    has_gpu = gpu_info["available"]

    # ── Step 2: Decide local vs SCO ──
    use_local = True
    if not has_gpu:
        needs_gpu = needs_gpu_heuristic(script_path, experiment_dir=script_path.parent)
        if needs_gpu:
            logger.info("No local GPU + experiment needs GPU → will use SCO")
            use_local = False
        else:
            logger.info("No local GPU, but experiment appears CPU-compatible → running locally")

    # ── Step 3: Local execution ──
    if use_local:
        logger.info("=== LOCAL EXECUTION ===")
        local_result = run_local_experiment(
            script_path, work_dir, log_dir,
            timeout=local_timeout,
            max_retries=max_local_retries,
            extra_env=extra_env,
        )

        if local_result.success:
            return ExperimentResult(
                backend="local", success=True,
                log_path=str(local_result.log_path),
                attempts=local_result.attempts,
            )

        # Local failed — if GPU was needed, try SCO as last resort
        if not has_gpu and needs_gpu_heuristic(script_path, experiment_dir=script_path.parent):
            logger.warning(
                "Local execution FAILED (attempts=%d). Experiment needs GPU → falling back to SCO.",
                local_result.attempts,
            )
            sco_result = _run_sco_path(script_path, job_name, log_dir, extra_env, sco_config)
            sco_result.error_summary = (
                f"Local failed after {local_result.attempts} attempts: {local_result.error_summary}. "
                f"Fell back to SCO."
            )
            return sco_result

        # CPU task failed locally — no SCO fallback (SCO won't help)
        return ExperimentResult(
            backend="local", success=False,
            log_path=str(local_result.log_path),
            attempts=local_result.attempts,
            error_summary=local_result.error_summary,
        )

    # ── Step 4: SCO path (no local GPU + needs GPU) ──
    logger.info("=== SCO CLOUD EXECUTION (local GPU unavailable) ===")
    return _run_sco_path(script_path, job_name, log_dir, extra_env, sco_config)


def run_with_debug_loop(
    script_path: str | Path,
    job_name: str = "slairesearch",
    work_dir: str | Path | None = None,
    log_dir: str | Path | None = None,
    local_timeout: int = 7200,
    max_local_retries: int = 20,
    extra_env: dict[str, str] | None = None,
    sco_config: "SCOConfig | None" = None,
    force_sco: bool = False,
    force_local: bool = False,
    max_debug_rounds: int = 5,
    project_slug: str = "",
) -> ExperimentResult:
    """Run an experiment with automatic diagnosis-fix-retry loop.

    Wraps run_experiment() with a multi-round debug cycle:
      1. Run the experiment via run_experiment().
      2. If success: return immediately.
      3. If failure:
         a. Read the error log.
         b. Query debug memory for similar past errors.
         c. Match against the known fix database (fix_db.py).
         d. If match found: apply fix to the shell script, resubmit with
            '-fixN' suffix on job_name.
         e. Save a DebugRecord to persistent debug memory.
         f. If no match: save an unresolved record, then return failure.
      4. Loop up to max_debug_rounds.
      5. Return the final ExperimentResult.

    The -fixN suffix convention is already handled by _cleanup_stale_jobs().
    Debug records are saved to workspace/<project>/debug_memory/records.jsonl.
    """
    script_path = Path(script_path).resolve()
    log_dir = Path(log_dir) if log_dir else (Path(work_dir) if work_dir else script_path.parent) / "logs"

    # Resolve workspace root for debug memory (walk up to find workspace dir)
    _ws_root = script_path
    while _ws_root.parent != _ws_root:
        if (_ws_root / "experiment").is_dir() and _ws_root.name != "experiment":
            break
        _ws_root = _ws_root.parent
    workspace_root = _ws_root if _ws_root.parent != _ws_root else script_path.parent.parent

    # Lazy-init debug memory store
    _memory: Any = None
    _memory_ready = False

    def _get_memory() -> Any | None:
        nonlocal _memory, _memory_ready
        if _memory_ready:
            return _memory
        _memory_ready = True
        try:
            from debug_memory import DebugMemoryStore
            _memory = DebugMemoryStore(workspace_root)
            return _memory
        except Exception:
            return None

    def _save_record(
        error_text: str,
        root_cause: str,
        fix_summary: str,
        files_modified: list[str],
        success: bool,
        fix_round: int,
        job_id: str,
        backend: str,
        error_type_override: str = "",
    ) -> None:
        """Save a debug record to persistent memory. Best-effort — never raises."""
        try:
            store = _get_memory()
            if store is None:
                return
            from debug_memory import DebugRecord, classify_error, compute_signature
            error_type = error_type_override or classify_error(error_text)
            record = DebugRecord(
                error_signature=compute_signature(error_text, "experiment_execution"),
                project_slug=project_slug or workspace_root.name,
                stage="experiment_execution",
                error_type=error_type,
                error_message=error_text[:500],
                error_key_lines=_extract_key_lines(error_text),
                root_cause=root_cause,
                fix_summary=fix_summary,
                files_modified=files_modified,
                fix_round=fix_round,
                total_rounds_attempted=max_debug_rounds,
                success=success,
                job_id=job_id,
                backend=backend,
            )
            store.save(record)
            logger.info("Debug record saved: %s | success=%s", error_type, success)
        except Exception:
            pass

    def _query_memory(error_text: str) -> str:
        """Return formatted context from past similar debug records, or ''."""
        try:
            store = _get_memory()
            if store is None:
                return ""
            slug = project_slug or workspace_root.name
            context = store.format_context_for_prompt(
                error_text, "experiment_execution", slug, limit=3,
            )
            return context
        except Exception:
            return ""

    applied_fixes: set[str] = set()
    total_attempts = 0
    final_result: ExperimentResult | None = None
    fix_db = FixDatabase()

    for debug_round in range(max_debug_rounds + 1):
        current_job_name = job_name
        if debug_round > 0:
            current_job_name = f"{job_name}-fix{debug_round}"

        logger.info(
            "Debug round %d/%d: %s (job=%s)",
            debug_round, max_debug_rounds, script_path.name, current_job_name,
        )
        result = run_experiment(
            script_path=script_path,
            job_name=current_job_name,
            work_dir=work_dir,
            log_dir=log_dir,
            local_timeout=local_timeout,
            max_local_retries=max_local_retries,
            extra_env=extra_env,
            sco_config=sco_config,
            force_sco=force_sco,
            force_local=force_local,
        )
        total_attempts += 1
        final_result = result

        if result.success:
            logger.info("Experiment succeeded on debug round %d", debug_round)
            # Save success record if a fix was applied in a prior round
            if applied_fixes and debug_round > 0:
                _save_record(
                    error_text="",
                    root_cause="Fixed via debug loop",
                    fix_summary=f"Applied {len(applied_fixes)} fix(es): {', '.join(applied_fixes)}",
                    files_modified=[str(script_path)],
                    success=True,
                    fix_round=debug_round,
                    job_id=result.job_id,
                    backend=result.backend,
                )
            return ExperimentResult(
                backend=result.backend,
                success=True,
                job_id=result.job_id,
                log_path=result.log_path,
                attempts=total_attempts,
                error_summary="",
            )

        logger.warning(
            "Debug round %d FAILED: %s",
            debug_round, result.error_summary or "(no summary)",
        )

        # Read error log for diagnosis
        error_text = ""
        if result.log_path:
            log_file = Path(result.log_path)
            if log_file.exists():
                try:
                    error_text = log_file.read_text(encoding="utf-8", errors="replace")[-5000:]
                except Exception as e:
                    logger.warning("Could not read error log %s: %s", log_file, e)

        if not error_text:
            logger.warning("No error log available — cannot diagnose")
            _save_record(
                error_text="(no log available)",
                root_cause="Unknown — no error log",
                fix_summary="",
                files_modified=[],
                success=False,
                fix_round=debug_round,
                job_id=result.job_id,
                backend=result.backend,
            )
            break

        if debug_round >= max_debug_rounds:
            logger.error("Max debug rounds (%d) reached. Giving up.", max_debug_rounds)
            _save_record(
                error_text=error_text,
                root_cause="Max debug rounds exhausted",
                fix_summary=f"Applied {len(applied_fixes)} fix(es): {', '.join(applied_fixes) or 'none'}",
                files_modified=[str(script_path)],
                success=False,
                fix_round=debug_round,
                job_id=result.job_id,
                backend=result.backend,
            )
            break

        # Query debug memory for context on similar past errors
        memory_ctx = _query_memory(error_text)
        if memory_ctx:
            logger.info("Debug memory context:\n%s", memory_ctx)

        # Match against known fix database
        fix = fix_db.match_fix(error_text)
        if fix is None:
            logger.warning("No known fix matches the error — cannot auto-fix")
            _save_record(
                error_text=error_text,
                root_cause="Unknown — no matching fix in fix_db",
                fix_summary="",
                files_modified=[],
                success=False,
                fix_round=debug_round,
                job_id=result.job_id,
                backend=result.backend,
            )
            break

        if fix.name in applied_fixes:
            logger.warning("Fix '%s' already applied — not re-applying", fix.name)
            _save_record(
                error_text=error_text,
                root_cause=fix.fix_description,
                fix_summary=f"Fix '{fix.name}' already applied in round {debug_round}",
                files_modified=[],
                success=False,
                fix_round=debug_round,
                job_id=result.job_id,
                backend=result.backend,
            )
            break

        logger.info("Matched fix '%s': %s", fix.name, fix.fix_description)

        # Apply fix to the shell script
        try:
            modified = fix_db.apply_fix_to_script(script_path, fix)
        except Exception as e:
            logger.error("Failed to apply fix '%s': %s", fix.name, e)
            _save_record(
                error_text=error_text,
                root_cause=fix.fix_description,
                fix_summary=f"Fix application FAILED: {e}",
                files_modified=[],
                success=False,
                fix_round=debug_round,
                job_id=result.job_id,
                backend=result.backend,
            )
            break

        if not modified:
            logger.info("Fix '%s' was already applied or could not be injected", fix.name)

        applied_fixes.add(fix.name)

        # Save a debug record for this round
        _save_record(
            error_text=error_text,
            root_cause=fix.fix_description,
            fix_summary=f"Applied fix '{fix.name}' to {script_path.name}",
            files_modified=[str(script_path)] if modified else [],
            success=False,  # Unknown yet — next round will tell
            fix_round=debug_round + 1,
            job_id=result.job_id,
            backend=result.backend,
        )

    # All rounds exhausted or unrecoverable error
    if final_result is None:
        final_result = ExperimentResult(
            backend="sco", success=False,
            error_summary="Debug loop ended without any experiment run",
            attempts=total_attempts,
        )

    return ExperimentResult(
        backend=final_result.backend,
        success=False,
        job_id=final_result.job_id,
        log_path=final_result.log_path,
        attempts=total_attempts,
        error_summary=(
            f"Debug loop exhausted after {total_attempts} attempt(s) "
            f"({len(applied_fixes)} fix(es) applied: {', '.join(applied_fixes) or 'none'}). "
            f"Last error: {final_result.error_summary}"
        ),
    )


def _extract_key_lines(error_text: str, max_lines: int = 5) -> list[str]:
    """Extract the most informative lines from an error traceback."""
    lines: list[str] = []
    for line in error_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Prioritize: Error lines, traceback file references
        if any(kw in stripped for kw in (
            "Error:", "Error ", "Traceback", "File \"",
            "ModuleNotFoundError", "ImportError",
        )):
            lines.append(stripped[:200])
    if not lines:
        # Fallback: last few non-empty lines
        lines = [l.strip()[:200] for l in error_text.splitlines() if l.strip()][-max_lines:]
    return lines[:max_lines]


def _ensure_wheels() -> bool:
    """Ensure shared wheel cache is populated. Returns True if wheels are ready.

    Checks workspace/.shared/cache/wheels/ —
    if empty, runs the download script (which auto-detects VPN if needed).
    This is called before SCO submission so containers install offline.
    """
    shared_wheels = Path(DOWNLOAD_WHEELS_DIR) if DOWNLOAD_WHEELS_DIR else _PROJECT_ROOT / "workspace/.shared/cache/wheels"
    download_script = _PROJECT_ROOT / "workspace/.shared/download_wheels.sh"

    # Already populated?
    wheel_files = list(shared_wheels.glob("*.whl")) if shared_wheels.exists() else []
    if len(wheel_files) >= 10:  # 10+ wheels = sufficiently populated
        logger.info("Shared wheel cache ready: %d wheels in %s", len(wheel_files), shared_wheels)
        return True

    # Need to download
    logger.info("Shared wheel cache empty or incomplete — downloading before SCO submission...")
    if not download_script.exists():
        logger.warning("Download script not found at %s — skipping wheel pre-download", download_script)
        return False

    try:
        result = subprocess.run(
            ["bash", str(download_script)],
            capture_output=True, text=True, timeout=600,  # 10 min max
        )
        if result.returncode == 0:
            wheel_files = list(shared_wheels.glob("*.whl")) if shared_wheels.exists() else []
            logger.info("Wheel download complete: %d wheels cached", len(wheel_files))
            return True
        else:
            logger.warning("Wheel download failed (exit=%d): %s", result.returncode,
                           (result.stderr or result.stdout)[:500])
            return False
    except subprocess.TimeoutExpired:
        logger.warning("Wheel download timed out — will rely on network install in container")
        return False
    except Exception as e:
        logger.warning("Wheel download error: %s", e)
        return False


def _ensure_model_cache(script_path: Path) -> bool:
    """Pre-download models referenced in an experiment script before SCO submission.

    Scans the script for download_hf_model / download_model.sh calls and
    pre-caches model weights into shared storage so the SCO container can
    access them without network.
    """
    try:
        from model_downloader import ThreeLayerDownloader

        dl = ThreeLayerDownloader()
        results = dl.pre_cache_from_script(str(script_path))

        succeeded = sum(1 for r in results if r.success)
        failed = sum(1 for r in results if not r.success)

        if results:
            logger.info("Model cache pre-check: %d succeeded, %d failed", succeeded, failed)
            for r in results:
                if not r.success:
                    logger.warning("Model pre-cache FAILED: %s (%s)", r.model_id, r.error)
        else:
            logger.info("Model cache pre-check: no models found in script")

        return failed == 0
    except ImportError:
        logger.info("model_downloader not available — skipping model pre-cache")
        return True
    except Exception as e:
        logger.warning("Model cache pre-check error (non-fatal): %s", e)
        return True


def _ast_str_value(node) -> str | None:
    """Extract a string literal value from an AST node."""
    import ast as _ast
    if isinstance(node, _ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _parse_datasets_from_data_utils(data_utils_path: Path) -> list[tuple[str, str | None]]:
    """Extract (dataset_path, subset) tuples from TaskConfig definitions.

    Uses AST parsing so it works without importing data_utils (which has
    heavy torch/transformers deps that may not be installed).
    """
    import ast as _ast

    try:
        tree = _ast.parse(data_utils_path.read_text())
    except SyntaxError as e:
        logger.warning("Cannot parse %s: %s", data_utils_path, e)
        return []

    datasets: set[tuple[str, str | None]] = set()

    for node in _ast.walk(tree):
        if isinstance(node, _ast.Call):
            func = node.func
            if isinstance(func, _ast.Name) and func.id == "TaskConfig":
                args = node.args
                if len(args) >= 2:
                    path = _ast_str_value(args[1])
                    subset = _ast_str_value(args[2]) if len(args) >= 3 else None
                    if path:
                        datasets.add((path, subset))

    return list(datasets)


def _ensure_dataset_cache(script_path: Path) -> bool:
    """Ensure datasets are available for offline SCO execution.

    First checks if the pre-built tarball exists in shared cache (fast path).
    Only falls back to network download if the tarball is missing.
    The tarball is extracted to ~/.cache/huggingface/ at experiment startup
    because AFS doesn't support flock() used by the datasets library.
    """
    script_dir = script_path if script_path.is_dir() else script_path.parent
    data_utils_path = script_dir / "data_utils.py"
    if not data_utils_path.exists():
        logger.info("No data_utils.py in %s — skipping dataset pre-cache", script_dir)
        return True

    # ── Fast path: pre-built tarball exists ──
    from config import DOWNLOAD_CACHE_DIR
    tarball = Path(DOWNLOAD_CACHE_DIR) / "datasets_cache.tar.gz"
    if tarball.exists():
        import stat as _stat
        size_mb = round(tarball.stat().st_size / (1024 * 1024), 1)
        logger.info("Dataset tarball found: %s (%d MB) — skipping network download", tarball, size_mb)
        # Verify the tarball contains the expected datasets by checking configs
        datasets = _parse_datasets_from_data_utils(data_utils_path)
        logger.info("  %d datasets covered by tarball (extract at experiment startup)", len(datasets))
        return True

    # ── Slow path: tarball missing, try network download ──
    datasets = _parse_datasets_from_data_utils(data_utils_path)
    if not datasets:
        logger.info("No dataset references found in %s", data_utils_path.name)
        return True

    logger.warning("Dataset tarball NOT found at %s — falling back to network download", tarball)
    try:
        from model_downloader import ThreeLayerDownloader
        dl = ThreeLayerDownloader()
    except ImportError as e:
        logger.warning("model_downloader not available — skipping dataset cache: %s", e)
        return True

    succeeded = 0
    for dataset_path, subset in datasets:
        logger.info("Pre-caching dataset: %s (subset=%s)", dataset_path, subset or "none")
        result = dl.download_dataset(dataset_path, subset)
        if result.success:
            succeeded += 1
            logger.info("  Dataset %s cached via %s", dataset_path, result.layer_used)
        else:
            logger.warning("  Dataset %s FAILED: %s", dataset_path, result.error)

    total = len(datasets)
    logger.info("Dataset pre-cache: %d/%d succeeded, %d failed", succeeded, total, total - succeeded)
    return succeeded == total


def _cleanup_stale_jobs(job_name_prefix: str) -> int:
    """Delete FAILED/CANCELLED/STOPPED/SUSPENDED SCO jobs matching a name prefix.

    Returns number of jobs cleaned up.  Failure to clean up is logged but
    never raised — stale jobs are a best-effort optimization, not a hard
    requirement.

    Strips -fixN and -local-fallback suffixes from the prefix so that
    fix-round-N can match stale jobs from fix rounds N-1, N-2, etc.
    """
    import re as _re
    cleaned = 0
    terminal_states = {"FAILED", "CANCELLED", "STOPPED", "SUSPENDED"}

    # Build a list of prefixes to match against.  The caller passes the
    # full job name (e.g. "cr-topic-fix18") but stale jobs are named with
    # earlier fix numbers ("cr-topic-fix17").  Strip the variable suffix
    # so we match across fix / fallback rounds.
    base = _re.sub(r'-(fix|local-fallback)\d*$', '', job_name_prefix)
    prefixes = [job_name_prefix]
    if base != job_name_prefix:
        prefixes.append(base)

    try:
        result = subprocess.run(
            ["sco", "acp", "jobs", "list", "--workspace-name", SCO_WORKSPACE,
             "--page-size", "50", "-o", "json"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            logger.warning("Failed to list jobs for pre-submit cleanup")
            return 0
        jobs = json.loads(result.stdout) if result.stdout.strip() else []
    except Exception as e:
        logger.warning("Failed to list jobs during cleanup: %s", e)
        return 0

    stale_ids = []
    for job in jobs:
        jid = job.get("name", "")
        dname = job.get("display_name", "")
        status = job.get("status", job.get("state", ""))
        # Match any of our prefixes against display_name or job id
        if status in terminal_states and any(p in dname or p in jid for p in prefixes):
            stale_ids.append(jid)

    if stale_ids:
        logger.info("Cleaning up %d stale terminal job(s): %s", len(stale_ids), stale_ids)
        try:
            del_cmd = ["sco", "acp", "jobs", "delete", "--workspace-name", SCO_WORKSPACE] + stale_ids
            del_result = subprocess.run(del_cmd, capture_output=True, text=True, timeout=60)
            if del_result.returncode == 0:
                cleaned = len(stale_ids)
                logger.info("Deleted %d stale job(s) to free quota", cleaned)
            else:
                logger.warning("Failed to delete stale jobs: %s", (del_result.stderr or del_result.stdout)[:500])
        except Exception as e:
            logger.warning("Exception during stale job deletion: %s", e)
    else:
        logger.info("No stale terminal jobs found for prefixes %s", prefixes)

    return cleaned


def _prepare_env_for_sco(experiment_dir: Path) -> bool:
    """Pre-install ALL missing Python packages to shared site-packages.

    Scans ALL experiment .py files AND requirements.txt for dependencies,
    pre-installs anything not already in the SCO container image or shared
    site-packages.  This MUST succeed before SCO submission so the container
    can run with CHENRESEARCH=1 (zero pip install at runtime).
    """
    prepare_env_sh = _PROJECT_ROOT / "workspace/.shared/prepare_env.sh"
    if not prepare_env_sh.exists():
        logger.warning("prepare_env.sh not found at %s — skipping env prep", prepare_env_sh)
        return False

    # ── 1. Run from-imports on ALL .py files (not just the first one) ──
    py_files = sorted(experiment_dir.glob("*.py"))
    if not py_files:
        logger.warning("No .py files found in %s — skipping env prep", experiment_dir)
        return False

    all_imports: set[str] = set()
    for py_file in py_files:
        logger.info("Scanning imports from %s ...", py_file.name)
        try:
            result = subprocess.run(
                ["bash", str(prepare_env_sh), "--from-imports", str(py_file)],
                capture_output=True, text=True, timeout=300,
            )
            output = (result.stdout or "") + (result.stderr or "")
            # Extract missing packages from prepare_env.sh output
            for line in output.split("\n"):
                if "Installing" in line or "Missing" in line:
                    logger.info("  %s", line.strip()[:120])
        except Exception as e:
            logger.warning("prepare_env.sh error for %s: %s", py_file.name, e)

    # ── 2. Offline-first install from requirements.txt ──
    req_file = experiment_dir / "requirements.txt"
    if req_file.exists():
        site_packages = str(_PROJECT_ROOT / "env/site-packages")
        wheels_dir = str(Path(DOWNLOAD_CACHE_DIR) / "wheels")
        find_links = f"--find-links={wheels_dir}" if Path(wheels_dir).exists() else ""
        logger.info("Pre-installing from requirements.txt (offline-first)...")

        # Try offline first (no network, use cached wheels)
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(req_file),
                 "--target", site_packages, "-q", "--no-index", find_links,
                 "--no-deps"],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                logger.info("requirements.txt installed (offline/from cache)")
            else:
                logger.info("requirements.txt offline incomplete — %d packages still needed, trying online...",
                           len([l for l in (result.stderr+result.stdout).split('\n') if 'ERROR' in l]))
        except subprocess.TimeoutExpired:
            pass  # offline timed out, try online
        except Exception:
            pass

        # Only try online if needed (with short timeout, non-fatal)
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(req_file),
                 "--target", site_packages, "-q", "--no-deps"],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                logger.info("requirements.txt installed (online)")
        except subprocess.TimeoutExpired:
            logger.info("requirements.txt online timed out — skipping (critical packages handled separately)")
        except Exception as e:
            pass

    # ── 3. Auto-install missing critical packages ──
    _ensure_critical_packages()

    # ── 4. Hard verification: are packages actually in site-packages? ──
    # Use directory check (NOT import — triggers torch CUDA loading).
    site_packages = str(_PROJECT_ROOT / "env/site-packages")
    missing = []
    for pkg, import_name in [("datasets", "datasets"), ("accelerate", "accelerate"),
                               ("scikit-learn", "sklearn")]:
        if not (Path(site_packages) / import_name).exists() and \
           not list(Path(site_packages).glob(f"{import_name}-*.dist-info")):
            missing.append(pkg)

    if missing:
        logger.error("CRITICAL packages still missing from site-packages: %s", missing)
        logger.error("SCO container will fail. Run env prep locally with network first.")
        return False

    logger.info("Environment verified: all critical packages in %s", site_packages)
    logger.info("Environment pre-configuration complete for %s", experiment_dir.name)
    return True


def _ensure_critical_packages() -> None:
    """Ensure critical Python packages are available in shared site-packages.

    Uses directory-based detection (NOT import) because importing packages
    like accelerate triggers torch CUDA loading which takes 60+ seconds
    on CPU-only machines and causes subprocess timeouts.
    """
    site_packages = str(_PROJECT_ROOT / "env/site-packages")
    critical = ["datasets", "accelerate", "sentence-transformers", "scikit-learn"]

    for pkg in critical:
        pkg_import = pkg.replace("-", "_")
        sp_dir = Path(site_packages)

        # Fast check: does package directory or dist-info exist?
        pkg_dir = sp_dir / pkg_import
        dist_matches = list(sp_dir.glob(f"{pkg_import}-*.dist-info"))
        if pkg_dir.exists() or dist_matches:
            continue  # Already installed to site-packages

        # Also check if importable from default python (lazy, avoid if possible)
        try:
            # Use a quick python -c that exits fast if pkg isn't there
            r = subprocess.run(
                [sys.executable, "-c",
                 f"import importlib.util; print('ok' if importlib.util.find_spec('{pkg_import}') else 'no')"],
                capture_output=True, timeout=5,
            )
            if r.returncode == 0 and b"ok" in r.stdout:
                continue  # Already in system python path
        except subprocess.TimeoutExpired:
            pass  # fall through to install
        except Exception:
            pass

        logger.info("Pre-installing critical package: %s", pkg)
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", pkg,
                 "--target", site_packages, "-q"],
                capture_output=True, text=True, timeout=120,
            )
            logger.info("  %s installed", pkg)
        except Exception as e:
            logger.warning("  %s install failed: %s", pkg, e)


def _run_sco_path(
    script_path: Path,
    job_name: str,
    log_dir: Path,
    extra_env: dict[str, str] | None,
    sco_config: "SCOConfig | None",
) -> ExperimentResult:
    """Submit to SCO and wait for completion."""
    log_dir.mkdir(parents=True, exist_ok=True)

    # ── Pre-submission: GPU utilization + compute budget checks ──
    gpu_count = int((extra_env or {}).get("GPU_COUNT", DEFAULT_GPU_COUNT))
    try:
        _pre_submit_check(script_path.parent, gpu_count)
    except RuntimeError as e:
        logger.error("Pre-submit check FAILED: %s", e)
        return ExperimentResult(
            backend="sco", success=False,
            error_summary=f"Pre-submit check failed: {e}",
        )

    # ── Pre-submission: worker spec validation (hard — block if invalid) ──
    if sco_config is not None:
        try:
            _validate_worker_spec(sco_config.worker_spec, "sco_config.worker_spec")
        except RuntimeError as e:
            logger.error("Worker spec validation FAILED: %s", e)
            return ExperimentResult(
                backend="sco", success=False,
                error_summary=f"Worker spec validation failed: {e}",
            )
    _ensure_wheels()

    # ── Pre-submission: pre-install missing packages to shared site-packages ──
    if not _prepare_env_for_sco(script_path.parent):
        logger.error("ENVIRONMENT PREP FAILED — critical packages missing from site-packages.")
        logger.error("Run env prep locally first: bash start.sh → select project → env prep.")
        return ExperimentResult(
            backend="sco", success=False,
            error_summary="Environment preparation failed: critical packages missing. "
                          "Run env prep locally with network access, then re-submit.",
        )

    # ── Pre-submission: pre-download models referenced in experiment script ──
    _ensure_model_cache(script_path)

    # ── Pre-submission: pre-download datasets to shared cache ──
    _ensure_dataset_cache(script_path)

    # ── Set offline mode for SCO containers (no network in cloud nodes) ──
    if extra_env is None:
        extra_env = {}
    extra_env.setdefault("HF_DATASETS_OFFLINE", "1")
    extra_env.setdefault(
        "HF_DATASETS_CACHE",
        str(Path(DOWNLOAD_DATASETS_DIR)),
    )

    # ── Pre-submission: clean up stale terminal jobs from previous attempts ──
    _cleanup_stale_jobs(job_name)

    # ── Submit with quota-exceeded fallback ──
    quota_attempts = 0
    max_quota_attempts = 12
    current_gpu_count = gpu_count
    current_config = sco_config
    _spot_tried = (current_config.quota_type or "default") == "spot"  # don't retry spot if already spot

    def _try_spot_quota(config: SCOConfig) -> ExperimentResult | None:
        """Try submitting with spot quota type (separate quota pool).

        Returns ExperimentResult on terminal result, None if spot also
        failed with quota error (caller should continue retrying default).
        """
        logger.warning(
            "Default quota saturated (spec=%s, %d GPUs). "
            "Trying quota_type='spot' (separate pool)...",
            config.worker_spec, _gpu_count_from_spec(config.worker_spec),
        )
        spot_config = SCOConfig(
            workspace=config.workspace,
            aec2=config.aec2,
            image=config.image,
            worker_spec=config.worker_spec,
            storage_mount=config.storage_mount,
            worker_nodes=config.worker_nodes,
            quota_type="spot",
            priority=config.priority,
            training_framework=config.training_framework,
            fault_tolerant=config.fault_tolerant,
            retry_times=config.retry_times,
        )
        _cleanup_stale_jobs(job_name)
        try:
            job = submit_job(script_path, job_name,
                             extra_env=extra_env or {}, config=spot_config)
            logger.info("Spot quota job submitted: %s", job.job_id)
            id_file = log_dir / "sco_job_id.txt"
            id_file.write_text(job.job_id)

            # Concurrent log capture (same rationale as main path)
            sco_log = log_dir / "sco_logs.txt"
            stream_output_s: dict[str, str] = {"logs": ""}

            def _capture_spot_logs():
                try:
                    stream_output_s["logs"] = stream_logs(job.job_id, None, config=spot_config)
                except Exception as e:
                    stream_output_s["logs"] = f"[stream_logs exception: {e}]"

            log_thread_s = threading.Thread(target=_capture_spot_logs, daemon=True)
            log_thread_s.start()
            time.sleep(5)

            job = wait_for_job(job.job_id, poll_interval=120,
                               max_wait=43200, config=spot_config)
            log_thread_s.join(timeout=30)

            final_log_s = stream_output_s.get("logs", "")
            persistent_log_s = log_dir / "run_output.log"
            if persistent_log_s.exists():
                ps = persistent_log_s.read_text()
                if ps.strip():
                    if final_log_s.strip():
                        final_log_s += "\n\n=== PERSISTENT LOG (AFS) ===\n" + ps
                    else:
                        final_log_s = ps
            sco_log.write_text(final_log_s)

            if job.status == "SUCCEEDED":
                return ExperimentResult(
                    backend="sco", success=True,
                    job_id=job.job_id, log_path=str(sco_log),
                )
            else:
                return ExperimentResult(
                    backend="sco", success=False,
                    job_id=job.job_id, log_path=str(sco_log),
                    error_summary=f"SCO spot job ended with status: {job.status}",
                )
        except RuntimeError as e:
            error_msg = str(e)
            if "MEMBER_QUOTA_EXCEEDED" in error_msg:
                logger.warning("Spot quota also exhausted: %s", error_msg[:200])
            else:
                logger.warning("Spot quota submission failed: %s", e)
        except Exception as e:
            logger.warning("Spot quota exception: %s", e)
        return None

    while quota_attempts < max_quota_attempts:
        try:
            logger.info("Submitting SCO job: spec=%s nodes=%d quota_type=%s",
                        current_config.worker_spec, current_config.worker_nodes,
                        current_config.quota_type or "default")
            job = submit_job(script_path, job_name,
                             extra_env=extra_env or {}, config=current_config)
            logger.info("SCO job submitted: %s (GPU_COUNT=%d)", job.job_id, current_gpu_count)

            # Save job_id immediately
            id_file = log_dir / "sco_job_id.txt"
            id_file.write_text(job.job_id)

            # Start log streaming concurrently BEFORE waiting — pods are
            # cleaned up quickly after reaching terminal state, so
            # stream-logs must connect while the pod is still alive.
            sco_log = log_dir / "sco_logs.txt"
            stream_output: dict[str, str] = {"logs": ""}

            def _capture_logs():
                try:
                    stream_output["logs"] = stream_logs(job.job_id, None, config=current_config)
                except Exception as e:
                    stream_output["logs"] = f"[stream_logs exception: {e}]"

            log_thread = threading.Thread(target=_capture_logs, daemon=True)
            log_thread.start()
            time.sleep(5)  # Give stream-logs a head-start on the pod

            # Wait for job to reach terminal state
            job = wait_for_job(job.job_id, poll_interval=120, max_wait=43200, config=current_config)

            # Give log thread time to flush remaining output
            log_thread.join(timeout=30)

            # Build the final log: stream-logs output + persistent fallback
            final_log = stream_output.get("logs", "")
            persistent_log = log_dir / "run_output.log"
            if persistent_log.exists():
                pfx = persistent_log.read_text()
                if pfx.strip():
                    if final_log.strip():
                        final_log += "\n\n=== PERSISTENT LOG (AFS) ===\n" + pfx
                    else:
                        final_log = pfx
            sco_log.write_text(final_log)

            if job.status == "SUCCEEDED":
                return ExperimentResult(
                    backend="sco", success=True,
                    job_id=job.job_id, log_path=str(sco_log),
                )
            else:
                return ExperimentResult(
                    backend="sco", success=False,
                    job_id=job.job_id, log_path=str(sco_log),
                    error_summary=f"SCO job ended with status: {job.status}",
                )

        except RuntimeError as e:
            error_msg = str(e)
            if "MEMBER_QUOTA_EXCEEDED" in error_msg:
                # Extract GPU counts from the quota error
                remaining_match = re.search(r'Remaining:\s*GPU:\s*(\d+)\s*card', error_msg)
                requested_match = re.search(r'Requested additional:\s*GPU:\s*(\d+)\s*card', error_msg)
                quota_remaining = int(remaining_match.group(1)) if remaining_match else 0
                quota_requested = int(requested_match.group(1)) if requested_match else 0

                effective_gpus = quota_requested if quota_requested > 0 else current_gpu_count * current_config.worker_nodes
                logger.warning(
                    "Quota exceeded: requested=%d remaining=%d effective=%d current_gpu=%d worker_nodes=%d spec=%s",
                    quota_requested, quota_remaining, effective_gpus, current_gpu_count,
                    current_config.worker_nodes, current_config.worker_spec,
                )

                # First tactic: reduce worker_nodes to 1 (kills the multiplier)
                if current_config.worker_nodes > 1:
                    old_wn = current_config.worker_nodes
                    current_config = SCOConfig(
                        worker_spec=current_config.worker_spec,
                        worker_nodes=1,
                    )
                    logger.warning("Reducing worker_nodes from %d to 1", old_wn)
                    quota_attempts += 1
                    continue

                # Second tactic: reduce GPU count per spec to fit remaining quota
                prev_spec_gpus = _gpu_count_from_spec(current_config.worker_spec)
                if current_gpu_count > 1:
                    prev_gpu = current_gpu_count
                    if quota_remaining > 0:
                        current_gpu_count = max(1, min(current_gpu_count - 1, quota_remaining))
                    else:
                        current_gpu_count = max(1, current_gpu_count - 1)
                    quota_attempts += 1
                    resolved_spec = resolve_worker_spec(current_gpu_count)
                    new_spec_gpus = _gpu_count_from_spec(resolved_spec)
                    if new_spec_gpus >= prev_spec_gpus:
                        logger.warning(
                            "Cannot reduce spec GPUs: %d → %d (resolved spec=%s still has %d GPUs).",
                            prev_spec_gpus, current_gpu_count, resolved_spec, new_spec_gpus,
                        )
                        current_gpu_count = 1
                    else:
                        current_config = SCOConfig(worker_spec=resolved_spec, worker_nodes=1)
                        actual_gpus = _gpu_count_from_spec(resolved_spec)
                        if extra_env:
                            extra_env["GPU_COUNT"] = str(actual_gpus)
                        logger.warning(
                            "Quota exceeded at GPU_COUNT=%d — retrying with spec=%s (%d GPUs) worker_nodes=1 (attempt %d/%d)",
                            prev_gpu, resolved_spec, actual_gpus, quota_attempts, max_quota_attempts,
                        )
                        continue

                # At minimum spec — workspace_member_default charges 4 GPU units
                # per job regardless of actual spec.  If remaining < 4, the default
                # quota pool is exhausted.  Try spot quota IMMEDIATELY (separate pool).
                if not _spot_tried and quota_attempts < max_quota_attempts - 1:
                    _spot_tried = True
                    spot_result = _try_spot_quota(current_config)
                    if spot_result is not None:
                        return spot_result
                    # Spot also failed — fall through to backoff retries for default quota
                    quota_attempts += 1
                    logger.warning(
                        "Spot quota also exhausted. Falling back to default quota "
                        "with backoff (attempt %d/%d).",
                        quota_attempts, max_quota_attempts,
                    )
                    backoff = min(30 * (2 ** max(quota_attempts - 1, 0)), 600)
                    _cleanup_stale_jobs(job_name)
                    time.sleep(backoff)
                    continue

                # Backoff: wait for other jobs to release shared quota
                if quota_attempts < max_quota_attempts - 1:
                    quota_attempts += 1
                    backoff = min(30 * (2 ** max(quota_attempts - 1, 0)), 600)
                    logger.warning(
                        "Quota exceeded at minimum config (spec=%s, %d GPUs, worker_nodes=%d, "
                        "platform remaining=%d). Cleaning up stale jobs, waiting %ds "
                        "(retry %d/%d)...",
                        current_config.worker_spec, prev_spec_gpus, current_config.worker_nodes,
                        quota_remaining, backoff, quota_attempts, max_quota_attempts,
                    )
                    _cleanup_stale_jobs(job_name)
                    time.sleep(backoff)
                    continue
                logger.error("Quota exceeded after %d retries: spec=%s (%d GPUs), "
                             "platform remaining=%d",
                             quota_attempts, current_config.worker_spec, prev_spec_gpus,
                             quota_remaining)
                return ExperimentResult(
                    backend="sco", success=False,
                    error_summary=(
                        f"GPU quota exceeded after {quota_attempts} retries — "
                        f"requested {prev_spec_gpus} GPU(s) but "
                        f"only {quota_remaining} available. "
                        f"Wait for other jobs to complete or release quota and retry later."
                    ),
                )
            else:
                logger.exception("SCO execution failed")
                return ExperimentResult(
                    backend="sco", success=False,
                    error_summary=f"SCO error: {e}",
                )

        except Exception as e:
            logger.exception("SCO execution failed")
            return ExperimentResult(
                backend="sco", success=False,
                error_summary=f"SCO error: {e}",
            )

    # Exhausted all retries
    return ExperimentResult(
        backend="sco", success=False,
        error_summary=(
            f"Member quota exceeded after {quota_attempts} retries "
            f"(final GPU_COUNT={current_gpu_count}). "
            f"Please release quota or wait for other jobs to complete."
        ),
    )


@dataclass
class SCOConfig:
    workspace: str = SCO_WORKSPACE
    aec2: str = SCO_AEC2
    image: str = SCO_IMAGE
    worker_spec: str = SCO_WORKER_SPEC
    storage_mount: str = SCO_STORAGE_MOUNT
    worker_nodes: int = SCO_WORKER_NODES
    quota_type: str = SCO_QUOTA_TYPE
    priority: str = SCO_PRIORITY
    training_framework: str = "pytorch"
    fault_tolerant: bool = True
    retry_times: int = 3


@dataclass
class SCOJob:
    job_id: str
    job_name: str
    status: str = "PENDING"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _sanitize_job_name(name: str) -> str:
    """Sanitize job name to match SCO DisplayName regex:
    ^[\\p{Han}a-zA-Z0-9][\\p{Han}a-zA-Z0-9._:-]{0,255}$

    Strategy: replace disallowed chars with '_', ensure first char is [a-zA-Z0-9].
    """
    import re
    # Replace any character NOT in the allowed set with underscore
    # Allowed: letters, digits, underscore, dot, colon, hyphen
    sanitized = re.sub(r'[^a-zA-Z0-9._:-]', '_', name)
    # Ensure first character is alphanumeric
    if sanitized and not sanitized[0].isalnum():
        first_alnum = re.search(r'[a-zA-Z0-9]', sanitized)
        if first_alnum:
            idx = first_alnum.start()
            sanitized = sanitized[idx] + sanitized[idx+1:]
        else:
            sanitized = 'job_' + sanitized
    # Truncate to 256 chars max
    return sanitized[:256]


def submit_job(
    script_path: str | Path,
    job_name: str,
    afs_work_dir: str | None = None,
    extra_env: dict[str, str] | None = None,
    config: SCOConfig | None = None,
    dry_run: bool = False,
) -> SCOJob:
    """
    提交 ACP 任务。

    ACP/CCI 存储共享：/data/ 下的脚本直接原地执行（cd && bash），无需复制。
    仅当脚本不在 /data/ 下时才复制到 AFS。
    """
    if shutil.which("sco") is None:
        raise RuntimeError("sco CLI not found on PATH")

    cfg = config or SCOConfig()

    if not cfg.image:
        raise RuntimeError(
            "SCO_IMAGE is not configured. "
            "Set it in .env or via environment variable: "
            "export SCO_IMAGE='registry.cn-sh-01.sensecore.cn/...'"
        )
    if not cfg.storage_mount:
        raise RuntimeError(
            "SCO_STORAGE_MOUNT is not configured. "
            "Set it in .env or via environment variable: "
            "export SCO_STORAGE_MOUNT='<uuid>:/data:<user_id>'"
        )

    # Sanitize job name to comply with SCO API DisplayName validation
    job_name = _sanitize_job_name(job_name)
    logger.info("Sanitized job name: %s", job_name)
    script_path = Path(script_path).resolve()
    experiment_dir = script_path.parent
    script_name = script_path.name

    # ── 路径判断：在 /data/ 下 → 直接原地执行，否则复制到 AFS ──
    if str(experiment_dir).startswith("/data/"):
        # ACP 容器可直接访问 /data/ 路径，无需复制
        work_dir = str(experiment_dir)
        logger.info("Experiment under /data/, running in-place: %s", work_dir)
    else:
        # 本地路径 → 复制到 AFS 共享目录
        if afs_work_dir is None:
            import time
            afs_work_dir = f"{AFS_BASE}/{job_name}_{int(time.time())}"
        Path(afs_work_dir).mkdir(parents=True, exist_ok=True)
        shutil.copytree(experiment_dir, afs_work_dir, dirs_exist_ok=True)
        work_dir = afs_work_dir
        logger.info("Experiment directory copied to AFS: %s", work_dir)

    command = _build_remote_command(work_dir, script_name, extra_env or {})

    cmd = [
        "sco", "acp", "jobs", "create",
        "--workspace-name", cfg.workspace,
        "--aec2-name", cfg.aec2,
        "--job-name", job_name,
        "--container-image-url", cfg.image,
        "--training-framework", cfg.training_framework,
        "--worker-nodes", str(cfg.worker_nodes),
        "--worker-spec", cfg.worker_spec,
        "--priority", cfg.priority,
        "--storage-mount", cfg.storage_mount,
        "--command", command,
    ]
    # Only include --quota-type if non-empty (platform uses default otherwise)
    if cfg.quota_type:
        cmd.insert(cmd.index("--priority") + 2, "--quota-type")
        cmd.insert(cmd.index("--priority") + 3, cfg.quota_type)

    if cfg.fault_tolerant:
        cmd[-2:-2] = ["--enable-fault-tolerance", "--retry-times", str(cfg.retry_times)]

    if dry_run:
        logger.info("[DRY RUN] %s", " ".join(cmd))
        return SCOJob(job_id="dry-run-0", job_name=job_name)

    logger.info("Submitting SCO job: %s", job_name)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"sco submit failed: {result.stderr}")

    job_id = _parse_job_id(result.stdout)
    logger.info("Job submitted: %s (id=%s)", job_name, job_id)
    return SCOJob(job_id=job_id, job_name=job_name)


def get_job_status(job_id: str, config: SCOConfig | None = None) -> str:
    cfg = config or SCOConfig()
    result = subprocess.run(
        ["sco", "acp", "jobs", "describe", "--workspace-name", cfg.workspace,
         "-o", "json", job_id],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"sco describe failed: {result.stderr}")
    data = json.loads(result.stdout) if result.stdout.strip() else {}
    return data.get("status", data.get("state", "UNKNOWN"))


def wait_for_job(
    job_id: str,
    poll_interval: int = 60,
    max_wait: int = 86400,
    config: SCOConfig | None = None,
) -> SCOJob:
    """Poll until job reaches a terminal state."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        status = get_job_status(job_id, config)
        logger.info("Job %s status: %s", job_id, status)
        if status in JOB_STATE_TERMINAL:
            return SCOJob(job_id=job_id, job_name="", status=status)
        time.sleep(poll_interval)
    raise TimeoutError(f"Job {job_id} did not finish within {max_wait}s")


def stream_logs(
    job_id: str,
    output_path: str | Path | None = None,
    config: SCOConfig | None = None,
) -> str:
    cfg = config or SCOConfig()
    try:
        result = subprocess.run(
            ["sco", "acp", "jobs", "stream-logs", "--workspace-name", cfg.workspace, job_id],
            capture_output=True, text=True, timeout=120,
        )
        logs = result.stdout or ""
        # Include stderr if stdout is empty (some SCO versions use stderr for logs)
        if not logs and result.stderr:
            logs = f"[stderr] {result.stderr}"
        # Log exit code if non-zero (signal platform issues)
        if result.returncode != 0:
            logs = (logs or "") + f"\n[sco stream-logs exit code: {result.returncode}]"
    except subprocess.TimeoutExpired:
        logs = "[stream_logs timed out after 120s — job may still be initializing]"
    except Exception as e:
        logs = f"[stream_logs error: {e}]"
    if output_path:
        Path(output_path).write_text(logs)
    return logs


def list_jobs(limit: int = 20, config: SCOConfig | None = None) -> list[dict]:
    cfg = config or SCOConfig()
    result = subprocess.run(
        ["sco", "acp", "jobs", "list", "--workspace-name", cfg.workspace,
         "--page-size", str(limit), "-o", "json"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"sco list failed: {result.stderr}")
    return json.loads(result.stdout) if result.stdout.strip() else []


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

AFS_BASE = os.environ.get(
    "SLAIRESEARCH_AFS_BASE",
    os.path.join("/data", os.environ.get("SCO_USER_ID", "research"), "slairesearch"),
)


def _build_remote_command(work_dir: str, script_name: str, extra_env: dict[str, str]) -> str:
    """
    规范格式（参考 sco-skill）：cd 到工作目录，执行指定脚本。
    不硬编码文件名 — 由调用方传入。

    Persistence: all output is tee'd to run_output.log on the AFS mount.
    This ensures logs survive pod termination — stream-logs often can't
    connect because the pod is gone by the time wait_for_job returns.
    """
    import shlex
    persistent_log = f"{work_dir}/logs/run_output.log"
    lines = []
    # pipefail is critical: without it, the pipeline exit code is tee's (always 0),
    # masking script failures from the SCO platform.
    lines.append("set -o pipefail")
    # Ensure logs directory exists on persistent storage BEFORE any output
    lines.append(f"mkdir -p {shlex.quote(work_dir + '/logs')}")
    # Wrap everything in a group command so tee captures ALL output,
    # including early diagnostics and cd failures.
    lines.append("{")
    # Early diagnostics BEFORE cd — ensures we see output even if storage
    # mount is missing or the work dir doesn't exist.
    lines.append("echo '=== SCO container started at' $(date 2>/dev/null || echo unknown) '==='")
    lines.append("echo 'Host:' $(hostname 2>/dev/null || echo unknown)")
    lines.append("echo 'Kernel:' $(uname -r 2>/dev/null || echo unknown)")
    lines.append("echo 'Shell:' $(readlink /proc/$$/exe 2>/dev/null || echo $SHELL)")
    lines.append("echo 'PWD:' $(pwd 2>/dev/null || echo unknown)")
    lines.append("echo 'Python:' $(python3 --version 2>&1 || python --version 2>&1 || echo 'NOT FOUND')")
    lines.append("echo 'CUDA:' $(nvidia-smi -L 2>/dev/null | head -1 || echo 'NOT DETECTED')")
    lines.append("echo 'GPU_COUNT env:' ${GPU_COUNT:-NOT SET}")
    lines.append("echo 'HF_DATASETS_OFFLINE:' ${HF_DATASETS_OFFLINE:-NOT SET}")
    # Handle cd failure explicitly so set -e doesn't hide the error
    lines.append(f"if cd {shlex.quote(work_dir)} 2>/dev/null; then")
    lines.append("    echo 'Work dir OK:' $(pwd)")
    lines.append("else")
    lines.append(f"    echo 'FATAL: cd to {shlex.quote(work_dir)} failed!'")
    lines.append("    echo 'Checking /data/ mount...'")
    lines.append("    ls -la /data/ 2>/dev/null || echo '/data/ NOT accessible'")
    lines.append("    echo 'Checking /data/AutoResearch/...'")
    lines.append("    ls -la /data/AutoResearch/ 2>/dev/null || echo '/data/AutoResearch/ NOT accessible'")
    lines.append("    exit 1")
    lines.append("fi")
    for k, v in (extra_env or {}).items():
        lines.append(f"export {shlex.quote(k)}={shlex.quote(v)}")
    # Safety net: auto-detect GPU count if not explicitly set.
    # This prevents 4-GPU containers from running single-GPU experiments
    # when GPU_COUNT propagation is broken.
    lines.append(
        "if [ -z \"${GPU_COUNT}\" ] || [ \"${GPU_COUNT}\" -le 0 ]; then"
    )
    lines.append(
        "  _DETECTED_GPUS=$(nvidia-smi -L 2>/dev/null | wc -l || echo 0)"
    )
    lines.append(
        "  if [ \"${_DETECTED_GPUS}\" -gt 0 ]; then"
    )
    lines.append(
        "    export GPU_COUNT=\"${_DETECTED_GPUS}\""
    )
    lines.append(
        "    echo '[sco_runner] GPU_COUNT not set — auto-detected:' ${GPU_COUNT} 'GPU(s)'"
    )
    lines.append(
        "  else"
    )
    lines.append(
        "    export GPU_COUNT=1"
    )
    lines.append(
        "    echo '[sco_runner] GPU_COUNT not set, no GPUs detected — defaulting to 1'"
    )
    lines.append(
        "  fi"
    )
    lines.append(
        "fi"
    )
    lines.append("echo 'Running experiment script...'")
    lines.append(f"bash {shlex.quote(script_name)}")
    lines.append("} 2>&1 | tee " + shlex.quote(persistent_log))
    return "\n".join(lines)


def _parse_job_id(stdout: str) -> str:
    try:
        data = json.loads(stdout)
        if isinstance(data, dict):
            return data.get("job_id") or data.get("id") or data.get("name", "")
    except json.JSONDecodeError:
        pass
    # "job pt-xxx submitted successfully, ..." → second word
    for line in stdout.strip().splitlines():
        line = line.strip()
        if line.startswith("job ") and "submitted" in line:
            parts = line.split()
            if len(parts) >= 2:
                return parts[1]
    for line in stdout.strip().splitlines():
        line = line.strip()
        if line and not line.startswith("+") and not line.startswith("|"):
            parts = line.split()
            if parts:
                return parts[0]
    return stdout.strip().split()[-1] if stdout.strip() else "unknown"
