# SLAIResearch — 全自动科研系统

一键启动交互式科研流水线。从研究想法 → 文献检索 → **ReAct 假说生成** → 实验设计 → SCO 云端 GPU 执行 → LaTeX 论文撰写 → 双轨审稿 → 自动修订迭代，全链条自动化。

**核心设计：`start.sh` 是控制器（Bash），Claude Code 是执行工具（`claude -p`）。每次审稿迭代重开新会话，上下文不累积。**

**v2.4 新特性**：审稿能力校准（从外部审稿中学习，动态进化内部审稿人）+ 磁盘证据验证（拒绝文字虚报）+ 门控实验自动修复循环 + 鲁棒的续跑/恢复。

---

## 快速开始

```bash
cd /data/AutoResearch/SLAIResearch
bash install.sh    # 安装 Python 依赖 + Claude Code + LaTeX
bash start.sh      # 首次运行配置 API Key → 选择/创建项目
```

启动后交互式输入研究想法：

```
你的想法: A novel attention mechanism for long sequences in transformer models

Agent: ━━━ Stage 1/6: 文献检索 ━━━
       arXiv + Semantic Scholar + OpenAlex 三方检索...
       ✓ 找到 23 篇论文
       ━━━ Stage 2/6: 假说生成 ━━━
       ...
```

---

## 系统架构全景

SLAIResearch 有**两个入口**，共享同一套核心模块和状态系统：

| 入口 | 文件 | 特点 |
|------|------|------|
| **Shell 控制器（主）** | `start.sh` | Bash 驱动，↑↓ 菜单选择项目，阶段路由，错误恢复，内部审稿门控 |
| **Python CLI** | `slairesearch.py` | `run / resume / status / list` 命令，适合脚本集成 |

```
┌──────────────────────────────────────────────────────────────┐
│                    start.sh (控制器)                           │
│  状态检测 → 阶段调度 → claude -p 执行 → 错误恢复 → 循环       │
├──────────────────────────────────────────────────────────────┤
│  质量闸门: 阶段性评审门 (每阶段) + 内部审稿门控 (最多 5 轮)    │
│  审稿进化: 校准系统 → 从外部审稿学习 → 动态注册审稿人          │
│  证据验证: 磁盘扫描 → 实验结果文件存在性检查 → 拒绝文字虚报    │
└──────────────────────────┬───────────────────────────────────┘
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                 ▼
   ┌─────────────┐  ┌─────────────┐  ┌─────────────┐
   │ 直接调用工具  │  │ claude -p   │  │ Python 模块  │
   │ search_papers │  │ 实验设计     │  │ sco_runner  │
   │ sco acp jobs  │  │ 论文撰写     │  │ paperreview │
   │ pdflatex      │  │ 论文修订     │  │ internal    │
   │ prompt_render │  │ 审稿校准     │  │ calibration │
   └─────────────┘  └─────────────┘  └─────────────┘
```

### 状态机模型

整个流水线是一台**有限状态机**，状态持久化为 JSON 文件（`state/<slug>/state.json`），支持任意时刻中断和恢复：

