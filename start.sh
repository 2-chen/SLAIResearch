#!/usr/bin/env bash
# =============================================================================
# ChenResearch — 代码驱动的会话管理器
# start.sh 是真正的控制器，Claude Code 是执行工具。
# 每次启动：检查 state → 确定当前阶段 → 生成精准 prompt → claude -p 执行
# 审稿后自动退出，下次重开继续迭代 — 保持每轮上下文干净。
# =============================================================================
set -uo pipefail  # 不用 set -e，关键节点显式错误处理

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 确保 SCO CLI 在 PATH 中
[[ -d "${HOME}/.sco/bin" ]] && export PATH="${HOME}/.sco/bin:${PATH}"

# Ctrl-C 优雅中断
trap 'echo -e "\n${YELLOW}收到中断信号，保存状态后退出...${NC}"; exit 130' INT TERM
cd "${SCRIPT_DIR}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

banner() {
    echo ""
    echo -e "${CYAN}╔══════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║        ChenResearch — 全自动科研系统         ║${NC}"
    echo -e "${CYAN}╚══════════════════════════════════════════════╝${NC}"
    echo ""
}

# ---------------------------------------------------------------------------
# 首次运行配置（+ 容器重启后自动修复）
# ---------------------------------------------------------------------------
NEED_CONFIG=0

# 检查配置标记文件
if [[ ! -f ".chenresearch_configured" ]]; then
    NEED_CONFIG=1
fi

