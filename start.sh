#!/usr/bin/env bash
# =============================================================================
# ChenResearch — 代码驱动的会话管理器
# start.sh 是真正的控制器，Claude Code 是执行工具。
# 每次启动：检查 state → 确定当前阶段 → 生成精准 prompt → claude -p 执行
# 审稿后自动退出，下次重开继续迭代 — 保持每轮上下文干净。
# =============================================================================
set -uo pipefail  # 不用 set -e，关键节点显式错误处理

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
# 首次运行配置
# ---------------------------------------------------------------------------
if [[ ! -f ".chenresearch_configured" ]]; then
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
export CLAUDE_API_KEY="${CLAUDE_API_KEY}"
export SEMANTIC_SCHOLAR_API_KEY="s2k-TxOJNhO0O615j3huoEbRfhfIUfnzoXLE2V9ZfEaq"
export PAPERREVIEW_EMAIL="250010008@slai.edu.cn"
export PAPERREVIEW_VENUE="AAAI"
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
# ---- _continue_project: 继续已有项目 ----
_continue_project() {
    echo -e "${CYAN}继续项目: ${TOPIC}${NC}"
    echo -e "阶段: ${STAGE} | 迭代: ${ITERATION}"
    echo ""

    # 找到最新的审稿
    # 查找最新外部审稿（兼容新旧路径）
    LATEST_REVIEW=$(ls -t "${WORKSPACE}/review/round_"*/external.md 2>/dev/null | head -1)

    if [[ -z "$LATEST_REVIEW" ]]; then
        echo -e "${YELLOW}该项目尚未提交审稿，或审稿文件路径不匹配。${NC}"
        echo "项目文件保留在: ${WORKSPACE}"
        echo ""
        echo "如果审稿已提交但 token 过期，需要重新获取:"
        echo "  python -c \"from paperreview_api import poll_review; ...\""
        exit 1
    fi

    REVIEW_NUM=$(echo "$LATEST_REVIEW" | grep -oP 'iter\K\d+')
    NEXT_ITER=$((ITERATION + 1))

    echo ""
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}  修订迭代 #${NEXT_ITER}${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo "  审稿意见: ${LATEST_REVIEW}"
    echo "  文献综述: ${WORKSPACE}/literature/literature_review.md"
    echo "  当前论文: ${WORKSPACE}/paper/paper.tex"
    echo ""

    # 阶段 A: 分析审稿 + 修订论文
    echo -e "${CYAN}━━━ [${NEXT_ITER}.1] 分析审稿 + 修订论文 ━━━${NC}"
    cat > /tmp/cr_revise_prompt.txt << PROMPT_EOF
你是一个论文修订专家。请根据审稿意见修改论文。

研究主题: ${TOPIC}
当前迭代: 第 ${NEXT_ITER} 轮修订

请依次阅读以下文件：
1. 审稿意见: ${LATEST_REVIEW}
2. 文献综述: ${WORKSPACE}/literature/literature_review.md
3. 当前论文: ${WORKSPACE}/paper/paper.tex

请完成：
1. 逐条分析审稿意见，确定哪些需要修改
2. 修改论文。如果需要补充实验，编写实验脚本
3. 如果补充了实验，提交 SCO 并等待结果
4. 将修订后的论文保存到: ${WORKSPACE}/paper/paper.tex
5. 重新编译 PDF: ${WORKSPACE}/paper/paper.pdf
6. 撰写 response letter: ${WORKSPACE}/paper/response_letter_iter${NEXT_ITER}.md