```
                    ┌──────────────┐
                    │  新项目创建    │
                    └──────┬───────┘
                           ▼
              ┌─────────────────────────┐
              │  literature_search      │ ← 文献检索
              │  (评审门: ≥ 6.0)        │
              └───────────┬─────────────┘
                          ▼
              ┌─────────────────────────┐
              │  hypothesis_generation  │ ← ReAct 假说生成
              │  (评审门: ≥ 7.0)        │   (多轮检索+PDF深读+对比)
              └───────────┬─────────────┘
                          ▼
              ┌─────────────────────────┐
              │  baseline_fetching      │ ← GitHub 基线代码获取
              └───────────┬─────────────┘
                          ▼
              ┌─────────────────────────┐
              │  experiment_design      │ ← Claude Code 实验科学家
              │  (sed→Python 模板渲染)   │   (全权接管设计+执行)
              └───────────┬─────────────┘
                          ▼
              ┌─────────────────────────┐
              │  environment_preparation│ ← 预下载 wheels/模型/数据集
              │  (manifest 优先)        │   (manifest.json → run.sh fallback)
              └───────────┬─────────────┘
                          ▼
              ┌─────────────────────────┐
              │  experiment_execution   │ ← 本地优先 → SCO fallback
              │  (评审门: ≥ 5.5)        │   (auto-fix loop: ≤20 轮)
              └───────────┬─────────────┘
                          ▼
              ┌─────────────────────────┐
              │  paper_writing          │ ← LaTeX 撰写+编译
              │  (评审门: ≥ 6.0)        │
              └───────────┬─────────────┘
                          ▼
              ┌─────────────────────────┐
              │  submit_review          │ ← 提交 paperreview.ai
              └───────────┬─────────────┘
                          ▼
              ┌─────────────────────────┐
              │  poll_review            │ ← 等待审稿结果
              └───────────┬─────────────┘
                          ▼
                   ┌─────────────┐
                   │  判定 verdict │
                   │  (6 层提取)   │
                   └──┬───────┬──┘
                      │       │
              accept  │       │  reject / borderline / weak reject
              weak    │       ▼
              accept  │  ┌──────────────────────────────┐
                      │  │  审稿能力校准 ★ NEW           │ ← 从外部审稿学习
                      │  │  (对比分析 → 动态注册审稿人)   │
                      │  └──────────────┬───────────────┘
                      │                 ▼
                      │  ┌──────────────────────────────┐
                      │  │  revise (Phase A/B/C 修订)    │
                      │  │  A: 分析+issue_tracker       │
                      │  │  B: 补充实验 (auto-fix loop)  │
                      │  │  C: 更新论文                 │
                      │  └──────────────┬───────────────┘
                      │                 ▼
                      │  ┌──────────────────────────────┐
                      │  │  内部审稿门控 (≤5 轮)         │
                      │  │  • 内部 5 人审稿 ≥ 6.0       │
                      │  │  • 磁盘证据验证 ★ NEW         │
                      │  │  • 外部意见解决率 ≥ 70%       │
                      │  │  • 门控实验 auto-fix ★ NEW    │
                      │  └──────────────┬───────────────┘
                      │                 ▼ (通过)
                      │  ┌──────────────────────────────┐
                      │  │  resubmit → 循环 poll         │
                      │  └──────────────────────────────┘
                      │                 │
                      ▼                 ▼ (达到 max_iterations)
              ┌──────────────┐  ┌──────────────┐
              │  done        │  │  done         │
              │  (论文通过)   │  │  (达到上限)   │
              └──────────────┘  └──────────────┘
```

---

## 假说生成引擎（ReAct）

**四阶段 ReAct 流程**：初始分析 → 多轮迭代检索（THOUGHT→ACT→OBSERVE）→ PDF 深度阅读（top-5 论文全文） → 假说生成（3-7 个，含创新性/影响力评分）。

### 假说输出结构

每个生成的假说包含：`title`、`description`、`method_outline`、`rationale`、`key_references`、`expected_outcome`、`novelty_score`、`impact_score`。

### 配置

```bash
export SLAIRESEARCH_HYPOTHESIS_MAX_ROUNDS=3   # ReAct 最大搜索轮次
export SLAIRESEARCH_HYPOTHESIS_TOP_K_PDFS=5   # PDF 深度阅读篇数
export SLAIRESEARCH_HYPOTHESIS_MAX_PAPERS=50  # 每轮检索最大论文数
```

---

## 审稿能力校准系统 ★ NEW v2.4

内部审稿系统**从外部审稿中学习**，每轮审稿后自动进化：

