#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────
# SLAIResearch module: review
# Functions: _run_internal_review, _check_addressed_items, _internal_review_gate
# Extracted from start.sh (lines 1796-2538)
# Sourced by start.sh — expects SCRIPT_DIR, WORKSPACE, TOPIC, SLUG, etc.
# ──────────────────────────────────────────────────────────

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

    # 提取上一轮审稿意见到记忆（如尚未提取）
    if [[ -d "${WORKSPACE}/review" ]]; then
        for prev_review in "${WORKSPACE}/review"/iter*.md "${WORKSPACE}/review"/round_*/internal/*.md; do
            [[ -f "$prev_review" ]] && python review_memory.py extract "$prev_review" -w "${WORKSPACE}" --round "${ITERATION:-0}" 2>/dev/null || true
        done
    fi

    # 加载审稿记忆并注入到 internal_review.py
    local MEMORY_FLAG=""
    if [[ -f "${WORKSPACE}/review/review_memory.md" ]]; then
        MEMORY_FLAG="--memory ${WORKSPACE}/review/review_memory.md"
        echo -e "${CYAN}  已加载审稿记忆${NC}"
    fi

    # 运行 internal_review.py，捕获输出
    REVIEW_OUTPUT=$(python internal_review.py "${PDF_FILE}" -o "${INTERNAL_REVIEW_DIR}/" ${MEMORY_FLAG} 2>&1) || true
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

    # 提取本轮审稿意见到记忆
    local LATEST_REVIEW_FILE=$(ls -t "${INTERNAL_REVIEW_DIR}"/iter*.md 2>/dev/null | head -1)
    if [[ -f "$LATEST_REVIEW_FILE" ]]; then
        python review_memory.py extract "$LATEST_REVIEW_FILE" -w "${WORKSPACE}" --round "${ITERATION:-0}" 2>/dev/null || true
        echo "  审稿意见已提取到 review_memory.md"
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

    # 找到 issue_tracker（优先 round 目录，fallback 到 review/ 平级）
    local ROUND_NUM=$(echo "$LATEST_EXTERNAL_REVIEW" | grep -oP 'round_\K\d+' | head -1)
    local ISSUE_TRACKER="${WORKSPACE}/review/round_${ROUND_NUM:-000}/issue_tracker.md"
    if [[ ! -f "$ISSUE_TRACKER" ]]; then
        ISSUE_TRACKER="${WORKSPACE}/review/issue_tracker.md"
    fi

    echo "  外部审稿: ${LATEST_EXTERNAL_REVIEW}"
    echo "  内部审稿: ${LATEST_INTERNAL:-无}"
    echo "  Issue Tracker: ${ISSUE_TRACKER}"
    echo "  论文: ${WORKSPACE}/paper/paper.tex"
    echo ""

    # ── 磁盘验证：检查 [实验] 类 issue 的实际结果文件是否存在 ──
    # 这提供了权威的磁盘级证据，防止 Claude 把文字描述误判为"已解决"
    local DISK_VERIFICATION=""
    DISK_VERIFICATION=$(python3 -c "
import json, os, re
from pathlib import Path

ws = Path('${WORKSPACE}')
lines = ['## 磁盘验证结果（自动化脚本检查，权威来源）', '']

# 1. Check main experiment results
main_results = ws / 'experiment' / 'results' / 'results.json'
if main_results.exists():
    try:
        data = json.loads(main_results.read_text())
        configs = data.get('results', {})
        n_configs = len(configs)
        lines.append(f'✅ 主实验结果: {main_results} ({n_configs} 组配置)')
        for k in list(configs.keys())[:5]:
            lines.append(f'   - {k}')
    except Exception as e:
        lines.append(f'❌ 主实验结果文件存在但无法解析: {e}')
else:
    lines.append(f'❌ 主实验结果不存在: {main_results}')

# 2. Check gate experiments
gate_dir = ws / 'experiment' / 'gate_experiments'
if gate_dir.exists():
    for exp_dir in sorted(gate_dir.glob('exp_*')):
        exp_name = exp_dir.name
        results_file = exp_dir / 'results' / 'results.json'
        log_files = list(exp_dir.glob('*.log')) + list((exp_dir / 'logs').glob('*.log') if (exp_dir / 'logs').exists() else [])
        if results_file.exists():
            try:
                data = json.loads(results_file.read_text())
                lines.append(f'✅ {exp_name}: results.json 存在')
            except:
                lines.append(f'⚠️ {exp_name}: results.json 存在但无法解析')
        else:
            # Check logs for errors
            error_msgs = []
            for lf in log_files[:3]:
                try:
                    content = lf.read_text()[-2000:]
                    if 'Error' in content or 'Traceback' in content or 'FAILED' in content:
                        # Extract the last error
                        err_match = re.search(r'(RuntimeError|AttributeError|ImportError|TypeError|ValueError|FileNotFoundError)[:\s].*', content)
                        if err_match:
                            error_msgs.append(err_match.group(0)[:150])
                except:
                    pass
            if error_msgs:
                lines.append(f'❌ {exp_name}: 无 results.json — 日志错误: {\" | \".join(error_msgs[:2])}')
            else:
                lines.append(f'❌ {exp_name}: 无 results.json — 实验未产生结果')

# 3. Check revision experiments
for rev_dir in sorted(ws.glob('experiment/revision_iter_*/exp_*')):
    exp_name = f'{rev_dir.parent.name}/{rev_dir.name}'
    results_file = rev_dir / 'results' / 'results.json'
    if results_file.exists():
        lines.append(f'✅ {exp_name}: results.json 存在')
    else:
        lines.append(f'❌ {exp_name}: 无 results.json')

