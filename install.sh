#!/usr/bin/env bash
# =============================================================================
# ChenResearch — 全自动科研系统 安装脚本
# =============================================================================
# 一键安装：Python 依赖 + Claude Code + LaTeX 编译环境
# -----------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "ChenResearch 安装脚本"
echo "项目路径: ${SCRIPT_DIR}"
echo ""

# ---------------------------------------------------------------------------
# 1. Python 依赖
# ---------------------------------------------------------------------------
echo "[1/4] 安装 Python 依赖 …"
pip install requests -q

# ---------------------------------------------------------------------------
# 2. Claude Code (作为项目的执行工具)
# ---------------------------------------------------------------------------
echo "[2/4] 配置 Claude Code …"

if ! command -v claude &>/dev/null; then
    echo "  安装 Claude Code CLI …"
    npm install -g @anthropic-ai/claude-code 2>/dev/null || \
    curl -fsSL https://claude.ai/install.sh | bash 2>/dev/null || \
    echo "  [WARN] Claude Code 安装失败，请手动安装: https://claude.ai/code"
fi

# 写入 Claude Code 项目级配置（从模板复制，如模板不存在则创建默认配置）
CLAUDE_SETTINGS="${SCRIPT_DIR}/.claude/settings.json"
CLAUDE_TEMPLATE="${SCRIPT_DIR}/.claude/settings.template.json"
mkdir -p "$(dirname "${CLAUDE_SETTINGS}")"

if [[ -f "${CLAUDE_TEMPLATE}" ]]; then
    cp "${CLAUDE_TEMPLATE}" "${CLAUDE_SETTINGS}"
    echo "  从模板复制 Claude Code 配置"
else
    cat > "${CLAUDE_SETTINGS}" <<'CLAUDE_EOF'
{
  "model": "deepseek-v4-pro",
  "env": {
    "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
    "ANTHROPIC_API_KEY": "your-api-key-here"
  },
  "permissions": {
    "allow": [
      "Bash(curl *)",
      "Bash(python *)",
      "Bash(sco *)",
      "Bash(pdflatex *)",
      "Bash(tectonic *)",
      "WebFetch(*)",
      "Read(*)",
      "Write(*)",
      "Edit(*)"
    ]
  }
}
CLAUDE_EOF
    echo "  已创建默认 Claude Code 配置（请填入 API Key）"
fi

echo "  Claude Code 配置已写入: ${CLAUDE_SETTINGS}"

# ---------------------------------------------------------------------------
# 3. LaTeX 编译环境
# ---------------------------------------------------------------------------
echo "[3/4] 检查 LaTeX 编译环境 …"

NEED_TEX=false
if ! command -v pdflatex &>/dev/null && ! command -v tectonic &>/dev/null; then
    NEED_TEX=true
fi

if $NEED_TEX; then
    if command -v apt-get &>/dev/null; then
        echo "  安装 texlive (apt) …"
        sudo apt-get update -qq && sudo apt-get install -y -qq texlive-latex-base texlive-latex-extra 2>/dev/null || \
        echo "  [WARN] texlive 安装失败，可手动安装: apt install texlive-latex-base"
    elif command -v brew &>/dev/null; then
        echo "  安装 tectonic (brew) …"
        brew install tectonic 2>/dev/null || \
        echo "  [WARN] tectonic 安装失败，可手动安装: brew install tectonic"
    else
        echo "  [WARN] 未检测到 apt-get 或 brew，请手动安装 LaTeX 编译器"
    fi
else
    echo "  LaTeX 已可用"
fi

# ---------------------------------------------------------------------------
# 4. 验证
# ---------------------------------------------------------------------------
echo "[4/4] 验证安装 …"

# Python
python -c "import requests; print('  requests: OK')"

# 配置
python -c "from config import SEMANTIC_SCHOLAR_API_KEY, CLAUDE_API_KEY; print(f'  S2 API Key: {\"***\" + SEMANTIC_SCHOLAR_API_KEY[-4:]}'); print(f'  Claude API Key: {\"***\" + CLAUDE_API_KEY[-4:]}')" 2>/dev/null || \
python3 -c "import sys; sys.path.insert(0, '${SCRIPT_DIR}'); from config import SEMANTIC_SCHOLAR_API_KEY, CLAUDE_API_KEY; print(f'  S2 API Key: {\"***\" + SEMANTIC_SCHOLAR_API_KEY[-4:]}'); print(f'  Claude API Key: {\"***\" + CLAUDE_API_KEY[-4:]}')"

# Claude Code
if command -v claude &>/dev/null; then
    echo "  Claude Code: $(claude --version 2>/dev/null || echo 'installed')"
else
    echo "  [WARN] Claude Code CLI 未安装 — 请手动执行: npm install -g @anthropic-ai/claude-code"
fi

# LaTeX
if command -v pdflatex &>/dev/null; then
    echo "  pdflatex: $(pdflatex --version 2>/dev/null | head -1 || echo 'installed')"
elif command -v tectonic &>/dev/null; then
    echo "  tectonic: $(tectonic --version 2>/dev/null | head -1 || echo 'installed')"
else
    echo "  [WARN] LaTeX 编译器未安装 — PDF 编译功能不可用"
fi

echo ""
echo "=============================="
echo "  ChenResearch 安装完成"
echo "=============================="
echo ""
echo "使用方法:"
echo "  python ${SCRIPT_DIR}/chenresearch.py run \"你的研究主题\""
echo "  python ${SCRIPT_DIR}/chenresearch.py status"
echo "  python ${SCRIPT_DIR}/chenresearch.py resume <topic_slug>"
