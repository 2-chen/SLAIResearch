#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────
# SLAIResearch module: continue_project
# Functions: _ensure_state, _find_first_incomplete, _mark_stage_done,
#   _resume_literature_search, _resume_hypothesis_generation,
#   _resume_baseline_fetching, _resume_experiment_design,
#   _resume_environment_preparation, _resume_experiment_execution,
#   _resume_paper_writing, _resume_submit_review, _resume_poll_review,
#   _resume_revise, _resume_resubmit, _continue_project
# ──────────────────────────────────────────────────────────

# ── 阶段顺序（权威来源）──
_STAGE_ORDER=(
    "literature_search"
    "hypothesis_generation"
    "baseline_fetching"
    "experiment_design"
    "environment_preparation"
    "experiment_execution"
    "paper_writing"
    "submit_review"
    "poll_review"
    "revise"
    "resubmit"
)

# ── _find_first_incomplete: 从 state.json 找到第一个未完成的阶段 ──
_find_first_incomplete() {
    python3 -c "
import json, sys
try:
    data = json.load(open('${WORKSPACE}/state.json'))
except Exception:
    print('literature_search')
    sys.exit(0)

stage_order = [
    'literature_search', 'hypothesis_generation', 'baseline_fetching',
    'experiment_design', 'environment_preparation', 'experiment_execution',
    'paper_writing', 'submit_review', 'poll_review', 'revise', 'resubmit'
]
stages = data.get('stages', {})
for s in stage_order:
    info = stages.get(s, {})
    if not isinstance(info, dict):
        info = {}
    status = info.get('status', 'pending')
    if status != 'completed':
        print(s)
        sys.exit(0)
print('done')
" 2>/dev/null || echo "literature_search"
}

# ── _ensure_state: 确保 state.json 存在（从产物反推/创建）──
_ensure_state() {
    if [[ -f "${WORKSPACE}/state.json" ]]; then
        STAGE=$(python3 -c "import json; d=json.load(open('${WORKSPACE}/state.json')); print(d.get('stage','literature_search'))" 2>/dev/null || echo "literature_search")
        ITERATION=$(python3 -c "import json; d=json.load(open('${WORKSPACE}/state.json')); print(d.get('iteration',0))" 2>/dev/null || echo "0")
        SLUG=$(python3 -c "import json; d=json.load(open('${WORKSPACE}/state.json')); print(d.get('topic_slug',''))" 2>/dev/null || echo "")
        return 0
    fi

    echo -e "${YELLOW}(未注册项目 — 从产物推断阶段)${NC}"

    # 从产物文件推断当前阶段（从后往前检查，越后面的产物越可靠）
    if [[ -f "${WORKSPACE}/paper/paper.pdf" ]]; then
        STAGE="paper_writing"
    elif [[ -f "${WORKSPACE}/experiment/experiment_results.json" ]]; then
        STAGE="experiment_execution"
    elif [[ -f "${WORKSPACE}/experiment/run_experiment.sh" ]] || [[ -f "${WORKSPACE}/experiment/run_experiment.py" ]]; then
        STAGE="environment_preparation"
    elif [[ -f "${WORKSPACE}/hypothesis/hypothesis_output.json" ]]; then
        STAGE="experiment_design"
    elif [[ -f "${WORKSPACE}/literature/literature_review.md" ]]; then
        STAGE="hypothesis_generation"
    else
        STAGE="literature_search"
    fi

    local dirname
    dirname=$(basename "$WORKSPACE")
    TOPIC="${TOPIC:-$(echo "$dirname" | tr '_' ' ' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')}"
    SLUG="${SLUG:-$(echo "$dirname" | tr 'A-Z_' 'a-z-' | sed 's/--*/-/g; s/^-//; s/-$//' | cut -c1-40)}"

    echo -e "  推断阶段: ${STAGE}"
    echo -e "  推断主题: ${TOPIC:0:80}"

    python3 -c "
import json, os
from datetime import datetime, timezone
state = {
    'topic': '''${TOPIC}''',
    'topic_slug': '${SLUG}',
    'stage': '${STAGE}',
    'iteration': 0,
    'max_iterations': 10,
    'created_at': datetime.now(timezone.utc).isoformat(),
    'updated_at': datetime.now(timezone.utc).isoformat(),
    'work_dir': '${WORKSPACE}',
    'literature_dir': '${WORKSPACE}/literature',
    'hypothesis_dir': '${WORKSPACE}/hypothesis',
    'experiment_dir': '${WORKSPACE}/experiment',
    'paper_dir': '${WORKSPACE}/paper',
    'review_dir': '${WORKSPACE}/review',
    'stages': {},
    'reviews': [],
}
os.makedirs('${WORKSPACE}', exist_ok=True)
json.dump(state, open('${WORKSPACE}/state.json','w'), indent=2, ensure_ascii=False)
" 2>/dev/null
    echo -e "  ${GREEN}state.json 已重建${NC}"
}