```
外部审稿 (external.md)
       +
内部审稿 (5 位 reviewer_*.md)
       │
       ▼
┌─────────────────────────────────────┐
│  _do_review_calibration()           │
│  渲染 review_calibration.md prompt  │
│  Claude Code 分析:                  │
│    1. 对比分析: 逐条对比发现差距      │
│    2. 漏检分析: 外部发现但内部遗漏   │
│    3. 审稿人评估: 每个角色的一致率   │
│    4. 改进建议: 新增/修改审稿人      │
├─────────────────────────────────────┤
│  输出:                              │
│  → review/calibration.md (分析报告) │
│  → review/reviewer_pool.json (注册) │
└─────────────────────────────────────┘
       │
       ▼ 下一轮内部审稿
┌─────────────────────────────────────┐
│  internal_review.py                 │
│  _load_dynamic_reviewers() 读取     │
│  reviewer_pool.json                 │
│  → 5 固定审稿人 + N 个动态审稿人    │
│  → 并行审稿，覆盖之前盲区           │
└─────────────────────────────────────┘
```

**接入点**：校准在收到外部审稿后、进入修订前自动触发（`_do_submit_review` 和 `poll_review` resume 两处）。每轮只校准一次（幂等）。

---

## 六阶段流水线（用户视角）

### Stage 1: 文献检索

**执行方式**：`python search_papers.py` 直接调用学术 API，然后 `claude -p` 补充分析。

| 数据源 | 说明 |
|--------|------|
| arXiv | 官方 API，免费，自动处理限流（429 重试 3 次） |
| Semantic Scholar | 需要 API Key，获取引用量/影响力数据 |
| OpenAlex | 免费开放学术图谱，补充元数据 |

**输出**：`workspace/<topic>/literature/literature_review.md` + `references.bib` + `papers_metadata.json`

### Stage 2: ReAct 假说生成

**执行方式**：`python hypothesis_engine.py` 运行四阶段 ReAct 引擎。

### Stage 3: 实验设计 + 执行

**执行方式**：Claude Code 实验科学家全权接管。使用 **Python 模板渲染**（非 sed）生成任务 prompt，自主完成环境检查→代码编写→执行→调试→报告。

**关键改进**：
- 模板渲染使用 Python `str.replace()` 替代 shell `sed`，避免多行变量导致的 `unterminated s command` 错误
- 环境准备优先读 `experiment_manifest.json`（权威来源），fallback 到扫描 `run_experiment.sh`
- 实验失败自动修复循环（最多 20 轮，含代码修复 + 重新提交）

### Stage 4: 论文撰写 + 双轨审稿 + 修订迭代

#### 4a. 论文撰写

**模板系统**：`templates/aaai.tex.j2`（Jinja2）+ `aaai2026.sty` + `aaai2026.bst`

#### 4b. 双轨审稿系统

**内部审稿 5 位专家** + 动态注册审稿人：

| 审稿人 | 关注维度 |
|--------|---------|
| **Methodology Expert** | 方法正确性、创新性、可复现性 |
| **Experiments Reviewer** | 数据集、基线、指标、统计、消融 |
| **Clarity & Writing Reviewer** | 结构、清晰度、图表、AI 写作检测 |
| **Related Work Reviewer** | 相关工作覆盖、缺失引用、定位 |
| **Devil's Advocate** | 过度声称、隐藏假设、替代解释 |
| ***动态审稿人** (从校准学习) | 由外部审稿发现的盲区决定 |

**外部审稿 verdict 提取**（6 层 fallback）：
1. 直接顶层字段（`recommendation`, `verdict`, `decision`）
2. `overall_assessment` section 文本解析
3. **语义分析**：strengths vs weaknesses 条目计数和关键词密度
4. JSON 树递归搜索（`_walk_for_verdict`）
5. 比率启发式（strength/weakness ≥2.0 → weak accept）
6. 文本长度/关键词兜底（至少返回 `borderline`）

#### 4c. 修订迭代（Phase A/B/C 管线）

