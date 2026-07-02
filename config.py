"""
SLAIResearch configuration — API keys, model settings, defaults.
All values can be overridden via environment variables.
"""

import os

# ---------------------------------------------------------------------------
# API Keys
# ---------------------------------------------------------------------------

SEMANTIC_SCHOLAR_API_KEY = os.environ.get(
    "SEMANTIC_SCHOLAR_API_KEY",
    "",
)

PAPERREVIEW_EMAIL = os.environ.get(
    "PAPERREVIEW_EMAIL",
    "",
)

PAPERREVIEW_VENUE = os.environ.get(
    "PAPERREVIEW_VENUE",
    "AAAI",
)

TAVILY_API_KEY = os.environ.get(
    "TAVILY_API_KEY",
    "",
)

# ---------------------------------------------------------------------------
# Claude Code (execution tool)
# ---------------------------------------------------------------------------

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "deepseek-v4-pro")
CLAUDE_API_KEY = os.environ.get(
    "CLAUDE_API_KEY",
    "",
)
CLAUDE_BASE_URL = os.environ.get(
    "CLAUDE_BASE_URL",
    "https://api.deepseek.com/anthropic",
)
CLAUDE_CMD = os.environ.get("CLAUDE_CMD", "claude")

# ---------------------------------------------------------------------------
# Accelerator preference (NVIDIA CUDA vs Huawei Ascend NPU)
# ---------------------------------------------------------------------------

# Which accelerator to prefer: "auto" (detect), "cuda" (NVIDIA only),
# "npu" (Ascend only), or "none" (CPU only, skip all accelerator detection).
ACCELERATOR_PREFERENCE = os.environ.get(
    "SLAIRESEARCH_ACCELERATOR", "auto",
).lower()

# When True, NPU is treated as a drop-in replacement for CUDA where possible.
# The accelerator module will set ENABLE_NPU=1 and ASCEND_VISIBLE_DEVICES.
NPU_ENABLED = os.environ.get(
    "SLAIRESEARCH_NPU_ENABLED", "true",
).lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# SCO / SenseCore defaults (user 2chen)
# ---------------------------------------------------------------------------

SCO_WORKSPACE = os.environ.get("SCO_WORKSPACE", "share-space")
SCO_AEC2 = os.environ.get("SCO_AEC2", "share-cluster")
SCO_IMAGE = os.environ.get(
    "SCO_IMAGE",
    "",
)
SCO_WORKER_SPEC = os.environ.get("SCO_WORKER_SPEC", "n6ls.iu.i40.2.16c256g")
SCO_STORAGE_MOUNT = os.environ.get(
    "SCO_STORAGE_MOUNT",
    "",
)
SCO_WORKER_NODES = int(os.environ.get("SCO_WORKER_NODES", "1"))
SCO_QUOTA_TYPE = os.environ.get("SCO_QUOTA_TYPE", "")
# NOTE: Empty string ("") means the platform uses its default quota type.
# "reserved" caused member_default fallthrough and 4-GPU quota miscounting.
SCO_PRIORITY = os.environ.get("SCO_PRIORITY", "normal")

# GPU count -> worker spec mapping. Each entry overridable via env.
# Spec format: {machine}.{type}.{variant}.{gpus}.{cpu}c{ram}g
# All specs verified working on share-cluster (2026-06-05).
# NOTE: The .XcYg suffix is REQUIRED — truncated specs cause silent container failures.
SCO_WORKER_SPEC_MAP = {
    1: os.environ.get("SCO_WORKER_SPEC_1GPU", "n6ls.iu.i40.1.8c128g"),
    2: os.environ.get("SCO_WORKER_SPEC_2GPU", "n6ls.iu.i40.2.16c256g"),
    3: os.environ.get("SCO_WORKER_SPEC_3GPU", "n6ls.iu.i40.2.16c256g"),
    4: os.environ.get("SCO_WORKER_SPEC_4GPU", "n6ls.iu.i40.4.32c512g"),
}
# GPU count used when experiment_manifest.json is missing (backward compat).
DEFAULT_GPU_COUNT = int(os.environ.get("SLAIRESEARCH_DEFAULT_GPU_COUNT", "1"))

