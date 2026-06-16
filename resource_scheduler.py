"""
Resource-aware scheduler for SLAIResearch experiments.

Detects whether an experiment is CPU-heavy or GPU-heavy and recommends
appropriate concurrency settings (JOBS_PER_GPU, GPU_COUNT, batch_size).

Key insight from NanoResearch comparison:
  Atari DQN experiments are CPU-bound (emulation + frame preprocessing).
  Running JOBS_PER_GPU=3 causes CPU contention with near-zero GPU utilization.
  This module detects such cases and recommends lower concurrency.

Usage:
  python resource_scheduler.py <experiment_dir>          # print recommendations
  python resource_scheduler.py <experiment_dir> --apply  # write manifest
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# Experiment type classification
# ---------------------------------------------------------------------------

@dataclass
class ResourceProfile:
    """Resource profile for an experiment."""

    # Classification
    experiment_type: str          # "cpu_heavy", "gpu_heavy", "balanced"
    confidence: float              # 0.0 - 1.0

    # Detected characteristics
    uses_replay_buffer: bool = False
    uses_atari_env: bool = False
    uses_transformer: bool = False
    uses_large_batch: bool = False  # batch_size >= 128
    uses_data_augmentation: bool = False

    # Recommendations
    recommended_jobs_per_gpu: int = 1
    recommended_gpu_count: int = 1
    recommended_batch_size_hint: str = ""  # "increase", "decrease", "keep"

    # Raw scores
    cpu_score: int = 0
    gpu_score: int = 0


# Keywords for detection
CPU_HEAVY_PATTERNS = [
    (8, re.compile(r"Atari|ALE|ale_py|gym\.make|gymnasium\.make", re.I)),
    (6, re.compile(r"cv2\.(cvtColor|resize|imread|IMREAD)", re.I)),
    (5, re.compile(r"replay_buffer|ReplayBuffer|prioritized.*replay", re.I)),
    (4, re.compile(r"env\.step|env\.reset", re.I)),
    (3, re.compile(r"frame_stack|frame_skip|sticky_action", re.I)),
    (2, re.compile(r"num_workers\s*=\s*[4-9]|\bDataLoader\b.*num_workers", re.I)),
    (1, re.compile(r"OpenCV|opencv", re.I)),
]

GPU_HEAVY_PATTERNS = [
    (5, re.compile(r"transformer|Transformer|attention.*mechanism|self_attention", re.I)),
    (5, re.compile(r"bert|gpt|llama|vit|resnet(?:50|101|152)|diffusion|unet", re.I)),
    (4, re.compile(r"batch_size\s*=\s*(?:128|256|512|1024)", re.I)),
    (4, re.compile(r"gradient_accumulation|mixed_precision|fp16|amp", re.I)),
    (3, re.compile(r"torch\.cuda\.amp|autocast|GradScaler", re.I)),
    (2, re.compile(r"pretrain|pretrained|finetune|fine_tune", re.I)),
    (2, re.compile(r"distributed|DataParallel|DistributedDataParallel|ddp", re.I)),
]


def analyze_experiment(experiment_dir: Path) -> ResourceProfile:
    """Analyze experiment code and return a resource profile."""
    py_files = list(experiment_dir.rglob("*.py"))
    sh_files = list(experiment_dir.rglob("*.sh"))

    all_code = ""
    for pf in py_files:
        try:
            all_code += pf.read_text(encoding="utf-8") + "\n"
        except Exception:
            pass

    cpu_score = 0
    gpu_score = 0
    matched_cpu: list[str] = []
    matched_gpu: list[str] = []

    for weight, pattern in CPU_HEAVY_PATTERNS:
        count = len(pattern.findall(all_code))
        if count > 0:
            cpu_score += weight * min(count, 3)
            matched_cpu.append(pattern.pattern[:40])

    for weight, pattern in GPU_HEAVY_PATTERNS:
        count = len(pattern.findall(all_code))
        if count > 0:
            gpu_score += weight * min(count, 3)
            matched_gpu.append(pattern.pattern[:40])

    # Determine type
    if cpu_score == 0 and gpu_score == 0:
        exp_type = "balanced"
        confidence = 0.3
    elif cpu_score >= gpu_score * 2:
        exp_type = "cpu_heavy"
        confidence = min(0.95, cpu_score / max(gpu_score, 1) / 8)
    elif gpu_score >= cpu_score * 3:
        exp_type = "gpu_heavy"
        confidence = min(0.95, gpu_score / max(cpu_score, 1) / 10)
    else:
        exp_type = "balanced"
        confidence = 0.6

    # Recommendations
    if exp_type == "cpu_heavy":
        jobs_per_gpu = 1
        gpu_count = _detect_gpu_count()
        batch_hint = "increase (GPU is underutilized, larger batch won't hurt)"
    elif exp_type == "gpu_heavy":
        jobs_per_gpu = 1   # GPU-heavy jobs need full GPU
        gpu_count = _detect_gpu_count()
        batch_hint = "keep or increase with gradient_accumulation"
    else:
        jobs_per_gpu = 2
        gpu_count = _detect_gpu_count()
        batch_hint = "keep"

    return ResourceProfile(
        experiment_type=exp_type,
        confidence=confidence,
        uses_replay_buffer=bool(re.search(r"replay_buffer|ReplayBuffer", all_code)),
        uses_atari_env=bool(re.search(r"Atari|ALE|ale_py", all_code, re.I)),
        uses_transformer=bool(re.search(r"transformer|Transformer", all_code, re.I)),
        uses_large_batch=bool(re.search(r"batch_size\s*=\s*(?:128|256|512)", all_code)),
        uses_data_augmentation=bool(re.search(r"augment|augmentation|random_crop|RandomCrop", all_code)),
        recommended_jobs_per_gpu=jobs_per_gpu,
        recommended_gpu_count=gpu_count,
        recommended_batch_size_hint=batch_hint,
        cpu_score=cpu_score,
        gpu_score=gpu_score,
    )


def _detect_gpu_count() -> int:
    """Detect available GPU count. Returns 1 if undetectable."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return max(1, len(result.stdout.strip().splitlines()))
    except Exception:
        pass
    return 1


