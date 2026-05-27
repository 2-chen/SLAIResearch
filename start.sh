#!/usr/bin/env bash
# =============================================================================
# ChenResearch — 启动脚本
# 一键启动交互式科研助手。首次运行会引导配置 API Key。
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

echo "╔══════════════════════════════════════════════╗"
echo "║        ChenResearch — 全自动科研系统         ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# ---------------------------------------------------------------------------
# 检查 Python 依赖
# ---------------------------------------------------------------------------
python -c "import requests" 2>/dev/null || {
    echo "[setup] 安装 Python 依赖..."
    pip install requests -q
}

# ---------------------------------------------------------------------------
# 首次运行：配置向导
# ---------------------------------------------------------------------------
if [[ ! -f ".chenresearch_configured" ]]; then
    echo "┌──────────────────────────────────────────────┐"
    echo "│  首次运行 — API Key 配置向导                  │"
    echo "└──────────────────────────────────────────────┘"
    echo ""

    # --- Model selection ---
    echo "选择 AI 模型:"
    echo "  [1] DeepSeek V4 Pro (推荐)"
    echo "  [2] 自定义模型"
    read -rp "请选择 [1]: " MODEL_CHOICE
    MODEL_CHOICE="${MODEL_CHOICE:-1}"

    if [[ "${MODEL_CHOICE}" == "2" ]]; then
        read -rp "模型名称: " CLAUDE_MODEL
        read -rp "API Base URL: " CLAUDE_BASE_URL
        read -rp "API Key: " CLAUDE_API_KEY
    else
        CLAUDE_MODEL="deepseek-v4-pro"
        CLAUDE_BASE_URL="https://api.deepseek.com/anthropic"
        read -rp "DeepSeek API Key [默认: sk-5d8ed00d568645efb4f6a544160b3849]: " CLAUDE_API_KEY
        CLAUDE_API_KEY="${CLAUDE_API_KEY:-sk-5d8ed00d568645efb4f6a544160b3849}"
    fi
    echo ""

    # --- Semantic Scholar key ---
    read -rp "Semantic Scholar API Key [默认: s2k-TxOJNhO0O615j3huoEbRfhfIUfnzoXLE2V9ZfEaq]: " S2_KEY
    S2_KEY="${S2_KEY:-s2k-TxOJNhO0O615j3huoEbRfhfIUfnzoXLE2V9ZfEaq}"

    # --- Email ---
    read -rp "联系邮箱 [默认: 250010008@slai.edu.cn]: " EMAIL
    EMAIL="${EMAIL:-250010008@slai.edu.cn}"

    # --- 写入配置 ---
    cat > .env <<EOF
export CLAUDE_MODEL="${CLAUDE_MODEL}"
export CLAUDE_BASE_URL="${CLAUDE_BASE_URL}"
export CLAUDE_API_KEY="${CLAUDE_API_KEY}"
export SEMANTIC_SCHOLAR_API_KEY="${S2_KEY}"
export PAPERREVIEW_EMAIL="${EMAIL}"
export PAPERREVIEW_VENUE="AAAI"
EOF

    source .env

    # 写入 Claude Code settings
    mkdir -p .claude
    cat > .claude/settings.json <<EOF
{
  "model": "${CLAUDE_MODEL}",
  "env": {
    "ANTHROPIC_BASE_URL": "${CLAUDE_BASE_URL}",
    "ANTHROPIC_API_KEY": "${CLAUDE_API_KEY}"
  },
  "permissions": {
    "allow": [
      "WebSearch(*)",
      "WebFetch(*)",
      "Bash(*)",
      "Read(*)",
      "Write(*)",
      "Edit(*)",
      "NotebookEdit(*)",
      "Task(*)",
      "Agent(*)",
      "Skill(*)",
      "Search(*)",
      "Grep(*)",
      "Glob(*)",
      "List(*)"
    ],
    "deny": []
  }
}
EOF

    touch .chenresearch_configured
    echo ""
    echo "✓ 配置完成！"
    echo ""
fi

# ---------------------------------------------------------------------------
# 加载环境变量
# ---------------------------------------------------------------------------
[[ -f ".env" ]] && source .env || true

# ---------------------------------------------------------------------------
# 检查 Claude Code
# ---------------------------------------------------------------------------
if ! command -v claude &>/dev/null; then
    echo "[setup] Claude Code 未安装，正在安装..."
    npm install -g @anthropic-ai/claude-code 2>/dev/null || {
        echo "请手动安装 Claude Code: https://claude.ai/code"
        exit 1
    }
fi

# ---------------------------------------------------------------------------
# 检查是否有进行中的项目
# ---------------------------------------------------------------------------
HAS_PROJECT=false
for state_dir in state/*/; do
    state_file="${state_dir}state.json"
    if [[ -f "$state_file" ]]; then
        HAS_PROJECT=true
        topic=$(python -c "import json; print(json.load(open('$state_file'))['topic'])" 2>/dev/null || echo "?")
        stage=$(python -c "import json; print(json.load(open('$state_file'))['stage'])" 2>/dev/null || echo "?")
        iteration=$(python -c "import json; print(json.load(open('$state_file'))['iteration'])" 2>/dev/null || echo "0")
        echo "  进行中的项目: ${topic}"
        echo "  当前阶段: ${stage} (第 ${iteration} 轮迭代)"
        echo ""

        # 检查是否有待处理的审稿
        slug=$(basename "$state_dir")
        review_count=$(ls workspace/*/review/review_iter*.md 2>/dev/null | wc -l)

        if [[ "$review_count" -gt 0 ]] && [[ "$stage" == "poll_review" || "$stage" == "revise" ]]; then
            echo "  ⚡ 检测到审稿意见待处理 — 将进入修订模式"
            echo "  Agent 会自动读取审稿意见并开始修订"
        fi
        break
    fi
done

# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
echo ""
echo "  模型: ${CLAUDE_MODEL:-deepseek-v4-pro}"
echo "  会场: ${PAPERREVIEW_VENUE:-AAAI}"
echo ""

if $HAS_PROJECT; then
    echo "  输入 \"继续\" 让 Agent 自动继续上一轮"
    echo "  或输入新的研究主题开始新项目"
else
    echo "  直接输入你的研究主题，例如："
    echo "    \"研究多智能体强化学习在机器人协作中的应用\""
fi
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

exec claude --model "${CLAUDE_MODEL:-deepseek-v4-pro}"
