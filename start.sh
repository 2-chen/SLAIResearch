#!/usr/bin/env bash
# =============================================================================
# ChenResearch — 代码驱动的会话管理器
# start.sh 是真正的控制器，Claude Code 是执行工具。
# 每次启动：检查 state → 确定当前阶段 → 生成精准 prompt → claude -p 执行
# 审稿后自动退出，下次重开继续迭代 — 保持每轮上下文干净。
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
# Claude Code 调用辅助 — 通过 stdin 传 prompt，避免多行解析问题
# ---------------------------------------------------------------------------
_claude_task() {
    local prompt="$1"
    echo "$prompt" | claude -p --model "${CLAUDE_MODEL:-deepseek-v4-pro}" --output-format text 2>&1
}

# ---------------------------------------------------------------------------
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
        PROJECT_STAGES+=("$(python -c "import json; print(json.load(open('$sf'))['stage'])" 2>/dev/null || echo "?")")
        PROJECT_ITERS+=("$(python -c "import json; print(json.load(open('$sf'))['iteration'])" 2>/dev/null || echo "0")")
        PROJECT_WORKSPACES+=("workspace/$(echo "${PROJECT_TOPICS[-1]}" | tr ' ' '_' | cut -c1-50)")
    fi
done

# ---- 选择项目 ----
if [[ ${#PROJECT_SLUGS[@]} -gt 0 ]]; then
    echo -e "${CYAN}已有项目:${NC}"
    echo ""
    i=1
    for idx in "${!PROJECT_SLUGS[@]}"; do
        s="${PROJECT_STAGES[$idx]}"
        # Translate stage to readable Chinese
        case "$s" in
            literature_search) s_disp="文献检索" ;;
            experiment_design) s_disp="实验设计" ;;
            experiment_execution) s_disp="实验执行" ;;
            paper_writing) s_disp="论文撰写" ;;
            submit_review) s_disp="提交审稿" ;;
            poll_review) s_disp="等待审稿" ;;
            revise) s_disp="修订中" ;;
            resubmit) s_disp="重新提交" ;;
            done) s_disp="已完成 ✓" ;;
            failed) s_disp="失败 ✗" ;;
            *) s_disp="$s" ;;
        esac
        echo "  [$i] ${PROJECT_TOPICS[$idx]:0:60}"
        echo "      阶段: ${s_disp} | 迭代: ${PROJECT_ITERS[$idx]}"
        echo ""
        i=$((i + 1))
    done
    echo "  [N]  开始全新研究"
    echo "  [Q]  退出"
    echo ""
    read -rp "请选择 [N]: " CHOICE
    CHOICE="${CHOICE:-N}"

    if [[ "$CHOICE" =~ ^[Qq]$ ]]; then
        exit 0
    elif [[ "$CHOICE" =~ ^[Nn]$ ]]; then
        # 开始新项目 — 清空变量
        PROJECT_SLUGS=()
    elif [[ "$CHOICE" =~ ^[0-9]+$ ]] && [[ "$CHOICE" -ge 1 ]] && [[ "$CHOICE" -le ${#PROJECT_SLUGS[@]} ]]; then
        idx=$((CHOICE - 1))
        SLUG="${PROJECT_SLUGS[$idx]}"
        TOPIC="${PROJECT_TOPICS[$idx]}"
        STAGE="${PROJECT_STAGES[$idx]}"
        ITERATION="${PROJECT_ITERS[$idx]}"
        WORKSPACE="${PROJECT_WORKSPACES[$idx]}"
        # 跳转到情况 B（继续已有项目）
        _continue_project
    else
        echo "无效选择"
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

        JOB_OUT=$(sco acp jobs create \
            --workspace-name share-space \
            --aec2-name share-cluster \
            --job-name "${JOB_NAME}" \
            --container-image-url registry.cn-sh-01.sensecore.cn/ccr-zhicheng-02/chen-mirror2:2chen-mini-20260410132739 \
            --training-framework pytorch \
            --worker-nodes 1 \
            --worker-spec n6ls.iu.i40.4.32c512g \
            --storage-mount 01995892-d478-76d8-aec7-13fd8284477e:/data:/250010008 \
            --command "$(cat ${EXP_SCRIPT})" 2>&1)

        JOB_ID=$(echo "$JOB_OUT" | grep -oP 'job \K\S+' || echo "")
        echo "Job ID: ${JOB_ID}"

        if [[ -n "$JOB_ID" ]]; then
            # 轮询等待
            echo "等待任务完成..."
            for i in $(seq 1 120); do
                STATUS=$(sco acp jobs describe --workspace-name share-space -o json "$JOB_ID" 2>/dev/null | python -c "import json,sys; print(json.load(sys.stdin).get('state','UNKNOWN'))" 2>/dev/null || echo "UNKNOWN")
                echo "  状态: ${STATUS} (${i}/120)"
                if [[ "$STATUS" == "SUCCEEDED" || "$STATUS" == "FAILED" || "$STATUS" == "STOPPED" ]]; then
                    break
                fi
                sleep 30
            done

            # 获取日志
            sco acp jobs stream-logs --workspace-name share-space "$JOB_ID" > "${WORKSPACE}/experiment/sco_logs.txt" 2>/dev/null || true

            python -c "
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.EXPERIMENT_EXECUTION, {'job_id': '${JOB_ID}', 'job_status': '${STATUS}'})
"
            echo -e "${GREEN}实验完成: ${STATUS}${NC}"
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

        # 内部审稿在后台运行
        python internal_review.py "${PDF_FILE}" -o "${WORKSPACE}/review/" &
        INTERNAL_REVIEW_PID=$!

        echo ""
        echo -e "${CYAN}━━━ Stage 6b: 等待 paperreview.ai 外部审稿 ━━━${NC}"
        echo "  等待 5 分钟后开始轮询..."

        REVIEW_DATA=$(python -c "
import sys; sys.path.insert(0, '.')
from paperreview_api import poll_review, review_to_markdown, extract_verdict
review = poll_review('${TOKEN}', initial_wait=300, interval=60, max_wait=7200)
md = review_to_markdown(review)
with open('${WORKSPACE}/review/review_iter00.md', 'w') as f: f.write(md)
verdict = extract_verdict(review)
print(f'VERDICT={verdict}')
" 2>&1)

        # 等待内部审稿完成
        if kill -0 ${INTERNAL_REVIEW_PID} 2>/dev/null; then
            echo "  等待内部审稿完成..."
            wait ${INTERNAL_REVIEW_PID} 2>/dev/null || true
        fi
        echo "  内部审稿已完成: ${WORKSPACE}/review/internal_review_iter00.md"

        VERDICT=$(echo "$REVIEW_DATA" | grep "VERDICT=" | cut -d= -f2)
        echo ""
        echo -e "审稿结果: ${YELLOW}${VERDICT}${NC}"
        echo "审稿详情: ${WORKSPACE}/review/review_iter00.md"

        python -c "
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.load('${SLUG}')
state.reviews[-1]['verdict'] = '${VERDICT}'
state.reviews[-1]['review_md_path'] = '${WORKSPACE}/review/review_iter00.md'
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
            echo -e "${YELLOW}  审稿未通过 (${VERDICT})${NC}"
            echo -e "${YELLOW}  审稿意见已保存到文件${NC}"
            echo -e "${YELLOW}  请运行 bash start.sh 启动新一轮修订${NC}"
            echo -e "${YELLOW}══════════════════════════════════════════════${NC}"
            exit 0
        fi
    fi
fi  # 情况 A 结束

# ---- _continue_project: 继续已有项目 ----
_continue_project() {
    echo -e "${CYAN}继续项目: ${TOPIC}${NC}"
    echo -e "阶段: ${STAGE} | 迭代: ${ITERATION}"
    echo ""

    # 找到最新的审稿
    LATEST_REVIEW=$(ls -t "${WORKSPACE}/review/review_iter"*.md 2>/dev/null | head -1)

    if [[ -z "$LATEST_REVIEW" ]]; then
        echo -e "${YELLOW}该项目尚未提交审稿（阶段: ${STAGE}）。${NC}"
        echo "无法自动继续。请在 Claude Code 中手动操作："
        echo "  cd ${WORKSPACE}"
        echo "  查看 prompts/ 下的任务模板"
        exit 1
    fi

    REVIEW_NUM=$(echo "$LATEST_REVIEW" | grep -oP 'iter\K\d+')
    NEXT_ITER=$((ITERATION + 1))

    echo -e "${CYAN}━━━ 修订迭代 #${NEXT_ITER} ━━━${NC}"
    echo ""

    # 用 Claude Code 修订论文
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

    # 重新提交审稿
    echo ""
    echo -e "${CYAN}━━━ 重新提交审稿 ━━━${NC}"

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
        python internal_review.py "${PDF_FILE}" -o "${WORKSPACE}/review/" &
        INTERNAL_REVIEW_PID=$!

        echo "等待 paperreview.ai 审稿结果..."
        REVIEW_DATA=$(python -c "
import sys; sys.path.insert(0, '.')
from paperreview_api import poll_review, review_to_markdown, extract_verdict
review = poll_review('${TOKEN}', initial_wait=300, interval=60, max_wait=7200)
md = review_to_markdown(review)
iter_num = '${NEXT_ITER}'
with open('${WORKSPACE}/review/review_iter' + iter_num.zfill(2) + '.md', 'w') as f: f.write(md)
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
state.reviews[-1]['review_md_path'] = '${WORKSPACE}/review/review_iter${NEXT_ITER}.md'
sm.complete_stage(state, Stage.POLL_REVIEW, {'verdict': '${VERDICT}'})
"

        if [[ "$VERDICT" == "accept" ]] || [[ "$VERDICT" == "weak accept" ]]; then
            echo -e "${GREEN}★ 论文已通过审稿！${NC}"
        else
            echo -e "${YELLOW}审稿未通过 (${VERDICT})，请运行 bash start.sh 继续修订${NC}"
        fi
    fi
}