# ---------------------------------------------------------------------------
# Baseline GitHub cloning (structural reference for LLM experiment design)
# ---------------------------------------------------------------------------

BASELINE_CLONING_ENABLED = os.environ.get(
    "SLAIRESEARCH_BASELINE_CLONING", "true",
).lower() in ("1", "true", "yes")

BASELINE_MAX_REPOS = int(os.environ.get("SLAIRESEARCH_BASELINE_MAX_REPOS", "5"))
BASELINE_CACHE_SIZE_MB = int(os.environ.get("SLAIRESEARCH_BASELINE_CACHE_MB", "500"))
BASELINE_CLONE_TIMEOUT = int(os.environ.get("SLAIRESEARCH_BASELINE_CLONE_TIMEOUT", "60"))

# Comma-separated GitHub mirror URLs (tried in order).  Used as a PREFIX:
#   "https://gitclone.com/github.com" + "/user/repo" = correct URL
#   "https://ghproxy.com/https://github.com" + "/user/repo" = correct URL
# Built-in fallbacks are tried after these.
_BASELINE_GIT_MIRRORS_ENV = os.environ.get("SLAIRESEARCH_GIT_MIRRORS", "")
BASELINE_GIT_MIRRORS = [
    m.strip() for m in _BASELINE_GIT_MIRRORS_ENV.split(",") if m.strip()
] if _BASELINE_GIT_MIRRORS_ENV else [
    # ── 国内常用 GitHub 加速镜像（无代理环境自动启用）──
    "https://gitclone.com/github.com",
    "https://ghproxy.com/https://github.com",
    "https://mirror.ghproxy.com/https://github.com",
    "https://gh.con.sh/https://github.com",
    "https://hub.yzuu.cf/https://github.com",
    "https://gh.api.99988866.xyz/https://github.com",
    "https://kgithub.com",
    "https://git.homegu.com",
]

# When true, try mirrors BEFORE direct GitHub clone (for networks where
# direct access is blocked, e.g. NPU/昇腾 environments without proxy).
BASELINE_MIRROR_FIRST = os.environ.get(
    "SLAIRESEARCH_GIT_MIRROR_FIRST", "",
).lower() in ("1", "true", "yes") or os.environ.get(
    "SLAIRESEARCH_NPU_ENABLED", "true",
).lower() in ("1", "true", "yes")

GITHUB_API_TOKEN = os.environ.get("GITHUB_API_TOKEN", "")

# ---------------------------------------------------------------------------
# Pipeline tuning
# ---------------------------------------------------------------------------

MAX_ITERATIONS = int(os.environ.get("SLAIRESEARCH_MAX_ITERATIONS", "10"))
POLL_INITIAL_WAIT = int(os.environ.get("SLAIRESEARCH_POLL_INITIAL_WAIT", "300"))
POLL_INTERVAL = int(os.environ.get("SLAIRESEARCH_POLL_INTERVAL", "60"))
POLL_MAX_WAIT = int(os.environ.get("SLAIRESEARCH_POLL_MAX_WAIT", "7200"))
TARGET_VERDICT = os.environ.get("SLAIRESEARCH_TARGET_VERDICT", "weak accept")

# ---------------------------------------------------------------------------
# Hypothesis generation (ReAct-based research gap finding)
# ---------------------------------------------------------------------------

HYPOTHESIS_MAX_REACT_ROUNDS = int(os.environ.get("SLAIRESEARCH_HYPOTHESIS_MAX_ROUNDS", "3"))
HYPOTHESIS_TOP_K_PDFS = int(os.environ.get("SLAIRESEARCH_HYPOTHESIS_TOP_K_PDFS", "5"))
HYPOTHESIS_MAX_PAPERS = int(os.environ.get("SLAIRESEARCH_HYPOTHESIS_MAX_PAPERS", "50"))

# ---------------------------------------------------------------------------
# Local vs SCO execution
# ---------------------------------------------------------------------------