完成后明确报告'修订完成，请提交审稿'。
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

    # 阶段 B: 重新提交审稿
    echo ""
    echo -e "${CYAN}━━━ [${NEXT_ITER}.2] 编译 + 提交审稿 ━━━${NC}"
    echo "  编译 PDF: ${WORKSPACE}/paper/paper.pdf"

    # 阶段 C: 等待审稿
    echo ""
    echo -e "${CYAN}━━━ [${NEXT_ITER}.3] 等待 paperreview.ai 审稿 ━━━${NC}"

    PDF_FILE="${WORKSPACE}/paper/paper.pdf"
    if [[ -f "$PDF_FILE" ]]; then
        TOKEN=$(python -c "
import sys; sys.path.insert(0, '.')
from paperreview_api import submit_paper
token = submit_paper('${PDF_FILE}', email='250010008@slai.edu.cn', venue='AAAI')
print(token)
" 2>&1)

        echo -e "新 Token: ${YELLOW}${TOKEN}${NC}"

        python -c "
from state_manager import StateManager, ReviewRecord
sm = StateManager('state')
state = sm.load('${SLUG}')
record = ReviewRecord(iteration=${NEXT_ITER}, token='${TOKEN}', submitted_at='${PDF_FILE}')
sm.add_review(state, record)
"

        # 内部审稿 + 外部审稿（并行）
        ROUND_DIR="${WORKSPACE}/review/round_$(printf "%03d" ${NEXT_ITER})"
        mkdir -p "${ROUND_DIR}/internal"
        python internal_review.py "${PDF_FILE}" -o "${ROUND_DIR}/internal/" &
        INTERNAL_REVIEW_PID=$!

        echo "等待 paperreview.ai 审稿结果..."
        REVIEW_DATA=$(python -c "
import sys; sys.path.insert(0, '.')
from paperreview_api import poll_review, review_to_markdown, extract_verdict
review = poll_review('${TOKEN}', initial_wait=300, interval=60, max_wait=7200)
md = review_to_markdown(review)
with open('${ROUND_DIR}/external.md', 'w') as f: f.write(md)
verdict = extract_verdict(review)
print(f'VERDICT={verdict}')
" 2>&1)

        if kill -0 ${INTERNAL_REVIEW_PID} 2>/dev/null; then
            wait ${INTERNAL_REVIEW_PID} 2>/dev/null || true
        fi

        VERDICT=$(echo "$REVIEW_DATA" | grep "VERDICT=" | cut -d= -f2)

        python -c "
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.load('${SLUG}')
state.reviews[-1]['verdict'] = '${VERDICT}'
state.reviews[-1]['review_md_path'] = '${ROUND_DIR}/external.md'
sm.complete_stage(state, Stage.POLL_REVIEW, {'verdict': '${VERDICT}'})
"

        if [[ "$VERDICT" == "accept" ]] || [[ "$VERDICT" == "weak accept" ]]; then
            echo -e "${GREEN}★ 论文已通过审稿！${NC}"
        else
            echo -e "${YELLOW}审稿未通过 (${VERDICT}) — 自动进入下一轮修订${NC}"
            ITERATION=${NEXT_ITER}
            _continue_project  # 递归自动迭代
        fi
    fi
}

# 核心：根据 state 决定执行什么
# ---------------------------------------------------------------------------
banner

# 收集所有项目
declare -a PROJECT_SLUGS=()
declare -a PROJECT_TOPICS=()
declare -a PROJECT_STAGES=()
declare -a PROJECT_ITERS=()
declare -a PROJECT_WORKSPACES=()

for d in state/*/; do
    sf="${d}state.json"
    if [[ -f "$sf" ]]; then
        PROJECT_SLUGS+=("$(basename "$d")")
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
        PROJECT_WORKSPACES+=("workspace/$(echo "${PROJECT_TOPICS[-1]}" | tr ' ' '_' | cut -c1-50)")
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
    echo -e "${YELLOW}开始全新研究。请输入研究主题。${NC}"
    echo ""
    read -rp "研究主题: " TOPIC
    if [[ -z "$TOPIC" ]]; then
        echo "主题不能为空。"
        exit 1
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
    python search_papers.py "${TOPIC}" -n 20 -o "${WORKSPACE}/literature/" 2>&1

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

    # ===== Stage 2: 实验设计 =====
    echo ""
    echo -e "${CYAN}━━━ Stage 2/4: 实验设计 ━━━${NC}"
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

    # ===== Stage 3: SCO 实验执行 =====
    echo ""
    echo -e "${CYAN}━━━ Stage 3/4: SCO 云端实验 ━━━${NC}"
    echo ""

    EXP_SCRIPT="${WORKSPACE}/experiment/run_experiment.sh"
    if [[ -f "$EXP_SCRIPT" ]]; then
        JOB_NAME="cr-${SLUG:0:30}"
        echo "提交 SCO 任务: ${JOB_NAME}"

        # 通过 sco_runner 规范提交（cp 到 AFS → cd && bash）
        JOB_OUT=$(python -c "
from sco_runner import submit_job
from pathlib import Path
job = submit_job(Path('${EXP_SCRIPT}'), '${JOB_NAME}')
print(f'JOB_ID={job.job_id}')
" 2>&1) && SCO_EXIT=0 || SCO_EXIT=$?

        echo "${JOB_OUT}"
        JOB_ID=$(echo "$JOB_OUT" | grep -oP 'JOB_ID=\K\S+')

        if [[ "$SCO_EXIT" -ne 0 ]] || [[ -z "$JOB_ID" ]]; then
            _on_error "Stage 3 (SCO 云端实验)" "${JOB_OUT}" "${WORKSPACE}"
            # Claude Code 修复后重试一次
            JOB_OUT=$(python -c "
from sco_runner import submit_job
from pathlib import Path
job = submit_job(Path('${EXP_SCRIPT}'), '${JOB_NAME}')
print(f'JOB_ID={job.job_id}')
" 2>&1) || true
            JOB_ID=$(echo "$JOB_OUT" | grep -oP 'JOB_ID=\K\S+')
            if [[ -z "$JOB_ID" ]]; then
                echo -e "${RED}SCO 提交仍然失败，跳过实验执行阶段${NC}"
            fi
        else
            # 轮询 + 失败自动重试（最多 3 次）
            MAX_RETRIES=3
            RETRY=0
            while [[ $RETRY -le $MAX_RETRIES ]]; do
                echo "等待任务完成... (第 $((RETRY+1)) 次尝试)"
                for i in $(seq 1 180); do
                    STATUS=$(sco acp jobs describe --workspace-name share-space -o json "$JOB_ID" 2>/dev/null | python -c "import json,sys; print(json.load(sys.stdin).get('state','UNKNOWN'))" 2>/dev/null || echo "UNKNOWN")
                    echo "  状态: ${STATUS} (${i}/180)"
                    if [[ "$STATUS" == "SUCCEEDED" || "$STATUS" == "FAILED" || "$STATUS" == "STOPPED" ]]; then
                        break
                    fi
                    sleep 30
                done

                # 获取日志
                sco acp jobs stream-logs --workspace-name share-space "$JOB_ID" > "${WORKSPACE}/experiment/sco_logs.txt" 2>/dev/null || true

                if [[ "$STATUS" == "SUCCEEDED" ]]; then
                    echo -e "${GREEN}实验成功！${NC}"
                    break
                fi

                # 失败了 → Claude Code 诊断修复
                if [[ $RETRY -lt $MAX_RETRIES ]]; then
                    echo -e "${YELLOW}实验失败 (${STATUS})，启动 Claude Code 诊断修复...${NC}"
                    ERROR_LOG=$(tail -100 "${WORKSPACE}/experiment/sco_logs.txt" 2>/dev/null || echo "无法读取日志")
                    cat > /tmp/cr_debug_prompt.txt << PROMPT_EOF
你是实验调试专家。SCO 云端实验失败了。

**任务状态**: ${STATUS}
**错误日志**:
${ERROR_LOG}

**实验脚本**: ${WORKSPACE}/experiment/run_experiment.sh
**工作目录**: ${WORKSPACE}/experiment/

你的任务:
1. 仔细分析错误日志，找出失败原因
2. 修改实验脚本或代码来修复问题
3. 保存修改后的文件
4. 报告 "FIX_READY" 表示已修复，等待重新提交

常见问题及修复:
- 依赖缺失 → 在 run_experiment.sh 中添加 pip install
- 路径错误 → 修正文件路径
- 内存不足 → 减小 batch_size 或模型大小
- 语法错误 → 修正代码
PROMPT_EOF
                    _claude_task "$(cat /tmp/cr_debug_prompt.txt)" 2>&1

                    # 重新提交
                    echo "重新提交实验..."
                    JOB_NAME="cr-${SLUG:0:25}-r$((RETRY+1))"
                    JOB_OUT=$(python -c "
from sco_runner import submit_job
from pathlib import Path
job = submit_job(Path('${EXP_SCRIPT}'), '${JOB_NAME}')
print(f'JOB_ID={job.job_id}')
" 2>&1) && SCO_EXIT=0 || SCO_EXIT=$?
                    JOB_ID=$(echo "$JOB_OUT" | grep -oP 'JOB_ID=\K\S+')
                    RETRY=$((RETRY + 1))
                else
                    echo -e "${RED}实验失败 ${MAX_RETRIES} 次，将基于已有日志撰写论文${NC}"
                    RETRY=$((RETRY + 1))
                fi
            done

            python -c "
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.EXPERIMENT_EXECUTION, {'job_id': '${JOB_ID}', 'job_status': '${STATUS}'})
"
            echo -e "${GREEN}实验阶段结束: ${STATUS}${NC}"
        fi
    else
        echo -e "${YELLOW}未找到实验脚本，跳过 SCO 执行${NC}"
    fi

    # ===== Stage 4: 论文撰写 =====
    echo ""
    echo -e "${CYAN}━━━ Stage 4/4: 论文撰写 ━━━${NC}"
    echo ""

    # 复制 AAAI 2026 样式文件
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
1. 撰写 LaTeX 论文，必须使用 \usepackage[submission]{aaai2026} 样式
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

    # ===== Stage 5: 提交审稿 =====
    echo ""
    echo -e "${CYAN}━━━ Stage 5: 提交 paperreview.ai 审稿 ━━━${NC}"
    echo ""

    PDF_FILE="${WORKSPACE}/paper/paper.pdf"
    if [[ -f "$PDF_FILE" ]]; then
        TOKEN=$(python -c "
import sys; sys.path.insert(0, '.')
from paperreview_api import submit_paper
token = submit_paper('${PDF_FILE}', email='250010008@slai.edu.cn', venue='AAAI')
print(token)
" 2>&1)

        echo -e "${GREEN}审稿已提交${NC}"
        echo -e "Token: ${YELLOW}${TOKEN}${NC}"
        echo "查看审稿: https://paperreview.ai/review?token=${TOKEN}"

        python -c "
from state_manager import StateManager, Stage, ReviewRecord
sm = StateManager('state')
state = sm.load('${SLUG}')
record = ReviewRecord(iteration=0, token='${TOKEN}', submitted_at='${PDF_FILE}')
sm.add_review(state, record)
sm.start_stage(state, Stage.POLL_REVIEW)
"

        # ===== Stage 6: 内部审稿 + 外部审稿（并行） =====
        echo ""
        echo -e "${CYAN}━━━ Stage 6a: 启动内部多维度审稿 ━━━${NC}"
        echo "  在等待 paperreview.ai 的同时，启动 5 位内部审稿人..."

        # Round 0: 内部审稿在后台运行
        ROUND_DIR="${WORKSPACE}/review/round_000"
        mkdir -p "${ROUND_DIR}/internal"
        python internal_review.py "${PDF_FILE}" -o "${ROUND_DIR}/internal/" &
        INTERNAL_REVIEW_PID=$!

        echo ""
        echo -e "${CYAN}━━━ Stage 6b: 等待 paperreview.ai 外部审稿 ━━━${NC}"
        echo "  等待 5 分钟后开始轮询..."

        REVIEW_DATA=$(python -c "
import sys; sys.path.insert(0, '.')
from paperreview_api import poll_review, review_to_markdown, extract_verdict
review = poll_review('${TOKEN}', initial_wait=300, interval=60, max_wait=7200)
md = review_to_markdown(review)
with open('${ROUND_DIR}/external.md', 'w') as f: f.write(md)
verdict = extract_verdict(review)
print(f'VERDICT={verdict}')
" 2>&1)

        # 等待内部审稿完成
        if kill -0 ${INTERNAL_REVIEW_PID} 2>/dev/null; then
            echo "  等待内部审稿完成..."
            wait ${INTERNAL_REVIEW_PID} 2>/dev/null || true
        fi

        VERDICT=$(echo "$REVIEW_DATA" | grep "VERDICT=" | cut -d= -f2)
        echo ""
        echo -e "paperreview.ai 审稿结果: ${YELLOW}${VERDICT}${NC}"
        echo "审稿文件: ${ROUND_DIR}/external.md"
        echo "内部审稿: ${ROUND_DIR}/internal/"

        python -c "
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.load('${SLUG}')
state.reviews[-1]['verdict'] = '${VERDICT}'
state.reviews[-1]['review_md_path'] = '${ROUND_DIR}/external.md'
sm.complete_stage(state, Stage.POLL_REVIEW, {'verdict': '${VERDICT}'})
"

        # ===== 判断是否完成 =====
        if [[ "$VERDICT" == "accept" ]] || [[ "$VERDICT" == "weak accept" ]]; then
            echo ""
            echo -e "${GREEN}══════════════════════════════════════════════${NC}"
            echo -e "${GREEN}  ★ 论文已通过审稿！Verdict: ${VERDICT}${NC}"
            echo -e "${GREEN}══════════════════════════════════════════════${NC}"
            python -c "
from state_manager import StateManager
sm = StateManager('state')
state = sm.load('${SLUG}')
state.stage = 'done'
sm.save(state)
"
            exit 0
        else
            echo ""
            echo -e "${YELLOW}══════════════════════════════════════════════${NC}"
            echo -e "${YELLOW}  审稿未通过 (${VERDICT}) — 自动进入修订迭代${NC}"
            echo -e "${YELLOW}══════════════════════════════════════════════${NC}"
            ITERATION=0  # 首次迭代
            _continue_project
        fi
    fi
fi  # 情况 A 结束
