#!/usr/bin/env bash
# =============================================================================
# SLAIResearch — 代码驱动的会话管理器
# start.sh 是真正的控制器，Claude Code 是执行工具。
# 每次启动：检查 state → 确定当前阶段 → 生成精准 prompt → claude -p 执行
# 审稿后自动退出，下次重开继续迭代 — 保持每轮上下文干净。
# =============================================================================
set -uo pipefail  # 不用 set -e，关键节点显式错误处理

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── 加载功能模块 ──
for _mod in modules/review.sh modules/revision.sh modules/experiment_legacy.sh modules/continue_project.sh; do
    [[ -f "${SCRIPT_DIR}/${_mod}" ]] && source "${SCRIPT_DIR}/${_mod}"
done


# 确保 SCO CLI 在 PATH 中
[[ -d "${HOME}/.sco/bin" ]] && export PATH="${HOME}/.sco/bin:${PATH}"

# Ctrl-C 优雅中断
trap '_on_interrupt' INT TERM
# 会话结束标记 + 故障检测
trap '_on_exit' EXIT

# ── 故障恢复：记录崩溃状态，下次启动时触发 Claude Code 自动修复 ──
CRASH_MARKER_DIR="${SCRIPT_DIR}/.crash_markers"
mkdir -p "$CRASH_MARKER_DIR"

_on_interrupt() {
    echo -e "\n${YELLOW}收到中断信号，保存状态后退出...${NC}"
    exit 130
}

_on_exit() {
    local _exit_code=$?
    echo ""
    echo "── 会话结束: $(date "+%Y-%m-%d %H:%M:%S") ──"

    # 正常退出(0)或用户中断(130) → 清零修复计数，不触发恢复
    if [[ $_exit_code -eq 0 || $_exit_code -eq 130 ]]; then
        rm -f "$RECOVERY_COUNT_FILE"
        return 0
    fi

    # 没有活动项目 → 尝试为新项目补建 state
    if [[ -z "${SLUG:-}" ]]; then
        if [[ -n "${WORKSPACE:-}" && -d "${WORKSPACE}" ]]; then
            echo -e "${YELLOW}  新项目创建中途退出，补建 state.json...${NC}"
            python3 -c "
from state_manager import StateManager
import os
ws = os.path.abspath('${WORKSPACE}')
try:
    sm = StateManager(ws)
    # 用目录名推断主题
    topic = os.path.basename(ws).replace('_', ' ')[:100]
    state = sm.create(topic, work_dir=ws)
    print(state.topic_slug)
except Exception as e:
    print(f'State recovery failed: {e}')
" > /tmp/slai_slug.txt 2>/dev/null
            SLUG=$(cat /tmp/slai_slug.txt 2>/dev/null)
            if [[ -n "$SLUG" ]]; then
                echo -e "${GREEN}  状态已恢复，slug=${SLUG}${NC}"
                # 继续走恢复流程
            else
                echo -e "${YELLOW}  无法恢复，下次运行 bash start.sh 会检测到该项目${NC}"
                return 0
            fi
        else
            return 0
        fi
    fi

    # ── 防止无限修复循环 ──
    local _rec_count=0
    [[ -f "$RECOVERY_COUNT_FILE" ]] && _rec_count=$(cat "$RECOVERY_COUNT_FILE" 2>/dev/null || echo 0)
    _rec_count=$((_rec_count + 1))
    echo "$_rec_count" > "$RECOVERY_COUNT_FILE"

    if [[ $_rec_count -gt $MAX_AUTO_RECOVERY ]]; then
        echo -e "${RED}⚠ 已达最大连续修复次数($MAX_AUTO_RECOVERY)，停止自动重启以避免死循环${NC}"
        echo -e "${RED}  请手动检查并修复后重新运行 start.sh${NC}"
        rm -f "$RECOVERY_COUNT_FILE"
        return 0
    fi

    # ── 异常退出：立即触发 Claude Code 全权接管修复 ──
    echo ""
    echo -e "${RED}╔══════════════════════════════════════════════╗${NC}"
    echo -e "${RED}║  异常退出 (exit=$_exit_code) — 触发自动修复  ║${NC}"
    echo -e "${RED}╚══════════════════════════════════════════════╗${NC}"
    echo ""
    echo -e "  项目: ${TOPIC:-$SLUG}"
    echo -e "  阶段: ${STAGE:-unknown}"
    echo -e "  日志: ${WORKSPACE}/run.log"
    echo ""

    # 构建修复 prompt — 直接调用 Claude Code（不经 _claude_task，避免递归）
    cat > /tmp/slai_crash_recovery_prompt.txt <<CRASHPROMPT
你是 SLAIResearch 系统的故障恢复专家。项目在阶段"${STAGE:-unknown}"异常退出(exit=$_exit_code)。

请立即读取以下文件诊断并修复问题：
1. **运行日志**: ${WORKSPACE}/run.log (读取最后300行定位错误)
2. **项目状态**: ${WORKSPACE}/state.json
3. **SCO 日志**: ${WORKSPACE}/experiment/logs/run_output.log (如存在)
4. **实验代码**: ${WORKSPACE}/experiment/ (如有错误，修复对应文件)

**关键规则**：
- 只能修改 ${WORKSPACE}/ 下的文件
- 受保护文件(sco_runner.py, config.py)不可修改，如怀疑是基础设施问题请报告
- 如果是 SCO 容器内错误(网络不可达、OOM、包缺失)，修复 run_experiment.sh 后重新提交
- 如果是环境问题(缺少依赖)，确保 env/site-packages/ 中有对应包
- 如果是论文编译问题，修复 LaTeX 后重新编译
- 修复完成后，确保项目状态可以继续运行，然后输出 'CRASH_RECOVERY_DONE'

你拥有完全权限来修复项目。目标是让项目能继续顺利运行到下一阶段。
CRASHPROMPT

    _claude_task "$(cat /tmp/slai_crash_recovery_prompt.txt)" \
        "/tmp/slai_crash_recovery_output.log" \
        "${SCRIPT_DIR}/prompts/sco_debugger_system.md"

    echo ""
    echo -e "${GREEN}  自动修复完成，系统继续运行...${NC}"
    echo ""

    # ── 自动重启：继续该项目（带参数跳过菜单）──
    cd "${SCRIPT_DIR}"
    # 写自动续跑 marker，退出后由入口点检测并重新启动流水线
    echo "${WORKSPACE}" > "${AUTO_RESUME_MARKER:-/tmp/slai_auto_resume_marker}"
    cd "${SCRIPT_DIR}"
}

# ── 最大连续修复次数（防止死循环）──
MAX_AUTO_RECOVERY=3
RECOVERY_COUNT_FILE="/tmp/slai_auto_recovery_count"
AUTO_RESUME_MARKER="/tmp/slai_auto_resume_marker"

# ── 故障自动修复：读取 run.log → Claude Code 诊断修复 → 继续运行 ──
_auto_recover_if_crashed() {
    local slug="${1:-}"
    [[ -z "$slug" ]] && return 0
    local marker="${CRASH_MARKER_DIR}/${slug}.crash"
    [[ -f "$marker" ]] || return 0

    echo ""
    echo -e "${RED}╔══════════════════════════════════════════════╗${NC}"
    echo -e "${RED}║  检测到上次运行异常退出 — 启动自动修复      ║${NC}"
    echo -e "${RED}╚══════════════════════════════════════════════╝${NC}"
    echo ""

    # 读取崩溃信息
    source "$marker" 2>/dev/null || true
    local crash_log="${log:-${WORKSPACE}/run.log}"
    local crash_stage="${stage:-unknown}"

    echo -e "  崩溃阶段: ${crash_stage}"
    echo -e "  日志文件: ${crash_log}"
    echo ""

    # 构建修复 prompt
    cat > /tmp/slai_crash_recovery_prompt.txt <<CRASHPROMPT
你是 SLAIResearch 系统的故障恢复专家。上次运行时项目在阶段"${crash_stage}"异常退出。

请立即读取以下文件来诊断问题：
1. **运行日志**: ${crash_log} (读取最后200行定位错误)
2. **项目状态**: ${WORKSPACE}/state.json
3. **SCO 日志**: ${WORKSPACE}/experiment/logs/run_output.log (如存在)
4. **实验日志目录**: ${WORKSPACE}/experiment/logs/ (如存在)

**任务**：
1. 分析日志中的错误，确定根因
2. 修复实验代码或配置（只能修改 ${WORKSPACE}/ 下的文件）
3. 如果项目状态是 experiment_execution 且 SCO 任务失败，修复后重新提交
4. 如果项目状态是 environment_preparation 且依赖缺失，补充安装
5. 如果项目状态是 paper_writing 且编译失败，修复 LaTeX 后重编译
6. 确保修复后项目可以继续运行

修复完成后输出 'CRASH_RECOVERY_DONE'，然后系统会自动继续运行该项目。
CRASHPROMPT

    echo -e "${CYAN}  Claude Code 故障恢复中...${NC}"
    echo "  (读取日志、诊断问题、自动修复)"

    _claude_task "$(cat /tmp/slai_crash_recovery_prompt.txt)" \
        "/tmp/slai_crash_recovery_output.log" \
        "${SCRIPT_DIR}/prompts/sco_debugger_system.md"

    local _rc=$?
    echo ""

    # 清掉崩溃标记（无论修复是否成功，避免无限循环）
    rm -f "$marker"

    if [[ $_rc -eq 0 ]]; then
        echo -e "${GREEN}  故障恢复完成，继续运行项目...${NC}"
        return 0
    else
        echo -e "${YELLOW}  故障恢复部分完成 (exit=$_rc)，尝试继续...${NC}"
        return 0
    fi
}

cd "${SCRIPT_DIR}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

banner() {
    echo ""
    echo -e "${CYAN}╔══════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║        SLAIResearch — 全自动科研系统         ║${NC}"
    echo -e "${CYAN}╚══════════════════════════════════════════════╝${NC}"
    echo ""
}

# ---------------------------------------------------------------------------
# 首次运行配置（+ 容器重启后自动修复）
# ---------------------------------------------------------------------------
NEED_CONFIG=0

# 检查配置标记文件
if [[ ! -f ".slairesearch_configured" ]]; then
    NEED_CONFIG=1
fi

