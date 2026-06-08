"""
ChenResearch configuration — API keys, model settings, defaults.
All values can be overridden via environment variables.
"""

import os

# ---------------------------------------------------------------------------
# API Keys
# ---------------------------------------------------------------------------

SEMANTIC_SCHOLAR_API_KEY = os.environ.get(
    "SEMANTIC_SCHOLAR_API_KEY",
    "s2k-TxOJNhO0O615j3huoEbRfhfIUfnzoXLE2V9ZfEaq",
)

PAPERREVIEW_EMAIL = os.environ.get(
    "PAPERREVIEW_EMAIL",
    "250010008@slai.edu.cn",
)

PAPERREVIEW_VENUE = os.environ.get(
    "PAPERREVIEW_VENUE",
    "AAAI",
)

# ---------------------------------------------------------------------------
# Claude Code (execution tool)
# ---------------------------------------------------------------------------

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "deepseek-v4-pro")
CLAUDE_API_KEY = os.environ.get(
    "CLAUDE_API_KEY",
    "sk-5d8ed00d568645efb4f6a544160b3849",
)
CLAUDE_BASE_URL = os.environ.get(
    "CLAUDE_BASE_URL",
    "https://api.deepseek.com/anthropic",
)
CLAUDE_CMD = os.environ.get("CLAUDE_CMD", "claude")

# ---------------------------------------------------------------------------
# SCO / SenseCore defaults (user 2chen)
# ---------------------------------------------------------------------------