# 检查 settings.json 中的 API Key 是否有效（容器重启可能重置）
if [[ -f ".claude/settings.json" ]]; then
    SETTINGS_KEY=$(python -c "
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
    echo "┌──────────────────────────────────────────────┐"
    echo "│  首次运行 — API Key 配置                      │"
    echo "└──────────────────────────────────────────────┘"
    echo ""
    read -rp "DeepSeek API Key: " CLAUDE_API_KEY
    CLAUDE_API_KEY="${CLAUDE_API_KEY:-sk-5d8ed00d568645efb4f6a544160b3849}"
    read -rp "模型名称 [deepseek-v4-pro]: " CLAUDE_MODEL
    CLAUDE_MODEL="${CLAUDE_MODEL:-deepseek-v4-pro}"
    read -rp "API Base URL [https://api.deepseek.com/anthropic]: " CLAUDE_BASE_URL
    CLAUDE_BASE_URL="${CLAUDE_BASE_URL:-https://api.deepseek.com/anthropic}"

    cat > .env <<EOF
export CLAUDE_MODEL="${CLAUDE_MODEL}"
export CLAUDE_BASE_URL="${CLAUDE_BASE_URL}"
export ANTHROPIC_BASE_URL="${CLAUDE_BASE_URL}"
export ANTHROPIC_API_KEY="${CLAUDE_API_KEY}"
export CLAUDE_API_KEY="${CLAUDE_API_KEY}"
export SEMANTIC_SCHOLAR_API_KEY="s2k-TxOJNhO0O615j3huoEbRfhfIUfnzoXLE2V9ZfEaq"
export PAPERREVIEW_EMAIL="250010008@slai.edu.cn"
export PAPERREVIEW_VENUE="AAAI"

# 实验执行：本地优先
export CHENRESEARCH_LOCAL_TIMEOUT=7200
export CHENRESEARCH_LOCAL_MAX_RETRIES=20
export CHENRESEARCH_FORCE_SCO=false
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
    touch .chenresearch_configured
    echo -e "${GREEN}✓ 配置完成${NC}"
fi

source .env 2>/dev/null || true

# ★ 自动修复 settings.json（容器重启可能导致 key 变回占位符）
if [[ -f ".claude/settings.json" ]] && [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
    SETTINGS_KEY=$(python -c "
import json
d = json.load(open('.claude/settings.json'))
print(d.get('env',{}).get('ANTHROPIC_API_KEY',''))
" 2>/dev/null)
    if [[ "$SETTINGS_KEY" == "your-api-key-here" ]] || [[ -z "$SETTINGS_KEY" ]]; then
        echo -e "${YELLOW}[auto-fix] 修复 settings.json 中的 API Key ...${NC}"
        python -c "
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
# Claude Code 调用辅助
# ---------------------------------------------------------------------------
_claude_task() {
    local prompt="$1"
    local log="${2:-/tmp/cr_claude_output.txt}"

    echo -e "${CYAN}  Claude Code 正在工作中...${NC}"
    echo "  (输出实时显示，可能需要几分钟)"

    # 实时输出到终端 + 同时保存到日志
    echo "$prompt" | claude -p --model "${CLAUDE_MODEL:-deepseek-v4-pro}" --output-format text 2>&1 | tee "$log"

    local rc=${PIPESTATUS[0]}
    echo ""
    if [[ $rc -eq 0 ]]; then
        echo -e "${GREEN}  Claude Code 完成${NC}"
    else
        echo -e "${YELLOW}  Claude Code 退出码: $rc${NC}"
    fi
    return $rc
}

# 故障接管：遇到报错时保存状态 → 启动 Claude Code 诊断修复
_on_error() {
    local stage="$1"
    local err_msg="$2"
    local ws="$3"

    # 保存错误状态（项目不丢）
    if [[ -n "${SLUG:-}" ]]; then
        python -c "
from state_manager import StateManager
sm = StateManager('state')
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

    cat > /tmp/cr_recover_prompt.txt << PROMPT_EOF
你是 ChenResearch 科研系统的故障恢复助手。流水线在 **${stage}** 阶段出错。

**错误信息**:
${err_msg}

**工作目录**: ${ws}

**你的任务**:
1. 检查工作目录下的文件，理解当前状态
2. 诊断错误原因并尝试修复
3. 如果修复成功，报告 "RECOVERY_OK"
4. 如果无法修复，报告 "RECOVERY_FAILED" 并说明原因

**关键**: 项目文件和工作目录都已保留，修复后流水线会继续。不要重新执行已完成的阶段。
PROMPT_EOF

    if _claude_task "$(cat /tmp/cr_recover_prompt.txt)" 2>&1 | grep -q "RECOVERY_OK"; then
        echo -e "${GREEN}Claude Code 修复成功，继续流水线${NC}"
    else
        echo -e "${YELLOW}Claude Code 无法完全修复，但项目已保留${NC}"
        echo -e "${YELLOW}后续阶段将继续执行（跳过当前阶段）${NC}"
    fi
    echo ""
}

# ---------------------------------------------------------------------------
# ---- _continue_experiment: 继续实验执行阶段 (local-first) ----
_continue_experiment() {
    echo -e "${CYAN}━━━ 继续实验执行 ━━━${NC}"
    echo ""

    # 检查之前用什么后端 (从 meta 子对象读取，向下兼容顶层)
    BACKEND=$(python -c "
import json
d = json.load(open('state/${SLUG}/state.json'))
ee = d.get('stages',{}).get('experiment_execution',{})
meta = ee.get('meta',{})
print(meta.get('backend') or ee.get('backend') or 'sco')
" 2>/dev/null)

    # 从 state 中获取 job_id (仅 SCO 场景) — 从 meta 子对象读取，向下兼容顶层
    if [[ "$BACKEND" == "sco" ]]; then
        JOB_ID=$(python -c "
import json
d = json.load(open('state/${SLUG}/state.json'))
ee = d.get('stages',{}).get('experiment_execution',{})
meta = ee.get('meta',{})
print(meta.get('job_id') or ee.get('job_id') or '')
" 2>/dev/null)
    else
        JOB_ID=""
    fi

    # 过滤无效 job_id：如果是完整 URI（之前 bug 存的），丢弃
    if [[ -n "$JOB_ID" ]] && [[ "$JOB_ID" == /subscriptions/* ]]; then
        echo -e "${YELLOW}检测到无效 job_id (完整 URI 格式)，清除并重新执行${NC}"
        JOB_ID=""
    fi

    if [[ "$BACKEND" != "sco" ]]; then
        echo -e "${YELLOW}之前实验使用 ${BACKEND} 后端执行${NC}"
        # 本地执行：检查日志是否存在
        LOCAL_LOG="${WORKSPACE}/experiment/logs/local_run_01.log"
        if [[ -f "$LOCAL_LOG" ]]; then
            echo "找到本地执行日志: ${LOCAL_LOG}"
            cp "$LOCAL_LOG" "${WORKSPACE}/experiment/sco_logs.txt" 2>/dev/null || true
            echo -e "${GREEN}本地实验已完成（日志已恢复）${NC}"
            # 标记完成并继续到论文撰写
            python -c "
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.EXPERIMENT_EXECUTION, {'backend': '${BACKEND}', 'status': 'completed'})
"
            _do_paper_writing
            _do_submit_review
            exit 0
        else
            echo -e "${YELLOW}未找到本地日志，重新执行实验（本地优先）...${NC}"
            EXP_SCRIPT="${WORKSPACE}/experiment/run_experiment.sh"
            if [[ -f "$EXP_SCRIPT" ]]; then
                JOB_NAME="cr-${SLUG:0:30}"
                EXEC_OUTPUT=$(python -c "
from sco_runner import run_experiment
from pathlib import Path
result = run_experiment(Path('${EXP_SCRIPT}'), '${JOB_NAME}')
print(f'BACKEND={result.backend}')
print(f'SUCCESS={result.success}')
print(f'JOB_ID={result.job_id}')
print(f'LOG_PATH={result.log_path}')
" 2>&1) || true
                echo "${EXEC_OUTPUT}"
                SUCCESS=$(echo "$EXEC_OUTPUT" | grep -oP 'SUCCESS=\K\S+')
                BACKEND=$(echo "$EXEC_OUTPUT" | grep -oP 'BACKEND=\K\S+')
                JOB_ID=$(echo "$EXEC_OUTPUT" | grep -oP 'JOB_ID=\K\S+')
                if [[ "$BACKEND" == "local" ]]; then
                    cp "$(echo "$EXEC_OUTPUT" | grep -oP 'LOG_PATH=\K\S+')" "${WORKSPACE}/experiment/sco_logs.txt" 2>/dev/null || true
                fi
            else
                echo -e "${RED}未找到实验脚本，无法继续${NC}"
                exit 1
            fi
        fi
    elif [[ -n "$JOB_ID" ]]; then
        echo "已有 SCO 任务: ${JOB_ID}，检查状态..."
        STATUS=$(sco acp jobs describe --workspace-name share-space -o json "$JOB_ID" 2>/dev/null | python -c "import json,sys; print(json.load(sys.stdin).get('state','UNKNOWN'))" 2>/dev/null || echo "UNKNOWN")
        echo "当前状态: ${STATUS}"

        if [[ "$STATUS" == "SUCCEEDED" ]]; then
            echo -e "${GREEN}实验已完成！${NC}"
            sco acp jobs stream-logs --workspace-name share-space "$JOB_ID" > "${WORKSPACE}/experiment/sco_logs.txt" 2>/dev/null || true
        elif [[ "$STATUS" == "RUNNING" || "$STATUS" == "PENDING" ]]; then
            echo "等待任务完成..."
            for i in $(seq 1 180); do
                STATUS=$(sco acp jobs describe --workspace-name share-space -o json "$JOB_ID" 2>/dev/null | python -c "import json,sys; print(json.load(sys.stdin).get('state','UNKNOWN'))" 2>/dev/null || echo "UNKNOWN")
                echo "  状态: ${STATUS} (${i}/180)"
                if [[ "$STATUS" == "SUCCEEDED" || "$STATUS" == "FAILED" || "$STATUS" == "STOPPED" || "$STATUS" == "UNKNOWN" ]]; then
                    break
                fi
                sleep 30
            done
            sco acp jobs stream-logs --workspace-name share-space "$JOB_ID" > "${WORKSPACE}/experiment/sco_logs.txt" 2>/dev/null || true
        else
            # FAILED / STOPPED / UNKNOWN / 其他 — 诊断 → 修复 → 重新提交 SCO
            echo -e "${YELLOW}SCO 任务不可用 (${STATUS})，先诊断失败原因...${NC}"
            sco acp jobs stream-logs --workspace-name share-space "$JOB_ID" > "${WORKSPACE}/experiment/sco_logs.txt" 2>/dev/null || true

            # 提取 SCO 日志中关键错误行
            SCO_ERROR_TAIL=$(tail -100 "${WORKSPACE}/experiment/sco_logs.txt" 2>/dev/null || echo "无法读取 SCO 日志")
            ERROR_KEY_LINES=$(echo "$SCO_ERROR_TAIL" | grep -iE "error|fail|exception|traceback|killed|oom|cuda|abort|segfault|module.*not found|no module|import error|command not found|no such file|permission denied|cannot find|could not find|timed out|connection refused" | tail -20 || echo "未找到明显错误关键字")

            echo "关键错误行 (SCO 日志):"
            echo "$ERROR_KEY_LINES" | head -10

            # ── 自动修复循环：诊断 SCO 日志 → 修脚本 → 重新提交 SCO ──
            SCO_DEBUG_ROUNDS=20
            EXP_SCRIPT="${WORKSPACE}/experiment/run_experiment.sh"
            if [[ ! -f "$EXP_SCRIPT" ]]; then
                echo -e "${RED}未找到实验脚本 (${EXP_SCRIPT})，无法继续${NC}"
                exit 1
            fi

            JOB_NAME="cr-${SLUG:0:30}"

            for ((sco_round=1; sco_round<=SCO_DEBUG_ROUNDS; sco_round++)); do
                echo ""
                echo -e "${CYAN}━━━ SCO 修复轮次 ${sco_round}/${SCO_DEBUG_ROUNDS} ━━━${NC}"

                # 用 Claude Code 诊断 SCO 日志并修复脚本
                cat > /tmp/cr_sco_debug_prompt.txt << PROMPT_EOF
你是实验调试专家。实验在 SCO 云端 GPU 集群上执行失败了（第 ${sco_round} 轮修复），请诊断并修复。

**后端**: SCO (云端 GPU)
**SCO 任务状态**: ${STATUS}
**SCO 错误日志 (关键行)**:
${ERROR_KEY_LINES}

**完整 SCO 日志尾部 (最后100行)**:
${SCO_ERROR_TAIL}

**实验脚本** ($(wc -l < "${EXP_SCRIPT}" 2>/dev/null) 行): ${EXP_SCRIPT}
**工作目录**: ${WORKSPACE}/experiment/

你的任务:
1. 仔细分析 SCO 错误日志，找出失败根因
2. 注意之前轮次的修复尝试（如果有重复错误）
3. 修改实验脚本或项目代码来修复问题
4. 保存修改后的文件
5. 报告 "FIX_READY" 表示已修复

常见 SCO 失败及修复:
- OOM / CUDA out of memory → 减小 batch_size, 减小模型, 加 gradient_accumulation
- ModuleNotFoundError / ImportError → 在脚本中添加 pip install
- CUDA / driver 不兼容 → 调整 CUDA_VISIBLE_DEVICES 或安装兼容版本
- 数据路径不存在 → 修正路径或先下载数据
- 脚本超时 (12h) → 减小数据量或增加 checkpoint 续跑
- 权限问题 → 修正文件权限或路径
PROMPT_EOF
                _claude_task "$(cat /tmp/cr_sco_debug_prompt.txt)" 2>&1

                # ── 修复后重新提交 SCO（不强制本地！）──
                echo ""
                echo -e "${YELLOW}修复完成，重新提交 SCO 任务...${NC}"
                EXEC_OUTPUT=$(python -c "
from sco_runner import run_experiment
from pathlib import Path
result = run_experiment(
    Path('${EXP_SCRIPT}'),
    job_name='${JOB_NAME}-fix${sco_round}',
    local_timeout=${CHENRESEARCH_LOCAL_TIMEOUT:-7200},
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

                # 如果是 SCO 后端，流式获取日志
                if [[ "$BACKEND" == "sco" ]] && [[ -n "$JOB_ID" ]]; then
                    sco acp jobs stream-logs --workspace-name share-space "$JOB_ID" > "${WORKSPACE}/experiment/sco_logs.txt" 2>/dev/null || true
                    # 更新错误日志供下一轮诊断
                    SCO_ERROR_TAIL=$(tail -100 "${WORKSPACE}/experiment/sco_logs.txt" 2>/dev/null || echo "无法读取日志")
                    ERROR_KEY_LINES=$(echo "$SCO_ERROR_TAIL" | grep -iE "error|fail|exception|traceback|killed|oom|cuda|abort|segfault|module.*not found|no module|import error|command not found|no such file|permission denied|cannot find|timed out|connection refused" | tail -20 || echo "未找到明显错误关键字")
                elif [[ "$BACKEND" == "local" ]] && [[ -n "$LOG_PATH" ]]; then
                    cp "${LOG_PATH}" "${WORKSPACE}/experiment/sco_logs.txt" 2>/dev/null || true
                fi

                if [[ "$SUCCESS" == "True" ]]; then
                    echo -e "${GREEN}★ SCO 修复成功！实验通过 (第 ${sco_round} 轮修复, 后端: ${BACKEND})${NC}"
                    STATUS="SUCCEEDED"
                    break
                else
                    echo -e "${YELLOW}第 ${sco_round} 轮修复后仍失败 (后端: ${BACKEND})${NC}"
                    if [[ $sco_round -lt $SCO_DEBUG_ROUNDS ]]; then
                        echo -e "${YELLOW}继续下一轮 SCO 诊断...${NC}"
                    else
                        echo -e "${RED}已达最大 SCO 修复轮次 (${SCO_DEBUG_ROUNDS})${NC}"
                        STATUS="FAILED"
                    fi
                fi
            done

            # ── 最终兜底：SCO 修复全部失败 → 尝试本地执行 ──
            if [[ "$STATUS" != "SUCCEEDED" ]]; then
                echo ""
                echo -e "${YELLOW}SCO 修复全部失败，最后尝试本地执行...${NC}"
                EXEC_OUTPUT=$(python -c "
from sco_runner import run_experiment
from pathlib import Path
result = run_experiment(
    Path('${EXP_SCRIPT}'),
    job_name='${JOB_NAME}-local-fallback',
    local_timeout=${CHENRESEARCH_LOCAL_TIMEOUT:-7200},
    max_local_retries=1,
    force_local=True,
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
                fi
                if [[ "$SUCCESS" == "True" ]]; then
                    echo -e "${GREEN}本地执行成功！${NC}"
                    STATUS="SUCCEEDED"
                fi
            fi
        fi
    else
        # 状态里没有 job_id，尝试按名称发现已有 SCO 任务
        JOB_NAME_PATTERN="cr-${SLUG:0:25}"
        echo -e "${YELLOW}状态文件中无 job_id，尝试按名称发现已有任务...${NC}"
        echo "  搜索模式: ${JOB_NAME_PATTERN}"

        # 列出最近的任务，按 display_name 匹配，提取 short name (非 URI id)
        FOUND_JOB=$(sco acp jobs list --workspace-name share-space -o json 2>/dev/null | python -c "
import json, sys
try:
    jobs = json.load(sys.stdin)
    if isinstance(jobs, list):
        for j in jobs:
            dn = j.get('display_name', '') or ''
            if '${JOB_NAME_PATTERN}' in dn:
                # 打印 short name (pt-xxx), 非完整 URI id
                print(j.get('name', ''))
                break
except: pass
" 2>/dev/null)

        # ── 决定是复用旧任务还是重新执行 ──
        REUSE_JOB=0
        if [[ -n "$FOUND_JOB" ]]; then
            echo -e "${GREEN}发现已有任务: ${FOUND_JOB}${NC}"
            STATUS=$(sco acp jobs describe --workspace-name share-space -o json "$FOUND_JOB" 2>/dev/null | python -c "import json,sys; print(json.load(sys.stdin).get('state','UNKNOWN'))" 2>/dev/null || echo "UNKNOWN")
            echo "当前状态: ${STATUS}"

            if [[ "$STATUS" == "SUCCEEDED" ]]; then
                echo -e "${GREEN}实验已完成！${NC}"
                sco acp jobs stream-logs --workspace-name share-space "$FOUND_JOB" > "${WORKSPACE}/experiment/sco_logs.txt" 2>/dev/null || true
                REUSE_JOB=1
                JOB_ID="$FOUND_JOB"
            elif [[ "$STATUS" == "RUNNING" || "$STATUS" == "PENDING" ]]; then
                echo "等待任务完成..."
                for i in $(seq 1 180); do
                    STATUS=$(sco acp jobs describe --workspace-name share-space -o json "$FOUND_JOB" 2>/dev/null | python -c "import json,sys; print(json.load(sys.stdin).get('state','UNKNOWN'))" 2>/dev/null || echo "UNKNOWN")
                    echo "  状态: ${STATUS} (${i}/180)"
                    if [[ "$STATUS" == "SUCCEEDED" || "$STATUS" == "FAILED" || "$STATUS" == "STOPPED" || "$STATUS" == "UNKNOWN" ]]; then
                        break
                    fi
                    sleep 30
                done
                sco acp jobs stream-logs --workspace-name share-space "$FOUND_JOB" > "${WORKSPACE}/experiment/sco_logs.txt" 2>/dev/null || true
                if [[ "$STATUS" == "SUCCEEDED" ]]; then
                    REUSE_JOB=1
                    JOB_ID="$FOUND_JOB"
                else
                    echo -e "${YELLOW}等待结束但任务状态为 ${STATUS}，回退到本地执行${NC}"
                    # REUSE_JOB 保持 0，继续到本地执行
                fi
            else
                # 失败或未知状态 → 获取日志后回退到本地执行
                echo -e "${YELLOW}旧 SCO 任务已失败/未知 (${STATUS})，获取日志后尝试本地执行...${NC}"
                sco acp jobs stream-logs --workspace-name share-space "$FOUND_JOB" > "${WORKSPACE}/experiment/sco_logs.txt" 2>/dev/null || true
                # 不设置 REUSE_JOB，继续到本地执行路径
            fi
        fi

        # ── 如果旧任务不可复用 → 本地优先重新执行 ──
        if [[ "$REUSE_JOB" == "0" ]]; then
            EXP_SCRIPT="${WORKSPACE}/experiment/run_experiment.sh"
            if [[ ! -f "$EXP_SCRIPT" ]]; then
                echo -e "${RED}未找到实验脚本 (${EXP_SCRIPT})，无法继续${NC}"
                exit 1
            fi

            echo "使用默认执行器运行实验（自动判断本地/SCO）..."
            JOB_NAME="cr-${SLUG:0:30}"
            EXEC_OUTPUT=$(python -c "
from sco_runner import run_experiment
from pathlib import Path
result = run_experiment(
    Path('${EXP_SCRIPT}'),
    job_name='${JOB_NAME}',
    local_timeout=${CHENRESEARCH_LOCAL_TIMEOUT:-7200},
    max_local_retries=${CHENRESEARCH_LOCAL_MAX_RETRIES:-20},
)
print(f'BACKEND={result.backend}')
print(f'SUCCESS={result.success}')
print(f'JOB_ID={result.job_id}')
print(f'LOG_PATH={result.log_path}')
" 2>&1) && EXEC_EXIT=0 || EXEC_EXIT=$?

            echo "${EXEC_OUTPUT}"
            BACKEND=$(echo "$EXEC_OUTPUT" | grep -oP 'BACKEND=\K\S+')
            SUCCESS=$(echo "$EXEC_OUTPUT" | grep -oP 'SUCCESS=\K\S+')
            JOB_ID=$(echo "$EXEC_OUTPUT" | grep -oP 'JOB_ID=\K\S+')
            LOG_PATH=$(echo "$EXEC_OUTPUT" | grep -oP 'LOG_PATH=\K\S+')

            if [[ "$BACKEND" == "local" ]] && [[ -n "$LOG_PATH" ]]; then
                cp "${LOG_PATH}" "${WORKSPACE}/experiment/sco_logs.txt" 2>/dev/null || true
            elif [[ "$BACKEND" == "sco" ]] && [[ -n "$JOB_ID" ]]; then
                sco acp jobs stream-logs --workspace-name share-space "$JOB_ID" > "${WORKSPACE}/experiment/sco_logs.txt" 2>/dev/null || true
            fi

            if [[ "$SUCCESS" == "True" ]]; then
                echo -e "${GREEN}实验成功！(后端: ${BACKEND})${NC}"
                STATUS="SUCCEEDED"
            else
                echo -e "${YELLOW}实验未成功 (后端: ${BACKEND})，启动 Claude Code 自动修复循环...${NC}"
                STATUS="FAILED"

                # ── 循环修复：诊断 → 修复 → 重试，最多 3 轮 ──
                MAX_DEBUG_ROUNDS=20
                for ((debug_round=1; debug_round<=MAX_DEBUG_ROUNDS; debug_round++)); do
                    echo ""
                    echo -e "${CYAN}━━━ 自动修复轮次 ${debug_round}/${MAX_DEBUG_ROUNDS} ━━━${NC}"

                    # 获取最新日志 (本地或 SCO)
                    LATEST_LOG=$(ls -t "${WORKSPACE}/experiment/logs/local_run_"*.log 2>/dev/null | head -1)
                    if [[ -z "$LATEST_LOG" ]] || [[ ! -f "$LATEST_LOG" ]]; then
                        LATEST_LOG="${WORKSPACE}/experiment/sco_logs.txt"
                    fi
                    ERROR_LOG=$(tail -100 "${LATEST_LOG}" 2>/dev/null || echo "无法读取日志")

                    cat > /tmp/cr_debug_prompt.txt << PROMPT_EOF
你是实验调试专家。实验执行失败了（第 ${debug_round} 轮修复），请诊断并修复。

**后端**: ${BACKEND}
**上一轮修复后的错误日志 (最后100行)**:
${ERROR_LOG}

**实验脚本**: ${EXP_SCRIPT}
**工作目录**: ${WORKSPACE}/experiment/

你的任务:
1. 仔细分析错误日志，找出失败原因
2. 注意之前轮次的修复尝试（如果有）
3. 修改实验脚本或代码来修复问题
4. 保存修改后的文件
5. 报告 "FIX_READY" 表示已修复

常见问题及修复:
- 依赖缺失 (如 python3-venv) → 先 apt-get install，再让脚本正常工作
- 路径错误 → 修正文件路径
- 虚拟环境损坏 → 删除 .venv 目录让脚本重建，或跳过 venv 直接用系统 Python
- 语法错误 → 修正代码
- pip 不可用 → 使用 python3 -m pip 代替裸 pip
- 环境不兼容 → 修改脚本适配当前环境
- OOM / CUDA out of memory → 减小 batch_size 或模型大小
- 实验被超时中断 → 不要删除 checkpoints/ 下的 .pth 文件，run_experiment.sh 已配置 --resume 自动续跑
PROMPT_EOF
                    _claude_task "$(cat /tmp/cr_debug_prompt.txt)" 2>&1

                    # ── 修复后重试 (自动判断本地/SCO，不强制本地) ──
                    echo ""
                    echo -e "${YELLOW}修复完成，重新执行实验...${NC}"
                    EXEC_OUTPUT=$(python -c "
from sco_runner import run_experiment
from pathlib import Path
result = run_experiment(
    Path('${EXP_SCRIPT}'),
    job_name='${JOB_NAME}-fix${debug_round}',
    local_timeout=${CHENRESEARCH_LOCAL_TIMEOUT:-7200},
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
                        STATUS="SUCCEEDED"
                        break
                    else
                        echo -e "${YELLOW}第 ${debug_round} 轮修复后仍失败，${NC}"
                        if [[ $debug_round -lt $MAX_DEBUG_ROUNDS ]]; then
                            echo -e "${YELLOW}继续下一轮诊断...${NC}"
                        else
                            echo -e "${RED}已达最大修复轮次 (${MAX_DEBUG_ROUNDS})，放弃${NC}"
                            STATUS="FAILED"
                        fi
                    fi
                done
            fi
        fi
    fi

    # 更新状态
    python -c "
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.EXPERIMENT_EXECUTION, {'job_id': '${JOB_ID}', 'job_status': '${STATUS}'})
"

    if [[ "$STATUS" != "SUCCEEDED" ]]; then
        echo -e "${RED}实验未成功 (${STATUS})，终止${NC}"
        exit 1
    fi

    # 实验成功 → 进入论文撰写
    echo ""
    echo -e "${GREEN}实验成功！进入论文撰写阶段...${NC}"
    _do_paper_writing
    _do_submit_review
}

# ---- _do_paper_writing: 论文撰写 ----
_do_paper_writing() {
    echo ""
    echo -e "${CYAN}━━━ 论文撰写 ━━━${NC}"
    echo ""

    cp templates/aaai2026.sty "${WORKSPACE}/paper/" 2>/dev/null || true
    cp templates/aaai2026.bst "${WORKSPACE}/paper/" 2>/dev/null || true

    cat > /tmp/cr_stage4_prompt.txt << PROMPT_EOF
你是一个学术论文撰写专家。请撰写完整的 AAAI 2026 格式论文。

研究主题: ${TOPIC}
会议: AAAI 2026

样式文件已放在 ${WORKSPACE}/paper/ 目录下（aaai2026.sty, aaai2026.bst）。
LaTeX 模板参考: templates/aaai.tex.j2

请阅读以下材料：
1. 文献综述: ${WORKSPACE}/literature/literature_review.md
2. 实验日志: ${WORKSPACE}/experiment/sco_logs.txt

请完成：
1. 撰写 LaTeX 论文，必须使用 \\usepackage[submission]{aaai2026} 样式
2. Preamble 必须包含: times, helvet, courier, natbib, caption, graphicx
3. 禁止使用的包: hyperref, authblk, geometry, float, titlesec, setspace, fullpage, ulem
4. 所有数据必须来自真实实验日志，不要编造
5. 保存到: ${WORKSPACE}/paper/paper.tex
6. 编译前确保 aaai2026.sty 和 aaai2026.bst 在同一目录
7. 用 pdflatex 编译为 PDF: ${WORKSPACE}/paper/paper.pdf
8. 保存 BibTeX: ${WORKSPACE}/paper/references.bib

重要：完成后明确报告'论文撰写完成'。
PROMPT_EOF
    _claude_task "$(cat /tmp/cr_stage4_prompt.txt)"

    python -c "
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.PAPER_WRITING)
"
}

# ---- _do_submit_review: 提交审稿 + 等待结果 ----
_do_submit_review() {
    echo ""
    echo -e "${CYAN}━━━ 提交 paperreview.ai 审稿 ━━━${NC}"
    echo ""

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

    # === 提交审稿（错误隔离）===
    TOKEN_RAW=$(python -c "
import sys; sys.path.insert(0, '.')
from paperreview_api import submit_paper
token = submit_paper('${PDF_FILE}', email='250010008@slai.edu.cn', venue='AAAI')
print(token)
" 2>&1)
    local SUBMIT_RC=$?

    # 验证 TOKEN：不包含 traceback 或错误信息
    if [[ $SUBMIT_RC -ne 0 ]] || echo "$TOKEN_RAW" | grep -qE "Traceback|Error:|HTTPError|SyntaxError|Insufficient|429|402|500" 2>/dev/null; then
        echo ""
        echo -e "${RED}══════════════════════════════════════════════${NC}"
        echo -e "${RED}  paperreview.ai 提交失败！${NC}"
        echo -e "${RED}  错误信息: ${TOKEN_RAW:0:500}${NC}"
        echo -e "${RED}══════════════════════════════════════════════${NC}"
        echo ""
        echo -e "${YELLOW}可能原因: API 额度不足 / 频率限制 / 服务不可用${NC}"
        echo -e "${YELLOW}项目已保留在: ${WORKSPACE}${NC}"
        echo -e "${YELLOW}修复后可运行 bash start.sh 继续${NC}"
        exit 1
    fi

    TOKEN="$TOKEN_RAW"
    echo -e "${GREEN}审稿已提交${NC}"
    echo -e "Token: ${YELLOW}${TOKEN}${NC}"

    # 用文件传递 TOKEN 避免 shell 注入
    python -c "
import sys; sys.path.insert(0, '.')
from state_manager import StateManager, Stage, ReviewRecord
sm = StateManager('state')
state = sm.load('${SLUG}')
record = ReviewRecord(iteration=${ITERATION:-0}, token=open('/tmp/cr_token.txt').read().strip(), submitted_at='${PDF_FILE}')
sm.add_review(state, record)
sm.start_stage(state, Stage.POLL_REVIEW)
" 2>/dev/null || {
        echo "$TOKEN" > /tmp/cr_token.txt
        python -c "
import sys; sys.path.insert(0, '.')
from state_manager import StateManager, Stage, ReviewRecord
sm = StateManager('state')
state = sm.load('${SLUG}')
record = ReviewRecord(iteration=${ITERATION:-0}, token=open('/tmp/cr_token.txt').read().strip(), submitted_at='${PDF_FILE}')
sm.add_review(state, record)
sm.start_stage(state, Stage.POLL_REVIEW)
"
    }

    # 内部审稿 + 外部审稿（并行）
    ROUND_DIR="${WORKSPACE}/review/round_$(printf "%03d" ${ITERATION:-0})"
    mkdir -p "${ROUND_DIR}/internal"
    python internal_review.py "${PDF_FILE}" -o "${ROUND_DIR}/internal/" &
    INTERNAL_REVIEW_PID=$!

    echo "等待 paperreview.ai 审稿结果..."
    REVIEW_DATA=$(echo "$TOKEN" | python -c "
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

    # 写入 state
    python -c "
import sys; sys.path.insert(0, '.')
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.load('${SLUG}')
if state.reviews:
    state.reviews[-1]['verdict'] = '${VERDICT}'
    state.reviews[-1]['review_md_path'] = '${ROUND_DIR}/external.md'
sm.complete_stage(state, Stage.POLL_REVIEW, {'verdict': '${VERDICT}'})
"

    if [[ "$VERDICT" == "accept" ]] || [[ "$VERDICT" == "weak accept" ]]; then
        echo -e "${GREEN}★ 论文已通过审稿！${NC}"
        python -c "
from state_manager import StateManager
sm = StateManager('state')
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
        _do_revise_and_resubmit "$VERDICT"
    fi
}

# ---- _run_internal_review: 运行内部多维度审稿 ----
# 参数: $1 = 迭代编号 (用于目录命名)
# 设置全局变量: INTERNAL_SCORE (平均分), INTERNAL_REVIEW_DIR (审稿目录)
_run_internal_review() {
    local iter_label="${1:-gate}"
    INTERNAL_REVIEW_DIR="${WORKSPACE}/review/internal_${iter_label}"
    mkdir -p "${INTERNAL_REVIEW_DIR}"

    echo ""
    echo -e "${CYAN}━━━ 内部审稿 (5位审稿人) ━━━${NC}"
    echo "  审稿目录: ${INTERNAL_REVIEW_DIR}"

    PDF_FILE="${WORKSPACE}/paper/paper.pdf"
    if [[ ! -f "$PDF_FILE" ]]; then
        echo -e "${RED}未找到 PDF: ${PDF_FILE}${NC}"
        INTERNAL_SCORE=0
        return 1
    fi

    # 运行 internal_review.py，捕获输出
    REVIEW_OUTPUT=$(python internal_review.py "${PDF_FILE}" -o "${INTERNAL_REVIEW_DIR}/" 2>&1) || true
    echo "${REVIEW_OUTPUT}"

    # 从输出中提取平均分 (格式: "Average score: X.X/10")
    INTERNAL_SCORE=$(echo "${REVIEW_OUTPUT}" | grep -oP 'Average score:\s*\K[\d.]+' | head -1)
    if [[ -z "$INTERNAL_SCORE" ]]; then
        # Fallback: 尝试从 summary 文件读取
        SUMMARY_FILE=$(ls -t "${INTERNAL_REVIEW_DIR}"/iter*_summary.md 2>/dev/null | head -1)
        if [[ -f "$SUMMARY_FILE" ]]; then
            INTERNAL_SCORE=$(grep -oP 'Average Score.*?\K[\d.]+' "$SUMMARY_FILE" | head -1)
        fi
    fi
    INTERNAL_SCORE="${INTERNAL_SCORE:-0}"

    # 安全检查：分数必须在 0-10 范围内
    if python -c "exit(0 if 0 <= float('${INTERNAL_SCORE}') <= 10 else 1)" 2>/dev/null; then
        :  # 分数合法
    else
        echo -e "${YELLOW}⚠ 内部审稿分数异常 (${INTERNAL_SCORE})，重置为 0${NC}"
        INTERNAL_SCORE=0
    fi

    echo ""
    echo -e "  内部审稿平均分: ${YELLOW}${INTERNAL_SCORE}/10${NC}"
}

# ---- _check_addressed_items: 逐条检查外部审稿意见是否已解决 ----
# 需要: LATEST_EXTERNAL_REVIEW (外部审稿文件), INTERNAL_REVIEW_DIR (内部审稿目录)
# 设置全局变量: ADDRESSED_PCT (解决百分比 0-100)
_check_addressed_items() {
    echo ""
    echo -e "${CYAN}━━━ 检查外部审稿意见解决情况 ━━━${NC}"

    LATEST_EXTERNAL_REVIEW=$(ls -t "${WORKSPACE}/review/round_"*/external.md 2>/dev/null | head -1)
    if [[ -z "$LATEST_EXTERNAL_REVIEW" ]]; then
        echo "  无外部审稿记录，跳过检查"
        ADDRESSED_PCT=100
        return 0
    fi

    # 找到最新的内部审稿
    LATEST_INTERNAL=$(ls -t "${INTERNAL_REVIEW_DIR}"/iter*.md 2>/dev/null | head -1)
    if [[ -z "$LATEST_INTERNAL" ]]; then
        LATEST_INTERNAL=$(ls -t "${INTERNAL_REVIEW_DIR}"/reviewer_*.md 2>/dev/null | head -1)
    fi

    echo "  外部审稿: ${LATEST_EXTERNAL_REVIEW}"
    echo "  内部审稿: ${LATEST_INTERNAL:-无}"
    echo "  论文: ${WORKSPACE}/paper/paper.tex"
    echo ""

    # 构建 Claude 检查 prompt
    cat > /tmp/cr_check_addressed.txt << CHECK_EOF
你是一个严格的审稿合规检查员。你的任务是逐条核对上一轮外部审稿意见是否已在修订版论文中得到解决。

请阅读以下文件：
1. 外部审稿意见: ${LATEST_EXTERNAL_REVIEW}
2. 论文: ${WORKSPACE}/paper/paper.tex
3. 内部审稿: ${LATEST_INTERNAL:-无}

**检查方法**:
1. 从外部审稿意见中提取所有具体的问题/建议（包括方法、实验、写作等方面）
2. 对每一条，检查修订版论文中是否已经解决
3. 判断标准：
   - "已解决" = 论文中有明确的对应修改（不只是文字调整）
   - "部分解决" = 有修改但不充分（如审稿要求补充实验但只加了讨论）
   - "未解决" = 论文中无对应修改
4. 特别注意：如果审稿意见要求补充实验而论文只修改了文字 → 标记为"未解决"

**输出格式** (严格要求):
\`\`\`
CHECKLIST:
1. [已解决/部分解决/未解决] <审稿意见摘要> → <论文中的对应修改>
2. [已解决/部分解决/未解决] <审稿意见摘要> → <论文中的对应修改>
...

SUMMARY:
已解决: N 条
部分解决: M 条
未解决: K 条
解决率: XX% (已解决 + 部分解决*0.5) / 总数 * 100

GATE: PASS (解决率 >= 70%) 或 GATE: FAIL (解决率 < 70%)
\`\`\`

重要：严格按照格式输出，最后一行必须是 "GATE: PASS" 或 "GATE: FAIL"。
CHECK_EOF

    CHECK_RESULT=$(cat /tmp/cr_check_addressed.txt | claude -p --model "${CLAUDE_MODEL:-deepseek-v4-pro}" --output-format text 2>&1) || true
    echo "${CHECK_RESULT}"

    # 提取解决率
    ADDRESSED_PCT=$(echo "${CHECK_RESULT}" | grep -oP '解决率:\s*\K[\d.]+' | head -1)
    ADDRESSED_PCT="${ADDRESSED_PCT:-0}"

    # 检查 GATE 判定
    if echo "${CHECK_RESULT}" | grep -q "GATE: PASS"; then
        GATE_PASS=1
    else
        GATE_PASS=0
    fi

    echo ""
    echo -e "  外部意见解决率: ${YELLOW}${ADDRESSED_PCT}%${NC}"
    if [[ "$GATE_PASS" == "1" ]]; then
        echo -e "  ${GREEN}✓ 外部意见大部分已解决${NC}"
    else
        echo -e "  ${RED}✗ 外部意见解决不足，需继续修订${NC}"
    fi
}

# ---- _internal_review_gate: 内部审稿门控 ----
# 反复运行内部审稿 + 修订，直到质量达标才允许提交外部审稿
# 参数: $1 = 最大迭代次数 (默认 5)
_internal_review_gate() {
    local max_retries="${1:-5}"
    local gate_iter=0
    local passed=0

    echo ""
    echo -e "${CYAN}╔══════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║  内部审稿门控 — 通过后才提交外部审稿         ║${NC}"
    echo -e "${CYAN}╚══════════════════════════════════════════════╝${NC}"
    echo "  阈值: 内部评分 ≥ 6.0/10, 外部意见解决率 ≥ 70%"
    echo "  最大迭代: ${max_retries}"
    echo ""

    while [[ $gate_iter -lt $max_retries ]]; do
        echo -e "${CYAN}━━━ 门控迭代 $((gate_iter + 1))/${max_retries} ━━━${NC}"

        # Step 1: 运行内部审稿
        _run_internal_review "gate_$((gate_iter + 1))"

        # Step 2: 检查外部意见解决情况
        _check_addressed_items

        # Step 3: 判定
        local score_ok=0
        local addressed_ok=0

        if python -c "exit(0 if float('${INTERNAL_SCORE:-0}') >= 6.0 else 1)" 2>/dev/null; then
            score_ok=1
        fi
        if python -c "exit(0 if float('${ADDRESSED_PCT:-0}') >= 70.0 else 1)" 2>/dev/null; then
            addressed_ok=1
        fi

        echo ""
        echo -e "  内部评分: ${INTERNAL_SCORE}/10 [$(if [[ $score_ok -eq 1 ]]; then echo -e "${GREEN}✓${NC}"; else echo -e "${RED}✗ 需≥6.0${NC}"; fi)]"
        echo -e "  解决率:   ${ADDRESSED_PCT}% [$(if [[ $addressed_ok -eq 1 ]]; then echo -e "${GREEN}✓${NC}"; else echo -e "${RED}✗ 需≥70%${NC}"; fi)]"

        if [[ $score_ok -eq 1 && $addressed_ok -eq 1 ]]; then
            echo ""
            echo -e "${GREEN}══════════════════════════════════════════════${NC}"
            echo -e "${GREEN}  内部审稿门控通过！可以提交外部审稿${NC}"
            echo -e "${GREEN}══════════════════════════════════════════════${NC}"
            passed=1
            break
        fi

        # Step 4: 未通过 — 用内部审稿意见继续修订
        gate_iter=$((gate_iter + 1))
        if [[ $gate_iter -ge $max_retries ]]; then
            echo ""
            echo -e "${YELLOW}══════════════════════════════════════════════${NC}"
            echo -e "${YELLOW}  内部审稿已达上限 (${max_retries}次)，强制提交${NC}"
            echo -e "${YELLOW}══════════════════════════════════════════════${NC}"
            break
        fi

        echo ""
        echo -e "${YELLOW}门控未通过，根据内部审稿意见继续修订...${NC}"
        echo ""

        # 收集内部审稿反馈
        INTERNAL_FEEDBACK=""
        for f in "${INTERNAL_REVIEW_DIR}"/reviewer_*.md; do
            if [[ -f "$f" ]]; then
                INTERNAL_FEEDBACK+="--- $(basename "$f" .md) ---"$'\n'
                INTERNAL_FEEDBACK+="$(head -200 "$f")"$'\n\n'
            fi
        done

        cat > /tmp/cr_gate_revise.txt << GATE_EOF
你是一个高标准的论文修订专家。你的论文在内部审稿中未达标，需要认真修订。

研究主题: ${TOPIC}
当前状态: 内部审稿门控第 ${gate_iter} 轮

**内部审稿评分**: ${INTERNAL_SCORE}/10 (需要 ≥ 6.0)
**外部意见解决率**: ${ADDRESSED_PCT}% (需要 ≥ 70%)

**内部审稿反馈**:
${INTERNAL_FEEDBACK}

**你的任务**:
1. 仔细阅读所有内部审稿意见
2. 对每一条批评进行实质性修改：
   - 如果审稿指出实验不足 → 补充实验代码和数据
   - 如果审稿指出方法缺陷 → 修改方法设计
   - 如果审稿指出写作问题 → 修改文本
   - 如果审稿指出缺少基线 → 添加基线对比
3. 只改文字不改实验是不可接受的 — 必须做实质性改进
4. 修改论文: ${WORKSPACE}/paper/paper.tex
5. 重新编译 PDF: ${WORKSPACE}/paper/paper.pdf
6. 完成后报告 "GATE_REVISION_DONE"

重要：不要只做表面修改，要解决审稿人指出的根本问题。
GATE_EOF
        _claude_task "$(cat /tmp/cr_gate_revise.txt)"

        # 保存中间状态
        python -c "
from state_manager import StateManager
sm = StateManager('state')
state = sm.load('${SLUG}')
state.stages.setdefault('revise', {})['internal_gate_iter'] = ${gate_iter}
state.stages['revise']['internal_gate_score'] = float('${INTERNAL_SCORE:-0}')
sm.save(state)
" 2>/dev/null || true
    done

    # 返回门控是否通过
    return $(( 1 - passed ))
}

# ---- _do_revise_and_resubmit: 修订 + 重新提交 ----
_do_revise_and_resubmit() {
    local verdict="${1:-unknown}"
    LATEST_REVIEW=$(ls -t "${WORKSPACE}/review/round_"*/external.md 2>/dev/null | head -1)
    NEXT_ITER=$((ITERATION + 1))

    echo ""
    echo -e "${CYAN}━━━ 修订迭代 #${NEXT_ITER} ━━━${NC}"
    echo "  外部审稿意见: ${LATEST_REVIEW}"
    echo "  当前 verdict: ${verdict}"
    echo ""

    # 第一步：根据外部审稿意见修订 (强化版 prompt)
    cat > /tmp/cr_revise_prompt.txt << PROMPT_EOF
你是一个严格的论文修订专家。请根据外部审稿意见对论文进行**实质性**修订。

研究主题: ${TOPIC}
当前迭代: 第 ${NEXT_ITER} 轮修订（上一轮外部审稿 verdict: ${verdict}）

请依次阅读以下文件：
1. 外部审稿意见: ${LATEST_REVIEW}
2. 文献综述: ${WORKSPACE}/literature/literature_review.md
3. 当前论文: ${WORKSPACE}/paper/paper.tex

**修订要求（非常重要）**:

1. **逐条回复**: 对审稿意见中的每一条问题/建议，都要有明确的修改
2. **实质性修改**:
   - 如果审稿要求补充实验 → 必须编写并运行实验代码
   - 如果审稿质疑方法有效性 → 必须修改方法或补充消融实验
   - 如果审稿指出缺少基线 → 必须添加基线对比
   - 如果审稿指出理论缺陷 → 必须补充理论分析
3. **禁止只改文字**: 纯文本修改（改措辞、加讨论段落）不能替代实验补充
4. **实验补充**: 如果需要补充实验，编写实验脚本到 ${WORKSPACE}/experiment/ 并执行
5. **修改论文**: ${WORKSPACE}/paper/paper.tex
6. **重新编译**: ${WORKSPACE}/paper/paper.pdf
7. **Response letter**: ${WORKSPACE}/paper/response_letter_iter${NEXT_ITER}.md — 逐条说明如何解决每个审稿意见

**自我检查**: 完成后问自己："如果审稿人再看一遍，他们会满意吗？" 如果答案是否定的，请继续修改。

完成后明确报告'修订完成，请提交内部审稿'。
PROMPT_EOF
    _claude_task "$(cat /tmp/cr_revise_prompt.txt)"

    python -c "
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.load('${SLUG}')
state.iteration = ${NEXT_ITER}
sm.save(state)
sm.complete_stage(state, Stage.REVISE)
"

    ITERATION=${NEXT_ITER}

    # 第二步：内部审稿门控 — 只有通过内部审稿才能提交外部
    _internal_review_gate 5

    # 第三步：提交外部审稿
    _do_submit_review
}

# ---- _continue_project: 继续已有项目（按阶段路由）----
_continue_project() {
    echo -e "${CYAN}继续项目: ${TOPIC}${NC}"
    echo -e "阶段: ${STAGE} | 迭代: ${ITERATION}"
    echo ""

    case "$STAGE" in
        experiment_execution)
            _continue_experiment
            ;;
        hypothesis_generation)
            echo -e "${YELLOW}假说生成阶段 — 重新执行假说生成...${NC}"
            echo ""
            mkdir -p "${WORKSPACE}/hypothesis/"

            python hypothesis_engine.py \
                --topic "${TOPIC}" \
                --literature-dir "${WORKSPACE}/literature/" \
                --work-dir "${WORKSPACE}/hypothesis/" \
                --max-react-rounds 3 --top-k-pdfs 5 2>&1

            if [[ -f "${WORKSPACE}/hypothesis/hypothesis_output.json" ]]; then
                HYPOTHESIS_JSON=$(cat "${WORKSPACE}/hypothesis/hypothesis_output.json" | head -300)
                cat > /tmp/cr_hypothesis_refine.txt << HYP_EOF
你是一位资深的科研假说评审专家。请审阅假说生成结果并优化。

研究主题: ${TOPIC}
文献综述: ${WORKSPACE}/literature/literature_review.md
假说生成结果: ${HYPOTHESIS_JSON}

请优化假说并将结果保存到 ${WORKSPACE}/hypothesis/hypothesis_report.md
完成后报告'假说生成完成'。
HYP_EOF
                _claude_task "$(cat /tmp/cr_hypothesis_refine.txt)" || true
            fi

            python -c "
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.HYPOTHESIS_GENERATION)
sm.start_stage(state, Stage.EXPERIMENT_DESIGN)
"

            # Fall through to experiment design
            cat > /tmp/cr_stage2_prompt.txt << PROMPT_EOF
你是一个机器学习研究员。请基于文献综述和假说设计实验方案。

研究主题: ${TOPIC}
文献综述: ${WORKSPACE}/literature/literature_review.md
研究假说: ${WORKSPACE}/hypothesis/hypothesis_report.md

请完成以下任务：
1. 阅读文献综述和研究假说
2. 基于选定的假说设计完整的实验方案，包括：研究问题和假设、方法/模型详细描述、数据集选择、基线方法、评估指标、实验配置（超参、硬件）、消融实验设计
3. 编写可执行的 Python 实验代码
4. 编写 run_experiment.sh（包含环境设置、依赖安装、实验执行的全部命令）
5. 保存实验方案到: ${WORKSPACE}/experiment/experiment_plan.md
6. 保存代码和脚本到: ${WORKSPACE}/experiment/

重要：只做实验设计，不要做其他事情。完成后明确报告'实验设计完成'。
PROMPT_EOF
            _claude_task "$(cat /tmp/cr_stage2_prompt.txt)"
            python -c "
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.EXPERIMENT_DESIGN)
sm.start_stage(state, Stage.EXPERIMENT_EXECUTION)
"
            _continue_experiment
            ;;
        experiment_design)
            echo -e "${YELLOW}实验设计阶段 — 重新执行实验设计...${NC}"
            cat > /tmp/cr_stage2_prompt.txt << PROMPT_EOF
你是一个机器学习研究员。请基于文献综述设计实验方案。

研究主题: ${TOPIC}
文献综述: ${WORKSPACE}/literature/literature_review.md

请完成以下任务：
1. 阅读文献综述
2. 设计完整的实验方案，包括：研究问题和假设、方法/模型详细描述、数据集选择、基线方法、评估指标、实验配置（超参、硬件）、消融实验设计
3. 编写可执行的 Python 实验代码
4. 编写 run_experiment.sh（包含环境设置、依赖安装、实验执行的全部命令）
5. 保存实验方案到: ${WORKSPACE}/experiment/experiment_plan.md
6. 保存代码和脚本到: ${WORKSPACE}/experiment/

重要：只做实验设计，不要做其他事情。完成后明确报告'实验设计完成'。
PROMPT_EOF
            _claude_task "$(cat /tmp/cr_stage2_prompt.txt)"
            python -c "
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.EXPERIMENT_DESIGN)
sm.start_stage(state, Stage.EXPERIMENT_EXECUTION)
"
            _continue_experiment
            ;;
        paper_writing)
            _do_paper_writing
            _do_submit_review
            ;;
        submit_review)
            _do_submit_review
            ;;
        poll_review)
            # 已有审稿 token，直接轮询
            LATEST_REVIEW=$(ls -t "${WORKSPACE}/review/round_"*/external.md 2>/dev/null | head -1)
            if [[ -n "$LATEST_REVIEW" ]]; then
                echo "已有审稿结果: ${LATEST_REVIEW}"
                VERDICT=$(python -c "
import json
d = json.load(open('state/${SLUG}/state.json'))
reviews = d.get('reviews', [])
print(reviews[-1].get('verdict', 'unknown') if reviews else 'unknown')
" 2>/dev/null)
                if [[ "$VERDICT" == "accept" || "$VERDICT" == "weak accept" ]]; then
                    echo -e "${GREEN}★ 论文已通过审稿！${NC}"
                    exit 0
                else
                    _do_revise_and_resubmit "$VERDICT"
                fi
            else
                echo "重新提交审稿..."
                _do_submit_review
            fi
            ;;
        revise|resubmit)
            _do_revise_and_resubmit
            ;;
        *)
            echo -e "${YELLOW}阶段 '${STAGE}' 无对应继续逻辑，请检查 state 文件${NC}"
            exit 1
            ;;
    esac
}

# 核心：根据 state 决定执行什么
# ---------------------------------------------------------------------------
banner

# 收集所有项目（仅列出 workspace/ 真实存在的）
declare -a PROJECT_SLUGS=()
declare -a PROJECT_TOPICS=()
declare -a PROJECT_STAGES=()
declare -a PROJECT_ITERS=()
declare -a PROJECT_WORKSPACES=()

for d in state/*/; do
    sf="${d}state.json"
    if [[ -f "$sf" ]]; then
        slug=$(basename "$d")
        # Read work_dir from state.json to get correct workspace path
        WORK_DIR=$(python -c "
import json
d = json.load(open('$sf'))
print(d.get('work_dir', ''))
" 2>/dev/null)
        if [[ -z "$WORK_DIR" ]]; then
            continue  # skip broken states
        fi
        if [[ ! -d "$WORK_DIR" ]]; then
            continue  # skip orphaned states silently
        fi
        PROJECT_SLUGS+=("$slug")
        PROJECT_TOPICS+=("$(python -c "import json; print(json.load(open('$sf'))['topic'])" 2>/dev/null || echo "?")")
        PROJECT_STAGES+=("$(python -c "
import json
d=json.load(open('$sf'))
s=d.get('stage','?')
for v in d.get('stages',{}).values():
    if isinstance(v,dict) and v.get('status')=='error': s+=' [有错误]'; break
print(s)
" 2>/dev/null || echo "?")")
        PROJECT_ITERS+=("$(python -c "import json; print(json.load(open('$sf'))['iteration'])" 2>/dev/null || echo "0")")
        PROJECT_WORKSPACES+=("$WORK_DIR")
    fi
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

    python menu.py "${MENU_ARGS[@]}"
    stty sane 2>/dev/null || true  # 恢复终端状态，防吞字
    CHOICE=$(cat /tmp/cr_menu_result.txt 2>/dev/null || echo "__QUIT__")

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

    # 调用 Agent 提炼研究主题
    echo ""
    echo -e "${CYAN}Agent 正在分析你的想法并提炼研究主题...${NC}"
    cat > /tmp/cr_topic_prompt.txt << PROMPT_EOF
你是一个科研助手。用户描述了以下研究想法：

"${USER_INPUT}"

你的任务：
1. 理解用户的核心意图
2. 提炼为一个清晰、具体、可执行的研究主题（英文，适合作为论文学术标题）
3. 如果用户想法太模糊，基于该方向给出 2-3 个具体的主题建议
4. 最终输出格式：TOPIC: <提炼后的研究主题>（一行，不要多余内容）

要求：主题要具体（包含方法+问题+场景），例如 "Improving Few-Shot Learning through Adaptive Prompt Optimization for Cross-Domain NLP Tasks"
PROMPT_EOF

    REFINED=$(cat /tmp/cr_topic_prompt.txt | claude -p --model "${CLAUDE_MODEL:-deepseek-v4-pro}" --output-format text 2>&1) || true

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
        echo "    - 删除 .chenresearch_configured 后重新运行: rm .chenresearch_configured && bash start.sh"
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

    SAFE_TOPIC=$(echo "$TOPIC" | tr ' ' '_' | cut -c1-50)
    WORKSPACE="workspace/${SAFE_TOPIC}"

    # 创建目录和初始 state
    python -c "
from state_manager import StateManager
sm = StateManager('state')
state = sm.create('${TOPIC}', work_dir='${WORKSPACE}')
print(state.topic_slug)
" > /tmp/cr_slug.txt
    SLUG=$(cat /tmp/cr_slug.txt)

    # ===== Stage 1: 文献检索 =====
    echo ""
    echo -e "${CYAN}━━━ Stage 1/4: 文献检索 ━━━${NC}"
    echo ""

    # 直接调用学术 API（arXiv + Semantic Scholar + OpenAlex）
    # --save-json 保存完整元数据供假说生成引擎使用
    python search_papers.py "${TOPIC}" -n 20 -o "${WORKSPACE}/literature/" \
        --save-json "${WORKSPACE}/literature/papers_metadata.json" 2>&1

    # 让 Claude Code 补充分析和整理
    if [[ -f "${WORKSPACE}/literature/literature_review.md" ]]; then
        REVIEW_MD="${WORKSPACE}/literature/literature_review.md"
        ANALYSIS_PROMPT="请阅读 ${REVIEW_MD}，基于检索到的论文进行分析：提炼领域概览和关键趋势、识别研究空白、提出具体的研究方向建议。将分析结果追加到 ${REVIEW_MD} 末尾。只做分析和建议，不超过500字。完成后报告'文献分析完成'。"
        _claude_task "${ANALYSIS_PROMPT}" || echo "[WARN] Claude 分析跳过（可手动完成）"
    else
        echo -e "${RED}文献检索失败：search_papers.py 未生成输出${NC}"
        exit 1
    fi

    python -c "
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.LITERATURE_SEARCH, {'papers_found': 0})
"

    # ===== Stage 2: 假说生成 (ReAct-based) =====
    echo ""
    echo -e "${CYAN}━━━ Stage 2/5: 假说生成 (ReAct 检索+对比+假说) ━━━${NC}"
    echo ""

    mkdir -p "${WORKSPACE}/hypothesis/"

    python hypothesis_engine.py \
        --topic "${TOPIC}" \
        --literature-dir "${WORKSPACE}/literature/" \
        --work-dir "${WORKSPACE}/hypothesis/" \
        --max-react-rounds 3 --top-k-pdfs 5 2>&1

    # 让 Claude Code 审阅和补充假说
    if [[ -f "${WORKSPACE}/hypothesis/hypothesis_output.json" ]]; then
        HYPOTHESIS_JSON=$(cat "${WORKSPACE}/hypothesis/hypothesis_output.json" | head -300)
        cat > /tmp/cr_hypothesis_refine.txt << HYP_EOF
你是一位资深的科研假说评审专家。请审阅以下假说生成结果，并进行优化。

研究主题: ${TOPIC}
文献综述: ${WORKSPACE}/literature/literature_review.md

假说生成结果 (JSON):
${HYPOTHESIS_JSON}

请完成:
1. 评估每个假说的创新性和可行性
2. 指出任何遗漏的研究角度
3. 如果有改进建议，直接修改假说描述
4. 将优化后的假说保存到 ${WORKSPACE}/hypothesis/hypothesis_report.md
5. 格式: 每个假说包含标题、详细描述、方法概述、支撑文献、预期结果

完成后报告'假说生成完成'。
HYP_EOF
        _claude_task "$(cat /tmp/cr_hypothesis_refine.txt)" || echo "[WARN] 假说审阅跳过"
    else
        echo -e "${YELLOW}hypothesis_engine.py 未生成输出，检查错误日志${NC}"
    fi

    python -c "
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.HYPOTHESIS_GENERATION)
"

    # ===== Stage 3: 实验设计 =====
    echo ""
    echo -e "${CYAN}━━━ Stage 3/5: 实验设计 ━━━${NC}"
    echo ""

    cat > /tmp/cr_stage2_prompt.txt << PROMPT_EOF
你是一个机器学习研究员。请基于文献综述设计实验方案。

研究主题: ${TOPIC}
文献综述: ${WORKSPACE}/literature/literature_review.md

请完成以下任务：
1. 阅读文献综述
2. 设计完整的实验方案，包括：研究问题和假设、方法/模型详细描述、数据集选择、基线方法、评估指标、实验配置（超参、硬件）、消融实验设计
3. 编写可执行的 Python 实验代码
4. 编写 run_experiment.sh（包含环境设置、依赖安装、实验执行的全部命令）
5. 保存实验方案到: ${WORKSPACE}/experiment/experiment_plan.md
6. 保存代码和脚本到: ${WORKSPACE}/experiment/

重要：只做实验设计，不要做其他事情。完成后明确报告'实验设计完成'。
PROMPT_EOF
    _claude_task "$(cat /tmp/cr_stage2_prompt.txt)"

    python -c "
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.EXPERIMENT_DESIGN)
sm.start_stage(state, Stage.EXPERIMENT_EXECUTION)
"

    # ===== Stage 4: 实验执行（本地优先，SCO 后备）=====
    echo ""
    echo -e "${CYAN}━━━ Stage 4/5: 实验执行（本地优先）━━━${NC}"
    echo ""

    EXP_SCRIPT="${WORKSPACE}/experiment/run_experiment.sh"
    if [[ -f "$EXP_SCRIPT" ]]; then
        JOB_NAME="cr-${SLUG:0:30}"

        # 使用新的 local-first 执行器
        echo "检测本地 GPU ..."
        GPU_INFO=$(python -c "
from sco_runner import detect_gpu
info = detect_gpu()
print(f'GPU_AVAILABLE={info[\"available\"]}')
print(f'GPU_COUNT={info[\"count\"]}')
" 2>&1) || true
        echo "${GPU_INFO}"

        HAS_GPU=$(echo "$GPU_INFO" | grep "GPU_AVAILABLE=True" && echo 1 || echo 0)
        GPU_COUNT=$(echo "$GPU_INFO" | grep -oP 'GPU_COUNT=\K\d+')

        if [[ "$HAS_GPU" == "1" ]]; then
            echo -e "${GREEN}检测到 ${GPU_COUNT} 个本地 GPU，优先本地执行${NC}"
        else
            echo -e "${YELLOW}未检测到本地 GPU${NC}"
            NEEDS_GPU=$(python -c "
from sco_runner import needs_gpu_heuristic
from pathlib import Path
print('NEEDS_GPU=' + ('True' if needs_gpu_heuristic(Path('${EXP_SCRIPT}')) else 'False'))
" 2>&1) || true
            if echo "$NEEDS_GPU" | grep -q "NEEDS_GPU=True"; then
                echo -e "${YELLOW}实验需要 GPU → 将使用 SCO 云端执行${NC}"
            else
                echo -e "${GREEN}实验不依赖 GPU → 本地执行${NC}"
            fi
        fi
        echo ""

        # 使用统一执行器 (local-first)
        EXEC_OUTPUT=$(python -c "
from sco_runner import run_experiment
from pathlib import Path
result = run_experiment(
    Path('${EXP_SCRIPT}'),
    job_name='${JOB_NAME}',
    local_timeout=${LOCAL_EXECUTION_TIMEOUT:-7200},
    max_local_retries=${LOCAL_EXECUTION_MAX_RETRIES:-20},
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
            python -c "
from state_manager import StateManager
sm = StateManager('state')
state = sm.load('${SLUG}')
stages = state.stages
if 'experiment_execution' not in stages:
    stages['experiment_execution'] = {}
stages['experiment_execution']['job_id'] = '${JOB_ID}'
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

                cat > /tmp/cr_debug_prompt.txt << PROMPT_EOF
你是实验调试专家。实验执行失败了（第 ${debug_round} 轮修复），请诊断并修复。

**后端**: ${BACKEND}
**原始错误**: ${ERROR_SUMMARY}
**上一轮修复后的错误日志 (最后100行)**:
${ERROR_LOG}

**实验脚本**: ${WORKSPACE}/experiment/run_experiment.sh
**工作目录**: ${WORKSPACE}/experiment/

你的任务:
1. 仔细分析错误日志，找出失败原因
2. 注意之前轮次的修复尝试（如果有）
3. 修改实验脚本或代码来修复问题
4. 保存修改后的文件
5. 报告 "FIX_READY" 表示已修复

常见问题及修复:
- 依赖缺失 (如 python3-venv) → 先 apt-get install，再让脚本正常工作
- 路径错误 → 修正文件路径
- 虚拟环境损坏 → 删除 .venv 目录让脚本重建，或跳过 venv 直接用系统 Python
- 语法错误 → 修正代码
- pip 不可用 → 使用 python3 -m pip 代替裸 pip
- 环境不兼容 → 修改脚本适配当前环境
- OOM / CUDA out of memory → 减小 batch_size 或模型大小
- 实验被超时中断 → 不要删除 checkpoints/ 下的 .pth 文件，run_experiment.sh 已配置 --resume 自动续跑
PROMPT_EOF
                _claude_task "$(cat /tmp/cr_debug_prompt.txt)" 2>&1

                # ── 修复后重试 ──
                echo ""
                echo -e "${YELLOW}修复完成，重新执行实验...${NC}"
                EXEC_OUTPUT=$(python -c "
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

        python -c "
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.EXPERIMENT_EXECUTION, {'job_id': '${JOB_ID}', 'backend': '${BACKEND}', 'status': '${SUCCESS}'})
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

    # ===== Stage 4: 论文撰写 + 审稿 =====
    ITERATION=0
    _do_paper_writing
    _do_submit_review
fi  # 情况 A 结束
