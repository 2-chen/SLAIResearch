"""
SCO CLI wrapper + local execution for ChenResearch experiment execution.
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
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

from config import (
    SCO_WORKSPACE, SCO_AEC2, SCO_IMAGE,
    SCO_WORKER_SPEC, SCO_STORAGE_MOUNT, SCO_WORKER_NODES,
    SCO_QUOTA_TYPE, SCO_PRIORITY,
    SCO_WORKER_SPEC_MAP, DEFAULT_GPU_COUNT,
    MAX_COMPUTE_BUDGET_GPU_HOURS,
)

logger = logging.getLogger(__name__)

JOB_STATE_TERMINAL = {"SUCCEEDED", "FAILED", "STOPPED", "CANCELLED"}

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


def resolve_worker_spec(gpu_count: int) -> str:
    """Map a GPU count to the best matching SCO worker spec.

    Strategy:
      1. Exact match in SCO_WORKER_SPEC_MAP.
      2. Smallest spec with >= gpu_count GPUs.
      3. Largest available spec if request exceeds all.
      4. Fallback to SCO_WORKER_SPEC.
    """
    if not SCO_WORKER_SPEC_MAP:
        logger.warning("SCO_WORKER_SPEC_MAP is empty, using default: %s", SCO_WORKER_SPEC)
        return SCO_WORKER_SPEC

    if gpu_count in SCO_WORKER_SPEC_MAP:
        spec = SCO_WORKER_SPEC_MAP[gpu_count]
        logger.info("Resolved %d GPU(s) -> exact match: %s", gpu_count, spec)
        return spec

    available = sorted(SCO_WORKER_SPEC_MAP.keys())
    for count in available:
        if count >= gpu_count:
            spec = SCO_WORKER_SPEC_MAP[count]
            logger.info("Resolved %d GPU(s) -> next available (%d GPUs): %s",
                        gpu_count, count, spec)
            return spec

    largest = available[-1]
    spec = SCO_WORKER_SPEC_MAP[largest]
    logger.warning("Requested %d GPU(s) but max available is %d, using: %s",
                   gpu_count, largest, spec)
    return spec


def _auto_detect_config(script_path: str | Path) -> tuple[dict[str, str], "SCOConfig"]:
    """Auto-detect GPU requirements from experiment manifest.

    Reads experiment_manifest.json in the script's parent directory.
    Returns (extra_env, sco_config) — always non-None.
    extra_env includes GPU_COUNT. sco_config has the resolved worker_spec.
    """
    exp_dir = Path(script_path).parent
    gpu_count = parse_gpu_requirement(exp_dir) or DEFAULT_GPU_COUNT
    extra_env = {
        "GPU_COUNT": str(gpu_count),
        "CHENRESEARCH": "1",
    }
    resolved_spec = resolve_worker_spec(gpu_count)
    sco_config = SCOConfig(worker_spec=resolved_spec)
    logger.info("Auto-detected config: GPU_COUNT=%d, worker_spec=%s", gpu_count, resolved_spec)
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
    elif has_inference_parallel and has_bg_processes:
        score = "partial"
    elif has_inference_parallel or has_bg_processes or has_multi_gpu_shell:
        score = "partial"
        issues.append(
            f"Certification/inference uses multi-GPU but training appears serial. "
            f"Training time will dominate with only 1/{gpu_count} GPUs active."
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

    # 1. GPU utilization check (soft — warn but don't block)
    util = check_gpu_utilization(exp_dir, gpu_count)
    if util["score"] == "poor":
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
        logger.error("Set CHENRESEARCH_MAX_GPU_HOURS to override (not recommended).")
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
    env["CHENRESEARCH_LOCAL"] = "1"

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
    job_name: str = "chenresearch",
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


def _ensure_wheels() -> bool:
    """Ensure shared wheel cache is populated. Returns True if wheels are ready.

    Checks /data/AutoResearch/ChenResearch/workspace/.shared/wheels/ —
    if empty, runs the download script (which auto-detects VPN if needed).
    This is called before SCO submission so containers install offline.
    """
    shared_wheels = Path("/data/AutoResearch/ChenResearch/workspace/.shared/wheels")
    download_script = Path("/data/AutoResearch/ChenResearch/workspace/.shared/download_wheels.sh")

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

    # ── Pre-submission: ensure wheels are cached ──
    _ensure_wheels()

    try:
        job = submit_job(script_path, job_name, extra_env=extra_env or {}, config=sco_config)
        logger.info("SCO job submitted: %s", job.job_id)

        # Save job_id immediately
        id_file = log_dir / "sco_job_id.txt"
        id_file.write_text(job.job_id)

        # Wait (longer timeout for SCO)
        job = wait_for_job(job.job_id, poll_interval=120, max_wait=43200, config=sco_config)
        sco_log = log_dir / "sco_logs.txt"
        stream_logs(job.job_id, sco_log, config=sco_config)

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
    except Exception as e:
        logger.exception("SCO execution failed")
        return ExperimentResult(
            backend="sco", success=False,
            error_summary=f"SCO error: {e}",
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
        "--quota-type", cfg.quota_type,
        "--storage-mount", cfg.storage_mount,
        "--command", command,
    ]

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
    result = subprocess.run(
        ["sco", "acp", "jobs", "stream-logs", "--workspace-name", cfg.workspace, job_id],
        capture_output=True, text=True, timeout=120,
    )
    logs = result.stdout
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

AFS_BASE = "/data/250010008/chenresearch"


def _build_remote_command(work_dir: str, script_name: str, extra_env: dict[str, str]) -> str:
    """
    规范格式（参考 sco-skill）：cd 到工作目录，执行指定脚本。
    不硬编码文件名 — 由调用方传入。
    """
    import shlex
    lines = ["set -euo pipefail"]
    lines.append(f"cd {shlex.quote(work_dir)}")
    for k, v in (extra_env or {}).items():
        lines.append(f"export {shlex.quote(k)}={shlex.quote(v)}")
    lines.append(f"bash {shlex.quote(script_name)}")
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
