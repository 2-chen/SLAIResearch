#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────
# SLAIResearch module: revision
# Functions: _rev_ckpt_path, _rev_ckpt_set, _rev_ckpt_get, _rev_ckpt_is_done, _do_revise_and_resubmit
# Sourced by start.sh
# ──────────────────────────────────────────────────────────

# ---- _do_revise_and_resubmit: 三阶段修订 + 重新提交 ----
# Phase A: 分析审稿意见 → 提取实验需求 + 文字修改清单
# Phase B: 实际执行补充实验（通过 sco_runner 提交 SCO / 本地执行）
# Phase C: 用真实实验结果更新论文 + response letter
_do_revise_and_resubmit() {
    local verdict="${1:-unknown}"
    LATEST_REVIEW=$(ls -t "${WORKSPACE}/review/round_"*/external.md 2>/dev/null | head -1)
    # fallback: 如果没有外部审稿，用最新内部审稿汇总
    if [[ -z "$LATEST_REVIEW" ]]; then
        LATEST_REVIEW=$(ls -t "${WORKSPACE}/review/"internal_*/summary.md "${WORKSPACE}/review/"internal_*/*_summary.md 2>/dev/null | head -1)
    fi
    ITERATION=$((ITERATION + 1))  # 立即更新，避免函数内混用新旧值
    NEXT_ITER=${ITERATION}        # 保持兼容，NEXT_ITER == ITERATION
    local REVISION_EXP_DIR="${WORKSPACE}/experiment/revision_iter_${NEXT_ITER}"

    echo ""
    echo -e "${CYAN}━━━ 修订迭代 #${NEXT_ITER} (三阶段修订) ━━━${NC}"
    if [[ -n "$LATEST_REVIEW" ]]; then
        echo "  审稿意见: ${LATEST_REVIEW}"
    else
        echo -e "  ${YELLOW}⚠ 无审稿意见文件，使用自动修订模式${NC}"
    fi
    echo "  当前 verdict: ${verdict}"
    echo ""

    # ── Tavily 补充检索：搜索审稿意见中提到的相关工作和对比方法 ──
    if [[ -n "${TAVILY_API_KEY:-}" ]] && [[ -n "$LATEST_REVIEW" ]] && [[ -f "$LATEST_REVIEW" ]]; then
        echo -e "${CYAN}── Tavily 补充检索审稿相关内容 ──${NC}"
        # 从审稿意见中提取关键论文/方法名用于搜索
        local REV_SEARCH_QUERY
        REV_SEARCH_QUERY=$(python3 -c "
import re, json
text = open('${LATEST_REVIEW}').read()
# 提取审稿人提到的'cite [X]', 'related work:', 'compare with' 等关键词
keywords = []
for pat in [r'(?i)cite\s*\[?(\d+)\]?', r'(?i)compare (?:with|to) (.+?)(?:\.|,)',
            r'(?i)related work[:\s]+(.+?)(?:\.)', r'(?i)missing (?:reference|baseline)[:\s]+(.+?)(?:\.)',
            r'(?i)should (?:cite|reference|compare) (.+?)(?:\.|,)']:
    for m in re.findall(pat, text):
        kw = m.strip()[:100] if isinstance(m, str) else m
        if len(kw) > 5: keywords.append(kw)
# 加上论文主题
keywords.insert(0, '${TOPIC:0:150}')
query = '; '.join(keywords[:5])
print(query[:400])
" 2>/dev/null || echo "${TOPIC:0:200}")
        if [[ -n "$REV_SEARCH_QUERY" ]]; then
            python tavily_search.py "${REV_SEARCH_QUERY}" --max-results 5 --mode related-work \
                --output "${WORKSPACE}/review/tavily_related_work.md" 2>/dev/null && \
                echo -e "  ${GREEN}✓ 补充检索完成 → review/tavily_related_work.md${NC}" || \
                echo -e "  ${YELLOW}  补充检索跳过（非致命）${NC}"
        fi
        echo ""
    fi

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

        # ── 精确跳转到上次中断的位置 ──
        # 全部完成 → 直接到审稿门控
        if [[ "$PHASE_A_DONE" == "true" && "$PHASE_B1_DONE" == "true" && \
              "$PHASE_B2_SUBMITTED" == "true" && "$PHASE_B2_DONE" == "true" && \
              "$PHASE_C_DONE" == "true" ]]; then
            echo -e "  ${GREEN}▶ 直接进入审稿门控 (所有阶段已完成)${NC}"
            echo ""
            _internal_review_gate 5
            _do_submit_review
            python -c "
from state_manager import StateManager, Stage
sm = StateManager('${WORKSPACE}')
state = sm.load('${SLUG}')
state.iteration = ${NEXT_ITER}
sm.save(state)
sm.complete_stage(state, Stage.REVISE)
"
            return 0
        fi
        # A+B1+B2 完成，C 未完成 → 直接跳到 Phase C
        if [[ "$PHASE_A_DONE" == "true" && "$PHASE_B1_DONE" == "true" && \
              "$PHASE_B2_DONE" == "true" ]]; then
            echo -e "  ${GREEN}▶ 直接跳到 Phase C (实验已完成，更新论文)${NC}"
            echo ""
            EXP_COUNT=$(python -c "import json; print(len(json.load(open('${REVISION_EXP_DIR}/revision_plan.json')).get('experiments',[])))" 2>/dev/null || echo "0")
            TOPIC="$TOPIC" NEXT_ITER="$NEXT_ITER" verdict="$verdict" \
              LATEST_REVIEW="$LATEST_REVIEW" REVISION_EXP_DIR="$REVISION_EXP_DIR" \
              _render_prompt "revision_phase_c.md" /tmp/slai_phase_c.txt
            _claude_task "$(cat /tmp/slai_phase_c.txt)" "/tmp/slai_phase_c_output.txt" || true
            _rev_ckpt_set "$NEXT_ITER" "phase_c" "done"
            _internal_review_gate 5
            _do_submit_review
            python -c "
from state_manager import StateManager, Stage
sm = StateManager('${WORKSPACE}')
state = sm.load('${SLUG}')
state.iteration = ${NEXT_ITER}
sm.save(state)
sm.complete_stage(state, Stage.REVISE)
"
            return 0
        fi
        # A+B1 完成，B2 未完成 → 直接跳到 B2 提交
        if [[ "$PHASE_A_DONE" == "true" && "$PHASE_B1_DONE" == "true" && \
              "$PHASE_B2_DONE" != "true" ]]; then
            echo -e "  ${GREEN}▶ 直接跳到 Phase B2 (代码已编写，提交实验)${NC}"
            EXP_COUNT=1  # 确保进入 B2
            PHASE_A_DONE=true; PHASE_B1_DONE=true
            _goto_b2=true
        fi
        echo ""
    fi

    # ========================================================================
    # Phase A: 分析审稿意见，分离「需要实验」和「只改文字」的需求
    # ========================================================================
    if [[ "${_goto_b2:-false}" == "true" ]]; then
        echo -e "  ${GREEN}⏩ 跳过 Phase A (已完成)，直接进入 Phase B2${NC}"
        echo ""
    elif [[ "$PHASE_A_DONE" == "true" ]]; then
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

    # ROUND_DIR for issue_tracker.md placement (alongside external/internal reviews)
    local ROUND_DIR="${WORKSPACE}/review/round_$(printf "%03d" ${NEXT_ITER})"
    mkdir -p "${ROUND_DIR}"

    TOPIC="$TOPIC" LATEST_REVIEW="$LATEST_REVIEW" \
      ROUND_DIR="$ROUND_DIR" NEXT_ITER="$NEXT_ITER" \
      _render_prompt "revision_phase_a.md" /tmp/slai_phase_a.txt

    _claude_task "$(cat /tmp/slai_phase_a.txt)" "/tmp/slai_phase_a_output.txt" || true

    # 提取 JSON 实验计划
    local EXP_COUNT=0
    if [[ -f /tmp/slai_phase_a_output.txt ]]; then
        EXP_COUNT=$(python -c "
import re, json
text = open('/tmp/slai_phase_a_output.txt').read()
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

    # ── 交叉验证：issue_tracker.md 中有 [实验] 标签的未解决项必须反映在 EXP_COUNT 中 ──
    # 防止 Claude Code 在 revision_plan.json 中静默丢弃实验需求
    local ISSUE_TRACKER="${ROUND_DIR}/issue_tracker.md"
    if [[ -f "$ISSUE_TRACKER" ]]; then
        local TRACKER_EXP_COUNT=$(python3 -c "
import re
text = open('${ISSUE_TRACKER}').read()
# Count [实验] items that are NOT marked as resolved
unresolved = len(re.findall(r'### (?:EXP-\d+):\s*\[实验\]', text))
# Also count items with status lines that indicate resolution
resolved = len(re.findall(r'### (?:EXP-\d+):\s*\[实验\].*?\n(?:.*\n)*?.*?(?:✅|已解决|已完成|resolved)', text))
print(max(0, unresolved - resolved))
" 2>/dev/null || echo "0")
        if [[ "$TRACKER_EXP_COUNT" -gt 0 && "$EXP_COUNT" -eq 0 ]]; then
            echo -e "  ${YELLOW}⚠ issue_tracker.md 有 ${TRACKER_EXP_COUNT} 个未解决的 [实验] issue，但 revision_plan.json 有 0 个实验${NC}"
            echo -e "  ${YELLOW}  强制 EXP_COUNT=${TRACKER_EXP_COUNT}（issue_tracker 为权威来源）${NC}"
            EXP_COUNT=$TRACKER_EXP_COUNT
            # 从 issue_tracker 生成最小 revision_plan.json
            python3 -c "
import json, re
text = open('${ISSUE_TRACKER}').read()
exp_items = re.findall(r'### (EXP-\d+):\s*\[实验\]\s*(.*?)(?=\n###|\Z)', text, re.DOTALL)
experiments = []
for eid, desc in exp_items:
    experiments.append({
        'id': eid,
        'description': desc.strip()[:200],
        'source': 'issue_tracker (auto-generated from unresolved EXP items)',
    })
plan = {'experiments': experiments, 'source': 'issue_tracker_cross_reference'}
json.dump(plan, open('${REVISION_EXP_DIR}/revision_plan.json', 'w'), indent=2)
print(f'  Generated revision_plan.json with {len(experiments)} experiments from issue_tracker')
" 2>/dev/null || true
        elif [[ "$TRACKER_EXP_COUNT" -gt 0 ]]; then
            echo -e "  ${GREEN}✓ issue_tracker: ${TRACKER_EXP_COUNT} EXP items, revision_plan: ${EXP_COUNT} experiments — consistent${NC}"
        fi
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
    if [[ "${_goto_b2:-false}" == "true" ]]; then
        echo -e "  ${GREEN}⏩ 跳过 Phase B1，直接进入 B2${NC}"
        echo ""
        EXP_COUNT=1  # ensure B2 runs
        PHASE_B1_DONE=true
        PHASE_B2_SUBMITTED=false  # allow initial submission
    elif [[ "$EXP_COUNT" -gt 0 ]]; then
        echo -e "${CYAN}━━━ Phase B: 执行 ${EXP_COUNT} 个补充实验 ━━━${NC}"
        echo ""

        # B1: Claude 编写实验代码（类似实验设计阶段，但只写补充实验）
        if [[ "$PHASE_B1_DONE" == "true" ]]; then
            echo -e "${CYAN}  B1: 跳过 (检查点已完成)${NC}"
        else
        echo -e "${CYAN}  B1: 编写实验代码...${NC}"
        REVISION_PLAN_JSON=$(cat "${REVISION_EXP_DIR}/revision_plan.json" 2>/dev/null || echo "见 revision_plan.json")
        TOPIC="$TOPIC" REVISION_PLAN_JSON="$REVISION_PLAN_JSON" REVISION_EXP_DIR="$REVISION_EXP_DIR" \
          _render_prompt "revision_phase_b.md" /tmp/slai_phase_b1.txt
        _claude_task "$(cat /tmp/slai_phase_b1.txt)" "/tmp/slai_phase_b1_output.txt" || true
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
        local missing_scripts=0

        for exp_subdir in "${REVISION_EXP_DIR}"/exp_*/; do
            [[ -d "$exp_subdir" ]] || continue
            local exp_script="${exp_subdir}/run_experiment.sh"

            # ★ 如果目录有 .py 文件但没有 run_experiment.sh，自动生成
            if [[ ! -f "$exp_script" ]]; then
                local py_count=$(ls "${exp_subdir}"/*.py 2>/dev/null | wc -l)
                if [[ $py_count -gt 0 ]]; then
                    local exp_base=$(basename "$exp_subdir")
                    local main_py=$(ls "${exp_subdir}"/*.py 2>/dev/null | head -1)
                    echo -e "  ${YELLOW}⚠ ${exp_base}: 缺少 run_experiment.sh，从 ${main_py##*/} 自动生成${NC}"
                    missing_scripts=$((missing_scripts + 1))
                    cat > "$exp_script" << 'RUNTMPL'
#!/bin/bash
set -euo pipefail
export PYTHONUNBUFFERED=1
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="${PYTHONPATH:-}"
source "${SCRIPT_DIR}/workspace/.shared/install_deps.sh"
install_dependencies
echo " [auto-gen] Fixing numpy/soxr ABI compatibility..."
pip install --quiet 'numpy>=1.24,<2' 2>/dev/null || true
trap 'rc=$?; echo " [auto-gen] FATAL ERROR at line $LINENO (exit=$rc)"; exit $rc' ERR
GPU_COUNT=0
command -v nvidia-smi &>/dev/null && GPU_COUNT=$(nvidia-smi -L 2>/dev/null | wc -l || echo 0)
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
echo " [auto-gen] GPUs: $GPU_COUNT | Dir: $PROJECT_DIR"
mkdir -p "${PROJECT_DIR}/results" "${PROJECT_DIR}/logs"
for pyf in "${PROJECT_DIR}"/*.py; do
    echo " [auto-gen] Running: $(basename "$pyf")"
    python3 "$pyf" 2>&1 | tee "${PROJECT_DIR}/logs/run_output.log" || {
        echo " [auto-gen] WARNING: $(basename "$pyf") failed, continuing..."
    }
done
echo " [auto-gen] Done."
RUNTMPL
                    chmod +x "$exp_script"
                fi
            fi

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
                    elif [[ "$_s" == "RUNNING" || "$_s" == "PENDING" || "$_s" == "QUEUED" || "$_s" == "STARTING" ]]; then
                        echo -e "  ${YELLOW}⏳ ${_w_name}: 仍在运行 (${_s})，轮询超时但任务未失败${NC}"
                        echo -e "  ${YELLOW}  请稍后手动检查: sco acp jobs describe --workspace-name share-space ${_w_jobid}${NC}"
                        # 不触发修复，保留等待标记以便下次续跑时继续等待
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
" > "/tmp/slai_rev_repair_${NEXT_ITER}_${_w_name}.txt" 2>&1
                        _r_ok=$(grep -oP 'SUCCESS=\K\S+' "/tmp/slai_rev_repair_${NEXT_ITER}_${_w_name}.txt" 2>/dev/null || echo "False")
                        _r_jid=$(grep -oP 'JOB_ID=\K\S+' "/tmp/slai_rev_repair_${NEXT_ITER}_${_w_name}.txt" 2>/dev/null || echo "?")
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
                local _repair_tmp="/tmp/slai_rev_repair_${NEXT_ITER}_${_r_name}.txt"
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
                for _repair_tmp in /tmp/slai_rev_repair_${NEXT_ITER}_*.txt; do
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
                PHASE_B2_DONE=true
                _rev_ckpt_set "$NEXT_ITER" "phase_b2" "done"
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
            # ── 提交前先检查每个实验的 SCO 状态（断点续跑保护）──
            for i in $(seq 0 $((${#exp_dirs[@]} - 1))); do
                local _exp_subdir="${exp_dirs[$i]}"
                local _exp_name="${exp_names[$i]}"
                local _jid_file="${_exp_subdir}/logs/sco_job_id.txt"
                if [[ -f "$_jid_file" ]]; then
                    local _jid=$(cat "$_jid_file" | tr -d '[:space:]')
                    if [[ -n "$_jid" ]]; then
                        local _sco_state=$(sco acp jobs describe --workspace-name share-space -o json "$_jid" 2>/dev/null | python3 -c "import json,sys; d=sys.stdin.read().strip(); print(json.loads(d).get('status', json.loads(d).get('state','UNKNOWN')) if d else 'UNKNOWN')" 2>/dev/null || echo "UNKNOWN")
                        case "$_sco_state" in
                            SUCCEEDED)
                                echo -e "  ${GREEN}✓ ${_exp_name}: SCO 任务成功 (${_jid})，下载日志，跳过重新提交${NC}"
                                sco acp jobs stream-logs --workspace-name share-space "$_jid" > "${_exp_subdir}/experiment_log.txt" 2>/dev/null || true
                                # 标记跳过，不重新提交
                                mkdir -p "${_exp_subdir}/logs"
                                echo "SKIPPED_ALREADY_DONE" > "${_exp_subdir}/logs/.skip_submit"
                                ;;
                            RUNNING|PENDING|QUEUED|STARTING)
                                echo -e "  ${CYAN}⏳ ${_exp_name}: SCO 任务运行中 (${_jid}, ${_sco_state})，等待完成，不重新提交${NC}"
                                mkdir -p "${_exp_subdir}/logs"
                                echo "WAIT_FOR_JOB ${_jid}" > "${_exp_subdir}/logs/.skip_submit"
                                ;;
                            *)
                                echo -e "  ${YELLOW}  ${_exp_name}: SCO 任务 ${_sco_state} (${_jid})，将重新提交${NC}"
                                ;;
                        esac
                    fi
                fi
            done

            # 提交实验（如果尚未提交）
            if [[ "$PHASE_B2_SUBMITTED" != "true" ]]; then
                for i in $(seq 0 $((${#exp_dirs[@]} - 1))); do
                    local exp_subdir="${exp_dirs[$i]}"
                    local exp_script="${exp_subdir}/run_experiment.sh"
                    local exp_name="${exp_names[$i]}"
                    local result_file="/tmp/slai_rev_exp_${NEXT_ITER}_${i}.txt"
                    result_files+=("$result_file")

                    # 跳过已成功或运行中的实验
                    local _skip_file="${exp_subdir}/logs/.skip_submit"
                    if [[ -f "$_skip_file" ]]; then
                        local _skip_reason=$(cat "$_skip_file")
                        if [[ "$_skip_reason" == SKIPPED_ALREADY_DONE* ]]; then
                            echo -e "  ${GREEN}⊘ ${exp_name}: 已完成，跳过${NC}"
                            echo "BACKEND=sco" > "$result_file"
                            echo "SUCCESS=True" >> "$result_file"
                            echo "JOB_ID=$(echo $_skip_reason | awk '{print $NF}')" >> "$result_file"
                            continue
                        elif [[ "$_skip_reason" == WAIT_FOR_JOB* ]]; then
                            local _w_jid=$(echo "$_skip_reason" | awk '{print $2}')
                            echo -e "  ${CYAN}⏳ ${exp_name}: 等待 SCO 任务 ${_w_jid}...${NC}"
                            (
                                for _wi in $(seq 1 120); do
                                    sleep 30
                                    _ws=$(sco acp jobs describe --workspace-name share-space -o json "$_w_jid" 2>/dev/null | python3 -c "import json,sys; d=sys.stdin.read().strip(); print(json.loads(d).get('status',json.loads(d).get('state','UNKNOWN')) if d else 'UNKNOWN')" 2>/dev/null || echo "UNKNOWN")
                                    case "$_ws" in SUCCEEDED|FAILED|STOPPED|SUSPENDED|CANCELLED) break ;; esac
                                done
                                if [[ "$_ws" == "SUCCEEDED" ]]; then
                                    sco acp jobs stream-logs --workspace-name share-space "$_w_jid" > "${exp_subdir}/experiment_log.txt" 2>/dev/null || true
                                    echo "BACKEND=sco" > "$result_file"
                                    echo "SUCCESS=True" >> "$result_file"
                                    echo "JOB_ID=$_w_jid" >> "$result_file"
                                fi
                            ) &
                            pids+=($!)
                            continue
                        fi
                    fi

                    echo -e "  ${YELLOW}▶ 提交实验 $((i+1))/${#exp_dirs[@]}: ${exp_name} → 日志: ${result_file}${NC}"
                    (
                        local _rev_exp_script="$exp_script"
                        local _rev_exp_name="$exp_name"
                        local _rev_result_file="$result_file"
                        local _rev_max_rounds=20
                        (
                            for ((_rev_round=1; _rev_round<=_rev_max_rounds; _rev_round++)); do
                                echo "━━━ 补充实验修复轮次 ${_rev_round}/${_rev_max_rounds}: ${_rev_exp_name} ━━━"
                                local _rev_output
                                _rev_output=$(python -c "
from sco_runner import run_experiment
from pathlib import Path
result = run_experiment(
    Path('${_rev_exp_script}'),
    job_name='${_rev_exp_name}-fix${_rev_round}',
    force_sco=True,
)
print(f'BACKEND={result.backend}')
print(f'SUCCESS={result.success}')
print(f'JOB_ID={result.job_id}')
print(f'LOG_PATH={result.log_path}')
print(f'ERROR={result.error_summary}')
" 2>&1)
                                echo "$_rev_output"
                                if echo "$_rev_output" | grep -q 'SUCCESS=True'; then
                                    echo "✓ ${_rev_exp_name}: 成功"
                                    break
                                fi
                                if [[ $_rev_round -lt _rev_max_rounds ]]; then
                                    echo "── Claude Code 诊断修复中... ──"
                                    python "${SCRIPT_DIR}/experiment_runner.py" build-prompt \
                                        "${WORKSPACE}/experiment/revision_iter_${NEXT_ITER}" \
                                        "${_rev_exp_script%.sh}_log.txt" \
                                        --script "${_rev_exp_script}" \
                                        --backend sco \
                                        --round "${_rev_round}" \
                                        --max-rounds "${_rev_max_rounds}" \
                                        --protected "sco_runner.py
config.py" \
                                        > "/tmp/slai_rev_debug_${NEXT_ITER}_${i}.txt" 2>/dev/null || \
                                        echo "补充实验 ${_rev_exp_name} 第 ${_rev_round} 轮失败，请检查" > "/tmp/slai_rev_debug_${NEXT_ITER}_${i}.txt"
                                    cat "/tmp/slai_rev_debug_${NEXT_ITER}_${i}.txt" | claude -p \
                                        --model "${CLAUDE_MODEL:-deepseek-v4-pro}" \
                                        --output-format text \
                                        --append-system-prompt "$(cat ${SCRIPT_DIR}/prompts/sco_debugger_system.md)" 2>&1
                                    echo "── 修复完成，重新提交... ──"
                                fi
                            done
                        ) 2>&1 | tee "$_rev_result_file"
                    ) &
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
    # Phase B→C 闸门: 检查补充实验是否产生了有效结果
    # 如果 issue_tracker 有 [实验] 类 issue 但 experiments 全部失败/未提交 → 阻止 Phase C
    # ========================================================================
    local BLOCK_PHASE_C=false
    local TRACKER_EXP_ITEMS=0
    local COMPLETED_EXPS=0
    local FAILED_EXPS=0

    # 统计 issue_tracker 中的 [实验] 类未解决项
    local ISSUE_TRACKER_FILE="${WORKSPACE}/review/issue_tracker.md"
    [[ ! -f "$ISSUE_TRACKER_FILE" ]] && ISSUE_TRACKER_FILE=$(ls -t "${WORKSPACE}/review/round_"*/issue_tracker.md 2>/dev/null | head -1)
    if [[ -f "$ISSUE_TRACKER_FILE" ]]; then
        TRACKER_EXP_ITEMS=$(python3 -c "
import re
text = open('${ISSUE_TRACKER_FILE}').read()
# Count [实验] items
exp_items = re.findall(r'### (?:EXP-\d+):\s*\[实验\]', text)
# Subtract resolved ones
resolved = re.findall(r'### (?:EXP-\d+):\s*\[实验\].*?\*\*状态\*\*:\s*(?:已解决|✅|resolved)', text, re.DOTALL)
print(max(0, len(exp_items) - len(resolved)))
" 2>/dev/null || echo "0")
    fi

    # 统计实际完成的实验（results.json 存在且非空）
    if [[ -d "$REVISION_EXP_DIR" ]]; then
        local exp_result
        exp_result=$(python3 -c "
import json, os
from pathlib import Path
rev_dir = Path('${REVISION_EXP_DIR}')
completed = 0
failed = 0
for d in sorted(rev_dir.glob('exp_*')):
    if d.is_dir():
        results_file = d / 'results' / 'results.json'
        if results_file.exists():
            try:
                data = json.loads(results_file.read_text())
                # Check it has actual data
                if data and len(str(data)) > 20:
                    completed += 1
                else:
                    failed += 1
            except:
                failed += 1
        else:
            # Check for log errors
            log_files = list(d.glob('*.log')) + list((d / 'logs').glob('*.log') if (d / 'logs').exists() else [])
            has_error = False
            for lf in log_files:
                try:
                    content = lf.read_text()[-2000:]
                    if 'Error' in content or 'Traceback' in content:
                        has_error = True
                        break
                except: pass
            failed += 1
print(f'{completed} {failed}')
" 2>/dev/null || echo "0 0")
        COMPLETED_EXPS=$(echo "$exp_result" | awk '{print $1}')
        FAILED_EXPS=$(echo "$exp_result" | awk '{print $2}')
    fi

    echo ""
    echo -e "${CYAN}━━━ Phase B→C 闸门: 实验完成度检查 ━━━${NC}"
    echo -e "  issue_tracker [实验] 未解决项: ${TRACKER_EXP_ITEMS}"
    echo -e "  实际完成实验: ${COMPLETED_EXPS}"
    echo -e "  失败/未完成实验: ${FAILED_EXPS}"

    if [[ "$TRACKER_EXP_ITEMS" -gt 0 && "$COMPLETED_EXPS" -eq 0 ]]; then
        echo -e "  ${RED}✗ 闸门阻止: issue_tracker 有 ${TRACKER_EXP_ITEMS} 个未解决的 [实验] issue，但 0 个实验成功完成${NC}"
        echo -e "  ${RED}  不能进入 Phase C（没有实验数据可以写到论文里）${NC}"
        echo -e "  ${YELLOW}  请检查:${NC}"
        echo -e "  ${YELLOW}    1. exp_*/run_experiment.sh 是否存在${NC}"
        echo -e "  ${YELLOW}    2. SCO 任务是否成功提交并完成${NC}"
        echo -e "  ${YELLOW}    3. 实验代码是否有 bug 需要修复${NC}"
        echo -e "  ${YELLOW}  修复后重新运行 start.sh 续跑，B2 会重新提交失败的实验${NC}"
        BLOCK_PHASE_C=true
    elif [[ "$TRACKER_EXP_ITEMS" -gt 0 && "$COMPLETED_EXPS" -lt "$TRACKER_EXP_ITEMS" ]]; then
        echo -e "  ${YELLOW}⚠ 部分完成: ${COMPLETED_EXPS}/${TRACKER_EXP_ITEMS} 个实验成功${NC}"
        echo -e "  ${YELLOW}  将进入 Phase C（用已有的实验数据更新论文）${NC}"
        echo -e "  ${YELLOW}  未完成的实验将在论文中如实标注${NC}"
    elif [[ "$COMPLETED_EXPS" -gt 0 ]]; then
        echo -e "  ${GREEN}✓ 实验完成度充足: ${COMPLETED_EXPS} 个实验有有效结果${NC}"
    fi
    echo ""

    # ========================================================================
    # Phase C: 用真实实验结果更新论文
    # ========================================================================
    if [[ "$BLOCK_PHASE_C" == "true" ]]; then
        echo -e "${RED}━━━ Phase C: 已阻止（实验未完成）━━━${NC}"
        echo ""
        # 不清除检查点，下次续跑从 B2 重新开始
        _rev_ckpt_set "$NEXT_ITER" "phase_b2_submitted" "false"
        _rev_ckpt_set "$NEXT_ITER" "phase_b2" "false"
        return 0  # 不退出，保留状态供下次续跑
    elif [[ "$PHASE_C_DONE" == "true" ]]; then
        echo -e "${CYAN}━━━ Phase C: 跳过 (检查点已完成) ━━━${NC}"
        echo ""
    else
    echo -e "${CYAN}━━━ Phase C: 更新论文 ━━━${NC}"
    echo ""

    TOPIC="$TOPIC" NEXT_ITER="$NEXT_ITER" verdict="$verdict" \
      LATEST_REVIEW="$LATEST_REVIEW" REVISION_EXP_DIR="$REVISION_EXP_DIR" \
      _render_prompt "revision_phase_c.md" /tmp/slai_phase_c.txt
    _claude_task "$(cat /tmp/slai_phase_c.txt)" "/tmp/slai_phase_c_output.txt" || true
    # 保存检查点: Phase C 完成
    _rev_ckpt_set "$NEXT_ITER" "phase_c" "done"
    fi  # end of Phase C skip block

    # 更新迭代号（gate 需要引用正确的 revision_iter_* 目录）

    # 内部审稿门控 — 只有通过内部审稿才能提交外部
    _internal_review_gate 5

    # 提交外部审稿
    _do_submit_review

    # 保存状态（在 gate + submit 都成功后，防止崩溃后状态不一致）
    python -c "
from state_manager import StateManager, Stage
sm = StateManager('${WORKSPACE}')
state = sm.load('${SLUG}')
state.iteration = ${NEXT_ITER}
sm.save(state)
sm.complete_stage(state, Stage.REVISE)
"
}


