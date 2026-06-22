"""
Unified accelerator abstraction layer for SLAIResearch.

Provides a single interface for detecting and working with different
hardware accelerators (NVIDIA CUDA, Huawei Ascend NPU, CPU fallback).

All GPU/CUDA-specific code paths remain intact as the primary/default
path. NPU support is added as an alternative, gated by the
``SLAIRESEARCH_ACCELERATOR`` environment variable.

Usage::

    from accelerator import detect_accelerator, get_accelerator_info

    info = detect_accelerator()
    print(info.accelerator_type)   # "cuda", "npu", or "none"
    print(info.device_count)       # number of devices
    print(info.is_available)       # True if any accelerator found

The legacy ``detect_gpu()`` function in ``sco_runner.py`` continues to
work unchanged — it is NOT modified by this module.  This module provides
*new* functions that NPU-aware code should use instead.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment-driven accelerator preference
# ---------------------------------------------------------------------------

_ACCELERATOR_ENV = os.environ.get("SLAIRESEARCH_ACCELERATOR", "auto").lower()
# Valid values: "auto" (detect), "cuda" (NVIDIA only), "npu" (Ascend only),
#               "none" (CPU only, skip all accelerator detection)


def _accelerator_preference() -> str:
    """Return the user's accelerator preference: auto, cuda, npu, or none."""
    return _ACCELERATOR_ENV


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class AcceleratorInfo:
    """Information about available hardware accelerators."""

    # Primary accelerator type: "cuda", "npu", or "none"
    accelerator_type: str = "none"

    # Is any accelerator available?
    is_available: bool = False

    # Device counts per type
    cuda_count: int = 0
    npu_count: int = 0

    # Detailed device info
    cuda_devices: list[dict[str, str]] = field(default_factory=list)
    npu_devices: list[dict[str, str]] = field(default_factory=list)

    # Combined device count (sum of all available)
    device_count: int = 0

    # Which type was actually detected (used when preference is "auto")
    detected_type: str = "none"

    # Messages / warnings generated during detection
    messages: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    # Whether this is a "compatible" environment for CUDA workloads
    # (True if CUDA is available OR if NPU can serve as a drop-in)
    supports_cuda_api: bool = False


# ---------------------------------------------------------------------------
# CUDA / NVIDIA detection
# ---------------------------------------------------------------------------


def _detect_cuda() -> tuple[int, list[dict[str, str]], list[str]]:
    """Detect NVIDIA CUDA GPUs. Returns (count, devices, messages)."""
    devices: list[dict[str, str]] = []
    messages: list[str] = []

    # Method 1: nvidia-smi
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().split("\n"):
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 2:
                    devices.append({"name": parts[0], "memory": parts[1]})
            if devices:
                messages.append(
                    f"CUDA detected via nvidia-smi: {len(devices)} device(s) — "
                    + ", ".join(d["name"] for d in devices)
                )
                return len(devices), devices, messages
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Method 2: CUDA_VISIBLE_DEVICES
    cvd = os.environ.get("CUDA_VISIBLE_DEVICES", "")
    if cvd and cvd.strip():
        count = len([x for x in cvd.split(",") if x.strip()])
        messages.append(f"CUDA_VISIBLE_DEVICES={cvd} (no nvidia-smi, but env set)")
        return count, [], messages

    # Method 3: torch.cuda
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import torch; print(torch.cuda.device_count())"],
            capture_output=True, text=True, timeout=15,
        )
        count = int(result.stdout.strip())
        if count > 0:
            messages.append(f"CUDA detected via torch.cuda: {count} device(s)")
            return count, [], messages
    except Exception:
        pass

    return 0, [], messages


# ---------------------------------------------------------------------------
# NPU / Huawei Ascend detection
# ---------------------------------------------------------------------------