SCO_WORKSPACE = os.environ.get("SCO_WORKSPACE", "share-space")
SCO_AEC2 = os.environ.get("SCO_AEC2", "share-cluster")
SCO_IMAGE = os.environ.get(
    "SCO_IMAGE",
    "registry.cn-sh-01.sensecore.cn/ccr-zhicheng-02/chen-mirror2:2chen-mini-20260410132739",
)
SCO_WORKER_SPEC = os.environ.get("SCO_WORKER_SPEC", "n6ls.iu.i40.2.16c256g")
SCO_STORAGE_MOUNT = os.environ.get(
    "SCO_STORAGE_MOUNT",
    "01995892-d478-76d8-aec7-13fd8284477e:/data:/250010008",
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
DEFAULT_GPU_COUNT = int(os.environ.get("CHENRESEARCH_DEFAULT_GPU_COUNT", "1"))

# ---------------------------------------------------------------------------
# Baseline GitHub cloning (structural reference for LLM experiment design)
# ---------------------------------------------------------------------------

BASELINE_CLONING_ENABLED = os.environ.get(
    "CHENRESEARCH_BASELINE_CLONING", "true",
).lower() in ("1", "true", "yes")

BASELINE_MAX_REPOS = int(os.environ.get("CHENRESEARCH_BASELINE_MAX_REPOS", "5"))
BASELINE_CACHE_SIZE_MB = int(os.environ.get("CHENRESEARCH_BASELINE_CACHE_MB", "500"))
BASELINE_CLONE_TIMEOUT = int(os.environ.get("CHENRESEARCH_BASELINE_CLONE_TIMEOUT", "60"))

GITHUB_API_TOKEN = os.environ.get("GITHUB_API_TOKEN", "")

# ---------------------------------------------------------------------------
# Pipeline tuning
# ---------------------------------------------------------------------------

MAX_ITERATIONS = int(os.environ.get("CHENRESEARCH_MAX_ITERATIONS", "10"))
POLL_INITIAL_WAIT = int(os.environ.get("CHENRESEARCH_POLL_INITIAL_WAIT", "300"))
POLL_INTERVAL = int(os.environ.get("CHENRESEARCH_POLL_INTERVAL", "60"))
POLL_MAX_WAIT = int(os.environ.get("CHENRESEARCH_POLL_MAX_WAIT", "7200"))
TARGET_VERDICT = os.environ.get("CHENRESEARCH_TARGET_VERDICT", "weak accept")

# ---------------------------------------------------------------------------
# Hypothesis generation (ReAct-based research gap finding)
# ---------------------------------------------------------------------------

HYPOTHESIS_MAX_REACT_ROUNDS = int(os.environ.get("CHENRESEARCH_HYPOTHESIS_MAX_ROUNDS", "3"))
HYPOTHESIS_TOP_K_PDFS = int(os.environ.get("CHENRESEARCH_HYPOTHESIS_TOP_K_PDFS", "5"))
HYPOTHESIS_MAX_PAPERS = int(os.environ.get("CHENRESEARCH_HYPOTHESIS_MAX_PAPERS", "50"))

# ---------------------------------------------------------------------------
# Local vs SCO execution
# ---------------------------------------------------------------------------

LOCAL_EXECUTION_TIMEOUT = int(os.environ.get("CHENRESEARCH_LOCAL_TIMEOUT", "7200"))
LOCAL_EXECUTION_MAX_RETRIES = int(os.environ.get("CHENRESEARCH_LOCAL_MAX_RETRIES", "20"))
FORCE_SCO = os.environ.get("CHENRESEARCH_FORCE_SCO", "").lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Stage-level review (per-stage approval gate)
# ---------------------------------------------------------------------------

# Enable/disable per-stage review.  When enabled, every pipeline stage output
# is reviewed by an LLM (or human) before the pipeline proceeds to the next
# stage.  Review failures trigger re-execution of that stage with feedback.
STAGE_REVIEW_ENABLED = os.environ.get("CHENRESEARCH_STAGE_REVIEW", "true").lower() in ("1", "true", "yes")

# Maximum retries per stage when review fails.  After this many failures the
# pipeline proceeds anyway (with a warning).
STAGE_REVIEW_MAX_RETRIES = int(os.environ.get("CHENRESEARCH_STAGE_REVIEW_MAX_RETRIES", "10"))

# Review mode: "llm" (default) or "human".  "llm" uses claude -p to simulate
# a human reviewer.  "human" prompts the user in the terminal.
STAGE_REVIEW_MODE = os.environ.get("CHENRESEARCH_STAGE_REVIEW_MODE", "llm")

# Model to use for stage review (empty = use CLAUDE_MODEL default)
STAGE_REVIEW_MODEL = os.environ.get("CHENRESEARCH_STAGE_REVIEW_MODEL", "")

# Per-stage review pass thresholds (can be overridden via env).
# The stage reviewer assigns a score 1-10; score >= threshold = pass.
STAGE_REVIEW_THRESHOLDS = {
    "literature_search": float(os.environ.get("CHENRESEARCH_THRESHOLD_LITERATURE", "6.0")),
    "hypothesis_generation": float(os.environ.get("CHENRESEARCH_THRESHOLD_HYPOTHESIS", "7.0")),
    "experiment_design": float(os.environ.get("CHENRESEARCH_THRESHOLD_DESIGN", "6.5")),
    "experiment_execution": float(os.environ.get("CHENRESEARCH_THRESHOLD_EXECUTION", "5.5")),
    "paper_writing": float(os.environ.get("CHENRESEARCH_THRESHOLD_WRITING", "6.0")),
    "paper_revision": float(os.environ.get("CHENRESEARCH_THRESHOLD_REVISION", "6.0")),
}

# GPU hours budget per single SCO task. Tasks exceeding this are rejected at
# submission time with a clear error message.
# Can be overridden via CHENRESEARCH_MAX_GPU_HOURS env var.
MAX_COMPUTE_BUDGET_GPU_HOURS = int(os.environ.get("CHENRESEARCH_MAX_GPU_HOURS", "32"))

# ---------------------------------------------------------------------------
# Revision engine (per-section revision loop with backpressure)
# ---------------------------------------------------------------------------

# Enable/disable the revision engine.  When enabled, the revise stage uses
# per-section LLM revision with backpressure, convergence detection, and
# meta-refine instead of a single Claude Code call.
REVISION_ENGINE_ENABLED = os.environ.get(
    "CHENRESEARCH_REVISION_ENGINE", "true",
).lower() in ("1", "true", "yes")

# Maximum revision rounds in the per-section loop.
REVISION_MAX_ROUNDS = int(os.environ.get(
    "CHENRESEARCH_REVISION_MAX_ROUNDS", "3",
))

# Only sections scoring below this threshold get revised.
REVISION_MIN_SECTION_SCORE = int(os.environ.get(
    "CHENRESEARCH_REVISION_MIN_SCORE", "6",
))

# Stop if average score improves by less than this in a round.
REVISION_CONVERGENCE_THRESHOLD = float(os.environ.get(
    "CHENRESEARCH_REVISION_CONVERGENCE", "0.3",
))

# ---------------------------------------------------------------------------
# Grounding protection (preserve real experiment results)
# ---------------------------------------------------------------------------

# Enable/disable grounding protection.  When enabled, after paper writing
# the system replaces LLM-generated result tables with artifact-grounded
# versions from actual experiment runs, and removes LLM-hallucinated tables.
GROUNDING_PROTECTION_ENABLED = os.environ.get(
    "CHENRESEARCH_GROUNDING_PROTECTION", "true",
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
    "CHENRESEARCH_DOWNLOAD_CACHE",
    os.path.join(os.path.dirname(__file__), "workspace", ".shared", "cache"),
)
DOWNLOAD_MODELS_DIR = os.path.join(DOWNLOAD_CACHE_DIR, "models")
DOWNLOAD_DATASETS_DIR = os.path.join(DOWNLOAD_CACHE_DIR, "datasets")
DOWNLOAD_WHEELS_DIR = os.path.join(DOWNLOAD_CACHE_DIR, "wheels")
DOWNLOAD_TIMEOUT = int(os.environ.get("CHENRESEARCH_DOWNLOAD_TIMEOUT", "300"))
DOWNLOAD_RETRY_COUNT = int(os.environ.get("CHENRESEARCH_DOWNLOAD_RETRIES", "2"))

PROXY_URL = os.environ.get("CHENRESEARCH_PROXY_URL", "http://127.0.0.1:7890")

# ---------------------------------------------------------------------------
# Citation tools — CrossRef / Google Scholar / DataCite (all FREE, no API keys)
# ---------------------------------------------------------------------------

CROSSREF_RATE_LIMIT_DELAY = float(os.environ.get(
    "CROSSREF_RATE_LIMIT_DELAY", "0.1",
))
"""Delay between CrossRef API requests (seconds).  0.1 = 10 req/s is well
within the polite-use limit."""

SCHOLARLY_MAX_RESULTS = int(os.environ.get(
    "CHENRESEARCH_SCHOLARLY_MAX_RESULTS", "50",
))
"""Max results from Google Scholar per query.  scholarly scrapes Google Scholar
and rate-limiting is aggressive — keep this moderate."""

DATACITE_RATE_LIMIT_DELAY = float(os.environ.get(
    "DATACITE_RATE_LIMIT_DELAY", "0.2",
))
"""Delay between DataCite API requests (seconds).  DataCite has no stated hard
limit for unauthenticated access; 0.2 s is polite."""