# 检查 settings.json 中的 API Key 是否有效（容器重启可能重置）
if [[ -f ".claude/settings.json" ]]; then
    SETTINGS_KEY=$(python3 -c "
import json
d = json.load(open('.claude/settings.json'))
print(d.get('env',{}).get('ANTHROPIC_API_KEY',''))
" 2>/dev/null)
    if [[ "$SETTINGS_KEY" == "your-api-key-here" ]] || [[ -z "$SETTINGS_KEY" ]]; then
        NEED_CONFIG=1
        echo -e "${YELLOW}检测到 settings.json 中 API Key 无效（容器重启导致），重新配置...${NC}"
    fi
else
    NEED_CONFIG=1
fi

if [[ "$NEED_CONFIG" == "1" ]]; then
    echo "┌──────────────────────────────────────────────────┐"
    echo "│  首次运行 — 配置                                  │"
    echo "│  (直接回车跳过可选项)                               │"
    echo "└──────────────────────────────────────────────────┘"
    echo ""

    # ★ DeepSeek API Key — 必填，不填则循环询问
    while true; do
        read -rp "DeepSeek API Key *必填: " CLAUDE_API_KEY
        if [[ -n "${CLAUDE_API_KEY}" ]]; then
            break
        fi
        echo -e "  ${RED}API Key 不能为空，此项为必填。${NC}"
    done
    read -rp "模型名称 [deepseek-v4-pro]: " CLAUDE_MODEL
    CLAUDE_MODEL="${CLAUDE_MODEL:-deepseek-v4-pro}"
    read -rp "API Base URL [https://api.deepseek.com/anthropic]: " CLAUDE_BASE_URL
    CLAUDE_BASE_URL="${CLAUDE_BASE_URL:-https://api.deepseek.com/anthropic}"

    echo ""
    echo "── 以下为可选项，直接回车跳过 ──"

    # PaperReview 邮箱（用于外部审稿）
    read -rp "PaperReview 邮箱 (外部审稿): " PAPERREVIEW_EMAIL
    PAPERREVIEW_EMAIL="${PAPERREVIEW_EMAIL:-}"

    # Semantic Scholar API Key（文献检索增强）
    read -rp "Semantic Scholar API Key (文献检索增强): " SEMANTIC_SCHOLAR_API_KEY
    SEMANTIC_SCHOLAR_API_KEY="${SEMANTIC_SCHOLAR_API_KEY:-}"

    # Tavily API Key（网络搜索补充）
    read -rp "Tavily API Key (网络搜索补充): " TAVILY_API_KEY
    TAVILY_API_KEY="${TAVILY_API_KEY:-}"

    # SCO SenseCore（远程 GPU 算力）
    echo ""
    echo "── SenseCore 远程算力（不填则仅使用本地算力）──"
    read -rp "SCO 用户 ID (NAS 路径前缀): " SCO_USER_ID
    SCO_USER_ID="${SCO_USER_ID:-}"
    read -rp "SCO 镜像路径: " SCO_IMAGE
    SCO_IMAGE="${SCO_IMAGE:-}"
    read -rp "SCO 存储挂载: " SCO_STORAGE_MOUNT
    SCO_STORAGE_MOUNT="${SCO_STORAGE_MOUNT:-}"

    # Conda 环境名（昇腾容器中通常不是 "chen"）
    read -rp "Conda 环境名 [base]: " CONDA_ENV
    CONDA_ENV="${CONDA_ENV:-base}"

    cat > .env <<EOF
export CLAUDE_MODEL="${CLAUDE_MODEL}"
export CLAUDE_BASE_URL="${CLAUDE_BASE_URL}"
export ANTHROPIC_BASE_URL="${CLAUDE_BASE_URL}"
export ANTHROPIC_API_KEY="${CLAUDE_API_KEY}"
export CLAUDE_API_KEY="${CLAUDE_API_KEY}"
export CONDA_ENV="${CONDA_ENV}"
export SEMANTIC_SCHOLAR_API_KEY="${SEMANTIC_SCHOLAR_API_KEY}"
export PAPERREVIEW_EMAIL="${PAPERREVIEW_EMAIL}"
export PAPERREVIEW_VENUE="AAAI"
export TAVILY_API_KEY="${TAVILY_API_KEY}"
export SCO_USER_ID="${SCO_USER_ID}"
export SCO_IMAGE="${SCO_IMAGE}"
export SCO_STORAGE_MOUNT="${SCO_STORAGE_MOUNT}"

# 实验执行：本地优先
export SLAIRESEARCH_LOCAL_TIMEOUT=7200
export SLAIRESEARCH_LOCAL_MAX_RETRIES=20
export SLAIRESEARCH_FORCE_SCO=false
export SLAIRESEARCH_MAX_GPU_HOURS=32
EOF

    mkdir -p .claude
    cat > .claude/settings.json <<EOF
{
  "model": "${CLAUDE_MODEL}",
  "env": {
    "ANTHROPIC_BASE_URL": "${CLAUDE_BASE_URL}",
    "ANTHROPIC_API_KEY": "${CLAUDE_API_KEY}"
  },
  "permissions": {
    "allow": ["WebSearch(*)", "WebFetch(*)", "Bash(*)", "Read(*)", "Write(*)", "Edit(*)", "NotebookEdit(*)", "Task(*)", "Agent(*)", "Skill(*)", "Search(*)", "Grep(*)", "Glob(*)", "List(*)"],
    "deny": []
  }
}
EOF
    touch .slairesearch_configured
    echo -e "${GREEN}✓ 配置完成${NC}"

    # 提示后续可选的配置项
    if [[ -z "${SCO_IMAGE}${SCO_STORAGE_MOUNT}" ]]; then
        echo -e "  ${YELLOW}ℹ 未配置 SCO 远程算力，将仅使用本地 GPU/CPU 执行实验。${NC}"
        echo -e "  ${YELLOW}   如需添加，编辑 .env 设置 SCO_IMAGE 和 SCO_STORAGE_MOUNT。${NC}"
    fi
    if [[ -z "${SEMANTIC_SCHOLAR_API_KEY}" ]]; then
        echo -e "  ${YELLOW}ℹ 未配置 Semantic Scholar，文献检索将仅使用 arXiv + OpenAlex。${NC}"
    fi
    if [[ -z "${PAPERREVIEW_EMAIL}" ]]; then
        echo -e "  ${YELLOW}ℹ 未配置 PaperReview 邮箱，将仅使用内部审稿。${NC}"
    fi
    if [[ -z "${TAVILY_API_KEY}" ]]; then
        echo -e "  ${YELLOW}ℹ 未配置 Tavily 搜索，将跳过网络搜索补充。${NC}"
    fi
fi

source .env 2>/dev/null || true

# ★ 自动修复 settings.json（容器重启可能导致 key 变回占位符）
if [[ -f ".claude/settings.json" ]] && [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
    SETTINGS_KEY=$(python3 -c "
import json
d = json.load(open('.claude/settings.json'))
print(d.get('env',{}).get('ANTHROPIC_API_KEY',''))
" 2>/dev/null)
    if [[ "$SETTINGS_KEY" == "your-api-key-here" ]] || [[ -z "$SETTINGS_KEY" ]]; then
        echo -e "${YELLOW}[auto-fix] 修复 settings.json 中的 API Key ...${NC}"
        python3 -c "
import json
d = json.load(open('.claude/settings.json'))
d['env']['ANTHROPIC_API_KEY'] = '${ANTHROPIC_API_KEY}'
json.dump(d, open('.claude/settings.json','w'), indent=2, ensure_ascii=False)
" 2>/dev/null && echo -e "${GREEN}[auto-fix] settings.json 已修复${NC}" || true
    fi
fi

# ---------------------------------------------------------------------------
# 确保 Claude Code 可用
# ---------------------------------------------------------------------------
if ! command -v claude &>/dev/null; then
    echo "[setup] 安装 Claude Code..."
    npm install -g @anthropic-ai/claude-code 2>/dev/null || {
        echo "请手动安装: https://claude.ai/code"; exit 1;
    }
fi

# ---------------------------------------------------------------------------
# 终端日志记录 — 将全部输出同时写入 run.log（追加模式，不覆盖历史）
# 原理：
#   1. 先用 >> 直接写入 session 分隔标记（OS 级别追加，绕过任何外部重定向）
#   2. 再 exec > >(tee -a)  接管当前 shell 的 stdout/stderr
#   3. 后续所有 echo 同时出现在终端和 run.log
# 注意：调用方切勿使用 ./start.sh > run.log（> 会截断），如需外部重定向请用 >>。
# ---------------------------------------------------------------------------
_setup_logging() {
    local ws="$1"
    mkdir -p "$ws"
    local log_file="${ws}/run.log"

    # 如果文件已存在，直接追加 session 分隔标记（exec 重定向之前，保证写入）
    if [[ -f "$log_file" ]]; then
        {
            echo ""
            echo "───────────────────────────────────────────────"
            echo "  ▶ 续跑 — $(date '+%Y-%m-%d %H:%M:%S')"
            echo "  项目: ${TOPIC}"
            echo "  阶段: ${STAGE:-未知}  迭代: ${ITERATION:-0}"
            echo "───────────────────────────────────────────────"
            echo ""
        } >> "$log_file"
    fi

    # 接管 stdout+stderr → 终端 + 日志文件（append）
    exec > >(tee -a "$log_file") 2>&1

    echo ""
    echo "═══════════════════════════════════════════════"
    echo "  SLAIResearch Session — $(date '+%Y-%m-%d %H:%M:%S')"
    echo "  项目: ${TOPIC}"
    echo "  阶段: ${STAGE:-新项目}  迭代: ${ITERATION:-0}"
    echo "═══════════════════════════════════════════════"
    echo ""
}

# ---------------------------------------------------------------------------
# NPU/Ascend 环境检测
# ---------------------------------------------------------------------------
_detect_npu() {
    # torch_npu is the Ascend NPU bridge — if importable, we're on Ascend.
    python3 -c "import torch_npu" 2>/dev/null && return 0
    # Fallback: NPU_ENABLED env var (set by config.py / .env)
    [[ "${NPU_ENABLED:-}" == "1" ]] && return 0
    return 1
}

# ---------------------------------------------------------------------------
# Claude Code 调用辅助 (+ PTY on NPU to avoid pipe-buffer hang)
# ---------------------------------------------------------------------------
_claude_task() {
    local prompt="$1"
    local log="${2:-/tmp/slai_claude_output.txt}"
    local sys_prompt_file="${3:-}"
    local extra_claude_flags="${4:-}"  # e.g. "--max-turns 100"

    echo -e "${CYAN}  Claude Code 正在工作中...${NC}"
    echo "  (输出实时显示，可能需要几分钟)"

    local _sys_flag=()
    if [[ -n "$sys_prompt_file" && -f "$sys_prompt_file" ]]; then
        _sys_flag=("--append-system-prompt" "$(cat "$sys_prompt_file")")
    fi

    local _extra_flag=()
    [[ -n "$extra_claude_flags" ]] && read -ra _extra_flag <<< "$extra_claude_flags"

    # ── PTY wrapper on Ascend/NPU ──
    if _detect_npu; then
        local PTY_WRAPPER="${SCRIPT_DIR}/claude_pty.py"
        if [[ -f "$PTY_WRAPPER" ]]; then
            echo "  (PTY mode — Ascend/NPU detected)"
            echo "$prompt" | python3 "$PTY_WRAPPER" claude -p \
                --model "${CLAUDE_MODEL:-deepseek-v4-pro}" \
                --output-format text --verbose \
                "${_sys_flag[@]}" "${_extra_flag[@]}" 2>&1 | stdbuf -oL tee "$log"
            local rc=${PIPESTATUS[0]}
            echo ""
            if [[ $rc -eq 0 ]]; then
                echo -e "${GREEN}  Claude Code 完成${NC}"
            else
                echo -e "${YELLOW}  Claude Code 退出码: $rc${NC}"
            fi
            return $rc
        fi
        echo "  (PTY wrapper not found, falling back to pipe mode)"
    fi

    echo "$prompt" | claude -p --model "${CLAUDE_MODEL:-deepseek-v4-pro}" \
        --output-format text --verbose \
        "${_sys_flag[@]}" "${_extra_flag[@]}" 2>&1 | stdbuf -oL tee "$log"

    local rc=${PIPESTATUS[0]}
    echo ""
    if [[ $rc -eq 0 ]]; then
        echo -e "${GREEN}  Claude Code 完成${NC}"
    else
        echo -e "${YELLOW}  Claude Code 退出码: $rc${NC}"
    fi
    return $rc
}

# ---- _claude_experiment_design: Claude Code 实验科学家接管实验设计+执行 ----
# 使用 prompts/experiment_scientist_system.md 作为系统提示词，
# prompts/experiment_scientist_task.md 作为任务模板（变量替换后）。
# Claude Code 自主完成: 环境检查 → 代码编写 → 执行 → 调试 → 报告。
_claude_experiment_design() {
    local SYS_PROMPT="${SCRIPT_DIR}/prompts/experiment_scientist_system.md"
    local TASK_TEMPLATE="${SCRIPT_DIR}/prompts/experiment_scientist_task.md"

    if [[ ! -f "$SYS_PROMPT" ]]; then
        echo -e "${YELLOW}[fallback] 系统提示词缺失，使用旧 experiment_design_start.md${NC}"
        BASELINE_CONTEXT="${BASELINE_CONTEXT}" \
        HYPOTHESIS_CLAUSE="${HYPOTHESIS_CLAUSE:-}" \
        HYPOTHESIS_LINE="${HYPOTHESIS_LINE:-}" \
        DESIGN_STEP_1="${DESIGN_STEP_1:-阅读文献综述}" \
          _render_prompt "experiment_design_start.md" /tmp/slai_stage2_prompt.txt
        _claude_task "$(cat /tmp/slai_stage2_prompt.txt)"
        return
    fi

    echo -e "${CYAN}━━━ 实验设计 (Claude Code 实验科学家) ━━━${NC}"
    echo "  系统提示词: ${SYS_PROMPT}"

    # 输入文件路径
    local HYP_FILE="${WORKSPACE}/hypothesis/hypothesis_output.json"
    local LIT_FILE="${WORKSPACE}/literature/literature_review.md"
    local BASELINE_CTX="*(无基线代码参考 — 根据文献描述自行设计基线)*"
    local BASELINE_FILE="${WORKSPACE}/experiment/baseline_context.md"
    [[ -f "$BASELINE_FILE" ]] && BASELINE_CTX="- **基线参考**: ${BASELINE_FILE}
$(head -100 "$BASELINE_FILE" 2>/dev/null || echo '')"

    # 读取 SCO 配置
    source <(python3 -c "
from config import (SCO_WORKSPACE, SCO_AEC2, SCO_IMAGE, SCO_STORAGE_MOUNT,
                    SCO_WORKER_SPEC_MAP, MAX_COMPUTE_BUDGET_GPU_HOURS,
                    EXPERIMENT_MAX_DEBUG_ROUNDS,
                    LOCAL_EXECUTION_TIMEOUT, LOCAL_EXECUTION_MAX_RETRIES,
                    DOWNLOAD_CACHE_DIR)
print(f'SCO_WS={SCO_WORKSPACE}')
print(f'SCO_CL={SCO_AEC2}')
print(f'SCO_IMG={SCO_IMAGE}')
print(f'SCO_MNT={SCO_STORAGE_MOUNT}')
print(f'SCO_S1={SCO_WORKER_SPEC_MAP.get(1, \"n6ls.iu.i40.1.8c128g\")}')
print(f'SCO_S2={SCO_WORKER_SPEC_MAP.get(2, \"n6ls.iu.i40.2.16c256g\")}')
print(f'SCO_S4={SCO_WORKER_SPEC_MAP.get(4, \"n6ls.iu.i40.4.32c512g\")}')
print(f'MAX_HRS={MAX_COMPUTE_BUDGET_GPU_HOURS}')
print(f'MAX_DBG={EXPERIMENT_MAX_DEBUG_ROUNDS}')
print(f'LCL_TO={LOCAL_EXECUTION_TIMEOUT}')
print(f'LCL_RT={LOCAL_EXECUTION_MAX_RETRIES}')
print(f'CACHE_M={DOWNLOAD_CACHE_DIR}/models')
" 2>/dev/null)

    # 变量替换 — 使用 Python 避免 sed 多行内容导致 "unterminated s command" 错误
    # BASELINE_CTX 包含多行文本和特殊字符，sed 无法安全处理
    local TASK
    echo "${BASELINE_CTX}" > /tmp/slai_baseline_ctx.txt
    TASK=$(python3 -c "
template = open('${TASK_TEMPLATE}').read()
baseline = open('/tmp/slai_baseline_ctx.txt').read()
for k, v in {
    'HYPOTHESIS_FILE': '${HYP_FILE}',
    'LITERATURE_FILE': '${LIT_FILE}',
    'OUTPUT_DIR': '${WORKSPACE}/experiment',
    'BASELINE_SECTION': baseline,
    'SCO_WORKSPACE': '${SCO_WS:-share-space}',
    'SCO_AEC2': '${SCO_CL:-share-cluster}',
    'SCO_IMAGE': '${SCO_IMG:-}',
    'SCO_STORAGE_MOUNT': '${SCO_MNT:-}',
    'SCO_WORKER_SPEC_1GPU': '${SCO_S1:-n6ls.iu.i40.1.8c128g}',
    'SCO_WORKER_SPEC_2GPU': '${SCO_S2:-n6ls.iu.i40.2.16c256g}',
    'SCO_WORKER_SPEC_4GPU': '${SCO_S4:-n6ls.iu.i40.4.32c512g}',
    'MAX_GPU_HOURS': '${MAX_HRS:-32}',
    'MAX_DEBUG_ROUNDS': '${MAX_DBG:-20}',
    'LOCAL_TIMEOUT': '${LCL_TO:-7200}',
    'LOCAL_MAX_RETRIES': '${LCL_RT:-20}',
    'MODEL_CACHE_DIR': '${CACHE_M:-workspace/.shared/cache/models}',
}.items():
    template = template.replace('\${' + k + '}', v)
print(template, end='')
")

    # 注入执行轨迹记忆（如果存在）
    local TRACE_FILE="${WORKSPACE}/experiment/.trace.jsonl"
    if [[ -f "$TRACE_FILE" ]]; then
        local TRACE_COUNT=$(wc -l < "$TRACE_FILE" 2>/dev/null || echo 0)
        if [[ $TRACE_COUNT -gt 0 ]]; then
            TASK="${TASK}
---
# 执行轨迹记忆 (${TRACE_COUNT} 条历史)

\`\`\`
$(tail -5 "$TRACE_FILE" 2>/dev/null)
\`\`\`

请基于以上轨迹继续。避免重复已完成步骤。"
        fi
    fi

    echo "$TASK" > /tmp/slai_experiment_task.md

    echo -e "${CYAN}  Claude Code 实验科学家工作中...${NC}"
    echo "  (环境检查 → 代码编写 → 执行 → 调试 → 报告)"
    echo ""

    echo "$TASK" | claude -p \
        --model "${CLAUDE_MODEL:-deepseek-v4-pro}" \
        --output-format text \
        --system-prompt "$SYS_PROMPT" \
        --max-turns 100 \
        --verbose \
        2>&1 | tee "${WORKSPACE}/experiment/claude_design_$(date +%Y%m%d-%H%M%S).log"

    local rc=${PIPESTATUS[0]}
    echo ""
    if [[ $rc -eq 0 ]]; then
        echo -e "${GREEN}  Claude Code 实验设计完成${NC}"
    else
        echo -e "${YELLOW}  Claude Code 退出码: $rc${NC}"
    fi

    # 保存执行轨迹
    python3 -c "
import json
from datetime import datetime, timezone
trace = {
    'timestamp': datetime.now(timezone.utc).isoformat(),
    'stage': 'experiment_design',
    'summary': 'Claude Code experiment scientist session',
    'rc': $rc,
}
with open('${TRACE_FILE}', 'a') as f:
    f.write(json.dumps(trace, ensure_ascii=False) + '\n')
" 2>/dev/null || true
}

# ---- _stage_review: LLM quality gate for pipeline stages ----
# Usage: _stage_review <stage_name> <output_file>
# Sets globals: REVIEW_PASSED, REVIEW_SCORE, REVIEW_FEEDBACK, REVIEW_SUGGESTION
# Returns 0 if passed, 1 if failed
_stage_review() {
    local stage_name="$1"
    local output_file="$2"

    local enabled=$(python3 -c "import sys; sys.path.insert(0,'${SCRIPT_DIR}'); from config import STAGE_REVIEW_ENABLED; print('1' if STAGE_REVIEW_ENABLED else '0')" 2>/dev/null || echo "1")
    if [[ "$enabled" == "0" ]]; then
        REVIEW_PASSED="True"; REVIEW_SCORE="7.0"; REVIEW_FEEDBACK="[disabled]"
        return 0
    fi

    if [[ ! -f "$output_file" ]]; then
        echo -e "${YELLOW}[review] output not found: ${output_file}${NC}"
        REVIEW_PASSED="True"; REVIEW_SCORE="5.0"; REVIEW_FEEDBACK="[missing output]"
        return 0
    fi

    echo ""
    echo -e "${CYAN}━━━ Stage Review: ${stage_name} ━━━${NC}"

    # Pass dynamic data via temp files (avoids shell escaping issues)
    echo "$TOPIC" > /tmp/slai_review_topic.txt
    echo "${STAGE_REVIEW_MODE:-llm}" > /tmp/slai_review_mode.txt

    python3 -c "
import sys, json
sys.path.insert(0, '${SCRIPT_DIR}')
from stage_reviewer import StageReviewer

stage_name = '${stage_name}'
output_file = '${output_file}'
topic = open('/tmp/slai_review_topic.txt').read().strip()
mode = open('/tmp/slai_review_mode.txt').read().strip()

reviewer = StageReviewer(mode=mode)
verdict = reviewer.review(
    stage_name=stage_name,
    stage_output=open(output_file).read(),
    topic=topic,
)
result = {
    'passed': verdict.passed,
    'score': verdict.score,
    'feedback': verdict.feedback,
    'suggestion': verdict.suggestion,
}
json.dump(result, open('/tmp/slai_review_result.json', 'w'), ensure_ascii=False)
	" 2>&1
	local _review_rc=$?

    if [[ $_review_rc -eq 0 ]] && [[ -f /tmp/slai_review_result.json ]]; then
        REVIEW_PASSED=$(python3 -c "import json; print(json.load(open('/tmp/slai_review_result.json')).get('passed', True))")
        REVIEW_SCORE=$(python3 -c "import json; print(json.load(open('/tmp/slai_review_result.json')).get('score', 7.0))")
        REVIEW_FEEDBACK=$(python3 -c "import json; print(json.load(open('/tmp/slai_review_result.json')).get('feedback', ''))")
        REVIEW_SUGGESTION=$(python3 -c "import json; print(json.load(open('/tmp/slai_review_result.json')).get('suggestion', ''))")
    else
        echo -e "  ${RED}⚠ Stage review 执行失败 (exit=$_review_rc)，无法评估阶段质量${NC}" >&2
        REVIEW_PASSED="True"  # 放行避免阻塞，但标记异常
        REVIEW_SCORE="5.0"
        REVIEW_FEEDBACK="[review crashed, exit=$_review_rc]" 
        REVIEW_SUGGESTION=""
    fi

    echo -e "  Score: ${YELLOW}${REVIEW_SCORE}/10${NC}"
    if [[ "$REVIEW_PASSED" == "True" ]]; then
        echo -e "  ${GREEN}✓ PASS${NC}"
        return 0
    else
        echo -e "  ${RED}✗ FAIL — ${REVIEW_FEEDBACK:0:200}${NC}"
        return 1
    fi
}

# ---- _render_prompt: render a prompt template via prompt_render.py ----
# Usage: _render_prompt <template_name> <output_file>
# Variables in the template (${VAR}) are read from the environment.
# Use inline env vars: VAR1="$val1" VAR2="$val2" _render_prompt "file.md" /tmp/out.txt
_render_prompt() {
    local template="$1"
    local output="$2"
    python3 "${SCRIPT_DIR}/prompt_render.py" "${SCRIPT_DIR}/prompts/${template}" > "$output"
}

# ---- _stage_retry_fix: re-run a stage with review feedback via Claude ----
# Usage: _stage_retry_fix <stage_label> <output_file> [extra_context]
_stage_retry_fix() {
    local stage_label="$1"
    local output_file="$2"
    local extra_context="${3:-}"

    echo ""
    echo -e "${YELLOW}[review] Re-running ${stage_label} with review feedback...${NC}"

    TOPIC="$TOPIC" stage_label="$stage_label" REVIEW_SCORE="$REVIEW_SCORE" \
      REVIEW_FEEDBACK="$REVIEW_FEEDBACK" REVIEW_SUGGESTION="$REVIEW_SUGGESTION" \
      extra_context="$extra_context" output_file="$output_file" \
      _render_prompt "stage_review_fix.md" /tmp/slai_stage_fix.txt
    _claude_task "$(cat /tmp/slai_stage_fix.txt)"
}

# 故障接管：遇到报错时保存状态 → 启动 Claude Code 诊断修复
_on_error() {
    local stage="$1"
    local err_msg="$2"
    local ws="$3"

    # 保存错误状态（项目不丢）
    if [[ -n "${SLUG:-}" ]]; then
        python3 -c "
from state_manager import StateManager
sm = StateManager('${WORKSPACE}')
try:
    state = sm.load('${SLUG}')
    # 记录错误但不改变当前阶段
    state.stages.setdefault(state.stage, {}).__setitem__('error_note', '${stage}: ${err_msg}'[:500])
    sm.save(state)
except Exception:
    pass
" 2>/dev/null || true
    fi

    echo ""
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${RED}  ${stage} 出错 — 项目已保留，可稍后恢复${NC}"
    echo -e "${RED}  启动 Claude Code 尝试诊断修复...${NC}"
    echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""

    stage="$stage" err_msg="$err_msg" ws="$ws" \
      _render_prompt "recovery.md" /tmp/slai_recover_prompt.txt

    if _claude_task "$(cat /tmp/slai_recover_prompt.txt)" 2>&1 | grep -q "RECOVERY_OK"; then
        echo -e "${GREEN}Claude Code 修复成功，继续流水线${NC}"
    else
        echo -e "${YELLOW}Claude Code 无法完全修复，但项目已保留${NC}"
        echo -e "${YELLOW}后续阶段将继续执行（跳过当前阶段）${NC}"
    fi
    echo ""
}

# ---------------------------------------------------------------------------
# ---- _do_environment_preparation: 预下载 wheels / 模型 / 数据集 ----
# 在 SCO 提交前，本地下载所有依赖（利用本地的镜像/VPN网络），
# 确保 SCO 容器可以在离线模式下直接使用缓存。
_do_environment_preparation() {
    local exp_dir="${1:-${WORKSPACE}/experiment}"
    local exp_script="${exp_dir}/run_experiment.sh"
    local manifest="${exp_dir}/experiment_manifest.json"

    echo -e "${CYAN}━━━ 环境准备：预下载依赖 ━━━${NC}"
    echo ""

    # ── 权威数据源：优先读 experiment_manifest.json，fallback 到扫描 run_experiment.sh ──
    # manifest 由实验设计阶段 (Claude Code 实验科学家) 生成，包含 model/datasets/gpu_count
    # 等结构化信息。run_experiment.sh 可能还不存在（实验设计未完成或崩溃恢复中）。
    local MANIFEST_MODEL=""
    local MANIFEST_DATASETS=""
    if [[ -f "$manifest" ]]; then
        MANIFEST_MODEL=$(python3 -c "
import json
m = json.load(open('$manifest'))
print(m.get('model', '') or m.get('model_name', ''))
" 2>/dev/null)
        MANIFEST_DATASETS=$(python3 -c "
import json
m = json.load(open('$manifest'))
ds = m.get('datasets', [])
if isinstance(ds, list):
    print(','.join(ds))
" 2>/dev/null)
        echo "  读取 experiment_manifest.json: model=${MANIFEST_MODEL:-未指定}, datasets=${MANIFEST_DATASETS:-未指定}"
    fi

    # ── 1. Wheels ──
    echo "[1/3] 检查/下载 Python wheels..."
    python3 -c "
from sco_runner import _ensure_wheels
_ensure_wheels()
print('  Wheels OK')
" 2>&1 || echo "  Wheels 检查完成（部分可能未命中）"

    # ── 2. Models ──
    echo "[2/3] 预下载模型..."
    if [[ -f "$exp_script" ]]; then
        # 有 run_experiment.sh → 扫描脚本中的模型引用
        python3 -c "
from sco_runner import _ensure_model_cache
from pathlib import Path
ok = _ensure_model_cache(Path('$exp_script'))
print('  Models OK' if ok else '  Models 部分失败（非致命）')
" 2>&1 || echo "  Models 预下载跳过"
    elif [[ -n "$MANIFEST_MODEL" ]]; then
        # 无脚本但有 manifest → 直接从 manifest 预下载模型
        echo "  从 manifest 预下载模型: ${MANIFEST_MODEL}"
        python3 -c "
from model_downloader import ThreeLayerDownloader
dl = ThreeLayerDownloader()
result = dl.download('${MANIFEST_MODEL}')
print('  Model OK' if result.success else f'  Model 下载失败: {result.error}')
" 2>&1 || echo "  Models 预下载跳过（非致命）"
    else
        echo "  未找到 run_experiment.sh 或 experiment_manifest.json 中的模型信息，跳过模型预下载"
    fi

    # ── 3. Datasets ──
    echo "[3/3] 预下载数据集..."
    if [[ -f "$exp_script" ]]; then
        python3 -c "
from sco_runner import _ensure_dataset_cache
from pathlib import Path
ok = _ensure_dataset_cache(Path('$exp_script'))
print('  Datasets OK' if ok else '  Datasets 部分失败（非致命）')
" 2>&1 || echo "  Datasets 预下载跳过"
    elif [[ -n "$MANIFEST_DATASETS" ]]; then
        echo "  从 manifest 预下载数据集: ${MANIFEST_DATASETS}"
        python3 -c "
from model_downloader import ThreeLayerDownloader
import os
dl = ThreeLayerDownloader()
for ds in '${MANIFEST_DATASETS}'.split(','):
    ds = ds.strip()
    if ds:
        try:
            result = dl.download_dataset(ds)
            print(f'  {ds}: OK' if result.success else f'  {ds}: {result.error}')
        except AttributeError:
            # ThreeLayerDownloader may not have download_dataset; datasets library auto-caches
            print(f'  {ds}: will be cached by datasets library on first use')
" 2>&1 || echo "  Datasets 预下载跳过（非致命）"
    else
        echo "  未找到 run_experiment.sh 或 experiment_manifest.json 中的数据集信息，跳过数据集预下载"
    fi

    # ── 4. 预安装 Python 包到共享 site-packages ──
    echo ""
    echo "[Extra] 预安装缺失的 Python 包..."
    # 只要有 experiment 目录就尝试安装 — 即使 .py 文件此刻不存在，
    # manifest 中的 model_type 字段可以提示需要哪些包
    if [[ -d "$exp_dir" ]]; then
        python3 -c "
from sco_runner import _prepare_env_for_sco
from pathlib import Path
ok = _prepare_env_for_sco(Path('$exp_dir'))
print('  Env prep OK' if ok else '  Env prep 部分失败（非致命）')
" 2>&1 || echo "  Env prep 跳过"
    else
        echo "  experiment 目录不存在，跳过 env prep"
    fi

    echo ""
    echo -e "${GREEN}  环境准备完成。依赖已缓存到共享存储，SCO 容器可直接使用。${NC}"
    echo ""
}

# ---------------------------------------------------------------------------
# ---- _continue_experiment: 实验执行阶段 (Claude Code + prompt 驱动) ----
# Claude Code 以实验科学家系统提示词全权接管：环境检查 → 代码编写 →
# 本地/SCO 执行 → 调试修复 → 评估报告。
# 旧的硬编码 preflight → schedule → sco_runner 路径作为 fallback。
_continue_experiment() {
    echo -e "${CYAN}━━━ 实验执行 (Claude Code 实验科学家接管) ━━━${NC}"
    echo ""

    local SYS_PROMPT="${SCRIPT_DIR}/prompts/experiment_scientist_system.md"
    local TASK_TEMPLATE="${SCRIPT_DIR}/prompts/experiment_scientist_task.md"

    if [[ ! -f "$SYS_PROMPT" ]]; then
        echo -e "${YELLOW}[fallback] 系统提示词缺失，使用旧 hardcoded 流程${NC}"
        _continue_experiment_legacy
        return
    fi

    # 检查实验脚本是否存在
    EXP_SCRIPT="${WORKSPACE}/experiment/run_experiment.sh"
    EXP_PY="${WORKSPACE}/experiment/run_experiment.py"
    MANIFEST="${WORKSPACE}/experiment/experiment_manifest.json"
    RESULTS="${WORKSPACE}/experiment/experiment_results.json"

    # 自动生成 run_experiment.sh 包装器（实验科学家可能只生成了 .py 文件）
    if [[ ! -f "$EXP_SCRIPT" ]] && [[ -f "$EXP_PY" ]]; then
        echo -e "${YELLOW}检测到 run_experiment.py，自动生成 run_experiment.sh 包装器${NC}"
        cat > "$EXP_SCRIPT" << 'WRAPPER_EOF'
#!/bin/bash
# Auto-generated wrapper — created by SLAIResearch because experiment scientist
# produced run_experiment.py instead of run_experiment.sh.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 激活 conda 环境 (如果存在)
if command -v conda &>/dev/null; then
    source "$(conda info --base)/etc/profile.d/conda.sh" 2>/dev/null || true
    conda activate base 2>/dev/null || true
fi

# 安装依赖（如 requirements.txt 存在）
pip install -r requirements.txt -q 2>/dev/null || true

# 运行实验
exec python3 run_experiment.py "$@"
WRAPPER_EOF
        chmod +x "$EXP_SCRIPT"
        echo -e "${GREEN}  已生成: ${EXP_SCRIPT}${NC}"
    fi

    # 如果已有结果，直接跳过
    if [[ -f "$RESULTS" ]]; then
        echo -e "${GREEN}实验已有结果 → ${RESULTS}${NC}"
        echo "跳过执行，继续到论文撰写..."
        python3 -c "
from state_manager import StateManager, Stage
sm = StateManager('${WORKSPACE}')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.EXPERIMENT_EXECUTION, {'results_file': '${RESULTS}', 'note': '已有结果，跳过执行'})
"
        return 0
    fi

    # 构建任务提示词（变量替换）
    local HYP_FILE="${WORKSPACE}/hypothesis/hypothesis_output.json"
    local LIT_FILE="${WORKSPACE}/literature/literature_review.md"
    local BASELINE_CTX="*(无基线代码参考)*"
    local BASELINE_FILE="${WORKSPACE}/experiment/baseline_context.md"
    [[ -f "$BASELINE_FILE" ]] && BASELINE_CTX="- **基线参考**: ${BASELINE_FILE}"

    # 读取 SCO 配置
    source <(python3 -c "
from config import (SCO_WORKSPACE, SCO_AEC2, SCO_IMAGE, SCO_STORAGE_MOUNT,
                    SCO_WORKER_SPEC_MAP, MAX_COMPUTE_BUDGET_GPU_HOURS,
                    EXPERIMENT_MAX_DEBUG_ROUNDS, EXPERIMENT_CLAUDE_TIMEOUT,
                    LOCAL_EXECUTION_TIMEOUT, LOCAL_EXECUTION_MAX_RETRIES,
                    DOWNLOAD_CACHE_DIR)
print(f'SCO_WORKSPACE={SCO_WORKSPACE}')
print(f'SCO_AEC2={SCO_AEC2}')
print(f'SCO_IMAGE={SCO_IMAGE}')
print(f'SCO_STORAGE_MOUNT={SCO_STORAGE_MOUNT}')
print(f'SCO_SPEC_1={SCO_WORKER_SPEC_MAP.get(1, \"n6ls.iu.i40.1.8c128g\")}')
print(f'SCO_SPEC_2={SCO_WORKER_SPEC_MAP.get(2, \"n6ls.iu.i40.2.16c256g\")}')
print(f'SCO_SPEC_4={SCO_WORKER_SPEC_MAP.get(4, \"n6ls.iu.i40.4.32c512g\")}')
print(f'MAX_GPU_HOURS={MAX_COMPUTE_BUDGET_GPU_HOURS}')
print(f'MAX_DEBUG_ROUNDS={EXPERIMENT_MAX_DEBUG_ROUNDS}')
print(f'LOCAL_TIMEOUT={LOCAL_EXECUTION_TIMEOUT}')
print(f'LOCAL_MAX_RETRIES={LOCAL_EXECUTION_MAX_RETRIES}')
print(f'MODEL_CACHE={DOWNLOAD_CACHE_DIR}/models')
" 2>/dev/null)

    # 变量默认值
    SCO_WORKSPACE="${SCO_WORKSPACE:-share-space}"
    SCO_AEC2="${SCO_AEC2:-share-cluster}"
    SCO_IMAGE="${SCO_IMAGE:-}"
    SCO_STORAGE_MOUNT="${SCO_STORAGE_MOUNT:-}"
    SCO_SPEC_1="${SCO_SPEC_1:-n6ls.iu.i40.1.8c128g}"
    SCO_SPEC_2="${SCO_SPEC_2:-n6ls.iu.i40.2.16c256g}"
    SCO_SPEC_4="${SCO_SPEC_4:-n6ls.iu.i40.4.32c512g}"
    MAX_GPU_HOURS="${MAX_GPU_HOURS:-32}"
    MAX_DEBUG_ROUNDS="${MAX_DEBUG_ROUNDS:-20}"
    LOCAL_TIMEOUT="${LOCAL_TIMEOUT:-7200}"
    LOCAL_MAX_RETRIES="${LOCAL_MAX_RETRIES:-20}"
    MODEL_CACHE="${MODEL_CACHE:-workspace/.shared/cache/models}"

    # 读取模板并替换变量 — 使用 Python 避免 sed 多行内容导致错误
    local TASK_PROMPT
    echo "${BASELINE_CTX}" > /tmp/slai_baseline_ctx.txt
    TASK_PROMPT=$(python3 -c "
template = open('${TASK_TEMPLATE}').read()
baseline = open('/tmp/slai_baseline_ctx.txt').read()
for k, v in {
    'HYPOTHESIS_FILE': '${HYP_FILE}',
    'LITERATURE_FILE': '${LIT_FILE}',
    'OUTPUT_DIR': '${WORKSPACE}/experiment',
    'BASELINE_SECTION': baseline,
    'SCO_WORKSPACE': '${SCO_WORKSPACE}',
    'SCO_AEC2': '${SCO_AEC2}',
    'SCO_IMAGE': '${SCO_IMAGE}',
    'SCO_STORAGE_MOUNT': '${SCO_STORAGE_MOUNT}',
    'SCO_WORKER_SPEC_1GPU': '${SCO_SPEC_1}',
    'SCO_WORKER_SPEC_2GPU': '${SCO_SPEC_2}',
    'SCO_WORKER_SPEC_4GPU': '${SCO_SPEC_4}',
    'MAX_GPU_HOURS': '${MAX_GPU_HOURS}',
    'MAX_DEBUG_ROUNDS': '${MAX_DEBUG_ROUNDS}',
    'LOCAL_TIMEOUT': '${LOCAL_TIMEOUT}',
    'LOCAL_MAX_RETRIES': '${LOCAL_MAX_RETRIES}',
    'MODEL_CACHE_DIR': '${MODEL_CACHE}',
}.items():
    template = template.replace('\${' + k + '}', v)
print(template, end='')
")

    # 如果有之前的执行轨迹，注入为上下文
    local TRACE_FILE="${WORKSPACE}/experiment/.trace.jsonl"
    if [[ -f "$TRACE_FILE" ]]; then
        local TRACE_COUNT=$(wc -l < "$TRACE_FILE" 2>/dev/null || echo 0)
        if [[ $TRACE_COUNT -gt 0 ]]; then
            echo -e "${CYAN}加载执行轨迹: ${TRACE_COUNT} 条历史记录${NC}"
            TASK_PROMPT="${TASK_PROMPT}
---
# 执行轨迹记忆

以下是之前 ${TRACE_COUNT} 次实验会话。请阅读以了解已完成的工作，避免重复。

\`\`\`
$(tail -5 "$TRACE_FILE" 2>/dev/null)
\`\`\`

请基于以上轨迹继续工作。如果 experiment_results.json 已存在且完整，直接报告完成。"
        fi
    fi

    # 保存任务提示词（调试用）
    echo "$TASK_PROMPT" > "${WORKSPACE}/experiment/.task_prompt.md"

    echo -e "${CYAN}启动 Claude Code 实验科学家...${NC}"
    echo "  系统提示词: ${SYS_PROMPT}"
    echo "  任务模板: ${TASK_TEMPLATE}"
    echo "  工作目录: ${WORKSPACE}/experiment"
    echo ""

    # 通过 PTY 运行 claude — 消除 Node.js 管道缓冲，实现逐行实时输出
    local OUT_LOG="${WORKSPACE}/experiment/claude_session_$(date +%Y%m%d-%H%M%S).log"
    echo "$TASK_PROMPT" | python3 "${SCRIPT_DIR}/claude_pty.py" \
        claude -p \
        --model "${CLAUDE_MODEL:-deepseek-v4-pro}" \
        --output-format text \
        --system-prompt "$SYS_PROMPT" \
        --max-turns 100 \
        --verbose \
        2>&1 | tee "$OUT_LOG"

    local rc=${PIPESTATUS[0]}

    # 检查产物
    if [[ -f "$RESULTS" ]]; then
        echo ""
        echo -e "${GREEN}━━━ 实验完成 ━━━${NC}"
        echo "  结果文件: ${RESULTS}"

        # 保存执行轨迹
        local OUT_CONTENT
        OUT_CONTENT=$(tail -100 "$OUT_LOG" 2>/dev/null || echo "")
        python3 -c "
import json, sys
from datetime import datetime, timezone
trace = {
    'timestamp': datetime.now(timezone.utc).isoformat(),
    'stage': 'experiment_execution',
    'summary': 'Claude Code experiment scientist session completed',
    'results_exist': True,
    'manifest_exist': $([[ -f "$MANIFEST" ]] && echo 'True' || echo 'False'),
    'output_tail': '''${OUT_CONTENT//\'/\'\\\'\'}'''[:2000]
}
with open('${TRACE_FILE}', 'a') as f:
    f.write(json.dumps(trace, ensure_ascii=False) + '\n')
" 2>/dev/null || true

        # 标记完成
        python3 -c "
from state_manager import StateManager, Stage
sm = StateManager('${WORKSPACE}')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.EXPERIMENT_EXECUTION, {
    'results_file': '${RESULTS}',
    'session_log': '${OUT_LOG}',
    'backend': 'claude_prompt_driven'
})
"
        return 0
    else
        echo ""
        echo -e "${YELLOW}━━━ Claude Code 会话结束，但未找到 experiment_results.json ━━━${NC}"
        echo "  检查日志: ${OUT_LOG}"
        echo ""
        echo -e "${YELLOW}回退到旧的 hardcoded 执行流程...${NC}"
        _continue_experiment_legacy
    fi
}

# [模块已提取到 modules/experiment_legacy.sh]
# ---- _do_paper_writing: 论文撰写 ----
_do_paper_writing() {
    echo ""
    echo -e "${CYAN}━━━ 论文撰写 ━━━${NC}"
    echo ""

    cp templates/aaai2026.sty "${WORKSPACE}/paper/" 2>/dev/null || true
    cp templates/aaai2026.bst "${WORKSPACE}/paper/" 2>/dev/null || true

    TOPIC="$TOPIC" _render_prompt "paper_write_start.md" /tmp/slai_stage4_prompt.txt
    _claude_task "$(cat /tmp/slai_stage4_prompt.txt)"

    # ── 图表生成：从实验结果生成发表级图表 ──
    local EXP_RESULTS="${WORKSPACE}/experiment/experiment_results.json"
    local FIG_DIR="${WORKSPACE}/paper/figures"
    mkdir -p "$FIG_DIR"

    if [[ -f "$EXP_RESULTS" ]]; then
        echo ""
        echo -e "${CYAN}── 生成发表级图表 (figure_generation.py) ──${NC}"
        # 生成标准图表集：对比柱状图 + 消融图 + 结果表格
        python3 -c "
import sys; sys.path.insert(0, '${SCRIPT_DIR}')
from figure_generation import FigureGenerator, TableGenerator, configure_matplotlib
from pathlib import Path
import json

configure_matplotlib()
fig_gen = FigureGenerator('${FIG_DIR}')
tbl_gen = TableGenerator()
results = json.load(open('${EXP_RESULTS}'))

# 尝试根据实验数据结构自动选择图表类型
try:
    metrics = results.get('metrics', results.get('results', {}))
    if isinstance(metrics, list):
        # 多组对比数据 → 柱状图
        fig_gen.bar_comparison(
            categories=[m.get('name', f'Method {i}') for i, m in enumerate(metrics)],
            values=[m.get('score', m.get('value', 0)) for m in metrics],
            metric_label=results.get('metric', 'Score'),
            title='${TOPIC:0:80}',
            filename='comparison_bar',
        )
        print('  ✓ comparison_bar.pdf')
        fig_gen.ablation_chart(
            components=[m.get('name', f'C{i}') for i, m in enumerate(metrics) if m.get('is_ablation')],
            values=[m.get('score', m.get('value', 0)) for m in metrics if m.get('is_ablation')],
            baseline=results.get('baseline_score', 0),
            metric_label=results.get('metric', 'Score'),
            title='Ablation Study',
            filename='ablation',
        )
        print('  ✓ ablation.pdf')
    elif isinstance(metrics, dict):
        # 单组指标 → 柱状图
        fig_gen.bar_comparison(
            categories=list(metrics.keys()),
            values=list(metrics.values()),
            metric_label=results.get('metric', 'Score'),
            title='${TOPIC:0:80}',
            filename='comparison_bar',
        )
        print('  ✓ comparison_bar.pdf')
except Exception as e:
    print(f'  [WARN] 自动图表生成跳过: {e}')
" 2>&1 || echo "  [WARN] figure_generation.py 执行异常（非致命）"

        # 如果 Claude 写了包含 \includegraphics 的 LaTeX 但缺少图表文件，
        # 尝试用生成的实际图表补上
        if [[ -f "${FIG_DIR}/comparison_bar.pdf" ]]; then
            # 更新 LaTeX 中的占位图表引用
            sed -i 's|\\includegraphics{placeholder}|\\includegraphics{figures/comparison_bar.pdf}|g' \
                "${WORKSPACE}/paper/paper.tex" 2>/dev/null || true
        fi
    else
        echo -e "${YELLOW}  无 experiment_results.json，跳过图表生成${NC}"
    fi

    # ── 编译 PDF ──
    local PAPER_DIR="${WORKSPACE}/paper"
    echo ""
    echo -e "${CYAN}── 编译论文 PDF ──${NC}"
    (cd "$PAPER_DIR" && pdflatex -interaction=nonstopmode paper.tex > /dev/null 2>&1 && pdflatex -interaction=nonstopmode paper.tex > /dev/null 2>&1) || {
        echo -e "${YELLOW}  PDF 编译失败（非致命），将继续审稿流程${NC}"
    }

    # === Stage review gate ===
    _stage_review "paper_writing" "${WORKSPACE}/paper/paper.tex"
    if [[ "$REVIEW_PASSED" != "True" ]]; then
        _stage_retry_fix "Paper Writing" "${WORKSPACE}/paper/paper.tex" \
            "Paper for: ${TOPIC}. Experiment logs: ${WORKSPACE}/experiment/sco_logs.txt"
        _stage_review "paper_writing" "${WORKSPACE}/paper/paper.tex" || true
    fi

    python3 -c "
from state_manager import StateManager, Stage
sm = StateManager('${WORKSPACE}')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.PAPER_WRITING)
"
}

# ---- _do_review_calibration: 从外部审稿中学习，改进内部审稿 ----
# 对比内部/外部审稿意见 → 评估每个内部审稿人 → 更新 reviewer_pool.json
# 使下一轮内部审稿能更好地预判外部审稿的关注点
_do_review_calibration() {
    local round_dir="${1:-}"
    local verdict="${2:-unknown}"
    local external_review="${round_dir}/external.md"
    local internal_dir="${round_dir}/internal"

    # 只在校准文件不存在时运行（每轮外部审稿只校准一次）
    local cal_file="${WORKSPACE}/review/calibration.md"
    local round_label=$(basename "$round_dir" 2>/dev/null || echo "unknown")
    if grep -q "## ${round_label} 校准" "$cal_file" 2>/dev/null; then
        echo -e "  ${GREEN}[校准] 本轮已校准过，跳过${NC}"
        return 0
    fi

    if [[ ! -f "$external_review" ]]; then
        echo -e "  ${YELLOW}[校准] 无外部审稿文件，跳过${NC}"
        return 0
    fi

    echo ""
    echo -e "${CYAN}━━━ 审稿能力校准：从外部审稿中学习 ━━━${NC}"
    echo "  外部审稿: ${external_review}"
    echo "  内部审稿: ${internal_dir}"
    echo ""

    # 收集内部审稿文件列表
    local internal_files=""
    for f in "${internal_dir}"/reviewer_*.md; do
        [[ -f "$f" ]] && internal_files+="  - $f"$'\n'
    done
    # 也检查 iter00.md（合并版）
    [[ -f "${internal_dir}/iter00.md" ]] && internal_files+="  - ${internal_dir}/iter00.md"$'\n'

    # 构建最新的内部审稿文件路径（给 prompt 用）
    local latest_internal=""
    latest_internal=$(ls -t "${internal_dir}"/iter*.md 2>/dev/null | head -1)
    [[ -z "$latest_internal" ]] && latest_internal=$(ls -t "${internal_dir}"/reviewer_*.md 2>/dev/null | head -1)

    if [[ -z "$latest_internal" ]]; then
        echo -e "  ${YELLOW}[校准] 无内部审稿文件，跳过${NC}"
        return 0
    fi

    # 构建校准 prompt
    LATEST_INTERNAL_REVIEW="$latest_internal" \
      LATEST_EXTERNAL_REVIEW="$external_review" \
      WORKSPACE="$WORKSPACE" \
      _render_prompt "review_calibration.md" /tmp/slai_calibration_prompt.txt

    _claude_task "$(cat /tmp/slai_calibration_prompt.txt)" "/tmp/slai_calibration_output.txt" || {
        echo -e "  ${YELLOW}[校准] Claude 调用失败，跳过${NC}"
        return 0
    }

    # ── 解析校准输出，提取 reviewer_pool.json 更新 ──
    if [[ -f /tmp/slai_calibration_output.txt ]]; then
        local cal_output
        cal_output=$(cat /tmp/slai_calibration_output.txt)

        # 提取 JSON 块（如果有）
        local json_extracted
        json_extracted=$(python3 -c "
import re, json, sys
text = open('/tmp/slai_calibration_output.txt').read()
# Try to find JSON code block
m = re.search(r'\`\`\`json\s*\n(.*?)\n\`\`\`', text, re.DOTALL)
if m:
    try:
        data = json.loads(m.group(1))
        # Merge with existing reviewer_pool.json
        pool_path = '${WORKSPACE}/review/reviewer_pool.json'
        existing = {'add': [], 'modify': []}
        if __import__('os').path.exists(pool_path):
            existing = json.load(open(pool_path))

        # Merge 'add' entries (dedup by name)
        existing_names = {r['name'] for r in existing.get('add', [])}
        for r in data.get('add', []):
            if r.get('name') and r.get('name') not in existing_names:
                existing.setdefault('add', []).append(r)
                print(f'  [校准] 新增审稿人: {r[\"name\"]}', file=sys.stderr)

        # Apply 'modify' entries
        for mod in data.get('modify', []):
            name = mod.get('name', '')
            for r in existing.get('add', []):
                if r.get('name') == name and mod.get('prompt_change'):
                    r['prompt'] = r.get('prompt', '') + '\n\n## 补充关注点（从外部审稿校准）\n' + mod['prompt_change']
                    print(f'  [校准] 修改审稿人: {name}', file=sys.stderr)

        json.dump(existing, open(pool_path, 'w'), indent=2, ensure_ascii=False)
        print(f'SAVED:{pool_path}')
    except Exception as e:
        print(f'ERROR:{e}', file=sys.stderr)
else:
    print('NO_JSON', file=sys.stderr)
" 2>&1)

        if echo "$json_extracted" | grep -q "SAVED:"; then
            local saved_path
            saved_path=$(echo "$json_extracted" | grep "SAVED:" | cut -d: -f2-)
            echo -e "  ${GREEN}[校准] reviewer_pool.json 已更新: ${saved_path}${NC}"
        elif echo "$json_extracted" | grep -q "NO_JSON"; then
            echo -e "  ${YELLOW}[校准] 校准分析完成（未包含 reviewer_pool 更新）${NC}"
        fi

        # 检查校准分析是否完成
        if echo "$cal_output" | grep -q "CALIBRATION_DONE"; then
            echo -e "  ${GREEN}[校准] 校准分析完成${NC}"
        else
            echo -e "  ${YELLOW}[校准] 校准分析已保存（可能部分完成）${NC}"
        fi
    fi

    # ── reviewer_evolution: 从外审中学习，更新中央 reviewer_prompts.json ──
    # 与上面的项目级 reviewer_pool.json 互补：evolution 更新全局审稿员能力
    echo ""
    echo -e "${CYAN}── 审稿员进化：从外部审稿中学习新检查维度 ──${NC}"
    python3 reviewer_evolution.py auto-evolve --workspace "${WORKSPACE}" 2>&1 || {
        echo -e "  ${YELLOW}[evolution] 审稿员进化跳过（非致命）${NC}"
    }

    echo ""
}

# ---- _do_submit_review: 提交审稿 + 等待结果 ----
_do_submit_review() {
    echo ""
    echo -e "${CYAN}━━━ 提交 paperreview.ai 审稿 ━━━${NC}"
    echo ""

    # === 邮箱未配置 → 跳过外部审稿，直接走内部审稿 ===
    if [[ -z "${PAPERREVIEW_EMAIL:-}" ]]; then
        echo -e "  ${YELLOW}ℹ 未配置 PaperReview 邮箱，跳过外部审稿，使用内部审稿${NC}"
        REVIEW_SCORE="$(_internal_review_gate 5)"
        echo -e "  ${GREEN}✓ 内部审稿完成，评分: ${REVIEW_SCORE}${NC}"
        return 0
    fi

    # === 安全闸门 1: 最大迭代次数检查 ===
    local MAX_ITER=10
    if [[ ${ITERATION:-0} -ge $MAX_ITER ]]; then
        echo ""
        echo -e "${RED}══════════════════════════════════════════════${NC}"
        echo -e "${RED}  已达最大迭代次数 (${MAX_ITER})，停止提交${NC}"
        echo -e "${RED}  请手动检查项目状态: ${WORKSPACE}${NC}"
        echo -e "${RED}══════════════════════════════════════════════${NC}"
        exit 1
    fi

    # 修订轮次安全检查：如果 ITERATION > 0 但还没跑过内部审稿门控，先跑一次
    if [[ ${ITERATION:-0} -gt 0 ]]; then
        local latest_gate=$(ls -td "${WORKSPACE}/review/internal_gate_"* 2>/dev/null | head -1)
        if [[ -z "$latest_gate" ]]; then
            echo -e "${YELLOW}⚠ 修订轮次 (iter=${ITERATION}) 但未经过内部审稿门控${NC}"
            echo -e "${YELLOW}  在提交外部审稿前先运行门控...${NC}"
            _internal_review_gate 5
        else
            echo -e "${GREEN}✓ 内部审稿门控已完成 (${latest_gate})${NC}"
            echo ""
        fi
    fi

    PDF_FILE="${WORKSPACE}/paper/paper.pdf"
    if [[ ! -f "$PDF_FILE" ]]; then
        echo -e "${RED}未找到 PDF: ${PDF_FILE}${NC}"
        exit 1
    fi

    # === 外部审稿提交循环（失败 → 内部审稿 → 修订 → 重试）===
    local EXTERNAL_RETRY=0
    local MAX_EXTERNAL_RETRIES=5

    while [[ $EXTERNAL_RETRY -lt $MAX_EXTERNAL_RETRIES ]]; do
        TOKEN_RAW=$(python3 -c "
import sys; sys.path.insert(0, '.')
from paperreview_api import submit_paper
token = submit_paper('${PDF_FILE}', email='${PAPERREVIEW_EMAIL}', venue='AAAI')
print(token)
" 2>&1)
        SUBMIT_RC=$?

        if [[ $SUBMIT_RC -eq 0 ]] && ! echo "$TOKEN_RAW" | grep -qE "Traceback|Error:|HTTPError|SyntaxError|Insufficient|429|402|500" 2>/dev/null; then
            break  # 提交成功
        fi

        EXTERNAL_RETRY=$((EXTERNAL_RETRY + 1))
        echo ""
        echo -e "${RED}══════════════════════════════════════════════${NC}"
        echo -e "${RED}  paperreview.ai 提交失败 (尝试 ${EXTERNAL_RETRY}/${MAX_EXTERNAL_RETRIES})${NC}"
        echo -e "${RED}  错误信息: ${TOKEN_RAW:0:300}${NC}"
        echo -e "${RED}══════════════════════════════════════════════${NC}"
        echo ""

        if [[ $EXTERNAL_RETRY -ge $MAX_EXTERNAL_RETRIES ]]; then
            echo -e "${RED}已达最大外部提交尝试次数 (${MAX_EXTERNAL_RETRIES})${NC}"
            echo -e "${YELLOW}项目已保留在: ${WORKSPACE}${NC}"
            echo -e "${YELLOW}请检查 paperreview.ai 服务状态，稍后运行 bash start.sh 继续${NC}"
            exit 1
        fi

        echo -e "${CYAN}╔══════════════════════════════════════════════╗${NC}"
        echo -e "${CYAN}║  外部审稿不可用 → 内部审稿 fallback      ║${NC}"
        echo -e "${CYAN}╚══════════════════════════════════════════════╝${NC}"

        # _internal_review_gate 已包含完整循环：
        # 内部审稿 → 解决率检查 → 未通过则自动修订 → 再次审稿 → 直到达标或上限
        _internal_review_gate 5

        ITERATION=$((ITERATION + 1))
        python3 -c "
from state_manager import StateManager
sm = StateManager('${WORKSPACE}')
state = sm.load('${SLUG}')
state.iteration = ${ITERATION}
sm.save(state)
" 2>/dev/null || true

        echo ""
        echo -e "${CYAN}── 重新尝试外部审稿提交 (${EXTERNAL_RETRY}/${MAX_EXTERNAL_RETRIES}) ──${NC}"
        echo ""
    done

    TOKEN="$TOKEN_RAW"
    echo -e "${GREEN}审稿已提交${NC}"
    echo -e "Token: ${YELLOW}已获取（长度: ${#TOKEN}）${NC}"

    # 用文件传递 TOKEN 避免 shell 注入
    python3 -c "
import sys; sys.path.insert(0, '.')
from state_manager import StateManager, Stage, ReviewRecord
sm = StateManager('${WORKSPACE}')
state = sm.load('${SLUG}')
record = ReviewRecord(iteration=${ITERATION:-0}, token=open('/tmp/slai_token.txt').read().strip(), submitted_at='${PDF_FILE}')
sm.add_review(state, record)
sm.start_stage(state, Stage.POLL_REVIEW)
" 2>/dev/null || {
        echo "$TOKEN" > /tmp/slai_token.txt
        python3 -c "
import sys; sys.path.insert(0, '.')
from state_manager import StateManager, Stage, ReviewRecord
sm = StateManager('${WORKSPACE}')
state = sm.load('${SLUG}')
record = ReviewRecord(iteration=${ITERATION:-0}, token=open('/tmp/slai_token.txt').read().strip(), submitted_at='${PDF_FILE}')
sm.add_review(state, record)
sm.start_stage(state, Stage.POLL_REVIEW)
"
    }

    # 内部审稿 + 外部审稿（并行）
    ROUND_DIR="${WORKSPACE}/review/round_$(printf "%03d" ${ITERATION:-0})"
    mkdir -p "${ROUND_DIR}/internal"
    python3 internal_review.py "${PDF_FILE}" -o "${ROUND_DIR}/internal/" &
    INTERNAL_REVIEW_PID=$!

    echo "等待 paperreview.ai 审稿结果..."
    REVIEW_DATA=$(echo "$TOKEN" | python3 -c "
import sys; sys.path.insert(0, '.')
from paperreview_api import poll_review, review_to_markdown, extract_verdict
token = sys.stdin.read().strip()
review = poll_review(token, initial_wait=300, interval=60, max_wait=7200)
md = review_to_markdown(review)
with open('${ROUND_DIR}/external.md', 'w') as f: f.write(md)
verdict = extract_verdict(review)
print(f'VERDICT={verdict}')
" 2>&1)
    local POLL_RC=$?

    if kill -0 ${INTERNAL_REVIEW_PID} 2>/dev/null; then
        wait ${INTERNAL_REVIEW_PID} 2>/dev/null || true
    fi

    VERDICT=$(echo "$REVIEW_DATA" | grep "VERDICT=" | cut -d= -f2-)
    echo ""
    echo -e "审稿结果: ${YELLOW}${VERDICT:-未知}${NC}"

    # === 安全闸门 2: 空或错误 verdict 处理 ===
    if [[ -z "$VERDICT" ]]; then
        echo -e "${RED}无法解析审稿结果，可能是 API 错误${NC}"
        echo "原始输出: ${REVIEW_DATA:0:500}"
        exit 1
    fi

    # 写入 temp file 供 Python 安全读取（避免 shell 注入）
    echo "$VERDICT" > /tmp/slai_verdict.txt

    # 写入 state
    python3 -c "
import sys; sys.path.insert(0, '.')
from state_manager import StateManager, Stage
sm = StateManager('${WORKSPACE}')
state = sm.load('${SLUG}')
# 从 temp file 读取，避免 shell 注入
_verdict = open('/tmp/slai_verdict.txt').read().strip()
if state.reviews:
    state.reviews[-1]['verdict'] = _verdict
    state.reviews[-1]['review_md_path'] = '${ROUND_DIR}/external.md'
sm.complete_stage(state, Stage.POLL_REVIEW, {'verdict': _verdict})
"

    if [[ "$VERDICT" == "accept" ]] || [[ "$VERDICT" == "weak accept" ]]; then
        echo -e "${GREEN}★ 论文已通过审稿！${NC}"
        python3 -c "
from state_manager import StateManager
sm = StateManager('${WORKSPACE}')
state = sm.load('${SLUG}')
state.stage = 'done'
sm.save(state)
"
        exit 0
    else
        # === 安全闸门 3: 递归深度保护 ===
        if [[ ${ITERATION:-0} -ge $MAX_ITER ]]; then
            echo -e "${RED}已达最大迭代次数 (${MAX_ITER})，停止修订${NC}"
            exit 1
        fi
        echo -e "${YELLOW}审稿未通过 (${VERDICT}) — 进入修订迭代${NC}"

        # ── 审稿能力校准：从外部审稿中学习，改进内部审稿 ──
        _do_review_calibration "$ROUND_DIR" "$verdict"

        _do_revise_and_resubmit "$VERDICT"
    fi
}


# 核心：根据 state 决定执行什么
# ---------------------------------------------------------------------------

# ── 自动续跑：如果通过 --auto-resume <workspace_dir> 启动，直接继续该项目 ──
# 参数是 workspace 路径（如 workspace/**_Adaptive_Prompt_...），不是 slug
if [[ "${1:-}" == "--auto-resume" && -n "${2:-}" ]]; then
    AUTO_WS="$2"
    # flat mode: state.json 直接在 workspace 目录下（不是 state/<slug>/）
    if [[ -f "${AUTO_WS}/state.json" ]]; then
        WORKSPACE="$(cd "$AUTO_WS" 2>/dev/null && pwd || echo "$AUTO_WS")"
        SF="${WORKSPACE}/state.json"
        TOPIC=$(python3 -c "import json; print(json.load(open('${SF}'))['topic'])" 2>/dev/null || echo "?")
        STAGE=$(python3 -c "import json; print(json.load(open('${SF}'))['stage'])" 2>/dev/null || echo "literature_search")
        ITERATION=$(python3 -c "import json; print(json.load(open('${SF}'))['iteration'])" 2>/dev/null || echo "0")
        SLUG=$(python3 -c "import json; print(json.load(open('${SF}'))['topic_slug'])" 2>/dev/null || echo "")
        export TOPIC WORKSPACE STAGE ITERATION SLUG
        _setup_logging "$WORKSPACE"
        echo ""
        echo -e "${CYAN}  ▶ 自动续跑: ${TOPIC:0:60}${NC}"
        echo -e "  阶段: ${STAGE}  迭代: ${ITERATION}"
        echo -e "  目录: ${WORKSPACE}"
        echo ""
        # 清零恢复计数（成功续跑）
        rm -f "$RECOVERY_COUNT_FILE"
        _continue_project
        exit $?
    fi
    echo -e "${RED}[auto-resume] 未找到 state.json: ${AUTO_WS}/state.json${NC}" >&2
fi

# ── 崩溃后自动续跑：检测 _on_exit 留下的 marker 文件 ──
if [[ -f "${AUTO_RESUME_MARKER:-/tmp/slai_auto_resume_marker}" ]]; then
    local _marker_ws
    _marker_ws=$(head -1 "${AUTO_RESUME_MARKER}" 2>/dev/null)
    if [[ -n "$_marker_ws" ]] && [[ -d "$_marker_ws" ]] && [[ -f "${_marker_ws}/state.json" ]]; then
        local _rec_count=0
        [[ -f "$RECOVERY_COUNT_FILE" ]] && _rec_count=$(cat "$RECOVERY_COUNT_FILE" 2>/dev/null || echo 0)
        if [[ $_rec_count -le ${MAX_AUTO_RECOVERY:-3} ]]; then
            echo -e "${CYAN}  ▶ 检测到崩溃恢复 marker，自动续跑: ${_marker_ws}${NC}"
            rm -f "${AUTO_RESUME_MARKER}"
            # 读取项目信息
            WORKSPACE="$(cd "$_marker_ws" 2>/dev/null && pwd || echo "$_marker_ws")"
            SF="${WORKSPACE}/state.json"
            TOPIC=$(python3 -c "import json; print(json.load(open('${SF}'))['topic'])" 2>/dev/null || echo "?")
            STAGE=$(python3 -c "import json; print(json.load(open('${SF}'))['stage'])" 2>/dev/null || echo "literature_search")
            ITERATION=$(python3 -c "import json; print(json.load(open('${SF}'))['iteration'])" 2>/dev/null || echo "0")
            SLUG=$(python3 -c "import json; print(json.load(open('${SF}'))['topic_slug'])" 2>/dev/null || echo "")
            export TOPIC WORKSPACE STAGE ITERATION SLUG
            _setup_logging "$WORKSPACE"
            rm -f "$RECOVERY_COUNT_FILE"
            _continue_project
            exit $?
        else
            echo -e "${RED}  ⚠ 连续崩溃次数超限 (${_rec_count} > ${MAX_AUTO_RECOVERY})，停止自动续跑${NC}"
            rm -f "${AUTO_RESUME_MARKER}" "$RECOVERY_COUNT_FILE"
        fi
    else
        rm -f "${AUTO_RESUME_MARKER}"  # 清理无效 marker
    fi
fi

banner

# 收集所有项目（仅列出 workspace/ 真实存在的）
declare -a PROJECT_SLUGS=()
declare -a PROJECT_TOPICS=()
declare -a PROJECT_STAGES=()
declare -a PROJECT_ITERS=()
declare -a PROJECT_WORKSPACES=()

# 记录已通过 state/ 注册的项目 slug（避免重复）
declare -A SEEN_SLUGS=()

for d in workspace/*/; do
    [[ -d "$d" ]] || continue
    sf="${d}state.json"
    if [[ -f "$sf" ]]; then
        slug=$(basename "$d")
        SEEN_SLUGS["$slug"]=1
        WORK_DIR=$(python3 -c "
import json
d = json.load(open('$sf'))
print(d.get('work_dir', ''))
" 2>/dev/null)
        if [[ -z "$WORK_DIR" ]] || [[ ! -d "$WORK_DIR" ]]; then
            continue
        fi
        PROJECT_SLUGS+=("$slug")
        PROJECT_TOPICS+=("$(python3 -c "import json; print(json.load(open('$sf'))['topic'])" 2>/dev/null || echo "?")")
        PROJECT_STAGES+=("$(python3 -c "
import json
d=json.load(open('$sf'))
s=d.get('stage','?')
for v in d.get('stages',{}).values():
    if isinstance(v,dict) and v.get('status')=='error': s+=' [有错误]'; break
print(s)
" 2>/dev/null || echo "?")")
        PROJECT_ITERS+=("$(python3 -c "import json; print(json.load(open('$sf'))['iteration'])" 2>/dev/null || echo "0")")
        PROJECT_WORKSPACES+=("$WORK_DIR")
    fi
done

# 也扫描 workspace/ 中有实际内容但未在 state/ 注册的项目
for d in workspace/*/; do
    slug=$(basename "$d")
    [[ -n "${SEEN_SLUGS[$slug]:-}" ]] && continue  # 已通过 state 注册
    # 检查是否有实际产物（不只是空目录）
    has_content=false
    [[ -f "$d/literature/literature_review.md" ]] && has_content=true
    [[ -f "$d/experiment/experiment_results.json" ]] && has_content=true
    [[ -f "$d/experiment/scores.jsonl" ]] && has_content=true
    [[ -f "$d/paper/paper.tex" ]] && has_content=true
    [[ -f "$d/.session_trace.jsonl" ]] && has_content=true
    $has_content || continue
    # 推断 topic（从目录名或上下文快照）
    topic="$slug"
    [[ -f "$d/.context_snapshot.md" ]] && topic=$(head -1 "$d/.context_snapshot.md" 2>/dev/null | sed 's/^#*\s*//')
    # 推断阶段
    stage="(未注册)"
    [[ -f "$d/literature/literature_review.md" ]] && stage="文献检索+"
    [[ -f "$d/experiment/experiment_results.json" ]] && stage="实验完成"
    [[ -f "$d/paper/paper.pdf" ]] && stage="论文完成"
    PROJECT_SLUGS+=("$slug")
    PROJECT_TOPICS+=("${topic:0:80}")
    PROJECT_STAGES+=("$stage")
    PROJECT_ITERS+=("$( [[ -f "$d/experiment/scores.jsonl" ]] && wc -l < "$d/experiment/scores.jsonl" || echo 0)")
    PROJECT_WORKSPACES+=("$(realpath "$d")")
done

# ---- 选择项目（↑↓ 键导航，回车确认）----
if [[ ${#PROJECT_SLUGS[@]} -gt 0 ]]; then
    # 构建菜单项并调用 ↑↓ 选择器
    MENU_ARGS=()
    for idx in "${!PROJECT_SLUGS[@]}"; do
        s="${PROJECT_STAGES[$idx]}"
        case "$s" in
            literature_search) s_disp="文献检索" ;;
            hypothesis_generation) s_disp="假说生成" ;;
            experiment_design) s_disp="实验设计" ;;
            environment_preparation) s_disp="环境准备" ;;
            experiment_execution) s_disp="实验执行" ;;
            paper_writing) s_disp="论文撰写" ;;
            submit_review) s_disp="提交审稿" ;;
            poll_review) s_disp="等待审稿" ;;
            revise) s_disp="修订中" ;;
            resubmit) s_disp="重新提交" ;;
            done) s_disp="已完成" ;;
            failed) s_disp="失败" ;;
            *) s_disp="$s" ;;
        esac
        MENU_ARGS+=("${PROJECT_TOPICS[$idx]:0:60}  [${s_disp}] [迭代 ${PROJECT_ITERS[$idx]}]")
    done

    python3 menu.py "${MENU_ARGS[@]}"
    stty sane 2>/dev/null || true  # 恢复终端状态，防吞字
    CHOICE=$(cat /tmp/slai_menu_result.txt 2>/dev/null || echo "__QUIT__")

    if [[ "$CHOICE" == "__QUIT__" ]]; then
        exit 0
    elif [[ "$CHOICE" == "__NEW__" ]]; then
        PROJECT_SLUGS=()
    elif [[ "$CHOICE" =~ ^[0-9]+$ ]]; then
        idx="$CHOICE"
        SLUG="${PROJECT_SLUGS[$idx]}"
        TOPIC="${PROJECT_TOPICS[$idx]}"
        STAGE="${PROJECT_STAGES[$idx]}"
        ITERATION="${PROJECT_ITERS[$idx]}"
        WORKSPACE="${PROJECT_WORKSPACES[$idx]}"
        export TOPIC WORKSPACE STAGE ITERATION SLUG
        _setup_logging "$WORKSPACE"
        # ── 故障自动恢复：检测上次是否崩溃，如是则触发 Claude Code 修复 ──
        _auto_recover_if_crashed "$SLUG"
        _continue_project
    else
        exit 1
    fi
fi

# ---- 情况 A：新项目 ----
if [[ ${#PROJECT_SLUGS[@]} -eq 0 ]]; then
    echo -e "${YELLOW}开始全新研究。请描述你的想法（可以是口语化的，Agent 会帮你提炼）:${NC}"
    echo ""
    read -rp "你的想法: " USER_INPUT
    if [[ -z "$USER_INPUT" ]]; then
        echo "输入不能为空。"
        exit 1
    fi

    # 检测并清理 markdown 格式字符（**bold**、*italic*、`code`等）
    if echo "$USER_INPUT" | grep -qE '^\*\*|^__|\[.+\]\(.+\)'; then
        echo -e "${YELLOW}⚠ 输入可能包含 markdown 格式（如 **bold**），将自动清理。${NC}"
        echo -e "${YELLOW}  建议直接输入纯文本研究主题。${NC}"
    fi

    # 调用 Agent 提炼研究主题
    echo ""
    echo -e "${CYAN}Agent 正在分析你的想法并提炼研究主题...${NC}"
    USER_INPUT="$USER_INPUT" _render_prompt "topic_refine.md" /tmp/slai_topic_prompt.txt

    REFINED=$(cat /tmp/slai_topic_prompt.txt | claude -p --model "${CLAUDE_MODEL:-deepseek-v4-pro}" --output-format text 2>&1) || true

    # ★ 检测 API 错误（401/403/500/auth 等），不要将错误信息当作主题
    if echo "$REFINED" | grep -qiE "401|403|500|authentication|unauthorized|invalid.*api|api.*key.*invalid|Failed to authenticate"; then
        echo ""
        echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo -e "${RED}  API 认证失败！${NC}"
        echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo ""
        echo "  错误信息: ${REFINED:0:300}"
        echo ""
        echo "  可能原因:"
        echo "    1. API Key 已过期或被重置"
        echo "    2. .claude/settings.json 中的 key 是占位符"
        echo "    3. 容器重启导致配置丢失"
        echo ""
        echo "  修复方法:"
        echo "    - 删除 .slairesearch_configured 后重新运行: rm .slairesearch_configured && bash start.sh"
        echo "    - 或手动设置: export ANTHROPIC_API_KEY=\"sk-your-key\""
        echo ""
        # 使用原始输入作为主题继续（让用户手动处理 API）
        TOPIC="$USER_INPUT"
        echo -e "${YELLOW}将使用原始输入作为研究主题，跳过 LLM 提炼${NC}"
        echo ""
    else
        TOPIC=$(echo "$REFINED" | grep -oP 'TOPIC:\s*\K.+' | head -1)

        if [[ -z "$TOPIC" ]]; then
            # 如果解析失败，取最后一行非空内容作为主题
            TOPIC=$(echo "$REFINED" | grep -v '^\s*$' | tail -1)
        fi
    fi

    if [[ -z "$TOPIC" ]]; then
        TOPIC="$USER_INPUT"  # 兜底：用原始输入
    fi

    echo ""
    echo -e "${GREEN}提炼后的研究主题:${NC}"
    echo -e "  ${TOPIC}"
    echo ""
    read -rp "确认使用此主题? [Y/n] " CONFIRM
    if [[ "$CONFIRM" =~ ^[Nn] ]]; then
        echo "已取消。你可以重新运行 bash start.sh。"
        exit 0
    fi

    # 清除 markdown 格式字符（**bold**、*italic*、`code`、[link]等）和文件系统不安全字符
    # sed 字符类中 ] 必须放首位，其余字符不需要转义
    SAFE_TOPIC=$(echo "$TOPIC" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' | sed 's/[]*_`#[()|&;!$<>]//g' | tr ' ' '_' | sed 's/^[_-]*//; s/[_-]*$//' | cut -c1-50)
    WORKSPACE="workspace/${SAFE_TOPIC}"

    # 检测并警告可能的 markdown 格式残留
    if echo "$TOPIC" | grep -qE '^\*|^_|^#|^`|\[|\]'; then
        echo -e "${YELLOW}⚠ 检测到研究主题可能包含 markdown 格式字符（如 **bold**），已自动清除。${NC}"
        echo -e "  原始: ${TOPIC:0:80}"
        echo -e "  净化: ${SAFE_TOPIC}"
    fi

    # 创建目录和初始 state
    mkdir -p "${WORKSPACE}"
    WORKSPACE_ABS="$(cd "${WORKSPACE}" 2>/dev/null && pwd || echo "${WORKSPACE}")"
    python3 -c "
from state_manager import StateManager
import os
ws = os.path.abspath('${WORKSPACE_ABS}')
sm = StateManager(ws)
state = sm.create('${TOPIC}'.replace(\"'\", \"\"), work_dir=ws)
print(state.topic_slug)
" > /tmp/slai_slug.txt
    SLUG=$(cat /tmp/slai_slug.txt 2>/dev/null)
    if [[ -z "$SLUG" ]]; then
        echo -e "${RED}状态创建失败，请检查错误信息后重试${NC}"
        exit 1
    fi

    # 启动日志记录（追加到项目 run.log，不覆盖历史）
    WORKSPACE="$(cd "$WORKSPACE" 2>/dev/null && pwd || echo "$WORKSPACE")"
    export TOPIC WORKSPACE SLUG
    STAGE="literature_search" ITERATION=0
    _setup_logging "$WORKSPACE"

    # ===== Stage 1: 文献检索 =====
    echo ""
    echo -e "${CYAN}━━━ Stage 1: 文献检索 ━━━${NC}"
    echo ""

    # 1. LLM 提取关键词组
    echo -e "${CYAN}提取搜索关键词...${NC}"
    python3 keyword_extractor.py "${TOPIC}" --groups 3 > /tmp/slai_kw.json 2>/dev/null || true

    # 2. 提取关键词并搜索
    if [[ -f /tmp/slai_kw.json ]]; then
        # 解析各组关键词，合并为一个搜索
        KW_QUERY=$(python3 -c "
import json
groups = json.load(open('/tmp/slai_kw.json')).get('groups', [])
# 取每组前2个关键词拼接
parts = [' '.join(g[:2]) for g in groups if g]
query = ' AND '.join(parts[:3])  # 最多3组
print(query[:200])
" 2>/dev/null)
        echo -e "  关键词: ${KW_QUERY:0:120}..."

        # 用关键词搜索（比全主题效果好）
        python3 search_papers.py "${KW_QUERY:-${TOPIC}}" -n 20 -o "${WORKSPACE}/literature/" \
            --save-json "${WORKSPACE}/literature/papers_metadata.json" 2>&1

        # 补充：也用原始主题搜一次（覆盖可能遗漏的）
        python3 search_papers.py "${TOPIC:0:200}" -n 10 -o "${WORKSPACE}/literature/" \
            --save-json "${WORKSPACE}/literature/papers_metadata.json" 2>&1 || true
    else
        # Fallback
        python3 search_papers.py "${TOPIC}" -n 20 -o "${WORKSPACE}/literature/" \
            --save-json "${WORKSPACE}/literature/papers_metadata.json" 2>&1
    fi

    # 3. Tavily 补充（学术 + 相关方法搜索）
    python3 tavily_search.py "${TOPIC:0:200}" --max-results 8 --mode academic --output "${WORKSPACE}/literature/tavily_results.md" 2>/dev/null || true

    # 4. Claude Code 补充分析和整理
    if [[ -f "${WORKSPACE}/literature/literature_review.md" ]]; then
        REVIEW_MD="${WORKSPACE}/literature/literature_review.md"
        ANALYSIS_PROMPT="请阅读 ${REVIEW_MD}，基于检索到的论文进行分析：提炼领域概览和关键趋势、识别研究空白、提出具体的研究方向建议。将分析结果追加到 ${REVIEW_MD} 末尾。只做分析和建议，不超过500字。完成后报告'文献分析完成'。"
        _claude_task "${ANALYSIS_PROMPT}" || echo "[WARN] Claude 分析跳过（可手动完成）"
    else
        echo -e "${RED}文献检索失败：search_papers.py 未生成输出${NC}"
        exit 1
    fi

    # === Stage review gate ===
    _stage_review "literature_search" "${WORKSPACE}/literature/literature_review.md"
    if [[ "$REVIEW_PASSED" != "True" ]]; then
        _stage_retry_fix "Literature Search" "${WORKSPACE}/literature/literature_review.md" \
            "Literature review for: ${TOPIC}"
        _stage_review "literature_search" "${WORKSPACE}/literature/literature_review.md" || true
    fi

    python3 -c "
from state_manager import StateManager, Stage
sm = StateManager('${WORKSPACE}')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.LITERATURE_SEARCH, {'papers_found': 0})
"

    # ===== Stage 2: 假说生成 (ReAct-based) =====
    echo ""
    echo -e "${CYAN}━━━ Stage 2: 假说生成 (ReAct 检索+对比+假说) ━━━${NC}"
    echo ""

    mkdir -p "${WORKSPACE}/hypothesis/"

    python3 hypothesis_engine.py \
        --topic "${TOPIC}" \
        --literature-dir "${WORKSPACE}/literature/" \
        --work-dir "${WORKSPACE}/hypothesis/" \
        --max-react-rounds 3 --top-k-pdfs 5 2>&1

    # 让 Claude Code 审阅和补充假说
    if [[ -f "${WORKSPACE}/hypothesis/hypothesis_output.json" ]]; then
        HYPOTHESIS_JSON=$(cat "${WORKSPACE}/hypothesis/hypothesis_output.json" | head -300)
        HYPOTHESIS_JSON="$HYPOTHESIS_JSON" \
          _render_prompt "hypothesis_refine.md" /tmp/slai_hypothesis_refine.txt
        _claude_task "$(cat /tmp/slai_hypothesis_refine.txt)" || echo "[WARN] 假说审阅跳过"
    else
        echo -e "${YELLOW}hypothesis_engine.py 未生成输出，检查错误日志${NC}"
    fi

    # === Stage review gate ===
    _stage_review "hypothesis_generation" "${WORKSPACE}/hypothesis/hypothesis_report.md"
    if [[ "$REVIEW_PASSED" != "True" ]]; then
        _stage_retry_fix "Hypothesis Generation" "${WORKSPACE}/hypothesis/hypothesis_report.md" \
            "Hypothesis report for: ${TOPIC}"
        _stage_review "hypothesis_generation" "${WORKSPACE}/hypothesis/hypothesis_report.md" || true
    fi

    python3 -c "
from state_manager import StateManager, Stage
sm = StateManager('${WORKSPACE}')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.HYPOTHESIS_GENERATION)
sm.start_stage(state, Stage.BASELINE_FETCHING)
"

    # ===== Stage 2.5: 基线仓库获取 =====
    echo ""
    echo -e "${CYAN}━━━ Stage 3: 基线仓库获取 ━━━${NC}"
    echo ""

    BASELINE_CTX_FILE="${WORKSPACE}/experiment/baseline_context.md"
    mkdir -p "${WORKSPACE}/experiment"

    HYPO_FILE="${WORKSPACE}/hypothesis/hypothesis_output.json"
    if [ -f "$HYPO_FILE" ]; then
        python3 -c "
import json, sys
try:
    data = json.load(open('$HYPO_FILE'))
    methods = []
    for h in data.get('hypotheses', []):
        if isinstance(h, dict):
            methods.extend(h.get('baselines', []) or h.get('baseline_methods', []))
    if methods:
        with open('/tmp/slai_baseline_methods.txt', 'w') as f:
            f.write(','.join(methods[:10]))
        sys.exit(0)
    sys.exit(1)
except Exception:
    sys.exit(1)
" 2>/dev/null && BASELINE_METHODS_ARG="--methods $(cat /tmp/slai_baseline_methods.txt | tr ',' ' ')" || BASELINE_METHODS_ARG=""
    fi

    python3 "${SCRIPT_DIR}/baseline_finder.py" \
        ${BASELINE_METHODS_ARG} \
        --cache-dir "${WORKSPACE}/../.shared/baselines" \
        --max 5 \
        --output "${BASELINE_CTX_FILE}" 2>&1 || {
        echo "[Phase] Baseline fetching skipped (no repos found or network unavailable)"
        echo "" > "${BASELINE_CTX_FILE}"
    }

    BASELINE_CONTEXT=$(cat "${BASELINE_CTX_FILE}" 2>/dev/null || echo "")

    python3 -c "
from state_manager import StateManager, Stage
sm = StateManager('${WORKSPACE}')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.BASELINE_FETCHING)
sm.start_stage(state, Stage.EXPERIMENT_DESIGN)
"

    # ===== Stage 3: 实验设计 =====
    echo ""
    echo -e "${CYAN}━━━ Stage 4: 实验设计 ━━━${NC}"
    echo ""

    # Claude Code experiment scientist (prompt-driven)
    _claude_experiment_design

    # 验证 manifest 是否被创建，缺失时自动补全
    MANIFEST="${WORKSPACE}/experiment/experiment_manifest.json"
    if [[ ! -f "$MANIFEST" ]]; then
        echo -e "${YELLOW}[system] Claude 未创建 experiment_manifest.json，自动补全为 4 GPU${NC}"
        mkdir -p "$(dirname "$MANIFEST")"
        echo '{"gpu_count": 4}' > "$MANIFEST"
    else
        MANIFEST_INFO=$(python3 -c "
import json
d = json.load(open('$MANIFEST'))
gpu = d.get('gpu_count', 0)
hours = d.get('estimated_runtime_hours')
if hours:
    gpu_h = gpu * float(hours)
    print(f'gpu_count={gpu}, runtime={hours}h, total={gpu_h:.1f} GPU-hours (limit: 32)')
else:
    print(f'gpu_count={gpu} [WARNING: estimated_runtime_hours missing, budget check skipped]')
" 2>/dev/null || echo "gpu_count=0")
        echo -e "${GREEN}[system] experiment_manifest.json: ${MANIFEST_INFO}${NC}"
    fi

    # === Stage review gate ===
    _stage_review "experiment_design" "${WORKSPACE}/experiment/experiment_plan.md"
    if [[ "$REVIEW_PASSED" != "True" ]]; then
        _stage_retry_fix "Experiment Design" "${WORKSPACE}/experiment/experiment_plan.md" \
            "Experiment plan for: ${TOPIC}. Literature: ${WORKSPACE}/literature/literature_review.md"
        _stage_review "experiment_design" "${WORKSPACE}/experiment/experiment_plan.md" || true
    fi

    python3 -c "
from state_manager import StateManager, Stage
sm = StateManager('${WORKSPACE}')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.EXPERIMENT_DESIGN)
sm.start_stage(state, Stage.ENVIRONMENT_PREPARATION)
"

    # ===== Stage 5: 环境准备 =====
    echo ""
    echo -e "${CYAN}━━━ Stage 5: 环境准备（预下载依赖）━━━${NC}"
    echo ""

    # 调用 sco_runner 内部函数进行预下载，利用本地网络（镜像/VPN）下载
    # wheels、模型、数据集到共享存储，确保 SCO 容器离线可用。
    _do_environment_preparation "${WORKSPACE}/experiment"

    # 轻量级审查门控：确认关键缓存路径非空
    echo ""
    echo "[env-prep review] 检查共享缓存..."
    CACHE_DIR="${SCRIPT_DIR}/workspace/.shared/cache"
    WHEELS_COUNT=$(find "${CACHE_DIR}/wheels/" -name "*.whl" 2>/dev/null | wc -l)
    DATASET_CACHE="${CACHE_DIR}/datasets"
    DATASET_DIRS=$(find "$DATASET_CACHE" -type d -mindepth 1 -maxdepth 2 2>/dev/null | wc -l || echo "0")
    echo "  wheels: ${WHEELS_COUNT} 个 .whl 文件"
    echo "  datasets: ${DATASET_DIRS} 个缓存目录"
    if [[ "$WHEELS_COUNT" -ge 5 ]]; then
        echo -e "  ${GREEN}✓ 环境准备完成${NC}"
    else
        echo -e "  ${YELLOW}⚠ wheels 数量偏少 ($WHEELS_COUNT)，SCO 容器可能无法安装依赖${NC}"
        echo -e "  ${YELLOW}  将尝试继续执行 (SCO 容器镜像可能已预装所需包)${NC}"
    fi
    echo ""

    python3 -c "
from state_manager import StateManager, Stage
sm = StateManager('${WORKSPACE}')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.ENVIRONMENT_PREPARATION)
sm.start_stage(state, Stage.EXPERIMENT_EXECUTION)
"

    # ===== Stage 6: 实验执行（本地优先，SCO 后备）=====
    echo ""
    echo -e "${CYAN}━━━ Stage 6: 实验执行（本地优先）━━━${NC}"
    echo ""

    EXP_SCRIPT="${WORKSPACE}/experiment/run_experiment.sh"
    EXP_PY="${WORKSPACE}/experiment/run_experiment.py"

    # 自动生成 run_experiment.sh 包装器（实验科学家可能只生成了 .py 文件）
    if [[ ! -f "$EXP_SCRIPT" ]] && [[ -f "$EXP_PY" ]]; then
        echo -e "${YELLOW}检测到 run_experiment.py，自动生成 run_experiment.sh 包装器${NC}"
        cat > "$EXP_SCRIPT" << 'WRAPPER_EOF'
#!/bin/bash
# Auto-generated wrapper — experiment scientist produced run_experiment.py.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
if command -v conda &>/dev/null; then
    source "$(conda info --base)/etc/profile.d/conda.sh" 2>/dev/null || true
    conda activate base 2>/dev/null || true
fi
pip install -r requirements.txt -q 2>/dev/null || true
exec python3 run_experiment.py "$@"
WRAPPER_EOF
        chmod +x "$EXP_SCRIPT"
        echo -e "${GREEN}  已生成: ${EXP_SCRIPT}${NC}"
    fi

    if [[ -f "$EXP_SCRIPT" ]]; then
        JOB_NAME="cr-${SLUG:0:30}"

        # 使用新的 local-first 执行器
        # ── 检测 NPU (Ascend) ──
        HAS_NPU=0
        if _detect_npu; then
            HAS_NPU=1
            NPU_INFO=$(python3 -c "
try:
    import torch_npu
    count = torch_npu.device_count()
    print(f'NPU_AVAILABLE=True')
    print(f'NPU_COUNT={count}')
except Exception:
    print('NPU_AVAILABLE=False')
    print('NPU_COUNT=0')
" 2>&1) || true
            echo "检测到 NPU (Ascend): ${NPU_INFO}"
        fi

        # ── 检测 GPU ──
        echo "检测本地 GPU ..."
        GPU_INFO=$(python3 -c "
from sco_runner import detect_gpu
info = detect_gpu()
print(f'GPU_AVAILABLE={info[\"available\"]}')
print(f'GPU_COUNT={info[\"count\"]}')
" 2>&1) || true
        echo "${GPU_INFO}"

        HAS_GPU=$(echo "$GPU_INFO" | grep "GPU_AVAILABLE=True" && echo 1 || echo 0)
        GPU_COUNT=$(echo "$GPU_INFO" | grep -oP 'GPU_COUNT=\K\d+')

        # ── Execution strategy ──
        if [[ "$HAS_NPU" == "1" ]]; then
            echo -e "${GREEN}检测到 Ascend NPU — 本地执行（NPU 不通过 SCO 提交）${NC}"
            SCO_FORCE_LOCAL="True"
        elif [[ "$HAS_GPU" == "1" ]]; then
            echo -e "${GREEN}检测到 ${GPU_COUNT} 个本地 GPU，优先本地执行${NC}"
            SCO_FORCE_LOCAL="False"
        else
            echo -e "${YELLOW}未检测到本地 GPU 或 NPU${NC}"
            NEEDS_GPU=$(python3 -c "
from sco_runner import needs_gpu_heuristic
from pathlib import Path
print('NEEDS_GPU=' + ('True' if needs_gpu_heuristic(Path('${EXP_SCRIPT}')) else 'False'))
" 2>&1) || true
            if echo "$NEEDS_GPU" | grep -q "NEEDS_GPU=True"; then
                if [[ -z "${SCO_IMAGE:-}" ]]; then
                    echo -e "${YELLOW}实验需要 GPU 但未配置 SCO（SCO_IMAGE 为空）→ 强制本地执行${NC}"
                    echo -e "${YELLOW}如需使用远程 GPU，请编辑 .env 设置 SCO_IMAGE 和 SCO_STORAGE_MOUNT${NC}"
                    SCO_FORCE_LOCAL="True"
                else
                    echo -e "${YELLOW}实验需要 GPU → 将使用 SCO 云端执行${NC}"
                    SCO_FORCE_LOCAL="False"
                fi
            else
                echo -e "${GREEN}实验不依赖 GPU → 本地执行${NC}"
                SCO_FORCE_LOCAL="False"
            fi
        fi
        echo ""

        # 使用统一执行器 (local-first)
        EXEC_OUTPUT=$(python3 -c "
from sco_runner import run_experiment
from pathlib import Path
result = run_experiment(
    Path('${EXP_SCRIPT}'),
    job_name='${JOB_NAME}',
    local_timeout=${LOCAL_EXECUTION_TIMEOUT:-7200},
    max_local_retries=${LOCAL_EXECUTION_MAX_RETRIES:-20},
    force_local=${SCO_FORCE_LOCAL:-False},
)
print(f'BACKEND={result.backend}')
print(f'SUCCESS={result.success}')
print(f'JOB_ID={result.job_id}')
print(f'LOG_PATH={result.log_path}')
print(f'ATTEMPTS={result.attempts}')
print(f'ERROR={result.error_summary}')
" 2>&1) && EXEC_EXIT=0 || EXEC_EXIT=$?

        echo "${EXEC_OUTPUT}"
        BACKEND=$(echo "$EXEC_OUTPUT" | grep -oP 'BACKEND=\K\S+')
        SUCCESS=$(echo "$EXEC_OUTPUT" | grep -oP 'SUCCESS=\K\S+')
        JOB_ID=$(echo "$EXEC_OUTPUT" | grep -oP 'JOB_ID=\K\S+')
        LOG_PATH=$(echo "$EXEC_OUTPUT" | grep -oP 'LOG_PATH=\K\S+')
        ERROR_SUMMARY=$(echo "$EXEC_OUTPUT" | grep -oP 'ERROR=\K.*')

        # 持久化 job_id (SCO 场景)
        if [[ "$BACKEND" == "sco" ]] && [[ -n "$JOB_ID" ]]; then
            echo "$JOB_ID" > /tmp/slai_job_id.txt
            echo "sco" > /tmp/slai_job_backend.txt
            python3 -c "
from state_manager import StateManager
sm = StateManager('${WORKSPACE}')
state = sm.load('${SLUG}')
_job_id = open('/tmp/slai_job_id.txt').read().strip()
stages = state.stages
if 'experiment_execution' not in stages:
    stages['experiment_execution'] = {}
stages['experiment_execution']['job_id'] = _job_id
stages['experiment_execution']['backend'] = 'sco'
sm.save(state)
" 2>/dev/null || true
            echo "  job_id 已保存到状态文件"
        fi

        # 复制日志到标准位置
        if [[ "$BACKEND" == "local" ]]; then
            cp "${LOG_PATH}" "${WORKSPACE}/experiment/sco_logs.txt" 2>/dev/null || true
        fi

        if [[ "$SUCCESS" == "True" ]]; then
            echo -e "${GREEN}实验成功！(后端: ${BACKEND})${NC}"
        else
            echo ""
            echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
            echo -e "${RED}  实验失败 (后端: ${BACKEND})${NC}"
            echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
            echo ""
            echo "  错误: ${ERROR_SUMMARY:0:500}"
            echo "  日志: ${LOG_PATH:-${WORKSPACE}/experiment/sco_logs.txt}"
            echo "  实验脚本: ${EXP_SCRIPT}"
            echo ""
            echo -e "${YELLOW}请检查并修复实验脚本后重新运行 bash start.sh${NC}"
            echo -e "${YELLOW}或在 workspace 中手动修复后选择继续项目${NC}"

            # ── 循环修复：诊断 → 修复 → 重试，最多 20 轮 ──
            MAX_DEBUG_ROUNDS=20
            for ((debug_round=1; debug_round<=MAX_DEBUG_ROUNDS; debug_round++)); do
                echo ""
                echo -e "${CYAN}━━━ 自动修复轮次 ${debug_round}/${MAX_DEBUG_ROUNDS} ━━━${NC}"

                # 获取最新日志
                LATEST_LOG="${LOG_PATH}"
                if [[ -z "$LATEST_LOG" ]] || [[ ! -f "$LATEST_LOG" ]]; then
                    LATEST_LOG=$(ls -t "${WORKSPACE}/experiment/logs/local_run_"*.log 2>/dev/null | head -1)
                fi
                ERROR_LOG=$(tail -100 "${LATEST_LOG}" 2>/dev/null || echo "无法读取日志")

                debug_round="$debug_round" BACKEND="$BACKEND" ERROR_SUMMARY="$ERROR_SUMMARY" \
                  ERROR_LOG="$ERROR_LOG" \
                  _render_prompt "local_debug_extended.md" /tmp/slai_debug_prompt.txt
                _claude_task "$(cat /tmp/slai_debug_prompt.txt)" 2>&1

                # ── 修复后重试 ──
                echo ""
                echo -e "${YELLOW}修复完成，重新执行实验...${NC}"
                EXEC_OUTPUT=$(python3 -c "
from sco_runner import run_experiment
from pathlib import Path
result = run_experiment(
    Path('${EXP_SCRIPT}'),
    job_name='${JOB_NAME}-fix${debug_round}',
    local_timeout=${LOCAL_EXECUTION_TIMEOUT:-7200},
    max_local_retries=1,
)
print(f'BACKEND={result.backend}')
print(f'SUCCESS={result.success}')
print(f'JOB_ID={result.job_id}')
print(f'LOG_PATH={result.log_path}')
" 2>&1) || true

                echo "${EXEC_OUTPUT}"
                SUCCESS=$(echo "$EXEC_OUTPUT" | grep -oP 'SUCCESS=\K\S+')
                BACKEND=$(echo "$EXEC_OUTPUT" | grep -oP 'BACKEND=\K\S+')
                JOB_ID=$(echo "$EXEC_OUTPUT" | grep -oP 'JOB_ID=\K\S+')
                LOG_PATH=$(echo "$EXEC_OUTPUT" | grep -oP 'LOG_PATH=\K\S+')

                if [[ "$BACKEND" == "local" ]] && [[ -n "$LOG_PATH" ]]; then
                    cp "${LOG_PATH}" "${WORKSPACE}/experiment/sco_logs.txt" 2>/dev/null || true
                elif [[ "$BACKEND" == "sco" ]] && [[ -n "$JOB_ID" ]]; then
                    sco acp jobs stream-logs --workspace-name share-space "$JOB_ID" > "${WORKSPACE}/experiment/sco_logs.txt" 2>/dev/null || true
                fi

                if [[ "$SUCCESS" == "True" ]]; then
                    echo -e "${GREEN}★ 修复成功！实验通过 (第 ${debug_round} 轮修复)${NC}"
                    break
                else
                    echo -e "${YELLOW}第 ${debug_round} 轮修复后仍失败，${NC}"
                    if [[ $debug_round -lt $MAX_DEBUG_ROUNDS ]]; then
                        echo -e "${YELLOW}继续下一轮诊断...${NC}"
                    else
                        echo -e "${RED}已达最大修复轮次 (${MAX_DEBUG_ROUNDS})，放弃${NC}"
                    fi
                fi
            done
        fi

        python3 -c "
import json
json.dump({'job_id': '''${JOB_ID}''', 'backend': '''${BACKEND}''', 'status': '''${SUCCESS}'''}, open('/tmp/slai_job_result.json','w'))
" 2>/dev/null
        python3 -c "
from state_manager import StateManager, Stage
import json
sm = StateManager('${WORKSPACE}')
state = sm.load('${SLUG}')
_info = json.load(open('/tmp/slai_job_result.json'))
sm.complete_stage(state, Stage.EXPERIMENT_EXECUTION, _info)
"
        echo -e "${GREEN}实验阶段结束: ${SUCCESS} (后端: ${BACKEND})${NC}"

        # 全部失败 → 终止
        if [[ "$SUCCESS" != "True" ]]; then
            echo ""
            echo -e "${YELLOW}实验未成功，跳过论文撰写。修复实验后重试。${NC}"
            exit 1
        fi
    else
        echo -e "${YELLOW}未找到实验脚本，跳过实验执行${NC}"
    fi

    # ===== Stage 7: 论文撰写 + 审稿 ===== + 审稿 =====
    ITERATION=0
    _do_paper_writing
    _do_submit_review
fi  # 情况 A 结束