def _detect_npu() -> tuple[int, list[dict[str, str]], list[str], list[str]]:
    """Detect Huawei Ascend NPUs. Returns (count, devices, messages, errors).

    Detection methods (in order):
      1. npu-smi info (Huawei's nvidia-smi equivalent)
      2. ASCEND_VISIBLE_DEVICES env var
      3. torch.npu via torch_npu package
    """
    devices: list[dict[str, str]] = []
    messages: list[str] = []
    errors: list[str] = []

    # Method 1: npu-smi info
    try:
        result = subprocess.run(
            ["npu-smi", "info", "-t", "board", "-c", "chip Name,Memory Total"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            # Parse npu-smi output (format varies by driver version)
            for line in result.stdout.strip().split("\n"):
                if line.strip() and not line.startswith("=") and not line.startswith("+-"):
                    parts = [p.strip() for p in re.split(r'\s{2,}|\t', line)]
                    if len(parts) >= 2 and any(c.isdigit() for c in parts[0]):
                        devices.append({"name": parts[1] if len(parts) > 1 else "Ascend NPU",
                                        "memory": parts[-1] if parts else "unknown"})
            if devices:
                messages.append(
                    f"NPU detected via npu-smi: {len(devices)} device(s)"
                )
                return len(devices), devices, messages, errors
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Method 1b: npu-smi info -m (alternative format, older drivers)
    if not devices:
        try:
            result = subprocess.run(
                ["npu-smi", "info", "-m"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                # Count NPU chip lines
                npu_lines = [l for l in result.stdout.strip().split("\n")
                           if "NPU" in l.upper() or "Ascend" in l]
                if npu_lines:
                    count = len(npu_lines)
                    for _ in range(count):
                        devices.append({"name": "Ascend NPU", "memory": "unknown"})
                    messages.append(
                        f"NPU detected via npu-smi -m: {count} device(s)"
                    )
                    return count, devices, messages, errors
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    # Method 2: ASCEND_VISIBLE_DEVICES
    avd = os.environ.get("ASCEND_VISIBLE_DEVICES", "")
    if avd and avd.strip():
        count = len([x for x in avd.split(",") if x.strip()])
        messages.append(f"ASCEND_VISIBLE_DEVICES={avd} (no npu-smi, but env set)")
        return count, [], messages, errors

    # Method 3: torch.npu via torch_npu
    try:
        result = subprocess.run(
            [sys.executable, "-c",
             "import torch; import torch_npu; "
             "print(torch.npu.device_count())"],
            capture_output=True, text=True, timeout=15,
        )
        count = int(result.stdout.strip())
        if count > 0:
            messages.append(f"NPU detected via torch.npu: {count} device(s)")
            return count, [], messages, errors
    except Exception:
        pass

    # Method 4: Check if torch_npu can be imported (no devices but package exists)
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import torch_npu; print('ok')"],
            capture_output=True, text=True, timeout=10,
        )
        if result.stdout.strip() == "ok":
            messages.append(
                "torch_npu package is installed but no NPU devices detected. "
                "If running in a container, ensure ASCEND_VISIBLE_DEVICES is set."
            )
    except Exception:
        pass

    return 0, [], messages, errors


# ---------------------------------------------------------------------------
# Main detection entry point
# ---------------------------------------------------------------------------


def detect_accelerator() -> AcceleratorInfo:
    """Detect available hardware accelerators (CUDA + NPU).

    Respects ``SLAIRESEARCH_ACCELERATOR`` environment variable to filter
    which types to detect.  Returns a unified ``AcceleratorInfo``.

    Detection order:
      1. CUDA (nvidia-smi → CUDA_VISIBLE_DEVICES → torch.cuda)
      2. NPU  (npu-smi → ASCEND_VISIBLE_DEVICES → torch_npu)
    """
    info = AcceleratorInfo()
    preference = _accelerator_preference()

    # ── CUDA detection ──
    if preference in ("auto", "cuda"):
        try:
            info.cuda_count, info.cuda_devices, msgs = _detect_cuda()
            info.messages.extend(msgs)
        except Exception as exc:
            info.errors.append(f"CUDA detection error: {exc}")

    # ── NPU detection ──
    if preference in ("auto", "npu"):
        try:
            info.npu_count, info.npu_devices, msgs, errs = _detect_npu()
            info.messages.extend(msgs)
            info.errors.extend(errs)
        except Exception as exc:
            info.errors.append(f"NPU detection error: {exc}")

    # ── Determine primary type ──
    if preference == "cuda":
        info.accelerator_type = "cuda" if info.cuda_count > 0 else "none"
        info.detected_type = info.accelerator_type
    elif preference == "npu":
        info.accelerator_type = "npu" if info.npu_count > 0 else "none"
        info.detected_type = info.accelerator_type
    elif preference == "none":
        info.accelerator_type = "none"
        info.detected_type = "none"
    else:  # auto
        if info.cuda_count > 0:
            info.accelerator_type = "cuda"
            info.detected_type = "cuda"
        elif info.npu_count > 0:
            info.accelerator_type = "npu"
            info.detected_type = "npu"
        else:
            info.accelerator_type = "none"
            info.detected_type = "none"

    info.is_available = info.accelerator_type != "none"
    info.device_count = info.cuda_count + info.npu_count
    info.supports_cuda_api = info.cuda_count > 0

    # ── Warnings ──
    if preference == "cuda" and info.cuda_count == 0:
        msg = (
            "SLAIRESEARCH_ACCELERATOR=cuda but no CUDA devices found. "
            "Set SLAIRESEARCH_ACCELERATOR=auto or =npu if using Ascend NPU."
        )
        info.warnings.append(msg)
        logger.warning(msg)

    if preference == "npu" and info.npu_count == 0:
        msg = (
            "SLAIRESEARCH_ACCELERATOR=npu but no NPU devices found. "
            "Install torch_npu and Ascend drivers, or set "
            "SLAIRESEARCH_ACCELERATOR=auto."
        )
        info.warnings.append(msg)
        logger.warning(msg)

    if info.cuda_count > 0 and info.npu_count > 0:
        msg = (
            f"Both CUDA ({info.cuda_count}) and NPU ({info.npu_count}) "
            f"devices detected. Using CUDA as primary. "
            f"Set SLAIRESEARCH_ACCELERATOR=npu to prefer Ascend NPU."
        )
        info.messages.append(msg)
        logger.info(msg)

    # ── NPU compatibility warning ──
    if info.accelerator_type == "npu":
        msg = (
            "NPU (Ascend) detected as primary accelerator. "
            "CUDA-specific code (e.g., torch.cuda, nvidia-smi) will NOT work. "
            "SCO cloud GPU submission requires NVIDIA GPUs — local-only "
            "execution will be used. "
            "Set SLAIRESEARCH_ACCELERATOR=cuda to skip NPU detection."
        )
        info.warnings.append(msg)
        logger.warning(msg)

    # ── Log summary ──
    for msg in info.messages:
        logger.info(msg)
    for err in info.errors:
        logger.error(err)

    return info


def get_accelerator_info(cached: bool = True) -> AcceleratorInfo:
    """Get accelerator info, optionally using a cached result.

    Detection runs once and is cached for the process lifetime.
    Pass ``cached=False`` to force re-detection.
    """
    if not cached:
        return detect_accelerator()
    # Module-level cache
    if _CACHED_INFO is None:
        _cached = detect_accelerator()
        globals()["_CACHED_INFO"] = _cached
        return _cached
    return _CACHED_INFO


_CACHED_INFO: AcceleratorInfo | None = None


# ---------------------------------------------------------------------------
# NPU-compatible keyword / heuristic helpers
# ---------------------------------------------------------------------------


# Keywords that indicate NPU usage in experiment code
NPU_KEYWORDS = [
    r'\.npu\(', r'torch\.npu', r'torch_npu',
    r'ascend', r'npu', r'NPU',
    r'ASCEND_VISIBLE_DEVICES',
    r'npu_id', r'per_npu',
]

# Keywords that indicate GPU usage (kept from sco_runner.py for reference)
CUDA_KEYWORDS = [
    r'\.cuda\(', r'\.to\(.*device', r'device.*cuda',
    r'torch\.cuda', r'CUDA', r'gpu',
    r'GPUS', r'n_gpu', r'num_gpus',
    r'DataParallel', r'DistributedDataParallel',
    r'deepspeed', r'FSDP', r'flash_attn',
    r'bf16', r'fp16.*training', r'amp',
]


def needs_accelerator_heuristic(script_path: Path) -> bool:
    """Check if an experiment script likely needs a hardware accelerator.

    Scans for both CUDA and NPU keywords.  More general than the
    original ``needs_gpu_heuristic()`` — use this in NPU-aware code.
    """
    all_keywords = CUDA_KEYWORDS + NPU_KEYWORDS
    try:
        text = script_path.read_text()
        for py_file in sorted(script_path.parent.glob("*.py")):
            try:
                text += "\n" + py_file.read_text()
            except Exception:
                pass

        for kw in all_keywords:
            if re.search(kw, text, re.IGNORECASE):
                logger.info(
                    "Accelerator heuristic: experiment likely needs "
                    "accelerator (matched '%s')", kw,
                )
                return True
    except Exception:
        pass

    logger.info("Accelerator heuristic: experiment appears CPU-compatible")
    return False


# ---------------------------------------------------------------------------
# NPU environment variable builder
# ---------------------------------------------------------------------------


def build_npu_env(accelerator_info: AcceleratorInfo | None = None) -> dict[str, str]:
    """Build environment variables for NPU execution.

    These are injected into experiment execution so the experiment code
    knows it should use NPU instead of CUDA.

    Returns an empty dict if NPU is not available.
    """
    info = accelerator_info or get_accelerator_info()
    if info.accelerator_type != "npu" or info.npu_count == 0:
        return {}

    env: dict[str, str] = {}
    # Tell experiment code to use NPU
    env["SLAIRESEARCH_ACCELERATOR_TYPE"] = "npu"
    env["NPU_COUNT"] = str(info.npu_count)
    # Set Ascend visible devices if not already set
    if not os.environ.get("ASCEND_VISIBLE_DEVICES"):
        env["ASCEND_VISIBLE_DEVICES"] = ",".join(str(i) for i in range(info.npu_count))
    # Flag for torch_npu compatibility
    env["ENABLE_NPU"] = "1"

    logger.info(
        "NPU environment variables prepared: NPU_COUNT=%d, ASCEND_VISIBLE_DEVICES=%s",
        info.npu_count, env.get("ASCEND_VISIBLE_DEVICES", "(not set)"),
    )
    return env


def build_cuda_env(accelerator_info: AcceleratorInfo | None = None) -> dict[str, str]:
    """Build environment variables for CUDA execution.

    Returns an empty dict if CUDA is not available.
    """
    info = accelerator_info or get_accelerator_info()
    if info.cuda_count == 0:
        return {}

    env: dict[str, str] = {}
    env["SLAIRESEARCH_ACCELERATOR_TYPE"] = "cuda"
    env["GPU_COUNT"] = str(info.cuda_count)
    return env


# ---------------------------------------------------------------------------
# NPU compatibility check for SCO cloud submission
# ---------------------------------------------------------------------------


def check_sco_npu_compatibility() -> dict[str, Any]:
    """Check whether the current environment is compatible with SCO cloud GPU.

    SCO (SenseCore Orchestration) cloud platform uses NVIDIA GPUs only.
    If the local environment is NPU-only, SCO submission will fail because
    the container image and worker specs are NVIDIA-specific.

    Returns a dict with:
      - compatible: bool — True if SCO submission is expected to work
      - reason: str — explanation if not compatible
      - recommendation: str — suggested action
    """
    info = get_accelerator_info()

    if info.cuda_count > 0:
        return {
            "compatible": True,
            "reason": f"CUDA available ({info.cuda_count} device(s))",
            "recommendation": "SCO cloud GPU submission is compatible.",
        }

    if info.npu_count > 0:
        return {
            "compatible": False,
            "reason": (
                "Local environment uses Ascend NPU, but SCO cloud platform "
                "requires NVIDIA CUDA GPUs. NPU-to-CUDA cross-compilation "
                "is not supported by the SCO platform."
            ),
            "recommendation": (
                "Use FORCE_LOCAL=1 to run experiments locally on NPU, "
                "or switch to a CUDA-capable environment for SCO submission."
            ),
        }

    return {
        "compatible": True,
        "reason": "No accelerators detected — CPU-only execution",
        "recommendation": (
            "Experiments will run locally on CPU. For GPU-accelerated "
            "experiments, SCO cloud GPU submission requires a CUDA-capable "
            "environment for job submission (the cloud nodes have GPUs)."
        ),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    """Print accelerator detection results."""
    info = detect_accelerator()
    print("=" * 60)
    print(" ACCELERATOR DETECTION")
    print("=" * 60)
    print(f" Preference:      {_accelerator_preference()}")
    print(f" Primary type:    {info.accelerator_type}")
    print(f" Available:       {info.is_available}")
    print(f" CUDA devices:    {info.cuda_count}")
    print(f" NPU devices:     {info.npu_count}")
    print(f" Total devices:   {info.device_count}")
    print(f" Supports CUDA:   {info.supports_cuda_api}")
    print()
    if info.cuda_devices:
        print(" CUDA Device Details:")
        for i, d in enumerate(info.cuda_devices):
            print(f"   [{i}] {d.get('name', 'unknown')} — {d.get('memory', 'unknown')}")
    if info.npu_devices:
        print(" NPU Device Details:")
        for i, d in enumerate(info.npu_devices):
            print(f"   [{i}] {d.get('name', 'unknown')} — {d.get('memory', 'unknown')}")
    if info.messages:
        print("\n Messages:")
        for m in info.messages:
            print(f"   [INFO] {m}")
    if info.warnings:
        print("\n Warnings:")
        for w in info.warnings:
            print(f"   [WARN] {w}")
    if info.errors:
        print("\n Errors:")
        for e in info.errors:
            print(f"   [ERROR] {e}")
    print("=" * 60)

    # SCO compatibility
    sco = check_sco_npu_compatibility()
    print("\n SCO Compatibility:")
    print(f"   Compatible: {sco['compatible']}")
    print(f"   Reason:     {sco['reason']}")
    print(f"   Suggestion: {sco['recommendation']}")
    print("=" * 60)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [accelerator] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    main()