# ── _mark_stage_done: 标记阶段完成 ──
_mark_stage_done() {
    local stage_name="$1"
    python -c "
from state_manager import StateManager, Stage
sm = StateManager('${WORKSPACE}')
state = sm.load('${SLUG}')
sm.complete_stage(state, Stage.$(echo "$stage_name" | tr 'a-z' 'A-Z'))
" 2>/dev/null || true
}

# ═══════════════════════════════════════════════════════════════
# 各阶段续跑函数（每个独立，检查完成状态后执行）
# ═══════════════════════════════════════════════════════════════

_resume_literature_search() {
    echo -e "${CYAN}━━━ 文献检索 ━━━${NC}"
    local LIT_DIR="${WORKSPACE}/literature"
    local REVIEW_MD="${LIT_DIR}/literature_review.md"

    if [[ -f "$REVIEW_MD" ]] && [[ -s "$REVIEW_MD" ]]; then
        local paper_count
        paper_count=$(grep -c '^[0-9]\+\. ' "$REVIEW_MD" 2>/dev/null || echo "0")
        if [[ "$paper_count" -ge 5 ]]; then
            echo -e "  ${GREEN}文献检索已完成 (${paper_count} 篇)${NC}"
            _mark_stage_done "literature_search"
            return 0
        fi
    fi

    mkdir -p "$LIT_DIR"

    echo -e "${CYAN}提取搜索关键词...${NC}"
    python keyword_extractor.py "${TOPIC}" --groups 3 > /tmp/slai_kw.json 2>/dev/null || true

    local KW_QUERY="$TOPIC"
    if [[ -f /tmp/slai_kw.json ]]; then
        KW_QUERY=$(python3 -c "
import json
groups = json.load(open('/tmp/slai_kw.json')).get('groups', [])
queries = [' AND '.join(g[:3]) for g in groups if g]
print('; '.join(queries[:3]) if queries else '${TOPIC}')
" 2>/dev/null || echo "$TOPIC")
    fi

    python search_papers.py "$KW_QUERY" -o "$LIT_DIR" --max 20 2>&1 || {
        echo -e "${RED}文献检索失败${NC}"
        return 1
    }

    if [[ -n "${TAVILY_API_KEY:-}" ]]; then
        python tavily_search.py "${TOPIC}" --max-results 5 --output "${LIT_DIR}/tavily_results.md" 2>/dev/null || true
    fi

    if [[ -s "$REVIEW_MD" ]]; then
        _claude_task "请阅读 ${REVIEW_MD}，基于检索到的论文进行分析：提炼领域概览和关键趋势、识别研究空白、提出具体的研究方向建议。将分析结果追加到 ${REVIEW_MD} 末尾。只做分析和建议，不超过500字。完成后报告'文献分析完成'。" || true
    fi

    _stage_review "literature_search" "$REVIEW_MD" || true
    _mark_stage_done "literature_search"
}

_resume_hypothesis_generation() {
    echo -e "${CYAN}━━━ 假说生成 (ReAct) ━━━${NC}"
    local HYPO_OUT="${WORKSPACE}/hypothesis/hypothesis_output.json"

    if [[ -f "$HYPO_OUT" ]] && [[ -s "$HYPO_OUT" ]]; then
        echo -e "  ${GREEN}假说已存在${NC}"
        _mark_stage_done "hypothesis_generation"
        return 0
    fi

    mkdir -p "${WORKSPACE}/hypothesis/"
    python hypothesis_engine.py \
        --topic "${TOPIC}" \
        --literature-dir "${WORKSPACE}/literature/" \
        --work-dir "${WORKSPACE}/hypothesis/" \
        --max-react-rounds 3 --top-k-pdfs 5 2>&1

    if [[ -f "$HYPO_OUT" ]]; then
        HYPOTHESIS_JSON=$(head -300 "$HYPO_OUT")
        HYPOTHESIS_JSON="$HYPOTHESIS_JSON" _render_prompt "hypothesis_refine.md" /tmp/slai_hypothesis_refine.txt
        _claude_task "$(cat /tmp/slai_hypothesis_refine.txt)" || true
    fi

    _stage_review "hypothesis_generation" "${WORKSPACE}/hypothesis/hypothesis_report.md" || true
    _mark_stage_done "hypothesis_generation"
}

_resume_baseline_fetching() {
    echo -e "${CYAN}━━━ 基线仓库获取 ━━━${NC}"
    local BASELINE_CTX="${WORKSPACE}/experiment/baseline_context.md"

    if [[ -f "$BASELINE_CTX" ]]; then
        echo -e "  ${GREEN}基线上下文已存在${NC}"
        BASELINE_CONTEXT=$(cat "$BASELINE_CTX" 2>/dev/null || echo "")
        _mark_stage_done "baseline_fetching"
        return 0
    fi

    mkdir -p "${WORKSPACE}/experiment"
    local BASELINE_METHODS=""
    local HYPO_FILE="${WORKSPACE}/hypothesis/hypothesis_output.json"

    if [[ -f "$HYPO_FILE" ]]; then
        python -c "
import json
data = json.load(open('$HYPO_FILE'))
methods = []
for h in data.get('hypotheses', []):
    if isinstance(h, dict):
        methods.extend(h.get('baselines', []) or h.get('baseline_methods', []))
if methods:
    with open('/tmp/slai_baseline_methods.txt', 'w') as f:
        f.write(','.join(methods[:10]))
" 2>/dev/null && BASELINE_METHODS="--methods $(cat /tmp/slai_baseline_methods.txt | tr ',' ' ')" || true
    fi

    python "${SCRIPT_DIR}/baseline_finder.py" \
        ${BASELINE_METHODS} \
        --cache-dir "${WORKSPACE}/../.shared/baselines" \
        --max 5 \
        --output "${BASELINE_CTX}" 2>&1 || {
        echo "[Phase] 基线获取跳过（网络不可用或无匹配仓库）"
        echo "" > "${BASELINE_CTX}"
    }

    BASELINE_CONTEXT=$(cat "${BASELINE_CTX}" 2>/dev/null || echo "")
    _mark_stage_done "baseline_fetching"
}

_resume_experiment_design() {
    echo -e "${CYAN}━━━ 实验设计 (Claude Code 实验科学家) ━━━${NC}"
    local MANIFEST="${WORKSPACE}/experiment/experiment_manifest.json"

    if [[ -f "$MANIFEST" ]]; then
        local has_code=false
        [[ -f "${WORKSPACE}/experiment/run_experiment.py" ]] && has_code=true
        [[ -f "${WORKSPACE}/experiment/run_experiment.sh" ]] && has_code=true
        if $has_code; then
            echo -e "  ${GREEN}实验已设计${NC}"
            _mark_stage_done "experiment_design"
            return 0
        fi
    fi

    local BASELINE_CTX="${WORKSPACE}/experiment/baseline_context.md"
    BASELINE_CONTEXT=$(cat "$BASELINE_CTX" 2>/dev/null || echo "")
    _claude_experiment_design

    if [[ ! -f "$MANIFEST" ]]; then
        echo -e "${YELLOW}[system] manifest 缺失，补全为 4 GPU${NC}"
        mkdir -p "$(dirname "$MANIFEST")"
        echo '{"gpu_count": 4}' > "$MANIFEST"
    fi

    _stage_review "experiment_design" "${WORKSPACE}/experiment/experiment_plan.md" || true
    _mark_stage_done "experiment_design"
}

_resume_environment_preparation() {
    echo -e "${CYAN}━━━ 环境准备（预下载依赖）━━━${NC}"
    _do_environment_preparation "${WORKSPACE}/experiment"
    _mark_stage_done "environment_preparation"
}

_resume_experiment_execution() {
    echo -e "${CYAN}━━━ 实验执行 ━━━${NC}"
    local RESULTS="${WORKSPACE}/experiment/experiment_results.json"

    if [[ -f "$RESULTS" ]] && [[ -s "$RESULTS" ]]; then
        echo -e "  ${GREEN}实验结果已存在${NC}"
        _mark_stage_done "experiment_execution"
        return 0
    fi

    _continue_experiment
}

_resume_paper_writing() {
    echo -e "${CYAN}━━━ 论文撰写 ━━━${NC}"
    local PAPER_PDF="${WORKSPACE}/paper/paper.pdf"

    if [[ -f "$PAPER_PDF" ]]; then
        echo -e "  ${GREEN}论文 PDF 已存在${NC}"
        _mark_stage_done "paper_writing"
        return 0
    fi

    _do_paper_writing
}

_resume_submit_review() {
    _do_submit_review
}

_resume_poll_review() {
    echo -e "${CYAN}━━━ 等待审稿结果 ━━━${NC}"
    local LATEST_REVIEW
    LATEST_REVIEW=$(ls -t "${WORKSPACE}/review/round_"*/external.md 2>/dev/null | head -1)

    if [[ -n "$LATEST_REVIEW" ]]; then
        echo "已有审稿结果: ${LATEST_REVIEW}"
        VERDICT=$(python3 -c "
import sys; sys.path.insert(0, '${SCRIPT_DIR}')
from paperreview_api import extract_verdict
import re
text = open('${LATEST_REVIEW}').read()
m = re.search(r'\*\*Parsed Verdict\*\*\s*:\s*\x60(\w+[\s\w]*)\x60', text)
if m:
    print(m.group(1).strip())
else:
    text_lower = text.lower()
    if 'insufficient for acceptance' in text_lower:
        print('reject')
    elif 'weak accept' in text_lower:
        print('weak accept')
    elif 'accept' in text_lower and 'not accept' not in text_lower:
        print('accept')
    else:
        print('unknown')
" 2>/dev/null || echo "unknown")

        echo "  Verdict: ${VERDICT}"
        if [[ "$VERDICT" == "accept" || "$VERDICT" == "weak accept" ]]; then
            echo -e "${GREEN}★ 论文已通过审稿！${NC}"
            _mark_stage_done "poll_review"
            return 0
        fi

        _do_review_calibration "$(dirname "$LATEST_REVIEW")" "$VERDICT"
        _do_revise_and_resubmit "$VERDICT"
        return 0
    fi

    # 无外部审稿 → 检查 token
    TOKEN=$(python3 -c "
import json
d = json.load(open('${WORKSPACE}/state.json'))
reviews = d.get('reviews', [])
if reviews: print(reviews[-1].get('token', ''))
" 2>/dev/null)

    if [[ -n "$TOKEN" ]]; then
        echo "  轮询审稿 token: ${TOKEN:0:20}..."
        local ROUND_DIR="${WORKSPACE}/review/round_$(printf '%03d' ${ITERATION:-0})"
        mkdir -p "$ROUND_DIR"
        REVIEW_DATA=$(echo "$TOKEN" | python -c "
import sys; sys.path.insert(0, '${SCRIPT_DIR}')
from paperreview_api import poll_review, review_to_markdown
token = sys.stdin.read().strip()
review = poll_review(token, initial_wait=60, interval=60, max_wait=7200)
md = review_to_markdown(review)
with open('${ROUND_DIR}/external.md', 'w') as f:
    f.write(md)
print('OK')
" 2>&1) || {
            echo -e "${YELLOW}  轮询失败，返回提交阶段${NC}"
            _resume_submit_review
            return $?
        }
    else
        echo -e "${YELLOW}  无审稿 token，返回提交阶段${NC}"
        _resume_submit_review
        return $?
    fi
}

_resume_revise() {
    echo -e "${CYAN}━━━ 修订迭代 ━━━${NC}"
    LATEST_REVIEW=$(ls -t "${WORKSPACE}/review/round_"*/external.md 2>/dev/null | head -1)
    if [[ -z "$LATEST_REVIEW" ]]; then
        LATEST_REVIEW=$(ls -t "${WORKSPACE}/review/"internal_*/*_summary.md 2>/dev/null | head -1)
    fi
    _do_revise_and_resubmit "unknown"
}

_resume_resubmit() {
    echo -e "${CYAN}━━━ 重新提交审稿 ━━━${NC}"
    _do_submit_review
}

# ═══════════════════════════════════════════════════════════════
# _continue_project — 状态驱动的项目续跑
# 读取 state.json → 找到第一个未完成阶段 → 顺序执行到结束
# ═══════════════════════════════════════════════════════════════
_continue_project() {
    echo -e "${CYAN}继续项目: ${TOPIC:0:80}${NC}"

    # 1. 确保 state 存在
    _ensure_state
    echo -e "阶段: ${STAGE} | 迭代: ${ITERATION} | slug: ${SLUG}"
    echo ""

    # 2. 找到第一个未完成阶段
    local NEXT_STAGE
    NEXT_STAGE=$(_find_first_incomplete)
    echo -e "  首个未完成阶段: ${CYAN}${NEXT_STAGE}${NC}"

    if [[ "$NEXT_STAGE" == "done" ]]; then
        echo -e "${GREEN}★ 所有阶段已完成！${NC}"
        return 0
    fi

    # 3. 阶段名称 → 续跑函数
    local -A STAGE_HANDLERS=(
        ["literature_search"]="_resume_literature_search"
        ["hypothesis_generation"]="_resume_hypothesis_generation"
        ["baseline_fetching"]="_resume_baseline_fetching"
        ["experiment_design"]="_resume_experiment_design"
        ["environment_preparation"]="_resume_environment_preparation"
        ["experiment_execution"]="_resume_experiment_execution"
        ["paper_writing"]="_resume_paper_writing"
        ["submit_review"]="_resume_submit_review"
        ["poll_review"]="_resume_poll_review"
        ["revise"]="_resume_revise"
        ["resubmit"]="_resume_resubmit"
    )

    # 4. 从未完成阶段开始，顺序执行
    local _skip=true
    for stage in "${_STAGE_ORDER[@]}"; do
        if [[ "$stage" == "$NEXT_STAGE" ]]; then
            _skip=false
        fi
        if $_skip; then
            continue
        fi

        local handler="${STAGE_HANDLERS[$stage]:-}"
        if [[ -z "$handler" ]]; then
            echo -e "${RED}未知阶段: ${stage}${NC}"
            exit 1
        fi

        echo ""
        echo -e "${CYAN}╔══ Stage: ${stage} ══╗${NC}"
        $handler || {
            echo -e "${RED}阶段 ${stage} 失败，项目已保留${NC}"
            return 1
        }

        STAGE="$stage"
        python -c "
from state_manager import StateManager
sm = StateManager('${WORKSPACE}')
state = sm.load('${SLUG}')
state.stage = '${stage}'
sm.save(state)
" 2>/dev/null || true
    done

    echo ""
    echo -e "${GREEN}══════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  流水线完成！${NC}"
    echo -e "${GREEN}══════════════════════════════════════════════${NC}"
}