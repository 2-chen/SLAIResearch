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
SCO_WORKER_SPEC = os.environ.get("SCO_WORKER_SPEC", "n6ls.iu.i40.4.32c512g")
SCO_STORAGE_MOUNT = os.environ.get(
    "SCO_STORAGE_MOUNT",
    "01995892-d478-76d8-aec7-13fd8284477e:/data:/250010008",
)
SCO_WORKER_NODES = int(os.environ.get("SCO_WORKER_NODES", "1"))
SCO_QUOTA_TYPE = os.environ.get("SCO_QUOTA_TYPE", "reserved")
SCO_PRIORITY = os.environ.get("SCO_PRIORITY", "normal")

# ---------------------------------------------------------------------------
# Pipeline tuning
# ---------------------------------------------------------------------------

MAX_ITERATIONS = int(os.environ.get("CHENRESEARCH_MAX_ITERATIONS", "10"))
POLL_INITIAL_WAIT = int(os.environ.get("CHENRESEARCH_POLL_INITIAL_WAIT", "300"))
POLL_INTERVAL = int(os.environ.get("CHENRESEARCH_POLL_INTERVAL", "60"))
POLL_MAX_WAIT = int(os.environ.get("CHENRESEARCH_POLL_MAX_WAIT", "7200"))
TARGET_VERDICT = os.environ.get("CHENRESEARCH_TARGET_VERDICT", "weak accept")