```
外部审稿结果 → 审稿能力校准 ★
     │
     ▼
┌──────────────────────────────────────────────┐
│  Phase A: 分析审稿意见                        │
│  分离 [实验] / [文字] / [引用] / [图表] / [理论] │
│  输出 issue_tracker.md → round_NNN/          │
│  ★ EXP_COUNT 交叉验证: tracker 为权威来源      │
└──────────────────┬───────────────────────────┘
                   ▼
┌──────────────────────────────────────────────┐
│  Phase B: 补充实验                            │
│  B1: Claude 编写实验代码                       │
│  B2: 并行提交到 SCO (含 auto-fix loop)         │
│  ★ 门控实验失败自动修复 (代码诊断 + 重新提交)   │
│  ★ .experiments_checked 条件持久化            │
└──────────────────┬───────────────────────────┘
                   ▼
┌──────────────────────────────────────────────┐
│  Phase C: 用真实实验数据更新论文               │
│  逐条修改 → 实际数字 → 重新编译               │
└──────────────────┬───────────────────────────┘
                   ▼
┌──────────────────────────────────────────────┐
│  内部审稿门控 (≤5 轮)                         │
│  • 内部评分 ≥ 6.0/10                         │
│  • 磁盘证据验证 ★ (扫描 results.json 存在性)   │
│  • 外部意见解决率 ≥ 70%                       │
│  • 门控实验 .experiments_checked 失败时清除   │
└──────────────────┬───────────────────────────┘
                   ▼ (通过)
            重新提交 paperreview.ai
```

---

## 续跑与恢复

### 各阶段续跑行为

| 续跑阶段 | 行为 |
|---------|------|
| `literature_search` | 重新执行文献检索 → fallthrough 到假说生成 |
| `hypothesis_generation` | 重新执行 ReAct 引擎 → baseline fetching → 实验设计 |
| `experiment_design` | 检查 manifest → env prep → 实验执行 |
| `environment_preparation` | 重新执行 env prep → 实验执行 |
| `experiment_execution` | Claude Code 实验科学家 + legacy fallback + auto-fix loop |
| `paper_writing` | 重新写论文 → 提交审稿 |
| `submit_review` | 直接提交 paperreview.ai |
| `poll_review` | **从 external.md 重新提取 verdict**（不信任 state.json 缓存）+ 安全闸门（`insufficient for acceptance` 检测）→ 校准 → 修订 |
| `revise` / `resubmit` | Phase A/B/C 修订 + 内部审稿门控 |

### 重复 state 防护

续跑时如果通过 workspace 目录名找不到 state.json，**先通过 work_dir 反向查找**已有的注册 state，避免创建重复的空白 state 文件。

---

## 双重质量闸门

### 第一层：阶段性评审门（Stage Review Gate）

| 阶段 | 通过阈值 |
|------|---------|
| literature_search | **6.0** |
| hypothesis_generation | **7.0**（最严） |
| experiment_design | **6.5** |
| experiment_execution | **5.5**（最宽） |
| paper_writing | **6.0** |
| revise | **6.0** |

### 第二层：内部审稿门控（Internal Review Gate）

| 检查项 | 阈值 | 说明 |
|--------|------|------|
| 内部审稿平均分 | ≥ 6.0/10 | 5+N 位审稿人平均分 |
| 外部意见解决率 | ≥ 70% | 逐条核对，磁盘证据优先 |
| 磁盘证据验证 | 实验 results.json 真实存在 | **拒绝文字虚报** |

**磁盘证据验证**：在 Claude 评估解决率之前，Python 脚本预先扫描实验目录中 `results.json` 的实际存在性和内容有效性。如果磁盘验证有多项失败但 Claude 返回解决率 >50%，强制限制 ≤50%。

---

## 模块清单

### 核心控制器

| 文件 | 行数 | 职责 |
|------|------|------|
| `start.sh` | ~3700 | **主控制器** — 交互菜单 → 状态检测 → 阶段路由 → claude -p 调用 → 错误恢复 → Phase A/B/C 修订管线 → SCO 断点恢复 → 审稿校准 |
| `slairesearch.py` | ~1750 | **Python CLI 备选** — `run / resume / status / list` |
| `prompt_render.py` | ~60 | **模板渲染器** — Python `${VAR}` 替换（替代 shell sed） |

### 状态系统