# 4. Check figures directory
fig_dir = ws / 'paper' / 'figures'
if fig_dir.exists():
    pdfs = list(fig_dir.glob('*.pdf')) + list(fig_dir.glob('*.png'))
    if pdfs:
        lines.append(f'✅ figures/: {len(pdfs)} 个图表文件')
    else:
        lines.append(f'⚠️ figures/: 目录存在但无 PDF/PNG 文件')
else:
    lines.append(f'⚠️ figures/: 目录不存在')

# 5. Summary
total_failures = sum(1 for l in lines if l.startswith('❌'))
if total_failures > 0:
    lines.append('')
    lines.append(f'### ⚠️ {total_failures} 项磁盘检查失败 — [实验] 类 issue 的解决率受此约束')
else:
    lines.append('')
    lines.append('### ✅ 所有磁盘检查通过')

print('\n'.join(lines))
" 2>/dev/null || echo "磁盘验证失败（脚本错误）")

    echo "  磁盘验证:"
    echo "$DISK_VERIFICATION" | head -20
    echo ""

    # 构建 Claude 检查 prompt（注入磁盘验证结果）
    ISSUE_TRACKER="$ISSUE_TRACKER" DISK_VERIFICATION="$DISK_VERIFICATION" \
      LATEST_EXTERNAL_REVIEW="$LATEST_EXTERNAL_REVIEW" LATEST_INTERNAL="${LATEST_INTERNAL:-无}" \
      _render_prompt "check_addressed.md" /tmp/slai_check_addressed.txt

    CHECK_RESULT=$(cat /tmp/slai_check_addressed.txt | claude -p --model "${CLAUDE_MODEL:-deepseek-v4-pro}" --output-format text 2>&1) || true
    echo "${CHECK_RESULT}"

    # 提取解决率
    ADDRESSED_PCT=$(echo "${CHECK_RESULT}" | grep -oP '解决率:\s*\K[\d.]+' | head -1)
    ADDRESSED_PCT="${ADDRESSED_PCT:-0}"

    # 如果磁盘验证显示主要实验结果缺失，**强制覆盖**解决率上限
    # 防止 Claude 把文字修改误判为实验完成
    local DISK_FAILURES=$(echo "$DISK_VERIFICATION" | grep -c '^❌' 2>/dev/null || echo "0")
    if [[ "$DISK_FAILURES" -gt 2 && "$ADDRESSED_PCT" -gt 50 ]]; then
        echo -e "  ${YELLOW}⚠ 磁盘验证有 ${DISK_FAILURES} 项失败，但 Claude 返回解决率 ${ADDRESSED_PCT}%${NC}"
        echo -e "  ${YELLOW}  强制限制解决率 ≤ 50%（磁盘证据优先）${NC}"
        ADDRESSED_PCT=50
    fi

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
    echo "  阈值: 内部评分 ≥ 6.0/10, 外部意见解决率 ≥ 90%"
    echo "  最大迭代: ${max_retries}"
    echo ""

    while [[ $gate_iter -lt $max_retries ]]; do
        echo -e "${CYAN}━━━ 门控迭代 $((gate_iter + 1))/${max_retries} ━━━${NC}"

        # Step 0: 预检 — 是否有实验在跑？审稿意见解决够了吗？
        local PREFLIGHT=$(python3 -c "
import os, subprocess, re, json

# 检查 SCO 和本地 GPU
running = 0
try:
    r = subprocess.run(['sco','acp','jobs','list','--workspace-name','share-space'],
                       capture_output=True, text=True, timeout=5)
    running += r.stdout.count('RUNNING') + r.stdout.count('PENDING')
except: pass
try:
    r = subprocess.run(['nvidia-smi','--query-compute-apps=pid,process_name','--format=csv,noheader'],
                       capture_output=True, text=True, timeout=5)
    running += sum(1 for l in r.stdout.split(chr(10)) if l.strip() and 'python' in l.lower())
except: pass

# 快速估算解决率
ws = '${WORKSPACE}'
review_files = []
rd = os.path.join(ws, 'review')
if os.path.isdir(rd):
    for f in sorted(os.listdir(rd)):
        if f.endswith('.md'):
            review_files.append(os.path.join(rd, f))

total, resolved = 0, 0
for rf in review_files[-3:]:  # 最近3个审稿文件
    try:
        c = open(rf).read()
        total += len(re.findall(r'(?i)(?:问题|issue|PROBLEM|TODO|需要|缺少|补[充全]|未完|待[做补]|missing|lacking)', c))
        resolved += len(re.findall(r'(?i)(?:✅|完成|解决|已修|DONE|RESOLVED|FIXED|添加了|补充了)', c))
    except: pass

pct = (resolved / total * 100) if total > 0 else 100
if running > 0:
    print(f'BLOCKED:{running}个实验在运行')
elif pct < 70:
    print(f'LOW:{pct:.0f}%')
else:
    print(f'OK:{pct:.0f}%')
" 2>/dev/null)

        if [[ "$PREFLIGHT" == BLOCKED* ]]; then
            echo -e "${YELLOW}  ⚠ ${PREFLIGHT}，跳过内部审稿，继续修订${NC}"
            return
        elif [[ "$PREFLIGHT" == LOW* ]]; then
            echo -e "${YELLOW}  ⚠ 内部审稿意见解决率${PREFLIGHT#LOW:} (需≥70%才重新审稿)，继续修订${NC}"
            return
        fi

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
        if python -c "exit(0 if float('${ADDRESSED_PCT:-0}') >= 90.0 else 1)" 2>/dev/null; then
            addressed_ok=1
        fi

        echo ""
        echo -e "  内部评分: ${INTERNAL_SCORE}/10 [$(if [[ $score_ok -eq 1 ]]; then echo -e "${GREEN}✓${NC}"; else echo -e "${RED}✗ 需≥6.0${NC}"; fi)]"
        echo -e "  解决率:   ${ADDRESSED_PCT}% [$(if [[ $addressed_ok -eq 1 ]]; then echo -e "${GREEN}✓${NC}"; else echo -e "${RED}✗ 需≥90%${NC}"; fi)]"

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
              _render_prompt "gate_experiment_check.md" /tmp/slai_gate_exp_check.txt
            _claude_task "$(cat /tmp/slai_gate_exp_check.txt)" "/tmp/slai_gate_exp_check_output.txt" || true

            local NEEDS_EXP="false"
            if [[ -f /tmp/slai_gate_exp_check_output.txt ]]; then
                NEEDS_EXP=$(python3 -c "
import re, json
text = open('/tmp/slai_gate_exp_check_output.txt').read()
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
                  _render_prompt "revision_phase_b.md" /tmp/slai_gate_b1.txt
                _claude_task "$(cat /tmp/slai_gate_b1.txt)" "/tmp/slai_gate_b1_output.txt" || true

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
                    local result_file="/tmp/slai_gate_exp_${gate_iter}_${gate_idx}.txt"
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
                    local failed_exps=()
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
                                failed_exps+=("$rf")
                            fi
                        fi
                    done
                    echo -e "  门控结果: ${GREEN}${gate_success} 成功${NC}, ${RED}${gate_failed} 失败${NC}"

                    # ── 自动修复：失败的门控实验尝试一次代码修复 + 重新提交 ──
                    if [[ $gate_failed -gt 0 ]]; then
                        echo ""
                        echo -e "${YELLOW}  [门控修复] ${gate_failed} 个实验失败，尝试代码修复...${NC}"

                        for rf in "${failed_exps[@]}"; do
                            # 找到对应的实验子目录（从 result file 名推断）
                            local exp_idx=$(echo "$rf" | grep -oP 'exp_\K\d+(?=\.txt$)' || echo "1")
                            local failed_exp_dir="${GATE_EXP_DIR}/exp_${exp_idx}"
                            if [[ ! -d "$failed_exp_dir" ]]; then
                                # fallback: 找第一个有 run_experiment.sh 且没有 results 的目录
                                for d in "${GATE_EXP_DIR}"/exp_*/; do
                                    if [[ -f "${d}/run_experiment.sh" ]] && [[ ! -f "${d}/results/results.json" ]]; then
                                        failed_exp_dir="$d"
                                        break
                                    fi
                                done
                            fi

                            if [[ -d "$failed_exp_dir" ]]; then
                                local err_text
                                err_text=$(grep -oP 'ERROR=\K.*' "$rf" 2>/dev/null | head -200 || echo "未知错误")
                                echo -e "  ${CYAN}修复实验: $(basename "$failed_exp_dir")${NC}"
                                echo "  错误: ${err_text:0:200}"

                                # 构建修复 prompt
                                cat > /tmp/slai_gate_fix_prompt.txt <<GATEFIX
你是实验调试专家。门控实验「$(basename "$failed_exp_dir")」执行失败。

错误信息: ${err_text}

实验目录: ${failed_exp_dir}
实验脚本: ${failed_exp_dir}/run_experiment.sh

请读取实验脚本和 .py 文件，诊断并修复代码错误。常见问题:
- PyTorch: requires_grad 未设置、tensor 未正确标记
- Transformers: 模型属性缺失 (main_input_name 等)
- 数据: 输入格式不匹配

修复后输出 "FIX_READY"。
GATEFIX

                                _claude_task "$(cat /tmp/slai_gate_fix_prompt.txt)" "/tmp/slai_gate_fix_output.txt" || true

                                if grep -q "FIX_READY" /tmp/slai_gate_fix_output.txt 2>/dev/null; then
                                    echo -e "  ${GREEN}代码已修复，重新提交...${NC}"
                                    # 重新提交（增加 debug rounds）
                                    python3 -c "
import sys; sys.path.insert(0, '${SCRIPT_DIR}')
from sco_runner import run_with_debug_loop
from pathlib import Path
result = run_with_debug_loop(Path('${failed_exp_dir}/run_experiment.sh'), 'cr-gate-fix-${SLUG:0:16}', force_sco=True, project_slug='${SLUG}', max_debug_rounds=5)
print(f'BACKEND={result.backend}')
print(f'SUCCESS={result.success}')
print(f'JOB_ID={result.job_id}')
print(f'LOG_PATH={result.log_path}')
print(f'ERROR={result.error_summary}')
" > "$rf" 2>&1

                                    local recheck_ok
                                    recheck_ok=$(grep -oP 'SUCCESS=\K\S+' "$rf" 2>/dev/null || echo "False")
                                    if [[ "$recheck_ok" == "True" ]]; then
                                        gate_success=$((gate_success + 1))
                                        gate_failed=$((gate_failed - 1))
                                        echo -e "  ${GREEN}✓ 修复后实验成功${NC}"
                                    else
                                        echo -e "  ${YELLOW}修复后仍失败，将在下一门控迭代中重试${NC}"
                                    fi
                                else
                                    echo -e "  ${YELLOW}Claude 无法自动修复，将在下一门控迭代中重试${NC}"
                                fi
                            fi
                            rm -f "$rf" 2>/dev/null || true
                        done

                        # 更新门控结果计数
                        echo -e "  修复后门控结果: ${GREEN}${gate_success} 成功${NC}, ${RED}${gate_failed} 失败${NC}"
                    else
                        # 清理成功的 result files
                        for rf in "${gate_result_files[@]}"; do
                            rm -f "$rf" 2>/dev/null || true
                        done
                    fi
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

            # 标记已检查，但仅在实验成功时持久化
            # 如果实验有失败，清除标记以便下一门控迭代可以重新尝试
            if [[ $gate_failed -eq 0 ]]; then
                touch "$GATE_EXP_CHECK"
                echo -e "  ${GREEN}[门控] 所有实验成功，标记 .experiments_checked${NC}"
            else
                # 清除标记 + 记录失败次数，下一轮用更多 debug rounds 重试
                rm -f "$GATE_EXP_CHECK"
                local fail_count_file="${GATE_EXP_DIR}/.failed_count"
                local prev_fails=0
                [[ -f "$fail_count_file" ]] && prev_fails=$(cat "$fail_count_file" 2>/dev/null || echo 0)
                echo $((prev_fails + 1)) > "$fail_count_file"
                echo -e "  ${YELLOW}[门控] ${gate_failed} 个实验失败，清除 .experiments_checked（失败计数: $((prev_fails + 1))）${NC}"
                echo -e "  ${YELLOW}[门控] 下一门控迭代将重新分析并尝试修复实验代码${NC}"
            fi
        else
            # .experiments_checked 存在 — 但检查是否有失败需要重试
            local fail_count_file="${GATE_EXP_DIR}/.failed_count"
            local prev_fails=0
            [[ -f "$fail_count_file" ]] && prev_fails=$(cat "$fail_count_file" 2>/dev/null || echo 0)

            if [[ $prev_fails -gt 0 ]]; then
                echo -e "  ${YELLOW}[门控] 前一轮有 ${prev_fails} 个实验失败，重新运行（debug_rounds=$((3 + prev_fails * 2))）${NC}"
                # 重新提交所有有 run_experiment.sh 但没有 results.json 的实验
                local retry_count=0
                local retry_ok=0
                for exp_subdir in "${GATE_EXP_DIR}"/exp_*/; do
                    [[ -d "$exp_subdir" ]] || continue
                    [[ -f "${exp_subdir}/run_experiment.sh" ]] || continue
                    # 跳过已经有成功结果的实验
                    if [[ -f "${exp_subdir}/results/results.json" ]]; then
                        echo -e "  ${GREEN}  $(basename "$exp_subdir"): 已有结果，跳过${NC}"
                        continue
                    fi
                    retry_count=$((retry_count + 1))
                    local exp_name="cr-gate-retry-${SLUG:0:12}-$(basename "$exp_subdir")"
                    echo -e "  ${YELLOW}▶ 重试: ${exp_name} (debug_rounds=$((3 + prev_fails * 2)))${NC}"
                    python3 -c "
import sys; sys.path.insert(0, '${SCRIPT_DIR}')
from sco_runner import run_with_debug_loop
from pathlib import Path
result = run_with_debug_loop(Path('${exp_subdir}/run_experiment.sh'), '${exp_name}', force_sco=True, project_slug='${SLUG}', max_debug_rounds=$((3 + prev_fails * 2)))
print(f'BACKEND={result.backend}')
print(f'SUCCESS={result.success}')
print(f'JOB_ID={result.job_id}')
print(f'LOG_PATH={result.log_path}')
print(f'ERROR={result.error_summary}')
" > "/tmp/slai_gate_retry_${gate_iter}_${retry_count}.txt" 2>&1
                    local retry_ok_flag
                    retry_ok_flag=$(grep -oP 'SUCCESS=\K\S+' "/tmp/slai_gate_retry_${gate_iter}_${retry_count}.txt" 2>/dev/null || echo "False")
                    if [[ "$retry_ok_flag" == "True" ]]; then
                        retry_ok=$((retry_ok + 1))
                        echo -e "  ${GREEN}  ✓ 重试成功${NC}"
                    else
                        echo -e "  ${RED}  ✗ 重试仍失败${NC}"
                    fi
                    rm -f "/tmp/slai_gate_retry_${gate_iter}_${retry_count}.txt" 2>/dev/null || true
                done
                if [[ $retry_ok -eq $retry_count ]] && [[ $retry_count -gt 0 ]]; then
                    # 全部成功 → 标记完成
                    touch "$GATE_EXP_CHECK"
                    rm -f "$fail_count_file"
                    echo -e "  ${GREEN}[门控] 所有重试实验成功！标记 .experiments_checked${NC}"
                elif [[ $retry_count -gt 0 ]]; then
                    echo -e "  ${YELLOW}[门控] ${retry_ok}/${retry_count} 重试成功，剩余将在下一迭代重试${NC}"
                else
                    echo -e "  ${CYAN}[门控] 实验代码已存在但缺少 run_experiment.sh — 跳过${NC}"
                fi
            else
                echo -e "  ${CYAN}[门控] 实验已完成且全部成功 (.experiments_checked)，跳过${NC}"
            fi
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
          _render_prompt "gate_revise.md" /tmp/slai_gate_revise.txt
        _claude_task "$(cat /tmp/slai_gate_revise.txt)"

        # 保存中间状态
        python -c "
from state_manager import StateManager
sm = StateManager('${WORKSPACE}')
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