# ---------------------------------------------------------------------------
# Manifest integration
# ---------------------------------------------------------------------------

def write_manifest(
    workspace_dir: Path,
    profile: ResourceProfile,
    envs: list[str] | None = None,
    total_steps: int = 100000,
) -> Path:
    """Write or update experiment_manifest.json with resource recommendations."""
    manifest_path = workspace_dir / "experiment_manifest.json"

    existing = {}
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text())
        except Exception:
            pass

    manifest = {
        "experiment_type": profile.experiment_type,
        "resource_profile": {
            "cpu_score": profile.cpu_score,
            "gpu_score": profile.gpu_score,
            "confidence": round(profile.confidence, 2),
            "uses_atari_env": profile.uses_atari_env,
            "uses_replay_buffer": profile.uses_replay_buffer,
            "uses_transformer": profile.uses_transformer,
        },
        # Recommended settings
        "jobs_per_gpu": profile.recommended_jobs_per_gpu,
        "gpu_count": existing.get("gpu_count", profile.recommended_gpu_count),
        "batch_size_hint": profile.recommended_batch_size_hint,
        # Preserve existing fields
        "envs": envs or existing.get("envs", []),
        "total_steps": total_steps,
        "total_jobs_estimate": existing.get("total_jobs_estimate", 0),
        "estimated_wall_time_hours": existing.get("estimated_wall_time_hours", 0),
        "wall_time_note": existing.get("wall_time_note", ""),
        "manifest_generated_by": "resource_scheduler.py",
    }

    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return manifest_path


# ---------------------------------------------------------------------------
# Environment variable generation
# ---------------------------------------------------------------------------

def export_env_vars(profile: ResourceProfile) -> dict[str, str]:
    """Generate environment variables for run_experiment.sh based on profile."""
    return {
        "JOBS_PER_GPU": str(profile.recommended_jobs_per_gpu),
        "GPU_COUNT": str(profile.recommended_gpu_count),
        "SLAIRESEARCH_EXPERIMENT_TYPE": profile.experiment_type,
        "SLAIRESEARCH_CPU_SCORE": str(profile.cpu_score),
        "SLAIRESEARCH_GPU_SCORE": str(profile.gpu_score),
    }


def print_recommendations(profile: ResourceProfile, experiment_dir: Path) -> None:
    """Print human-readable recommendations."""
    print("=" * 60)
    print(" RESOURCE SCHEDULER — Experiment Analysis")
    print("=" * 60)
    print(f" Directory: {experiment_dir}")
    print(f" Type:      {profile.experiment_type} (confidence: {profile.confidence:.0%})")
    print(f" CPU score: {profile.cpu_score}  |  GPU score: {profile.gpu_score}")
    print()
    print(" Detected characteristics:")
    print(f"   Atari environment:     {profile.uses_atari_env}")
    print(f"   Replay buffer:         {profile.uses_replay_buffer}")
    print(f"   Transformer/diffusion: {profile.uses_transformer}")
    print(f"   Large batch (>=128):   {profile.uses_large_batch}")
    print(f"   Data augmentation:     {profile.uses_data_augmentation}")
    print()
    print(" Recommendations:")
    print(f"   JOBS_PER_GPU:  {profile.recommended_jobs_per_gpu}")
    print(f"   GPU_COUNT:     {profile.recommended_gpu_count}")
    print(f"   Batch size:    {profile.recommended_batch_size_hint}")
    print()

    if profile.experiment_type == "cpu_heavy":
        print(" WARNING: This is a CPU-heavy experiment.")
        print(" Running too many concurrent jobs per GPU will cause CPU")
        print(" contention in the Atari emulator / data loader and leave")
        print(" the GPU nearly idle.  JOBS_PER_GPU=1 is recommended.")
        print()
        print(" To override (not recommended):")
        print("   export JOBS_PER_GPU=3")
    elif profile.experiment_type == "gpu_heavy":
        print(" This is a GPU-heavy experiment. Each job will saturate")
        print(" the GPU.  JOBS_PER_GPU=1 is appropriate.")
    print("=" * 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="Resource-aware experiment scheduler"
    )
    parser.add_argument(
        "experiment_dir", type=str,
        help="Path to experiment directory"
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Write recommendations to experiment_manifest.json"
    )
    parser.add_argument(
        "--env", action="store_true", dest="env_only",
        help="Output as shell-exportable env vars"
    )
    parser.add_argument(
        "--gpu-count", type=int, default=0,
        help="Override GPU count (0 = auto-detect)"
    )
    args = parser.parse_args()

    exp_dir = Path(args.experiment_dir)
    if not exp_dir.exists():
        print(f"ERROR: Directory not found: {exp_dir}", file=sys.stderr)
        sys.exit(1)

    profile = analyze_experiment(exp_dir)

    if args.gpu_count > 0:
        profile.recommended_gpu_count = args.gpu_count

    if args.env_only:
        for k, v in export_env_vars(profile).items():
            print(f"export {k}={v}")
    elif args.apply:
        workspace = exp_dir.parent
        manifest = write_manifest(workspace, profile)
        print(f"Manifest written: {manifest}")
        print_recommendations(profile, exp_dir)
    else:
        print_recommendations(profile, exp_dir)


if __name__ == "__main__":
    main()
