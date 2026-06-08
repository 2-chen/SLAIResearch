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
# 会话结束标记（写入 run.log）
trap 'echo ""; echo "── 会话结束: $(date "+%Y-%m-%d %H:%M:%S") ──"' EXIT
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
export CHENRESEARCH_MAX_GPU_HOURS=32
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
    echo "  ChenResearch Session — $(date '+%Y-%m-%d %H:%M:%S')"
    echo "  项目: ${TOPIC}"
    echo "  阶段: ${STAGE:-新项目}  迭代: ${ITERATION:-0}"
    echo "═══════════════════════════════════════════════"
    echo ""
}

# ---------------------------------------------------------------------------
# Claude Code 调用辅助
# ---------------------------------------------------------------------------
_claude_task() {
    local prompt="$1"
    local log="${2:-/tmp/cr_claude_output.txt}"
    local sys_prompt_file="${3:-}"

    echo -e "${CYAN}  Claude Code 正在工作中...${NC}"
    echo "  (输出实时显示，可能需要几分钟)"

    local _sys_flag=()
    if [[ -n "$sys_prompt_file" && -f "$sys_prompt_file" ]]; then
        _sys_flag=("--append-system-prompt" "$(cat "$sys_prompt_file")")
    fi

    # 实时输出到终端 + 同时保存到日志
    echo "$prompt" | claude -p --model "${CLAUDE_MODEL:-deepseek-v4-pro}" --output-format text --verbose "${_sys_flag[@]}" 2>&1 | tee "$log"

    local rc=${PIPESTATUS[0]}
    echo ""
    if [[ $rc -eq 0 ]]; then
        echo -e "${GREEN}  Claude Code 完成${NC}"
    else
        echo -e "${YELLOW}  Claude Code 退出码: $rc${NC}"
    fi
    return $rc
}