| 文件 | 职责 |
|------|------|
| `state_manager.py` | 状态机核心 — 9 个 Stage 枚举、ResearchState/StageState/ReviewRecord 数据类、JSON 持久化 |
| `menu.py` | 终端 ↑↓ 键交互菜单 |

### 学术工具

| 文件 | 职责 |
|------|------|
| `search_papers.py` | 文献检索 — arXiv + S2 + OpenAlex 三方聚合 |
| `hypothesis_engine.py` | **ReAct 假说生成引擎** — 四阶段流程 |
| `internal_review.py` | 内部审稿 — 5 固定 + N 动态审稿人并行 |
| `review_synthesis.py` | 审稿合成 — 内外审稿交叉对比 |
| `review_tools.py` | 自动化审稿检查 — AI 痕迹检测、引用覆盖、文献对比 |
| `revision_engine.py` | 逐章节修订循环 — backpressure + grounding |
| `revision_protocol.py` | 修订协议 — Phase A/B/C 标准化接口 + TODO 驱动的门控 |
| `literature_context.py` | 文献横向对比上下文构建 |
| `context_compressor.py` | 上下文压缩 |
| `figure_generation.py` | 发表级图表生成 — matplotlib + booktabs |
| `paperreview_api.py` | paperreview.ai 客户端 — 3 步上传 + 6 层 verdict 提取 + 语义分析 |
| `stage_reviewer.py` | 阶段性评审 — LLM 模拟审稿员 |

### 基础设施

| 文件 | 职责 |
|------|------|
| `sco_runner.py` | SCO CLI 封装 + 本地执行引擎 — GPU 检测、local-first、auto-fix loop |
| `model_downloader.py` | 三层下载器 — 直连 → 镜像 → VPN |
| `resource_scheduler.py` | 资源调度 — CPU/GPU 分类、JOBS_PER_GPU 推荐 |
| `experiment_runner.py` | 实验运行器 — preflight + schedule + diagnose + debug memory |
| `config.py` | 统一配置 — 全部可环境变量覆盖 |
| `exceptions.py` | 统一异常定义 |
| `progress.py` | 进度追踪 |

### Prompt 模板

| 文件 | 对应阶段 |
|------|---------|
| `prompts/literature_search.md` | Stage 1 — 文献检索 |
| `prompts/hypothesis_generation.md` | Stage 2 — 假说生成 |
| `prompts/hypothesis_refine.md` | Stage 2 — 假说优化 |
| `prompts/experiment_scientist_system.md` | Stage 3 — 实验科学家系统提示词 |
| `prompts/experiment_scientist_task.md` | Stage 3 — 实验科学家任务模板 |
| `prompts/paper_writing.md` | Stage 4a — 论文撰写 |
| `prompts/paper_revision.md` | Stage 4c — 论文修订 |
| `prompts/revision_phase_a.md` | 修订 Phase A — 审稿分析 |
| `prompts/revision_phase_b.md` | 修订 Phase B — 实验代码生成 |
| `prompts/revision_phase_c.md` | 修订 Phase C — 论文更新 |
| `prompts/gate_revise.md` | 门控修订 — 逐条解决内部审稿意见 |
| `prompts/gate_experiment_check.md` | 门控实验需求检查 |
| `prompts/check_addressed.md` | 审稿合规检查 — 磁盘证据验证 |
| `prompts/review_calibration.md` | ★ 审稿能力校准 — 从外部审稿学习 |
| `prompts/stage_review_fix.md` | 阶段评审修复 |
| `prompts/recovery.md` | 故障恢复 |
| `prompts/topic_refine.md` | 研究主题提炼 |

---

## 目录结构