LOCAL_EXECUTION_TIMEOUT = int(os.environ.get("SLAIRESEARCH_LOCAL_TIMEOUT", "7200"))
LOCAL_EXECUTION_MAX_RETRIES = int(os.environ.get("SLAIRESEARCH_LOCAL_MAX_RETRIES", "20"))
FORCE_SCO = os.environ.get("SLAIRESEARCH_FORCE_SCO", "").lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Stage-level review (per-stage approval gate)
# ---------------------------------------------------------------------------

# Enable/disable per-stage review.  When enabled, every pipeline stage output
# is reviewed by an LLM (or human) before the pipeline proceeds to the next
# stage.  Review failures trigger re-execution of that stage with feedback.
STAGE_REVIEW_ENABLED = os.environ.get("SLAIRESEARCH_STAGE_REVIEW", "true").lower() in ("1", "true", "yes")

# Maximum retries per stage when review fails.  After this many failures the
# pipeline proceeds anyway (with a warning).
STAGE_REVIEW_MAX_RETRIES = int(os.environ.get("SLAIRESEARCH_STAGE_REVIEW_MAX_RETRIES", "10"))

# Review mode: "llm" (default) or "human".  "llm" uses claude -p to simulate
# a human reviewer.  "human" prompts the user in the terminal.
STAGE_REVIEW_MODE = os.environ.get("SLAIRESEARCH_STAGE_REVIEW_MODE", "llm")

# Model to use for stage review (empty = use CLAUDE_MODEL default)
STAGE_REVIEW_MODEL = os.environ.get("SLAIRESEARCH_STAGE_REVIEW_MODEL", "")

# Per-stage review pass thresholds (can be overridden via env).
# The stage reviewer assigns a score 1-10; score >= threshold = pass.
STAGE_REVIEW_THRESHOLDS = {
    "literature_search": float(os.environ.get("SLAIRESEARCH_THRESHOLD_LITERATURE", "6.0")),
    "hypothesis_generation": float(os.environ.get("SLAIRESEARCH_THRESHOLD_HYPOTHESIS", "7.0")),
    "experiment_design": float(os.environ.get("SLAIRESEARCH_THRESHOLD_DESIGN", "6.5")),
    "experiment_execution": float(os.environ.get("SLAIRESEARCH_THRESHOLD_EXECUTION", "5.5")),
    "paper_writing": float(os.environ.get("SLAIRESEARCH_THRESHOLD_WRITING", "6.0")),
    "paper_revision": float(os.environ.get("SLAIRESEARCH_THRESHOLD_REVISION", "6.0")),
}

# GPU hours budget per single SCO task. Tasks exceeding this are rejected at
# submission time with a clear error message.
# Can be overridden via SLAIRESEARCH_MAX_GPU_HOURS env var.
MAX_COMPUTE_BUDGET_GPU_HOURS = int(os.environ.get("SLAIRESEARCH_MAX_GPU_HOURS", "32"))

# ---------------------------------------------------------------------------
# Revision engine (per-section revision loop with backpressure)
# ---------------------------------------------------------------------------

# Enable/disable the revision engine.  When enabled, the revise stage uses
# per-section LLM revision with backpressure, convergence detection, and
# meta-refine instead of a single Claude Code call.
REVISION_ENGINE_ENABLED = os.environ.get(
    "SLAIRESEARCH_REVISION_ENGINE", "true",
).lower() in ("1", "true", "yes")

# Maximum revision rounds in the per-section loop.
REVISION_MAX_ROUNDS = int(os.environ.get(
    "SLAIRESEARCH_REVISION_MAX_ROUNDS", "3",
))

# Only sections scoring below this threshold get revised.
REVISION_MIN_SECTION_SCORE = int(os.environ.get(
    "SLAIRESEARCH_REVISION_MIN_SCORE", "6",
))

# Stop if average score improves by less than this in a round.
REVISION_CONVERGENCE_THRESHOLD = float(os.environ.get(
    "SLAIRESEARCH_REVISION_CONVERGENCE", "0.3",
))

# ---------------------------------------------------------------------------
# Grounding protection (preserve real experiment results)
# ---------------------------------------------------------------------------