# ---- _stage_review: LLM quality gate for pipeline stages ----
# Usage: _stage_review <stage_name> <output_file>
# Sets globals: REVIEW_PASSED, REVIEW_SCORE, REVIEW_FEEDBACK, REVIEW_SUGGESTION
# Returns 0 if passed, 1 if failed
_stage_review() {
    local stage_name="$1"
    local output_file="$2"

    local enabled=$(python -c "import sys; sys.path.insert(0,'${SCRIPT_DIR}'); from config import STAGE_REVIEW_ENABLED; print('1' if STAGE_REVIEW_ENABLED else '0')" 2>/dev/null || echo "1")
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
    echo "$TOPIC" > /tmp/cr_review_topic.txt
    echo "${STAGE_REVIEW_MODE:-llm}" > /tmp/cr_review_mode.txt

    python -c "
import sys, json
sys.path.insert(0, '${SCRIPT_DIR}')
from stage_reviewer import StageReviewer

stage_name = '${stage_name}'
output_file = '${output_file}'
topic = open('/tmp/cr_review_topic.txt').read().strip()
mode = open('/tmp/cr_review_mode.txt').read().strip()

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
json.dump(result, open('/tmp/cr_review_result.json', 'w'), ensure_ascii=False)
" 2>&1 || true

    if [[ -f /tmp/cr_review_result.json ]]; then
        REVIEW_PASSED=$(python -c "import json; print(json.load(open('/tmp/cr_review_result.json')).get('passed', True))")
        REVIEW_SCORE=$(python -c "import json; print(json.load(open('/tmp/cr_review_result.json')).get('score', 7.0))")
        REVIEW_FEEDBACK=$(python -c "import json; print(json.load(open('/tmp/cr_review_result.json')).get('feedback', ''))")
        REVIEW_SUGGESTION=$(python -c "import json; print(json.load(open('/tmp/cr_review_result.json')).get('suggestion', ''))")
    else
        REVIEW_PASSED="True"; REVIEW_SCORE="7.0"; REVIEW_FEEDBACK="[review error, default pass]"
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
    python "${SCRIPT_DIR}/prompt_render.py" "${SCRIPT_DIR}/prompts/${template}" > "$output"
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
      _render_prompt "stage_review_fix.md" /tmp/cr_stage_fix.txt
    _claude_task "$(cat /tmp/cr_stage_fix.txt)"
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

    stage="$stage" err_msg="$err_msg" ws="$ws" \
      _render_prompt "recovery.md" /tmp/cr_recover_prompt.txt

    if _claude_task "$(cat /tmp/cr_recover_prompt.txt)" 2>&1 | grep -q "RECOVERY_OK"; then
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

    echo -e "${CYAN}━━━ 环境准备：预下载依赖 ━━━${NC}"
    echo ""

    # ── 1. Wheels ──
    echo "[1/3] 检查/下载 Python wheels..."
    python -c "
from sco_runner import _ensure_wheels
_ensure_wheels()
print('  Wheels OK')
" 2>&1 || echo "  Wheels 检查完成（部分可能未命中）"

    # ── 2. Models ──
    echo "[2/3] 预下载模型..."
    if [[ -f "$exp_script" ]]; then
        python -c "
from sco_runner import _ensure_model_cache
from pathlib import Path
ok = _ensure_model_cache(Path('$exp_script'))
print('  Models OK' if ok else '  Models 部分失败（非致命）')
" 2>&1 || echo "  Models 预下载跳过"
    else
        echo "  未找到 run_experiment.sh，跳过模型预下载"
    fi

    # ── 3. Datasets ──
    echo "[3/3] 预下载数据集..."
    if [[ -f "$exp_script" ]]; then
        python -c "
from sco_runner import _ensure_dataset_cache
from pathlib import Path
ok = _ensure_dataset_cache(Path('$exp_script'))
print('  Datasets OK' if ok else '  Datasets 部分失败（非致命）')
" 2>&1 || echo "  Datasets 预下载跳过"
    else
        echo "  未找到 run_experiment.sh，跳过数据集预下载"
    fi

    # ── 4. 预安装 Python 包到共享 site-packages ──
    echo ""
    echo "[Extra] 预安装缺失的 Python 包..."
    if [[ -d "$exp_dir" ]]; then
        python -c "
from sco_runner import _prepare_env_for_sco
from pathlib import Path
ok = _prepare_env_for_sco(Path('$exp_dir'))
print('  Env prep OK' if ok else '  Env prep 部分失败（非致命）')
" 2>&1 || echo "  Env prep 跳过"
    fi

    echo ""
    echo -e "${GREEN}  环境准备完成。依赖已缓存到共享存储，SCO 容器可直接使用。${NC}"
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

    # 从 sco_job_id.txt 读取最新任务 ID（优先），回退到 state.json
    JOB_ID_FILE="${WORKSPACE}/experiment/logs/sco_job_id.txt"
    if [[ "$BACKEND" == "sco" ]]; then
        if [[ -f "$JOB_ID_FILE" ]]; then
            JOB_ID=$(cat "$JOB_ID_FILE" | tr -d '[:space:]')
            echo "从 sco_job_id.txt 读取最新任务: ${JOB_ID}"
        else
            JOB_ID=$(python -c "
import json
d = json.load(open('state/${SLUG}/state.json'))
ee = d.get('stages',{}).get('experiment_execution',{})
meta = ee.get('meta',{})
print(meta.get('job_id') or ee.get('job_id') or '')
" 2>/dev/null)
        fi
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
                # ── Preflight + Resource Scheduling ──
                echo ""
                echo -e "${CYAN}━━━ 执行前检查 (Preflight) ━━━${NC}"
                python "${SCRIPT_DIR}/experiment_runner.py" preflight "${WORKSPACE}/experiment" 2>&1 || {
                    echo -e "${RED}Preflight 检查失败，但仍继续提交（阻断性错误已显示）${NC}"
                }
                echo ""
                echo -e "${CYAN}━━━ 资源调度分析 ━━━${NC}"
                SCHEDULE_OUT=$(python "${SCRIPT_DIR}/experiment_runner.py" schedule "${WORKSPACE}/experiment" 2>&1) || true
                echo "$SCHEDULE_OUT"
                # Apply recommended JOBS_PER_GPU and GPU_COUNT
                eval $(python "${SCRIPT_DIR}/experiment_runner.py" schedule "${WORKSPACE}/experiment" --env 2>/dev/null) || true
                echo "  JOBS_PER_GPU=${JOBS_PER_GPU:-1}  GPU_COUNT=${GPU_COUNT:-1}"
                export JOBS_PER_GPU="${JOBS_PER_GPU:-1}"
                export GPU_COUNT="${GPU_COUNT:-1}"
                echo ""

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
            echo "等待任务完成 (最长 12 小时)..."
            for i in $(seq 1 1440); do
                STATUS=$(sco acp jobs describe --workspace-name share-space -o json "$JOB_ID" 2>/dev/null | python -c "import json,sys; print(json.load(sys.stdin).get('state','UNKNOWN'))" 2>/dev/null || echo "UNKNOWN")
                echo "  状态: ${STATUS} (${i}/1440)"
                if [[ "$STATUS" == "SUCCEEDED" || "$STATUS" == "FAILED" || "$STATUS" == "STOPPED" || "$STATUS" == "SUSPENDED" || "$STATUS" == "CANCELLED" || "$STATUS" == "UNKNOWN" ]]; then
                    break
                fi
                sleep 30
            done
            sco acp jobs stream-logs --workspace-name share-space "$JOB_ID" > "${WORKSPACE}/experiment/sco_logs.txt" 2>/dev/null || true
        elif [[ "$STATUS" == "STOPPED" || "$STATUS" == "SUSPENDED" || "$STATUS" == "CANCELLED" ]]; then
            echo -e "${YELLOW}SCO 任务已终止 (${STATUS}, ${JOB_ID})，按失败处理，进入自动修复流程${NC}"
            STATUS="FAILED"
        fi
        if [[ "$STATUS" == "FAILED" ]]; then
            # FAILED / UNKNOWN / 其他 — 诊断 → 修复 → 重新提交 SCO
            if [[ "$STATUS" == "FAILED" ]]; then
                echo -e "${YELLOW}SCO 任务失败 (${STATUS})，诊断原因...${NC}"
            else
                echo -e "${YELLOW}SCO 任务状态异常 (${STATUS})，诊断原因...${NC}"
            fi
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

            # ── 加载受保护的基础设施文件（修复循环禁止修改）──
            PROTECTED_FILES=()
            PROTECTED_FILE="${SCRIPT_DIR}/.chenresearch_protected"
            if [[ -f "$PROTECTED_FILE" ]]; then
                while IFS= read -r line; do
                    [[ -z "$line" || "$line" == \#* ]] && continue
                    local _pf="${SCRIPT_DIR}/${line}"
                    [[ -f "$_pf" ]] && PROTECTED_FILES+=("$_pf")
                done < "$PROTECTED_FILE"
            fi
            # 兜底保护核心文件（即使 .chenresearch_protected 不存在）
            for _pf in "${SCRIPT_DIR}/sco_runner.py" "${SCRIPT_DIR}/config.py" \
                       "${SCRIPT_DIR}/workspace/.shared/install_deps.sh" \
                       "${SCRIPT_DIR}/workspace/.shared/prepare_env.sh"; do
                [[ -f "$_pf" ]] || continue
                local _already=0
                for _existing in "${PROTECTED_FILES[@]}"; do
                    [[ "$_existing" == "$_pf" ]] && _already=1 && break
                done
                [[ $_already -eq 0 ]] && PROTECTED_FILES+=("$_pf")
            done

            # ── 备份受保护文件的哈希（用于电路断路器）──
            declare -A PROTECTED_HASHES
            for _pf in "${PROTECTED_FILES[@]}"; do
                PROTECTED_HASHES["$_pf"]=$(md5sum "$_pf" 2>/dev/null | awk '{print $1}')
                cp "$_pf" "${_pf}.cr_backup" 2>/dev/null || true
            done

            # ── 修复轮次历史（传递给后续轮次，避免重复修复）──
            ROUND_HISTORY=""

            for ((sco_round=1; sco_round<=SCO_DEBUG_ROUNDS; sco_round++)); do
                echo ""
                echo -e "${CYAN}━━━ SCO 修复轮次 ${sco_round}/${SCO_DEBUG_ROUNDS} ━━━${NC}"

                # ── 构建受保护文件列表（展示在 prompt 中）──
                PROTECTED_LIST=""
                for _pf in "${PROTECTED_FILES[@]}"; do
                    PROTECTED_LIST+="  - ${_pf}"$'\n'
                done

                # 用 experiment_runner.py 构建增强版调试 prompt
                # (自动读取源文件、解析 traceback、检索 debug memory)
                python "${SCRIPT_DIR}/experiment_runner.py" build-prompt \
                    "${WORKSPACE}" \
                    "${WORKSPACE}/experiment/sco_logs.txt" \
                    --script "${EXP_SCRIPT}" \
                    --backend sco \
                    --round "${sco_round}" \
                    --max-rounds "${SCO_DEBUG_ROUNDS}" \
                    --history "${ROUND_HISTORY}" \
                    --protected "${PROTECTED_LIST}" \
                    > /tmp/cr_sco_debug_prompt.txt 2>/dev/null || {
                    # 回退：如果增强版 prompt 构建失败，用简化版
                    sco_round="$sco_round" ERROR_KEY_LINES="$ERROR_KEY_LINES" \
                      SCO_ERROR_TAIL="$SCO_ERROR_TAIL" EXP_SCRIPT="$EXP_SCRIPT" \
                      PROTECTED_LIST="$PROTECTED_LIST" ROUND_HISTORY="$ROUND_HISTORY" \
                      _render_prompt "sco_debug_fallback.md" /tmp/cr_sco_debug_prompt.txt
                }
                _claude_task "$(cat /tmp/cr_sco_debug_prompt.txt)" "/tmp/cr_claude_output.txt" "${SCRIPT_DIR}/prompts/sco_debugger_system.md" 2>&1

                # ── 电路断路器：检查受保护文件是否被意外修改 ──
                local _violations=0
                for _pf in "${PROTECTED_FILES[@]}"; do
                    if [[ -f "$_pf" ]]; then
                        local _new_hash=$(md5sum "$_pf" 2>/dev/null | awk '{print $1}')
                        if [[ "${_new_hash}" != "${PROTECTED_HASHES["$_pf"]}" ]]; then
                            echo -e "${RED}[断路器] 受保护文件被修改: ${_pf}${NC}"
                            # 从备份恢复
                            if [[ -f "${_pf}.cr_backup" ]]; then
                                cp "${_pf}.cr_backup" "$_pf"
                                echo -e "${YELLOW}  → 已从备份恢复${NC}"
                            fi
                            _violations=$((_violations + 1))
                        fi
                    fi
                done
                if [[ $_violations -gt 0 ]]; then
                    ROUND_HISTORY+="[轮次 ${sco_round}] 断路器触发 — ${_violations} 个受保护文件被修改并恢复。"$'\n'
                fi

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

                # ── 保存 debug 记录到持久记忆 ──
                local _is_success="false"
                [[ "$SUCCESS" == "True" ]] && _is_success="true"
                python "${SCRIPT_DIR}/experiment_runner.py" save-record \
                    "${WORKSPACE}" \
                    --slug "${SLUG}" \
                    --error-log "$(tail -500 "${WORKSPACE}/experiment/sco_logs.txt" 2>/dev/null | head -200)" \
                    --root-cause "" \
                    --fix-summary "SCO debug round ${sco_round}" \
                    --files-modified "$(find "${WORKSPACE}/experiment" -name '*.py' -o -name '*.sh' -newer /tmp/cr_sco_debug_prompt.txt 2>/dev/null | tr '\n' ',' | head -200)" \
                    --success "${_is_success}" \
                    --fix-round "${sco_round}" \
                    --total-rounds "${SCO_DEBUG_ROUNDS}" \
                    --backend "${BACKEND:-sco}" \
                    --job-id "${JOB_ID}" \
                    2>/dev/null || true

                if [[ "$SUCCESS" == "True" ]]; then
                    echo -e "${GREEN}★ SCO 修复成功！实验通过 (第 ${sco_round} 轮修复, 后端: ${BACKEND})${NC}"
                    STATUS="SUCCEEDED"
                    break
                else
                    echo -e "${YELLOW}第 ${sco_round} 轮修复后仍失败 (后端: ${BACKEND})${NC}"
                    # 记录本轮失败的关键信息，传递给下一轮
                    local _round_err=$(echo "$ERROR_KEY_LINES" | head -3 | tr '\n' ' ')
                    ROUND_HISTORY+="[轮次 ${sco_round}] 失败, 后端=${BACKEND}, 错误: ${_round_err:-无日志}"$'\n'
                    if [[ $sco_round -lt $SCO_DEBUG_ROUNDS ]]; then
                        echo -e "${YELLOW}继续下一轮 SCO 诊断...${NC}"
                    else
                        echo -e "${RED}已达最大 SCO 修复轮次 (${SCO_DEBUG_ROUNDS})${NC}"
                        STATUS="FAILED"
                    fi
                fi
            done

            # 清理备份文件
            for _pf in "${PROTECTED_FILES[@]}"; do
                rm -f "${_pf}.cr_backup" 2>/dev/null || true
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
                echo "等待任务完成 (最长 12 小时)..."
                for i in $(seq 1 1440); do
                    STATUS=$(sco acp jobs describe --workspace-name share-space -o json "$FOUND_JOB" 2>/dev/null | python -c "import json,sys; print(json.load(sys.stdin).get('state','UNKNOWN'))" 2>/dev/null || echo "UNKNOWN")
                    echo "  状态: ${STATUS} (${i}/1440)"
                    if [[ "$STATUS" == "SUCCEEDED" || "$STATUS" == "FAILED" || "$STATUS" == "STOPPED" || "$STATUS" == "SUSPENDED" || "$STATUS" == "CANCELLED" || "$STATUS" == "UNKNOWN" ]]; then
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
                # 失败/停止/未知状态 → 获取日志后回退到本地执行
                echo -e "${YELLOW}旧 SCO 任务状态异常 (${STATUS})，获取日志后尝试本地执行...${NC}"
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

                    # 用 experiment_runner.py 构建增强版调试 prompt
                    # (自动读取源文件、解析 traceback、检索 debug memory)
                    python "${SCRIPT_DIR}/experiment_runner.py" build-prompt \
                        "${WORKSPACE}" \
                        "${LATEST_LOG}" \
                        --script "${EXP_SCRIPT}" \
                        --backend "${BACKEND}" \
                        --round "${debug_round}" \
                        --max-rounds "${MAX_DEBUG_ROUNDS}" \
                        > /tmp/cr_debug_prompt.txt 2>/dev/null || {
                        # 回退：简化版 prompt
                        debug_round="$debug_round" BACKEND="$BACKEND" ERROR_LOG="$ERROR_LOG" \
                          EXP_SCRIPT="$EXP_SCRIPT" \
                          _render_prompt "local_debug_fallback.md" /tmp/cr_debug_prompt.txt
                    }
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

    TOPIC="$TOPIC" _render_prompt "paper_write_start.md" /tmp/cr_stage4_prompt.txt
    _claude_task "$(cat /tmp/cr_stage4_prompt.txt)"

    # === Stage review gate ===
    _stage_review "paper_writing" "${WORKSPACE}/paper/paper.tex"
    if [[ "$REVIEW_PASSED" != "True" ]]; then
        _stage_retry_fix "Paper Writing" "${WORKSPACE}/paper/paper.tex" \
            "Paper for: ${TOPIC}. Experiment logs: ${WORKSPACE}/experiment/sco_logs.txt"
        _stage_review "paper_writing" "${WORKSPACE}/paper/paper.tex" || true
    fi

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
    LATEST_EXTERNAL_REVIEW="$LATEST_EXTERNAL_REVIEW" LATEST_INTERNAL="${LATEST_INTERNAL:-无}" \
      _render_prompt "check_addressed.md" /tmp/cr_check_addressed.txt

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

        # ====================================================================
        # [NEW] 门控补充实验管线
        # 分析内部审稿反馈 → 提取实验需求 → 生成代码 → SCO 执行 (含 debug loop)
        # ====================================================================
        local GATE_EXP_DIR="${WORKSPACE}/experiment/gate_experiments"
        local GATE_EXP_CHECK="${GATE_EXP_DIR}/.experiments_checked"
        local GATE_EXP_RESULTS="${GATE_EXP_DIR}/experiment_results.json"

        if [[ ! -f "$GATE_EXP_CHECK" ]]; then
            mkdir -p "$GATE_EXP_DIR"
            echo -e "${CYAN}  [门控] 分析内部审稿意见，判断是否需要补充实验...${NC}"

            # Phase A: 分析内部反馈 → 提取实验需求
            INTERNAL_FEEDBACK="$INTERNAL_FEEDBACK" TOPIC="$TOPIC" \
              _render_prompt "gate_experiment_check.md" /tmp/cr_gate_exp_check.txt
            _claude_task "$(cat /tmp/cr_gate_exp_check.txt)" "/tmp/cr_gate_exp_check_output.txt" || true

            local NEEDS_EXP="false"
            if [[ -f /tmp/cr_gate_exp_check_output.txt ]]; then
                NEEDS_EXP=$(python3 -c "
import re, json
text = open('/tmp/cr_gate_exp_check_output.txt').read()
m = re.search(r'\`\`\`json\s*\n(.*?)\n\`\`\`', text, re.DOTALL)
if m:
    plan = json.loads(m.group(1))
    json.dump(plan, open('${GATE_EXP_DIR}/revision_plan.json', 'w'), indent=2)
    print('true' if plan.get('needs_experiments') else 'false')
else:
    print('false')
" 2>/dev/null || echo "false")
            fi

            if [[ "$NEEDS_EXP" == "true" ]]; then
                echo -e "${YELLOW}  [门控] 内部审稿要求补充实验，启动实验管线...${NC}"

                # Phase B1: Claude 编写实验代码
                echo -e "${CYAN}  [门控 B1] 生成实验代码...${NC}"
                local GATE_PLAN_JSON
                GATE_PLAN_JSON=$(cat "${GATE_EXP_DIR}/revision_plan.json" 2>/dev/null || echo "{}")
                REVISION_PLAN_JSON="$GATE_PLAN_JSON" REVISION_EXP_DIR="$GATE_EXP_DIR" TOPIC="$TOPIC" \
                  _render_prompt "revision_phase_b.md" /tmp/cr_gate_b1.txt
                _claude_task "$(cat /tmp/cr_gate_b1.txt)" "/tmp/cr_gate_b1_output.txt" || true

                # Phase B2: 并行提交到 SCO (含 debug loop)
                echo -e "${CYAN}  [门控 B2] 提交实验到 SCO...${NC}"
                local gate_pids=()
                local gate_result_files=()
                local gate_idx=0

                for exp_subdir in "${GATE_EXP_DIR}"/exp_*/; do
                    [[ -d "$exp_subdir" ]] || continue
                    [[ -f "${exp_subdir}/run_experiment.sh" ]] || continue
                    gate_idx=$((gate_idx + 1))
                    local exp_name="cr-gate-${SLUG:0:16}-e${gate_idx}"
                    local result_file="/tmp/cr_gate_exp_${gate_iter}_${gate_idx}.txt"
                    gate_result_files+=("$result_file")

                    echo -e "  ${YELLOW}▶ 门控实验 ${gate_idx}: ${exp_name}${NC}"

                    # 确保 manifest 存在
                    if [[ ! -f "${exp_subdir}/experiment_manifest.json" ]]; then
                        echo '{"gpu_count": 1, "estimated_runtime_hours": 2.0}' > "${exp_subdir}/experiment_manifest.json"
                    fi

                    python3 -c "
import sys; sys.path.insert(0, '${SCRIPT_DIR}')
from sco_runner import run_with_debug_loop
from pathlib import Path
result = run_with_debug_loop(Path('${exp_subdir}/run_experiment.sh'), '${exp_name}', force_sco=True, project_slug='${SLUG}', max_debug_rounds=3)
print(f'BACKEND={result.backend}')
print(f'SUCCESS={result.success}')
print(f'JOB_ID={result.job_id}')
print(f'LOG_PATH={result.log_path}')
print(f'ERROR={result.error_summary}')
" > "$result_file" 2>&1 &
                    gate_pids+=($!)
                done

                # 等待所有门控实验完成
                if [[ ${#gate_pids[@]} -gt 0 ]]; then
                    echo -e "  ${CYAN}等待 ${#gate_pids[@]} 个门控实验完成...${NC}"
                    for pid in "${gate_pids[@]}"; do
                        wait "$pid" 2>/dev/null || true
                    done

                    # 收集结果
                    local gate_success=0
                    local gate_failed=0
                    for rf in "${gate_result_files[@]}"; do
                        if [[ -f "$rf" ]]; then
                            local gs_ok
                            gs_ok=$(grep -oP 'SUCCESS=\K\S+' "$rf" 2>/dev/null || echo "False")
                            local gs_jid
                            gs_jid=$(grep -oP 'JOB_ID=\K\S+' "$rf" 2>/dev/null || echo "?")
                            if [[ "$gs_ok" == "True" ]]; then
                                gate_success=$((gate_success + 1))
                                echo -e "  ${GREEN}✓ 门控实验完成 (JOB=${gs_jid})${NC}"
                            else
                                gate_failed=$((gate_failed + 1))
                                local gs_err
                                gs_err=$(grep -oP 'ERROR=\K.*' "$rf" 2>/dev/null || echo "未知")
                                echo -e "  ${RED}✗ 门控实验失败 — ${gs_err}${NC}"
                            fi
                            rm -f "$rf" 2>/dev/null || true
                        fi
                    done
                    echo -e "  门控结果: ${GREEN}${gate_success} 成功${NC}, ${RED}${gate_failed} 失败${NC}"
                fi

                # 复制日志到实验目录
                for exp_subdir in "${GATE_EXP_DIR}"/exp_*/; do
                    [[ -d "$exp_subdir" ]] || continue
                    local lrf="${exp_subdir}/logs/result.txt"
                    if [[ -f "$lrf" ]]; then
                        local log_src
                        log_src=$(grep -oP 'LOG_PATH=\K\S+' "$lrf" 2>/dev/null || echo "")
                        if [[ -n "$log_src" && -f "$log_src" ]]; then
                            cp "$log_src" "${exp_subdir}/experiment_log.txt" 2>/dev/null || true
                        fi
                    fi
                done

                # 生成 experiment_results.json
                python3 -c "
import json
from pathlib import Path
gate_dir = Path('${GATE_EXP_DIR}')
exps = []
for d in sorted(gate_dir.glob('exp_*')):
    if d.is_dir():
        log_file = d / 'experiment_log.txt'
        exps.append({
            'id': d.name,
            'log_path': str(log_file) if log_file.exists() else None,
            'has_results': (d / 'results').exists(),
        })
json.dump(exps, open(gate_dir / 'experiment_results.json', 'w'), indent=2)
" 2>/dev/null || true
            else
                echo -e "  ${GREEN}[门控] 内部审稿无需补充实验，跳过实验管线${NC}"
            fi

            # 标记已检查，同一 gate 周期不重复分析
            touch "$GATE_EXP_CHECK"
        else
            echo -e "  ${CYAN}[门控] 实验需求已检查过 (.experiments_checked)，跳过分析${NC}"
        fi

        # ====================================================================
        # 组装实验结果引用（门控实验 + 修订实验），传给 gate_revise prompt
        # ====================================================================
        local REV_EXP_REF=""
        if [[ -f "$GATE_EXP_RESULTS" ]]; then
            REV_EXP_REF="
**门控补充实验结果**: ${GATE_EXP_RESULTS}
**实验日志目录**: ${GATE_EXP_DIR}/exp_*/
请阅读这些门控实验日志，将真实数据整合到论文中。不要编造数据。"
        fi
        if [[ -f "${WORKSPACE}/experiment/revision_iter_${ITERATION:-0}/experiment_results.json" ]]; then
            REV_EXP_REF+="
**修订实验结果**: ${WORKSPACE}/experiment/revision_iter_${ITERATION:-0}/experiment_results.json
**实验日志目录**: ${WORKSPACE}/experiment/revision_iter_${ITERATION:-0}/exp_*/"
        fi

        TOPIC="$TOPIC" gate_iter="$gate_iter" INTERNAL_SCORE="$INTERNAL_SCORE" \
          ADDRESSED_PCT="$ADDRESSED_PCT" INTERNAL_FEEDBACK="$INTERNAL_FEEDBACK" \
          REV_EXP_REF="$REV_EXP_REF" \
          _render_prompt "gate_revise.md" /tmp/cr_gate_revise.txt
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

# ---- Revision checkpoint helpers ----
# Fine-grained checkpoints within a revision iteration.
# Checkpoint file: state/<slug>/revision_checkpoint_<iter>.json
# Keys: phase_a, phase_b1, phase_b2_submitted, phase_b2, phase_c, exp_jobs

_rev_ckpt_path() {
    echo "state/${SLUG}/revision_checkpoint_${1}.json"
}

_rev_ckpt_set() {
    local iter="$1" key="$2" val="$3"
    local ckpt
    ckpt=$(_rev_ckpt_path "$iter")
    python3 -c "
import json
from pathlib import Path
from datetime import datetime, timezone
p = Path('${ckpt}')
d = json.loads(p.read_text()) if p.exists() and p.stat().st_size > 0 else {}
d['${key}'] = '${val}'
d['_updated'] = datetime.now(timezone.utc).isoformat()
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(d, indent=2, ensure_ascii=False))
"
}

_rev_ckpt_get() {
    local iter="$1" key="$2"
    local ckpt
    ckpt=$(_rev_ckpt_path "$iter")
    if [[ -f "$ckpt" ]]; then
        python3 -c "
import json
d = json.load(open('${ckpt}'))
v = d.get('${key}')
if v is None:
    pass
elif isinstance(v, bool):
    print('true' if v else 'false')
elif isinstance(v, dict):
    print(json.dumps(v, ensure_ascii=False))
else:
    print(v)
"
    fi
}

_rev_ckpt_is_done() {
    local iter="$1" key="$2"
    local val
    val=$(_rev_ckpt_get "$iter" "$key")
    [[ "$val" == "done" || "$val" == "true" ]]
}

# ---- _do_revise_and_resubmit: 三阶段修订 + 重新提交 ----
# Phase A: 分析审稿意见 → 提取实验需求 + 文字修改清单
# Phase B: 实际执行补充实验（通过 sco_runner 提交 SCO / 本地执行）
# Phase C: 用真实实验结果更新论文 + response letter
_do_revise_and_resubmit() {
    local verdict="${1:-unknown}"
    LATEST_REVIEW=$(ls -t "${WORKSPACE}/review/round_"*/external.md 2>/dev/null | head -1)
    NEXT_ITER=$((ITERATION + 1))
    local REVISION_EXP_DIR="${WORKSPACE}/experiment/revision_iter_${NEXT_ITER}"

    echo ""
    echo -e "${CYAN}━━━ 修订迭代 #${NEXT_ITER} (三阶段修订) ━━━${NC}"
    echo "  外部审稿意见: ${LATEST_REVIEW}"
    echo "  当前 verdict: ${verdict}"
    echo ""

    # ===== 检查点恢复：跳过已完成的阶段 =====
    local PHASE_A_DONE=false
    local PHASE_B1_DONE=false
    local PHASE_B2_SUBMITTED=false
    local PHASE_B2_DONE=false
    local PHASE_C_DONE=false
    if [[ -f "$(_rev_ckpt_path "$NEXT_ITER")" ]]; then
        echo -e "${YELLOW}  [检查点] 检测到修订检查点，恢复进度...${NC}"
        _rev_ckpt_is_done "$NEXT_ITER" "phase_a" && PHASE_A_DONE=true
        _rev_ckpt_is_done "$NEXT_ITER" "phase_b1" && PHASE_B1_DONE=true
        _rev_ckpt_is_done "$NEXT_ITER" "phase_b2_submitted" && PHASE_B2_SUBMITTED=true
        _rev_ckpt_is_done "$NEXT_ITER" "phase_b2" && PHASE_B2_DONE=true
        _rev_ckpt_is_done "$NEXT_ITER" "phase_c" && PHASE_C_DONE=true
        echo -e "  [检查点] A=$PHASE_A_DONE B1=$PHASE_B1_DONE B2提交=$PHASE_B2_SUBMITTED B2完成=$PHASE_B2_DONE C=$PHASE_C_DONE"
        echo ""
    fi

    # ========================================================================
    # Phase A: 分析审稿意见，分离「需要实验」和「只改文字」的需求
    # ========================================================================
    if [[ "$PHASE_A_DONE" == "true" ]]; then
        echo -e "${CYAN}━━━ Phase A: 跳过 (检查点已完成) ━━━${NC}"
        echo ""
        # 仍需从已有 revision_plan.json 读取 EXP_COUNT
        EXP_COUNT=$(python -c "
import json
plan = json.load(open('${REVISION_EXP_DIR}/revision_plan.json'))
print(len(plan.get('experiments', [])))
" 2>/dev/null || echo "0")
        echo -e "  审稿分析已跳过: 从检查点恢复, 需要 ${EXP_COUNT} 个补充实验"
        echo ""
    else
    echo -e "${CYAN}━━━ Phase A: 分析审稿意见，提取实验需求 ━━━${NC}"
    echo ""
    mkdir -p "${REVISION_EXP_DIR}"

    TOPIC="$TOPIC" LATEST_REVIEW="$LATEST_REVIEW" \
      _render_prompt "revision_phase_a.md" /tmp/cr_phase_a.txt

    _claude_task "$(cat /tmp/cr_phase_a.txt)" "/tmp/cr_phase_a_output.txt" || true

    # 提取 JSON 实验计划
    local EXP_COUNT=0
    if [[ -f /tmp/cr_phase_a_output.txt ]]; then
        EXP_COUNT=$(python -c "
import re, json
text = open('/tmp/cr_phase_a_output.txt').read()
m = re.search(r'\`\`\`json\s*\n(.*?)\n\`\`\`', text, re.DOTALL)
if m:
    plan = json.loads(m.group(1))
    json.dump(plan, open('${REVISION_EXP_DIR}/revision_plan.json', 'w'), indent=2)
    print(len(plan.get('experiments', [])))
else:
    # fallback: try to find JSON without code fences
    m2 = re.search(r'\{[\s\S]*\"experiments\"[\s\S]*\}', text)
    if m2:
        plan = json.loads(m2.group(0))
        json.dump(plan, open('${REVISION_EXP_DIR}/revision_plan.json', 'w'), indent=2)
        print(len(plan.get('experiments', [])))
    else:
        print(0)
" 2>/dev/null || echo "0")
    fi

    echo ""
    echo -e "  审稿分析完成: 需要 ${EXP_COUNT} 个补充实验"
    echo ""
    # 保存检查点: Phase A 完成
    _rev_ckpt_set "$NEXT_ITER" "phase_a" "done"
    fi  # end of Phase A skip block

    # ========================================================================
    # Phase B: 实际执行补充实验
    # ========================================================================
    if [[ "$EXP_COUNT" -gt 0 ]]; then
        echo -e "${CYAN}━━━ Phase B: 执行 ${EXP_COUNT} 个补充实验 ━━━${NC}"
        echo ""

        # B1: Claude 编写实验代码（类似实验设计阶段，但只写补充实验）
        if [[ "$PHASE_B1_DONE" == "true" ]]; then
            echo -e "${CYAN}  B1: 跳过 (检查点已完成)${NC}"
        else
        echo -e "${CYAN}  B1: 编写实验代码...${NC}"
        REVISION_PLAN_JSON=$(cat "${REVISION_EXP_DIR}/revision_plan.json" 2>/dev/null || echo "见 revision_plan.json")
        TOPIC="$TOPIC" REVISION_PLAN_JSON="$REVISION_PLAN_JSON" REVISION_EXP_DIR="$REVISION_EXP_DIR" \
          _render_prompt "revision_phase_b.md" /tmp/cr_phase_b1.txt
        _claude_task "$(cat /tmp/cr_phase_b1.txt)" "/tmp/cr_phase_b1_output.txt" || true
        # 保存检查点: Phase B1 完成
        _rev_ckpt_set "$NEXT_ITER" "phase_b1" "done"
        fi  # end of B1 skip block

        # B1.5: 环境准备 — 预下载所有依赖（本地执行，利用镜像/VPN）
        echo ""
        echo -e "${CYAN}  B1.5: 环境准备（预下载 wheels/模型/数据集）...${NC}"
        for exp_subdir in "${REVISION_EXP_DIR}"/exp_*/; do
            [[ -d "$exp_subdir" ]] || continue
            local exp_name_tmp="$(basename "$exp_subdir")"
            echo "    准备: ${exp_name_tmp}"
            python -c "
from sco_runner import _ensure_wheels, _ensure_model_cache, _ensure_dataset_cache, _prepare_env_for_sco
from pathlib import Path
d = Path('$exp_subdir')
_ensure_wheels()
_ensure_model_cache(d / 'run_experiment.sh')
_ensure_dataset_cache(d / 'run_experiment.sh')
_prepare_env_for_sco(d)
" 2>&1 | tail -3
        done
        echo -e "  ${GREEN}环境准备完成${NC}"

        # B2: 并行提交所有补充实验 (充分利用 GPU)
        echo ""
        echo -e "${CYAN}  B2: 并行提交补充实验...${NC}"

        # 读取主实验 manifest 获取总 GPU 数
        local MAIN_MANIFEST="${WORKSPACE}/experiment/experiment_manifest.json"
        local TOTAL_GPU=4
        if [[ -f "$MAIN_MANIFEST" ]]; then
            TOTAL_GPU=$(python -c "import json; print(json.load(open('$MAIN_MANIFEST')).get('gpu_count', 4))" 2>/dev/null || echo "4")
        fi

        # 第一遍：收集所有实验目录、计算总 GPU 需求
        local exp_dirs=()
        local exp_names=()
        local exp_gpus=()
        local total_gpu_demand=0
        local exp_idx=0

        for exp_subdir in "${REVISION_EXP_DIR}"/exp_*/; do
            [[ -d "$exp_subdir" ]] || continue
            local exp_script="${exp_subdir}/run_experiment.sh"
            [[ -f "$exp_script" ]] || continue

            # 确保 manifest 存在 — 继承主实验的 GPU 数量
            if [[ ! -f "${exp_subdir}/experiment_manifest.json" ]]; then
                echo "{\"gpu_count\": ${TOTAL_GPU}, \"estimated_runtime_hours\": 2.0}" > "${exp_subdir}/experiment_manifest.json"
            fi

            exp_idx=$((exp_idx + 1))
            local gpu_n=$(python -c "import json; print(json.load(open('${exp_subdir}/experiment_manifest.json')).get('gpu_count', 1))" 2>/dev/null || echo "1")
            local exp_name="cr-rev-${SLUG:0:20}-iter${NEXT_ITER}-e${exp_idx}"

            exp_dirs+=("$exp_subdir")
            exp_names+=("$exp_name")
            exp_gpus+=("$gpu_n")
            total_gpu_demand=$((total_gpu_demand + gpu_n))

            echo -e "  [${exp_idx}] $(basename "$exp_subdir") — ${gpu_n} GPU — ${exp_name}"
        done

        echo ""
        echo -e "  总 GPU 需求: ${total_gpu_demand} / 可用: ${TOTAL_GPU}"
        echo -e "  并行提交 ${#exp_dirs[@]} 个实验 (SCO 自动调度)"
        echo ""

        # 第二遍：并行提交所有实验 (后台进程)
        local pids=()
        local result_files=()

        if [[ "$PHASE_B2_DONE" == "true" ]]; then
            echo -e "  ${GREEN}[检查点] B2 已完成，跳过实验执行与等待${NC}"
            # 确保 experiment_results.json 存在（可能丢失）
            if [[ ! -f "${REVISION_EXP_DIR}/experiment_results.json" ]]; then
                python -c "
import json
from pathlib import Path
rev_dir = Path('${REVISION_EXP_DIR}')
exps = []
for d in sorted(rev_dir.glob('exp_*')):
    if d.is_dir():
        log_file = d / 'experiment_log.txt'
        exps.append({
            'id': d.name,
            'log_path': str(log_file) if log_file.exists() else None,
            'has_results': (d / 'results').exists(),
        })
json.dump(exps, open(rev_dir / 'experiment_results.json', 'w'), indent=2)
" 2>/dev/null || true
            fi
        elif [[ "$PHASE_B2_SUBMITTED" == "true" ]]; then
            echo -e "  ${YELLOW}[检查点] 实验已提交，尝试从 SCO 恢复结果...${NC}"
            echo ""
            local all_ready=true
            local recovered=0
            local still_running=0
            local failed_count=0
            local repaired_count=0
            local bg_pids=()

            # ── Pass 1: quick status check on ALL experiments (no blocking) ──
            # Collect jobs that need waiting or repair, then launch in parallel.
            local _wait_list=()   # "exp_name|exp_subdir|job_id" for RUNNING jobs
            local _repair_list=() # "exp_name|exp_subdir" for FAILED jobs

            for exp_subdir in "${REVISION_EXP_DIR}"/exp_*/; do
                [[ -d "$exp_subdir" ]] || continue
                [[ -f "${exp_subdir}/run_experiment.sh" ]] || continue
                local exp_name=$(basename "$exp_subdir")

                # 已有本地日志 → 跳过
                if [[ -f "${exp_subdir}/experiment_log.txt" ]]; then
                    echo -e "  ${GREEN}✓ ${exp_name}: 日志已存在${NC}"
                    continue
                fi

                # 尝试从 sco_job_id.txt 恢复
                local job_id_file="${exp_subdir}/logs/sco_job_id.txt"
                if [[ -f "$job_id_file" ]]; then
                    local job_id=$(cat "$job_id_file" | tr -d '[:space:]')
                    if [[ -n "$job_id" ]]; then
                        echo -ne "  ⏳ ${exp_name}: 查询 SCO 任务 ${job_id} ... "
                        local sco_status=$(sco acp jobs describe --workspace-name share-space -o json "$job_id" 2>/dev/null | python3 -c "import json,sys; d=sys.stdin.read().strip(); print(json.loads(d).get('status', json.loads(d).get('state', 'UNKNOWN')) if d else 'UNKNOWN')" 2>/dev/null || echo "UNKNOWN")
                        echo "${sco_status}"

                        case "$sco_status" in
                            SUCCEEDED)
                                echo -e "  ${GREEN}✓ ${exp_name}: 从 SCO 获取日志${NC}"
                                sco acp jobs stream-logs --workspace-name share-space "$job_id" > "${exp_subdir}/experiment_log.txt" 2>/dev/null || true
                                recovered=$((recovered + 1))
                                ;;
                            RUNNING|PENDING|QUEUED)
                                echo -e "  ${CYAN}  ${exp_name}: 仍在运行 (${sco_status})，后台等待...${NC}"
                                still_running=$((still_running + 1))
                                all_ready=false
                                _wait_list+=("${exp_name}|${exp_subdir}|${job_id}")
                                ;;
                            *)
                                echo -e "  ${RED}  ${exp_name}: 状态 ${sco_status}，立即后台修复...${NC}"
                                failed_count=$((failed_count + 1))
                                all_ready=false
                                _repair_list+=("${exp_name}|${exp_subdir}")
                                ;;
                        esac
                    else
                        all_ready=false
                        failed_count=$((failed_count + 1))
                    fi
                else
                    echo -e "  ${YELLOW}  ${exp_name}: 无 job_id 文件，将重新提交${NC}"
                    all_ready=false
                    failed_count=$((failed_count + 1))
                    _repair_list+=("${exp_name}|${exp_subdir}")
                fi
            done

            # ── Pass 2: launch background waiters + repairs IN PARALLEL ──
            # Each background process is independent; they all run concurrently.

            # 2a: background waiters for RUNNING jobs
            # If the job eventually fails, the waiter immediately triggers a
            # repair (resubmit to SCO) so we don't need to wait for the next
            # iteration.  Results are written to the same tmp-file convention
            # as 2b so Pass 3 can display them uniformly.
            for entry in "${_wait_list[@]}"; do
                IFS='|' read -r _w_name _w_dir _w_jobid <<< "$entry"
                (
                    _s="RUNNING"
                    for _i in $(seq 1 120); do
                        sleep 30
                        _s=$(sco acp jobs describe --workspace-name share-space -o json "${_w_jobid}" 2>/dev/null | python3 -c "import json,sys; d=sys.stdin.read().strip(); print(json.loads(d).get('status', json.loads(d).get('state', 'UNKNOWN')) if d else 'UNKNOWN')" 2>/dev/null || echo "UNKNOWN")
                        case "$_s" in
                            SUCCEEDED|FAILED|STOPPED|SUSPENDED|CANCELLED) break ;;
                        esac
                    done
                    if [[ "$_s" == "SUCCEEDED" ]]; then
                        sco acp jobs stream-logs --workspace-name share-space "${_w_jobid}" > "${_w_dir}/experiment_log.txt" 2>/dev/null || true
                        echo -e "  ${GREEN}✓ ${_w_name}: 完成，日志已获取${NC}"
                    else
                        echo -e "  ${RED}✗ ${_w_name}: 最终状态 ${_s}，立即触发修复...${NC}"
                        # Immediately resubmit — same logic as the repairer in 2b
                        python3 -c "
import sys; sys.path.insert(0, '${SCRIPT_DIR}')
from sco_runner import run_with_debug_loop
from pathlib import Path
result = run_with_debug_loop(Path('${_w_dir}/run_experiment.sh'), '${_w_name}', force_sco=True, project_slug='${SLUG}', max_debug_rounds=3)
print(f'BACKEND={result.backend}')
print(f'SUCCESS={result.success}')
print(f'JOB_ID={result.job_id}')
print(f'LOG_PATH={result.log_path}')
print(f'ERROR={result.error_summary}')
" > "/tmp/cr_rev_repair_${NEXT_ITER}_${_w_name}.txt" 2>&1
                        _r_ok=$(grep -oP 'SUCCESS=\K\S+' "/tmp/cr_rev_repair_${NEXT_ITER}_${_w_name}.txt" 2>/dev/null || echo "False")
                        _r_jid=$(grep -oP 'JOB_ID=\K\S+' "/tmp/cr_rev_repair_${NEXT_ITER}_${_w_name}.txt" 2>/dev/null || echo "?")
                        if [[ "$_r_ok" == "True" ]]; then
                            echo -e "  ${GREEN}✓ ${_w_name}: 已重新提交 (JOB_ID=${_r_jid})${NC}"
                        else
                            echo -e "  ${RED}✗ ${_w_name}: 重新提交失败${NC}"
                        fi
                    fi
                ) &
                bg_pids+=($!)
            done

            # 2b: background repairs for FAILED/STOPPED/SUSPENDED/CANCELLED jobs
            # Each repair writes results to a tmp file (no stdout echo — avoids
            # interleaving with main-script output).  Results are displayed in
            # Pass 3 after all backgrounds complete.
            for entry in "${_repair_list[@]}"; do
                IFS='|' read -r _r_name _r_dir <<< "$entry"
                local _repair_tmp="/tmp/cr_rev_repair_${NEXT_ITER}_${_r_name}.txt"
                echo -ne "  🔧 ${_r_name}: 正在重新提交到 SCO...\r"
                (
                    python3 -c "
import sys; sys.path.insert(0, '${SCRIPT_DIR}')
from sco_runner import run_with_debug_loop
from pathlib import Path
result = run_with_debug_loop(Path('${_r_dir}/run_experiment.sh'), '${_r_name}', force_sco=True, project_slug='${SLUG}', max_debug_rounds=3)
print(f'BACKEND={result.backend}')
print(f'SUCCESS={result.success}')
print(f'JOB_ID={result.job_id}')
print(f'LOG_PATH={result.log_path}')
print(f'ERROR={result.error_summary}')
" > "${_repair_tmp}" 2>&1
                    echo $? > "${_repair_tmp}.exit"
                ) &
                bg_pids+=($!)
                repaired_count=$((repaired_count + 1))
            done

            # ── Pass 3: wait for ALL background processes, then show coordinated results ──
            if [[ ${#bg_pids[@]} -gt 0 ]]; then
                echo ""
                echo -e "  ${CYAN}后台任务: ${#bg_pids[@]} 个 (等待=${#_wait_list[@]} 修复=${#_repair_list[@]})${NC}"
                for pid in "${bg_pids[@]}"; do
                    wait "$pid" 2>/dev/null || true
                done
                echo -e "  ${GREEN}所有后台任务已完成${NC}"
                echo ""

                # Show repair results from ALL sources (direct repairs + waiter-triggered repairs)
                local _actual_repair_count=0
                for _repair_tmp in /tmp/cr_rev_repair_${NEXT_ITER}_*.txt; do
                    [[ -f "$_repair_tmp" ]] || continue
                    local _rf_name=$(basename "$_repair_tmp" .txt)
                    # Extract experiment name from filename pattern: cr_rev_repair_ITER_EXPNAME.txt
                    local _rf_exp="${_rf_name#cr_rev_repair_${NEXT_ITER}_}"
                    local _r_jobid=$(grep -oP 'JOB_ID=\K\S+' "$_repair_tmp" 2>/dev/null || echo "?")
                    local _r_ok=$(grep -oP 'SUCCESS=\K\S+' "$_repair_tmp" 2>/dev/null || echo "False")
                    if [[ "$_r_ok" == "True" ]]; then
                        echo -e "  ${GREEN}✓ ${_rf_exp}: 已重新提交 (JOB_ID=${_r_jobid})${NC}"
                    else
                        local _r_err=$(grep -oP 'ERROR=\K.*' "$_repair_tmp" 2>/dev/null || echo "未知错误")
                        echo -e "  ${RED}✗ ${_rf_exp}: 重新提交失败 — ${_r_err}${NC}"
                    fi
                    rm -f "$_repair_tmp" "${_repair_tmp}.exit" 2>/dev/null || true
                    _actual_repair_count=$((_actual_repair_count + 1))
                done
                if [[ $_actual_repair_count -gt 0 ]]; then
                    echo ""
                fi
                # Update repaired_count to include waiter-triggered repairs
                repaired_count=$_actual_repair_count
            fi

            if [[ $recovered -gt 0 ]]; then
                echo -e "  ${GREEN}从 SCO 恢复了 ${recovered} 个实验的日志${NC}"
            fi

            # 重新检查 all_ready
            all_ready=true
            for exp_subdir in "${REVISION_EXP_DIR}"/exp_*/; do
                [[ -d "$exp_subdir" ]] || continue
                [[ -f "${exp_subdir}/run_experiment.sh" ]] || continue
                if [[ ! -f "${exp_subdir}/experiment_log.txt" ]]; then
                    all_ready=false
                    break
                fi
            done

            if [[ "$all_ready" == "true" ]]; then
                echo -e "  ${GREEN}所有实验日志已就绪，跳过重新提交${NC}"
                PHASE_B2_SUBMITTED=true
            elif [[ $repaired_count -gt 0 || $still_running -gt 0 ]]; then
                # 已修复的实验需要等待 SCO 执行；仍在运行的实验需要等待完成。
                # 两种情况都应保持 submitted 状态，让下一轮检查点来收割结果。
                echo -e "  ${CYAN}[检查点] 保持等待 (已修复=${repaired_count} 运行中=${still_running})，下一轮检查点继续${NC}"
                PHASE_B2_SUBMITTED=true
            else
                echo -e "  ${YELLOW}[检查点] 部分实验需要重新提交${NC}"
                PHASE_B2_SUBMITTED=false
            fi
        fi

        if [[ "$PHASE_B2_DONE" != "true" ]]; then
            # 提交实验（如果尚未提交）
            if [[ "$PHASE_B2_SUBMITTED" != "true" ]]; then
                for i in $(seq 0 $((${#exp_dirs[@]} - 1))); do
                    local exp_subdir="${exp_dirs[$i]}"
                    local exp_script="${exp_subdir}/run_experiment.sh"
                    local exp_name="${exp_names[$i]}"
                    local result_file="/tmp/cr_rev_exp_${NEXT_ITER}_${i}.txt"
                    result_files+=("$result_file")

                    echo -e "  ${YELLOW}▶ 提交实验 $((i+1))/${#exp_dirs[@]}: ${exp_name}${NC}"

                    python -c "
import sys; sys.path.insert(0, '${SCRIPT_DIR}')
from sco_runner import run_with_debug_loop
from pathlib import Path
result = run_with_debug_loop(Path('${exp_script}'), '${exp_name}', force_sco=True, project_slug='${SLUG}', max_debug_rounds=5)
print(f'BACKEND={result.backend}')
print(f'SUCCESS={result.success}')
print(f'JOB_ID={result.job_id}')
print(f'LOG_PATH={result.log_path}')
print(f'ERROR={result.error_summary}')
" > "$result_file" 2>&1 &
                    pids+=($!)
                done

                echo ""
                echo -e "  ${CYAN}已提交 ${#pids[@]} 个任务，等待全部完成...${NC}"
                echo "  (SCO 会根据可用 GPU 自动调度，无需等待串行)"

                # 保存检查点: 实验已提交
                _rev_ckpt_set "$NEXT_ITER" "phase_b2_submitted" "done"
            else
                echo -e "  ${CYAN}[检查点] 等待之前提交的实验完成...${NC}"
            fi

            # 等待所有后台进程完成（仅当有新提交时）
            if [[ ${#pids[@]} -gt 0 ]]; then
                for i in $(seq 0 $((${#pids[@]} - 1))); do
                    local pid="${pids[$i]}"
                    local exp_name="${exp_names[$i]}"
                    wait "$pid" 2>/dev/null || true
                    echo -e "  [$((i+1))/${#pids[@]}] ${exp_name} — 进程结束 (pid=${pid})"
                done
            else
                # 恢复场景：没有新提交，等待实验日志出现
                echo -e "  ${CYAN}等待实验日志...${NC}"
                local wait_cycles=0
                local max_wait_cycles=60  # 最多等 60 × 30s = 30 分钟
                while [[ $wait_cycles -lt $max_wait_cycles ]]; do
                    local all_done=true
                    for exp_subdir in "${REVISION_EXP_DIR}"/exp_*/; do
                        [[ -d "$exp_subdir" ]] || continue
                        [[ -f "${exp_subdir}/run_experiment.sh" ]] || continue
                        if [[ ! -f "${exp_subdir}/experiment_log.txt" ]]; then
                            all_done=false
                            break
                        fi
                    done
                    if [[ "$all_done" == "true" ]]; then
                        echo -e "  ${GREEN}所有实验日志已就绪${NC}"
                        break
                    fi
                    wait_cycles=$((wait_cycles + 1))
                    sleep 30
                    if [[ $((wait_cycles % 4)) -eq 0 ]]; then
                        echo -e "  ... 已等待 $((wait_cycles / 2)) 分钟 ..."
                    fi
                done
            fi

            echo ""
            echo -e "${CYAN}  所有实验进程结束，汇总结果...${NC}"
            echo ""

            # 第三遍：收集结果
            local exp_success=0
            local exp_failed=0
            if [[ ${#pids[@]} -gt 0 ]]; then
                # 有新提交：从 result_files 收集
                for i in $(seq 0 $((${#exp_dirs[@]} - 1))); do
                    local exp_subdir="${exp_dirs[$i]}"
                    local exp_name="${exp_names[$i]}"
                    local result_file="${result_files[$i]}"

                    echo -e "  ── 实验 $((i+1))/${#exp_dirs[@]}: $(basename "$exp_subdir") ──"

                    local EXP_OUTPUT=""
                    if [[ -f "$result_file" ]]; then
                        EXP_OUTPUT=$(cat "$result_file")
                        echo "${EXP_OUTPUT}" | head -5
                    else
                        echo "  ${RED}无输出文件${NC}"
                    fi

                    local exp_backend=$(echo "$EXP_OUTPUT" | grep -oP 'BACKEND=\K\S+')
                    local exp_ok=$(echo "$EXP_OUTPUT" | grep -oP 'SUCCESS=\K\S+')

                    local exp_log_path=$(echo "$EXP_OUTPUT" | grep -oP 'LOG_PATH=\K\S+')
                    if [[ -n "$exp_log_path" && -f "$exp_log_path" ]]; then
                        cp "$exp_log_path" "${exp_subdir}/experiment_log.txt" 2>/dev/null || true
                    fi
                    echo "$EXP_OUTPUT" > "${exp_subdir}/logs/result.txt" 2>/dev/null || true

                    if [[ "$exp_ok" == "True" ]]; then
                        echo -e "  ${GREEN}✓ 完成 (${exp_backend})${NC}"
                        exp_success=$((exp_success + 1))
                    else
                        echo -e "  ${RED}✗ 失败 (${exp_backend})${NC}"
                        exp_failed=$((exp_failed + 1))
                    fi
                    echo ""
                done
            else
                # 恢复场景：从持久化的 result.txt 解析真实结果
                for exp_subdir in "${REVISION_EXP_DIR}"/exp_*/; do
                    [[ -d "$exp_subdir" ]] || continue
                    [[ -f "${exp_subdir}/run_experiment.sh" ]] || continue
                    echo -e "  ── 实验: $(basename "$exp_subdir") ──"

                    local _rec_ok="False"
                    local _rec_backend="unknown"
                    local _persist_result="${exp_subdir}/logs/result.txt"

                    if [[ -f "$_persist_result" ]]; then
                        _rec_ok=$(grep -oP 'SUCCESS=\K\S+' "$_persist_result" 2>/dev/null || echo "False")
                        _rec_backend=$(grep -oP 'BACKEND=\K\S+' "$_persist_result" 2>/dev/null || echo "unknown")
                        # Show result summary
                        grep -oP '^(BACKEND|SUCCESS|ERROR)=' "$_persist_result" 2>/dev/null | head -3 || true
                    elif [[ -f "${exp_subdir}/experiment_log.txt" ]]; then
                        # Fallback: check log for error patterns
                        if grep -qE "AttributeError|Traceback|SyntaxError|FAILED" "${exp_subdir}/experiment_log.txt" 2>/dev/null; then
                            echo "  (从日志检测到错误模式)"
                            _rec_ok="False"
                            _rec_backend="sco"
                        else
                            echo "  (旧格式日志，无法确定结果)"
                            _rec_ok="False"
                            _rec_backend="unknown"
                        fi
                    fi

                    if [[ "$_rec_ok" == "True" ]]; then
                        echo -e "  ${GREEN}✓ 完成 (${_rec_backend})${NC}"
                        exp_success=$((exp_success + 1))
                    else
                        echo -e "  ${RED}✗ 失败 (${_rec_backend})${NC}"
                        exp_failed=$((exp_failed + 1))
                    fi
                    echo ""
                done
            fi

            echo -e "  补充实验结果: ${GREEN}${exp_success} 成功${NC}, ${RED}${exp_failed} 失败${NC}"

            # 全部失败 → 终止，不进入 Phase C
            if [[ "$exp_success" -eq 0 && "$exp_failed" -gt 0 ]]; then
                echo ""
                echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
                echo -e "${RED}  所有补充实验均失败，无法更新论文。${NC}"
                echo -e "${RED}  请先检查实验脚本和 SCO 任务状态后再重试。${NC}"
                echo -e "${RED}  实验目录: ${REVISION_EXP_DIR}${NC}"
                echo -e "${RED}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
                exit 1
            fi

            # 部分失败 → 警告但继续
            if [[ "$exp_failed" -gt 0 ]]; then
                echo -e "  ${YELLOW}⚠ ${exp_failed} 个实验失败，将仅使用成功实验的数据更新论文${NC}"
            fi

            # 汇总所有实验日志路径到文件，供 Phase C 使用
            python -c "
import json
from pathlib import Path
rev_dir = Path('${REVISION_EXP_DIR}')
exps = []
for d in sorted(rev_dir.glob('exp_*')):
    if d.is_dir():
        log_file = d / 'experiment_log.txt'
        exps.append({
            'id': d.name,
            'log_path': str(log_file) if log_file.exists() else None,
            'has_results': (d / 'results').exists(),
        })
json.dump(exps, open(rev_dir / 'experiment_results.json', 'w'), indent=2)
" 2>/dev/null || true

            # 保存检查点: Phase B2 完成
            _rev_ckpt_set "$NEXT_ITER" "phase_b2" "done"
        fi  # end of B2 execution block
    else
        echo -e "${CYAN}━━━ Phase B: 跳过 (无需补充实验) ━━━${NC}"
        echo ""
    fi

    # ========================================================================
    # Phase C: 用真实实验结果更新论文
    # ========================================================================
    if [[ "$PHASE_C_DONE" == "true" ]]; then
        echo -e "${CYAN}━━━ Phase C: 跳过 (检查点已完成) ━━━${NC}"
        echo ""
    else
    echo -e "${CYAN}━━━ Phase C: 更新论文 ━━━${NC}"
    echo ""

    TOPIC="$TOPIC" NEXT_ITER="$NEXT_ITER" verdict="$verdict" \
      LATEST_REVIEW="$LATEST_REVIEW" REVISION_EXP_DIR="$REVISION_EXP_DIR" \
      _render_prompt "revision_phase_c.md" /tmp/cr_phase_c.txt
    _claude_task "$(cat /tmp/cr_phase_c.txt)" "/tmp/cr_phase_c_output.txt" || true
    # 保存检查点: Phase C 完成
    _rev_ckpt_set "$NEXT_ITER" "phase_c" "done"
    fi  # end of Phase C skip block

    # 更新迭代号（gate 需要引用正确的 revision_iter_* 目录）
    ITERATION=${NEXT_ITER}

    # 内部审稿门控 — 只有通过内部审稿才能提交外部
    _internal_review_gate 5

    # 提交外部审稿
    _do_submit_review

    # 保存状态（在 gate + submit 都成功后，防止崩溃后状态不一致）
    python -c "
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.load('${SLUG}')
state.iteration = ${NEXT_ITER}
sm.save(state)
sm.complete_stage(state, Stage.REVISE)
"
}

# ---- _continue_project: 继续已有项目（按阶段路由）----
_continue_project() {
    echo -e "${CYAN}继续项目: ${TOPIC}${NC}"
    echo -e "阶段: ${STAGE} | 迭代: ${ITERATION}"
    echo ""

    case "$STAGE" in
        environment_preparation)
            echo -e "${CYAN}环境准备阶段 — 重新执行环境准备...${NC}"
            _do_environment_preparation "${WORKSPACE}/experiment"
            echo ""
            # 完成环境准备，进入实验执行
            python -c "
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.ENVIRONMENT_PREPARATION)
sm.start_stage(state, Stage.EXPERIMENT_EXECUTION)
"
            _continue_experiment
            ;;
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
                HYPOTHESIS_JSON="$HYPOTHESIS_JSON" \
                  _render_prompt "hypothesis_refine.md" /tmp/cr_hypothesis_refine.txt
                _claude_task "$(cat /tmp/cr_hypothesis_refine.txt)" || true
            fi

            # === Stage review gate ===
            _stage_review "hypothesis_generation" "${WORKSPACE}/hypothesis/hypothesis_report.md"
            if [[ "$REVIEW_PASSED" != "True" ]]; then
                _stage_retry_fix "Hypothesis Generation" "${WORKSPACE}/hypothesis/hypothesis_report.md" \
                    "Hypothesis report for: ${TOPIC}"
                _stage_review "hypothesis_generation" "${WORKSPACE}/hypothesis/hypothesis_report.md" || true
            fi

            python -c "
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.HYPOTHESIS_GENERATION)
sm.start_stage(state, Stage.BASELINE_FETCHING)
"

            # ── Baseline fetching: clone GitHub repos for structural reference ──
            echo "[Phase] Baseline fetching — searching GitHub for reference implementations..."
            BASELINE_CTX_FILE="${WORKSPACE}/experiment/baseline_context.md"
            mkdir -p "${WORKSPACE}/experiment"

            # Extract method names from hypothesis
            HYPO_FILE="${WORKSPACE}/hypothesis/hypothesis_output.json"
            if [ -f "$HYPO_FILE" ]; then
                python -c "
import json, sys
try:
    data = json.load(open('$HYPO_FILE'))
    methods = []
    for h in data.get('hypotheses', []):
        if isinstance(h, dict):
            methods.extend(h.get('baselines', []) or h.get('baseline_methods', []))
    if methods:
        with open('/tmp/cr_baseline_methods.txt', 'w') as f:
            f.write(','.join(methods[:10]))
        print(f'Found {len(methods)} baseline methods')
    else:
        print('No baselines in hypothesis — extracting from literature standards')
        sys.exit(1)
except Exception as e:
    print(f'Hypothesis parse error: {e}')
    sys.exit(1)
" 2>/dev/null && BASELINE_METHODS="--methods $(cat /tmp/cr_baseline_methods.txt | tr ',' ' ')" || {
                    # Fallback: extract from landscape standard_baselines
                    BASELINE_METHODS=""
                }
            else
                BASELINE_METHODS=""
            fi

            # Run baseline finder (non-fatal)
            python "${SCRIPT_DIR}/baseline_finder.py" \
                ${BASELINE_METHODS} \
                --cache-dir "${WORKSPACE}/../.shared/baselines" \
                --max 5 \
                --output "${BASELINE_CTX_FILE}" 2>&1 || {
                echo "[Phase] Baseline fetching skipped (no repos found or network unavailable)"
                echo "" > "${BASELINE_CTX_FILE}"
            }

            BASELINE_CONTEXT=$(cat "${BASELINE_CTX_FILE}" 2>/dev/null || echo "")

            python -c "
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.BASELINE_FETCHING)
sm.start_stage(state, Stage.EXPERIMENT_DESIGN)
"

            # Fall through to experiment design
            BASELINE_CONTEXT="${BASELINE_CONTEXT}" \
            HYPOTHESIS_CLAUSE="和假说" \
              HYPOTHESIS_LINE="研究假说: ${WORKSPACE}/hypothesis/hypothesis_report.md" \
              DESIGN_STEP_1="阅读文献综述和研究假说" \
              _render_prompt "experiment_design_start.md" /tmp/cr_stage2_prompt.txt
            _claude_task "$(cat /tmp/cr_stage2_prompt.txt)"

            # 验证 manifest 是否被创建，缺失时自动补全
            MANIFEST="${WORKSPACE}/experiment/experiment_manifest.json"
            if [[ ! -f "$MANIFEST" ]]; then
                echo -e "${YELLOW}[system] Claude 未创建 experiment_manifest.json，自动补全为 4 GPU${NC}"
                mkdir -p "$(dirname "$MANIFEST")"
                echo '{"gpu_count": 4}' > "$MANIFEST"
            else
                MANIFEST_INFO=$(python -c "
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
                    "Experiment plan for: ${TOPIC}"
                _stage_review "experiment_design" "${WORKSPACE}/experiment/experiment_plan.md" || true
            fi

            python -c "
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.EXPERIMENT_DESIGN)
sm.start_stage(state, Stage.ENVIRONMENT_PREPARATION)
"

            # ── 环境准备 ──
            _do_environment_preparation "${WORKSPACE}/experiment"

            python -c "
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.ENVIRONMENT_PREPARATION)
sm.start_stage(state, Stage.EXPERIMENT_EXECUTION)
"
            _continue_experiment
            ;;
        experiment_design)
            echo -e "${YELLOW}实验设计阶段 — 重新执行实验设计...${NC}"
            BASELINE_CTX_FILE="${WORKSPACE}/experiment/baseline_context.md"
            BASELINE_CONTEXT=$(cat "${BASELINE_CTX_FILE}" 2>/dev/null || echo "")
            BASELINE_CONTEXT="${BASELINE_CONTEXT}" \
            HYPOTHESIS_CLAUSE="" HYPOTHESIS_LINE="" DESIGN_STEP_1="阅读文献综述" \
              _render_prompt "experiment_design_start.md" /tmp/cr_stage2_prompt.txt
            _claude_task "$(cat /tmp/cr_stage2_prompt.txt)"

            # 验证 manifest 是否被创建，缺失时自动补全
            MANIFEST="${WORKSPACE}/experiment/experiment_manifest.json"
            if [[ ! -f "$MANIFEST" ]]; then
                echo -e "${YELLOW}[system] Claude 未创建 experiment_manifest.json，自动补全为 4 GPU${NC}"
                mkdir -p "$(dirname "$MANIFEST")"
                echo '{"gpu_count": 4}' > "$MANIFEST"
            else
                MANIFEST_INFO=$(python -c "
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
                    "Experiment plan for: ${TOPIC}"
                _stage_review "experiment_design" "${WORKSPACE}/experiment/experiment_plan.md" || true
            fi

            python -c "
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.EXPERIMENT_DESIGN)
sm.start_stage(state, Stage.ENVIRONMENT_PREPARATION)
"

            # ── 环境准备 ──
            _do_environment_preparation "${WORKSPACE}/experiment"

            python -c "
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.ENVIRONMENT_PREPARATION)
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
        export TOPIC WORKSPACE STAGE ITERATION SLUG
        _setup_logging "$WORKSPACE"
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
    USER_INPUT="$USER_INPUT" _render_prompt "topic_refine.md" /tmp/cr_topic_prompt.txt

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

    # 启动日志记录（追加到项目 run.log，不覆盖历史）
    WORKSPACE="$(cd "$WORKSPACE" 2>/dev/null && pwd || echo "$WORKSPACE")"
    export TOPIC WORKSPACE SLUG
    STAGE="literature_search" ITERATION=0
    _setup_logging "$WORKSPACE"

    # ===== Stage 1: 文献检索 =====
    echo ""
    echo -e "${CYAN}━━━ Stage 1/6: 文献检索 ━━━${NC}"
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

    # === Stage review gate ===
    _stage_review "literature_search" "${WORKSPACE}/literature/literature_review.md"
    if [[ "$REVIEW_PASSED" != "True" ]]; then
        _stage_retry_fix "Literature Search" "${WORKSPACE}/literature/literature_review.md" \
            "Literature review for: ${TOPIC}"
        _stage_review "literature_search" "${WORKSPACE}/literature/literature_review.md" || true
    fi

    python -c "
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.LITERATURE_SEARCH, {'papers_found': 0})
"

    # ===== Stage 2: 假说生成 (ReAct-based) =====
    echo ""
    echo -e "${CYAN}━━━ Stage 2/6: 假说生成 (ReAct 检索+对比+假说) ━━━${NC}"
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
        HYPOTHESIS_JSON="$HYPOTHESIS_JSON" \
          _render_prompt "hypothesis_refine.md" /tmp/cr_hypothesis_refine.txt
        _claude_task "$(cat /tmp/cr_hypothesis_refine.txt)" || echo "[WARN] 假说审阅跳过"
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

    python -c "
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.HYPOTHESIS_GENERATION)
sm.start_stage(state, Stage.BASELINE_FETCHING)
"

    # ===== Stage 2.5: 基线仓库获取 =====
    echo ""
    echo -e "${CYAN}━━━ Stage 2.5/6: 基线仓库获取 ━━━${NC}"
    echo ""

    BASELINE_CTX_FILE="${WORKSPACE}/experiment/baseline_context.md"
    mkdir -p "${WORKSPACE}/experiment"

    HYPO_FILE="${WORKSPACE}/hypothesis/hypothesis_output.json"
    if [ -f "$HYPO_FILE" ]; then
        python -c "
import json, sys
try:
    data = json.load(open('$HYPO_FILE'))
    methods = []
    for h in data.get('hypotheses', []):
        if isinstance(h, dict):
            methods.extend(h.get('baselines', []) or h.get('baseline_methods', []))
    if methods:
        with open('/tmp/cr_baseline_methods.txt', 'w') as f:
            f.write(','.join(methods[:10]))
        sys.exit(0)
    sys.exit(1)
except Exception:
    sys.exit(1)
" 2>/dev/null && BASELINE_METHODS_ARG="--methods $(cat /tmp/cr_baseline_methods.txt | tr ',' ' ')" || BASELINE_METHODS_ARG=""
    fi

    python "${SCRIPT_DIR}/baseline_finder.py" \
        ${BASELINE_METHODS_ARG} \
        --cache-dir "${WORKSPACE}/../.shared/baselines" \
        --max 5 \
        --output "${BASELINE_CTX_FILE}" 2>&1 || {
        echo "[Phase] Baseline fetching skipped (no repos found or network unavailable)"
        echo "" > "${BASELINE_CTX_FILE}"
    }

    BASELINE_CONTEXT=$(cat "${BASELINE_CTX_FILE}" 2>/dev/null || echo "")

    python -c "
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.BASELINE_FETCHING)
sm.start_stage(state, Stage.EXPERIMENT_DESIGN)
"

    # ===== Stage 3: 实验设计 =====
    echo ""
    echo -e "${CYAN}━━━ Stage 3/6: 实验设计 ━━━${NC}"
    echo ""

    BASELINE_CONTEXT="${BASELINE_CONTEXT}" \
    HYPOTHESIS_CLAUSE="" HYPOTHESIS_LINE="" DESIGN_STEP_1="阅读文献综述" \
      _render_prompt "experiment_design_start.md" /tmp/cr_stage2_prompt.txt
    _claude_task "$(cat /tmp/cr_stage2_prompt.txt)"

    # 验证 manifest 是否被创建，缺失时自动补全
    MANIFEST="${WORKSPACE}/experiment/experiment_manifest.json"
    if [[ ! -f "$MANIFEST" ]]; then
        echo -e "${YELLOW}[system] Claude 未创建 experiment_manifest.json，自动补全为 4 GPU${NC}"
        mkdir -p "$(dirname "$MANIFEST")"
        echo '{"gpu_count": 4}' > "$MANIFEST"
    else
        MANIFEST_INFO=$(python -c "
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

    python -c "
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.EXPERIMENT_DESIGN)
sm.start_stage(state, Stage.ENVIRONMENT_PREPARATION)
"

    # ===== Stage 4/6: 环境准备 =====
    echo ""
    echo -e "${CYAN}━━━ Stage 4/6: 环境准备（预下载依赖）━━━${NC}"
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

    python -c "
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.ENVIRONMENT_PREPARATION)
sm.start_stage(state, Stage.EXPERIMENT_EXECUTION)
"

    # ===== Stage 5/6: 实验执行（本地优先，SCO 后备）=====
    echo ""
    echo -e "${CYAN}━━━ Stage 5/6: 实验执行（本地优先）━━━${NC}"
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

                debug_round="$debug_round" BACKEND="$BACKEND" ERROR_SUMMARY="$ERROR_SUMMARY" \
                  ERROR_LOG="$ERROR_LOG" \
                  _render_prompt "local_debug_extended.md" /tmp/cr_debug_prompt.txt
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