```
SLAIResearch/
├── start.sh                    # ★ 主控制器（入口，~3700 行）
├── install.sh                  # 一键安装脚本
│
├── slairesearch.py             # Python CLI 备选
├── prompt_render.py            # Python 模板渲染器（${VAR} 替换）
├── state_manager.py            # 状态机
├── search_papers.py            # 文献检索
├── hypothesis_engine.py        # ReAct 假说生成
├── stage_reviewer.py           # 阶段评审门
├── internal_review.py          # 内部审稿（5+N 人并行）
├── review_synthesis.py         # 审稿合成
├── review_tools.py             # 自动化审稿检查
├── revision_engine.py          # 逐章节修订循环
├── revision_protocol.py        # Phase A/B/C 修订协议 + TODO 门控
├── literature_context.py       # 文献上下文构建
├── context_compressor.py       # 上下文压缩
├── figure_generation.py        # 图表生成
├── paperreview_api.py          # paperreview.ai 客户端 + verdict 提取
├── sco_runner.py               # SCO 集群 + 本地执行 + auto-fix loop
├── model_downloader.py         # 三层下载器
├── resource_scheduler.py       # 资源调度
├── experiment_runner.py        # 实验运行器 (preflight/diagnose/debug)
├── config.py                   # 统一配置
├── exceptions.py               # 异常定义
├── progress.py                 # 进度追踪
├── menu.py                     # ↑↓ 键终端菜单
│
├── prompts/                    # Claude Code 提示模板（19 个）
│   ├── literature_search.md
│   ├── hypothesis_generation.md
│   ├── hypothesis_refine.md
│   ├── experiment_scientist_system.md
│   ├── experiment_scientist_task.md
│   ├── paper_writing.md
│   ├── paper_revision.md
│   ├── revision_phase_a.md
│   ├── revision_phase_b.md
│   ├── revision_phase_c.md
│   ├── gate_revise.md
│   ├── gate_experiment_check.md
│   ├── check_addressed.md
│   ├── review_calibration.md       # ★ 审稿能力校准
│   ├── stage_review_fix.md
│   ├── recovery.md
│   ├── sco_debug_fallback.md
│   ├── local_debug_fallback.md
│   └── topic_refine.md
│
├── templates/                  # AAAI 2026 LaTeX 模板
│   ├── aaai.tex.j2
│   ├── aaai2026.sty
│   └── aaai2026.bst
│
├── examples/                   # 使用示例和指南
│
├── state/                      # 流水线状态持久化
│   └── <topic_slug>/
│       ├── state.json
│       └── revision_checkpoint_<iter>.json
│
└── workspace/                  # 研究产物
    └── <topic>/
        ├── literature/         # literature_review.md + references.bib
        ├── hypothesis/         # hypothesis_output.json + report
        ├── experiment/         # experiment_plan.md + *.py + run_experiment.sh
        │   ├── gate_experiments/   # 门控补充实验
        │   │   ├── .experiments_checked  # 条件标记（仅全成功时持久化）
        │   │   └── exp_*/
        │   └── revision_iter_*/   # 修订迭代实验
        ├── paper/              # paper.tex + paper.pdf + figures/
        └── review/             # 审稿产物
            ├── calibration.md      # ★ 审稿校准分析报告
            ├── reviewer_pool.json  # ★ 动态注册审稿人
            ├── issue_tracker.md    # Phase A 产出（顶层缓存）
            ├── internal_todo.md    # 门控修订 TODO
            ├── round_000/          # 第 0 轮审稿
            │   ├── external.md
            │   ├── issue_tracker.md    # ★ 与审稿意见同目录
            │   └── internal/
            ├── round_001/
            └── internal_gate_*/   # 内部审稿门控产物
```

---

## 配置参考

### API 密钥

```bash
export ANTHROPIC_API_KEY="sk-..."
export ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic"
export CLAUDE_MODEL="deepseek-v4-pro"
export SEMANTIC_SCHOLAR_API_KEY="s2k-..."
export PAPERREVIEW_EMAIL="your-email@example.com"
export PAPERREVIEW_VENUE="AAAI"
```

### 流水线参数