# Enable/disable grounding protection.  When enabled, after paper writing
# the system replaces LLM-generated result tables with artifact-grounded
# versions from actual experiment runs, and removes LLM-hallucinated tables.
GROUNDING_PROTECTION_ENABLED = os.environ.get(
    "SLAIRESEARCH_GROUNDING_PROTECTION", "true",
).lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Model / dataset download (three-layer fallback: direct → mirror → VPN)
# ---------------------------------------------------------------------------

HF_TOKEN = os.environ.get("HF_TOKEN", "")
HF_ENDPOINT = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")

# ── Download cache ──
# All downloaded resources (models, datasets, wheels) go here.
# Structure:  cache/models/  cache/datasets/  cache/wheels/
DOWNLOAD_CACHE_DIR = os.environ.get(
    "SLAIRESEARCH_DOWNLOAD_CACHE",
    os.path.join(os.path.dirname(__file__), "workspace", ".shared", "cache"),
)
DOWNLOAD_MODELS_DIR = os.path.join(DOWNLOAD_CACHE_DIR, "models")
DOWNLOAD_DATASETS_DIR = os.path.join(DOWNLOAD_CACHE_DIR, "datasets")
DOWNLOAD_WHEELS_DIR = os.path.join(DOWNLOAD_CACHE_DIR, "wheels")
DOWNLOAD_TIMEOUT = int(os.environ.get("SLAIRESEARCH_DOWNLOAD_TIMEOUT", "300"))
DOWNLOAD_RETRY_COUNT = int(os.environ.get("SLAIRESEARCH_DOWNLOAD_RETRIES", "2"))

PROXY_URL = os.environ.get("SLAIRESEARCH_PROXY_URL", "http://127.0.0.1:7890")

# ---------------------------------------------------------------------------
# Citation tools — CrossRef / Google Scholar / DataCite (all FREE, no API keys)
# ---------------------------------------------------------------------------

CROSSREF_RATE_LIMIT_DELAY = float(os.environ.get(
    "CROSSREF_RATE_LIMIT_DELAY", "0.1",
))
"""Delay between CrossRef API requests (seconds).  0.1 = 10 req/s is well
within the polite-use limit."""

SCHOLARLY_MAX_RESULTS = int(os.environ.get(
    "SLAIRESEARCH_SCHOLARLY_MAX_RESULTS", "50",
))
"""Max results from Google Scholar per query.  scholarly scrapes Google Scholar
and rate-limiting is aggressive — keep this moderate."""

DATACITE_RATE_LIMIT_DELAY = float(os.environ.get(
    "DATACITE_RATE_LIMIT_DELAY", "0.2",
))
"""Delay between DataCite API requests (seconds).  DataCite has no stated hard
limit for unauthenticated access; 0.2 s is polite."""

# ---------------------------------------------------------------------------
# Experiment scientist — Claude Code prompt-driven experiment execution
# ---------------------------------------------------------------------------

EXPERIMENT_SYSTEM_PROMPT = os.environ.get(
    "SLAIRESEARCH_EXPERIMENT_SYSTEM_PROMPT",
    os.path.join(os.path.dirname(__file__), "prompts", "experiment_scientist_system.md"),
)
"""Path to the experiment scientist system prompt.  Claude Code loads this
via ``--system-prompt`` when taking over the experiment phase."""

EXPERIMENT_TASK_TEMPLATE = os.environ.get(
    "SLAIRESEARCH_EXPERIMENT_TASK_TEMPLATE",
    os.path.join(os.path.dirname(__file__), "prompts", "experiment_scientist_task.md"),
)
"""Path to the per-task prompt template (``${VAR}``-style placeholders)."""

EXPERIMENT_MAX_DEBUG_ROUNDS = int(os.environ.get(
    "SLAIRESEARCH_EXPERIMENT_MAX_DEBUG_ROUNDS", "20",
))
"""Maximum auto-debug rounds when an experiment fails.  After this many rounds
the experiment is marked as failed and the controller moves on."""

EXPERIMENT_CLAUDE_TIMEOUT = int(os.environ.get(
    "SLAIRESEARCH_EXPERIMENT_CLAUDE_TIMEOUT", "3600",
))
"""Maximum time (seconds) a single Claude Code experiment session can run
before the controller times it out."""
