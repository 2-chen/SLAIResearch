#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────
# SLAIResearch module: experiment_legacy
# Functions: _continue_experiment_legacy
# Extracted from start.sh (lines 952-1491)
# Sourced by start.sh — expects SCRIPT_DIR, WORKSPACE, TOPIC, SLUG, etc.
# ──────────────────────────────────────────────────────────

# ---- _continue_experiment_legacy: 旧的硬编码实验执行流程 (fallback) ----
_continue_experiment_legacy() {
    echo -e "${CYAN}━━━ 继续实验执行 (legacy hardcoded) ━━━${NC}"
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
sm = StateManager('${WORKSPACE}')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.EXPERIMENT_EXECUTION, {'backend': '${BACKEND}', 'status': 'completed'})
"
            _do_paper_writing
            _do_submit_review
            return
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
            PROTECTED_FILE="${SCRIPT_DIR}/.slairesearch_protected"
            if [[ -f "$PROTECTED_FILE" ]]; then
                while IFS= read -r line; do
                    [[ -z "$line" || "$line" == \#* ]] && continue
                    local _pf="${SCRIPT_DIR}/${line}"
                    [[ -f "$_pf" ]] && PROTECTED_FILES+=("$_pf")
                done < "$PROTECTED_FILE"
            fi
            # 兜底保护核心文件（即使 .slairesearch_protected 不存在）
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
                    > /tmp/slai_sco_debug_prompt.txt 2>/dev/null || {
                    # 回退：如果增强版 prompt 构建失败，用简化版
                    sco_round="$sco_round" ERROR_KEY_LINES="$ERROR_KEY_LINES" \
                      SCO_ERROR_TAIL="$SCO_ERROR_TAIL" EXP_SCRIPT="$EXP_SCRIPT" \
                      PROTECTED_LIST="$PROTECTED_LIST" ROUND_HISTORY="$ROUND_HISTORY" \
                      _render_prompt "sco_debug_fallback.md" /tmp/slai_sco_debug_prompt.txt
                }
                _claude_task "$(cat /tmp/slai_sco_debug_prompt.txt)" "/tmp/slai_claude_output.txt" "${SCRIPT_DIR}/prompts/sco_debugger_system.md" 2>&1

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
    local_timeout=${SLAIRESEARCH_LOCAL_TIMEOUT:-7200},
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
                    --files-modified "$(find "${WORKSPACE}/experiment" -name '*.py' -o -name '*.sh' -newer /tmp/slai_sco_debug_prompt.txt 2>/dev/null | tr '\n' ',' | head -200)" \
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
    local_timeout=${SLAIRESEARCH_LOCAL_TIMEOUT:-7200},
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
    local_timeout=${SLAIRESEARCH_LOCAL_TIMEOUT:-7200},
    max_local_retries=${SLAIRESEARCH_LOCAL_MAX_RETRIES:-20},
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
                        > /tmp/slai_debug_prompt.txt 2>/dev/null || {
                        # 回退：简化版 prompt
                        debug_round="$debug_round" BACKEND="$BACKEND" ERROR_LOG="$ERROR_LOG" \
                          EXP_SCRIPT="$EXP_SCRIPT" \
                          _render_prompt "local_debug_fallback.md" /tmp/slai_debug_prompt.txt
                    }
                    _claude_task "$(cat /tmp/slai_debug_prompt.txt)" 2>&1

                    # ── 修复后重试 (自动判断本地/SCO，不强制本地) ──
                    echo ""
                    echo -e "${YELLOW}修复完成，重新执行实验...${NC}"
                    EXEC_OUTPUT=$(python -c "
from sco_runner import run_experiment
from pathlib import Path
result = run_experiment(
    Path('${EXP_SCRIPT}'),
    job_name='${JOB_NAME}-fix${debug_round}',
    local_timeout=${SLAIRESEARCH_LOCAL_TIMEOUT:-7200},
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
sm = StateManager('${WORKSPACE}')
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