```bash
export SLAIRESEARCH_MAX_ITERATIONS=10
export SLAIRESEARCH_POLL_INITIAL_WAIT=300
export SLAIRESEARCH_POLL_INTERVAL=60
export SLAIRESEARCH_POLL_MAX_WAIT=7200
export SLAIRESEARCH_TARGET_VERDICT="weak accept"

# 假说生成
export SLAIRESEARCH_HYPOTHESIS_MAX_ROUNDS=3
export SLAIRESEARCH_HYPOTHESIS_TOP_K_PDFS=5
export SLAIRESEARCH_HYPOTHESIS_MAX_PAPERS=50

# 实验执行
export SLAIRESEARCH_LOCAL_TIMEOUT=7200
export SLAIRESEARCH_LOCAL_MAX_RETRIES=20
export SLAIRESEARCH_FORCE_SCO=false
export SLAIRESEARCH_MAX_GPU_HOURS=32
```

### 阶段性评审门

```bash
export SLAIRESEARCH_STAGE_REVIEW=true
export SLAIRESEARCH_STAGE_REVIEW_MODE=llm
export SLAIRESEARCH_STAGE_REVIEW_MAX_RETRIES=10
```

---

## v2.4 变更日志

### 鲁棒性修复
- **sed→Python 模板渲染**：`start.sh` 中两处 16 个串联 `sed -e` 替换为 Python `str.replace()`，解决多行变量导致 `unterminated s command` 崩溃
- **环境准备读 manifest**：优先读 `experiment_manifest.json`（权威来源），fallback 到扫描 `run_experiment.sh`
- **verdict 提取 6 层 fallback**：直接字段 → overall_assessment → 语义分析 → JSON 树遍历 → 比率启发式 → 文本兜底。不再返回 `unknown`
- **poll_review 续跑不信任缓存**：从 `external.md` 重新提取 verdict + `insufficient for acceptance` 安全闸门
- **重复 state 防护**：续跑时通过 `work_dir` 反向查找已有 state，避免创建空白重复

### 修订管线改进
- **issue_tracker.md 路径**：保存到 `review/round_NNN/` 与审稿意见同目录
- **EXP_COUNT 交叉验证**：Phase A 后对比 issue_tracker 的 EXP 项与 revision_plan.json，tracker 为权威来源
- **磁盘证据验证**：`_check_addressed_items` 预先扫描 `results.json` 存在性，注入 Claude prompt，强制限制文字虚报的解决率
- **门控实验 auto-fix loop**：失败实验自动诊断代码 → Claude 修复 → 重新提交（debug_rounds 递增）
- **`.experiments_checked` 条件化**：仅在所有实验成功时持久化；失败时清除标记 + 递增 `.failed_count`，下一迭代自动重试
- **`gate_revise.md` 强制规则**：明确禁止"在 Limitations 中记录" / "在 Future Work 中提及" / "添加讨论段落" 三种虚假解决

### 新功能
- **审稿能力校准**：收到外部审稿后自动分析差距 → 动态注册审稿人 → 下一轮内部审稿加载
- **`literature_search` 续跑分支**：补充缺失的 case，fallthrough 到假说生成

---

## 设计原则

1. **代码驱动，而非对话驱动** — `start.sh` 是有状态的控制器，`claude -p` 是无状态的执行器
2. **文件传递上下文** — 所有中间产物落盘，Claude Code 通过读取文件获取上下文
3. **错误不丢项目** — 任何阶段出错都保存状态和产物，支持人工修复后继续
4. **双重质量闸门 + 磁盘证据** — 阶段评审 + 内部审稿门控 + 文件系统验证
5. **实时迭代闭环** — 外部审稿 → 校准学习 → 修订 → 门控 → 重新提交
6. **内部审稿自我进化** — 每轮从外部审稿中学习，动态注册审稿人角色

---

## 相关资源

- [paperreview.ai](https://paperreview.ai) — Stanford Agentic Reviewer
- [arXiv API](https://info.arxiv.org/help/api/) — 学术文献检索
- [Semantic Scholar API](https://api.semanticscholar.org/) — 文献引用数据
- [SenseCore Open Platform](https://www.sensecore.cn) — SCO GPU 集群
