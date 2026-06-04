# ChenResearch — 全自动科研系统

一键启动交互式科研流水线。从研究想法 → 文献检索 → **ReAct 假说生成** → 实验设计 → SCO 云端 GPU 执行 → LaTeX 论文撰写 → 双轨审稿 → 自动修订迭代，全链条自动化。

**核心设计：`start.sh` 是控制器（Bash），Claude Code 是执行工具（`claude -p`）。每次审稿迭代重开新会话，上下文不累积。**

**v2.2 新特性**：ReAct 假说生成 + **Phase A/B/C 修订管线**（含补充实验执行、SCO 断点恢复、内部审稿门控）+ 实验全失败自动终止保护。详见 [修订迭代管线](#5c-修订迭代)。

---

## 快速开始

```bash
cd /data/AutoResearch/ChenResearch
bash install.sh    # 安装 Python 依赖 + Claude Code + LaTeX
bash start.sh      # 首次运行配置 API Key → 选择/创建项目
```

启动后交互式输入研究想法：

```
你的想法: A novel attention mechanism for long sequences in transformer models

Agent: ━━━ Stage 1/4: 文献检索 ━━━
       arXiv + Semantic Scholar + OpenAlex 三方检索...
       ✓ 找到 23 篇论文
       ━━━ Stage 2/4: 实验设计 ━━━
       ...
```

---

## 系统架构全景

ChenResearch 有**两个入口**，共享同一套核心模块和状态系统：

| 入口 | 文件 | 特点 |
|------|------|------|
| **Shell 控制器（主）** | `start.sh` | Bash 驱动，↑↓ 菜单选择项目，阶段路由，错误恢复，内部审稿门控 |
| **Python CLI** | `chenresearch.py` | `run / resume / status / list` 命令，适合脚本集成 |

```
┌──────────────────────────────────────────────────────────┐
│                    start.sh (控制器)                       │
│  状态检测 → 阶段调度 → claude -p 执行 → 错误恢复 → 循环   │
└──────────────────────────┬───────────────────────────────┘
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                 ▼
   ┌─────────────┐  ┌─────────────┐  ┌─────────────┐
   │ 直接调用工具  │  │ claude -p   │  │ Python 模块  │
   │ search_papers │  │ 实验设计     │  │ sco_runner  │
   │ sco acp jobs  │  │ 论文撰写     │  │ paperreview │
   │ pdflatex      │  │ 论文修订     │  │ internal    │
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
              │  experiment_design      │ ← 实验设计
              │  (评审门: ≥ 6.5)        │
              └───────────┬─────────────┘
                          ▼
              ┌─────────────────────────┐
              │  experiment_execution   │ ← SCO 云端执行
              │  (评审门: ≥ 5.5)        │
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
                   └──┬───────┬──┘
                      │       │
              accept  │       │  reject / borderline / weak reject
              weak    │       ▼
              accept  │  ┌─────────────────────┐
                      │  │  revise              │ ← 内部审稿门控修订
                      │  │  (内部审稿门控通过    │
                      │  │   后才允许重新提交)   │
                      │  └──────────┬──────────┘
                      │             ▼
                      │  ┌─────────────────────┐
                      │  │  resubmit            │ ← 重新提交 → 循环 poll
                      │  └─────────────────────┘
                      │             │
                      ▼             ▼ (达到 max_iterations)
              ┌──────────────┐  ┌──────────────┐
              │  done        │  │  done         │
              │  (论文通过)   │  │  (达到上限)   │
              └──────────────┘  └──────────────┘
```

每个阶段都有独立的状态追踪：`pending → in_progress → reviewing → completed`（或 `review_failed → 重试`）。

---

## 假说生成引擎（ReAct）

**v2.1 新增** — 在文献检索和实验设计之间插入一个深度分析阶段，通过 ReAct（Reasoning + Acting）模式系统性地寻找研究空白并生成可验证的研究假说。

### 设计理念

传统流水线的致命缺陷：文献检索只是"广撒网"，缺乏对文献的深度理解和对比分析。假说生成引擎通过**多轮迭代检索 + PDF 深度阅读 + 系统对比**，将 "可能的灵感" 转化为 "有文献支撑的、可验证的研究假说"。

参考 NanoResearch 的 ideation 阶段设计，Adapted 到 ChenResearch 的 `claude -p` 执行模型。

### 四阶段 ReAct 流程

```
Phase 1: INITIAL_ANALYSIS
  阅读文献综述 + 论文元数据
  → claude -p "分析研究空白"
  → 提取已知事实和空白列表

Phase 2: REACT_LOOP (最多 3 轮)
  ┌─ THOUGHT: claude -p "还需要了解什么？生成精确检索查询"
  ├─ ACT:     python search_papers.py "针对性查询" --save-json
  ├─ OBSERVE: 阅读新论文元数据，更新知识库
  └─ 重复直到: 信息充足 OR 达到最大轮次

Phase 3: PDF_DEEP_READ
  下载 top-5 最高引用/最相关论文的 PDF
  → pdftotext 提取全文
  → claude -p "深度对比这些论文的方法、结论、矛盾和空白"

Phase 4: HYPOTHESIS_GEN
  基于所有积累的知识生成 3-5 个研究假说
  → 每个假说包含: 标题、方法概述、文献支撑、预期结果
  → 假说之间相互比较，选出最优先发的
```

### 假说输出结构

每个生成的假说包含：

| 字段 | 说明 |
|------|------|
| `title` | 学术风格的假说标题 |
| `description` | 详细描述（2-3段） |
| `method_outline` | 提出的方法概述 |
| `rationale` | 为什么值得研究（基于文献空白） |
| `key_references` | 至少 3 篇关键支撑文献 |
| `expected_outcome` | 预期结果 |
| `novelty_score` | 创新性评分（1-10） |
| `impact_score` | 影响力评分（1-10） |

### 元数据保留

`search_papers.py` 新增功能：
- `--save-json <path>`: 保存完整论文元数据（标题、作者、年份、摘要、URL、arXiv ID、PDF URL、来源、引用量、分类）
- `--download-pdfs <dir>`: 自动下载 arXiv PDF 到指定目录
- `download_top_pdfs()`: 按引用量排序，下载 top-K 篇 PDF

### 配置

```bash
export CHENRESEARCH_HYPOTHESIS_MAX_ROUNDS=3   # ReAct 最大搜索轮次
export CHENRESEARCH_HYPOTHESIS_TOP_K_PDFS=5   # PDF 深度阅读篇数
export CHENRESEARCH_HYPOTHESIS_MAX_PAPERS=50  # 每轮检索最大论文数
```

### 阶段评审门

假说生成阶段的评审标准最严格（通过阈值 **7.0/10**）：

- 缺口分析深度
- 假说具体性
- 文献支撑充分性
- 创新性评估
- 可行性
- 假说对比质量
- ReAct 检索质量

---

## 五阶段流水线（用户视角）

### Stage 1: 文献检索

**执行方式**：`python search_papers.py` 直接调用学术 API，然后 `claude -p` 补充分析。

| 数据源 | 说明 |
|--------|------|
| arXiv | 官方 API，免费，自动处理限流（429 重试 3 次） |
| Semantic Scholar | 需要 API Key，获取引用量/影响力数据 |
| OpenAlex | 免费开放学术图谱，补充元数据 |

**输出**：`workspace/<topic>/literature/literature_review.md` + `references.bib` + `papers_metadata.json`

**评审门（Stage Review Gate）**：
- 审稿人角色：资深文献综述审稿人
- 评估维度：覆盖面、组织、缺口分析、引用质量、可执行方向、参考文献格式
- 通过阈值：**6.0/10**，不通过自动重试（最多 10 次），每次注入评审反馈

### Stage 2: ReAct 假说生成

**执行方式**：`python hypothesis_engine.py` 运行四阶段 ReAct 引擎，然后 `claude -p` 审阅优化。

**ReAct 循环**：
1. **初始分析** — 阅读文献综述和论文元数据，识别已知事实和研究空白
2. **迭代检索** — 最多 3 轮：THOUGHT（判断还需了解什么）→ ACT（精确检索）→ OBSERVE（更新知识库）
3. **PDF 深度阅读** — 下载 top-5 最高引用论文的 PDF，提取全文，系统对比方法、结论和矛盾
4. **假说生成** — 生成 3-5 个具体假说，每个包含方法概述、文献支撑、预期结果

**检索特性**：
- 按需检索：假说生成过程中可以触发新的定向检索
- 元数据保留：`--save-json` 保存所有论文的完整元信息（标题、作者、摘要、年份、来源、引用量、arXiv ID、PDF URL）
- PDF 下载：对高引用/高相关论文自动下载 PDF 全文，`pdftotext` 提取文本后深度阅读

**输出**：`workspace/<topic>/hypothesis/hypothesis_output.json` + `hypothesis_report.md` + `all_papers.json`

**评审门**：审稿重点 — 缺口分析深度、假说具体性、文献支撑、创新性、可行性、对比质量、ReAct 检索质量。通过阈值 **7.0/10**（最严格的阶段，确保研究方向的正确性）。

### Stage 3: 实验设计

**执行方式**：`claude -p` 阅读文献综述和选定的研究假说后生成实验方案。

**产出内容**：
1. 研究问题和假设
2. 方法/模型详细描述
3. 数据集选择 + 基线方法 + 评估指标
4. 实验配置（超参、硬件） + 消融实验设计
5. 可执行的 Python 实验代码
6. `run_experiment.sh`（含环境设置、依赖安装、全部执行命令）

**输出**：`workspace/<topic>/experiment/experiment_plan.md` + 实验代码和脚本

**评审门**：审稿重点 — 假设清晰性、方法合理性、基线覆盖、统计严谨性。通过阈值 **6.5/10**（最严格的阶段）。

### Stage 4: 实验执行（本地优先）

**执行方式**：`sco_runner.py` 的 `run_experiment()` 统一接口，实现本地优先（local-first）策略。

**决策逻辑**：
```
检测本地 GPU
  ├─ 有 GPU → 本地执行（最多重试 3 次）
  │    ├─ 成功 → 进入下一阶段
  │    └─ 失败 → 如果实验需要 GPU → SCO 后备
  │
  └─ 无 GPU → 启发式判断实验是否需要 GPU
       ├─ 需要 GPU → SCO 云端执行
       └─ 不依赖 GPU → 本地 CPU 执行
```

**GPU 检测**：
- 首选：`nvidia-smi` 查询 GPU 名称和显存
- 备选：检查 `CUDA_VISIBLE_DEVICES` 环境变量
- 最后：PyTorch `torch.cuda.device_count()`

**GPU 需求启发式判断**：扫描脚本和同目录 .py 文件中的 GPU 关键词（`.cuda()`, `torch.cuda`, `DataParallel`, `deepspeed`, `flash_attn`, `bf16`, `fp16` 等）。

**本地执行特性**：
- 超时时间：7200 秒（可配置）
- 最大重试：3 次（可配置）
- 每轮日志保存到 `experiment/logs/local_run_NN.log`
- 失败后自动调用 Claude Code 诊断修复

**SCO 后备**（仅在本地无 GPU 且实验需要 GPU 时启用）：

| 配置项 | 默认值 |
|--------|--------|
| 集群 | `share-cluster` |
| GPU 规格 | `n6ls.iu.i40.4.32c512g`（4×N6LS-80G） |
| 镜像 | `chen-mirror2:2chen-mini-20260410132739` |
| Worker 节点 | 1 |

**手动强制 SCO**：`export CHENRESEARCH_FORCE_SCO=true`

**输出**：`workspace/<topic>/experiment/sco_logs.txt`（统一命名，兼容后续阶段）

**评审门**：审稿重点 — 执行成功、结果完整性、合理性、可复现性。通过阈值 **5.5/10**（最宽松，允许实验有瑕疵）。

### Stage 5: 论文撰写 + 双轨审稿 + 修订迭代

#### 5a. 论文撰写

**执行方式**：`claude -p` 基于文献综述和实验日志撰写完整 AAAI 2026 格式 LaTeX 论文。

**模板系统**：
- `templates/aaai.tex.j2` — Jinja2 LaTeX 模板
- `templates/aaai2026.sty` — AAAI 2026 样式文件
- `templates/aaai2026.bst` — BibTeX 样式
- 自动编译 `pdflatex` → PDF

**评审门**：结构、清晰性、技术准确性、声明-证据匹配、图表质量、排版、引用完整性。通过阈值 **6.0/10**。

#### 5b. 双轨审稿系统

提交 PDF 到 paperreview.ai 后，**内部审稿和外部审稿并行运行**，充分利用等待时间：

```
提交 PDF 到 paperreview.ai
        │
        ├─────────────────────────────────────┐
        ▼                                     ▼
┌───────────────────────┐       ┌────────────────────────┐
│  内部审稿（并行）       │       │  外部审稿                │
│  5 位审稿人 × claude -p │       │  paperreview.ai         │
│  ~3-5 分钟完成          │       │  5min 后每 60s 轮询一次   │
│                        │       │  10-30 分钟出结果         │
└───────────┬───────────┘       └───────────┬────────────┘
            │                               │
            └────────── 合并判定 ────────────┘
```

**内部审稿 5 位专家**：

| 审稿人 | 关注维度 | 评分维度 |
|--------|---------|---------|
| **Methodology Expert** | 方法正确性、创新性、可复现性 | 技术深度、理论支撑 |
| **Experiments Reviewer** | 数据集、基线、指标、统计、消融 | 实验严谨性 |
| **Clarity & Writing Reviewer** | 结构、清晰度、图表、AI 写作检测 | 表达质量 |
| **Related Work Reviewer** | 相关工作覆盖、缺失引用、定位 | 文献完整度 |
| **Devil's Advocate** | 过度声称、隐藏假设、替代解释、数据泄漏 | 防盲区、防自欺 |

每位审稿人输出 `PROBLEM → IMPACT → FIX` 格式的详细修改意见，独立打分（1-10）。

**外部审稿**：paperreview.ai（Stanford Agentic Reviewer），3 步上传流程：
1. `POST /api/get-upload-url` → 预签名 S3 URL
2. `POST <presigned_url>` → 直接上传 PDF
3. `POST /api/confirm-upload` → 获取 review token

#### 5c. 修订迭代（Phase A/B/C 管线）

收到外部审稿意见后，进入三阶段修订管线：

```
外部审稿结果
  ├─ accept / weak accept → ✅ 论文通过，完成
  └─ reject / borderline / weak reject
       │
       ▼
  ┌──────────────────────────────────────────────┐
  │  Phase A: 分析审稿意见                        │
  │  分离 [需要实验] / [文字修改] / [理论补充]      │
  │  输出 revision_plan.json（实验需求列表）       │
  └──────────────────┬───────────────────────────┘
                     ▼
  ┌──────────────────────────────────────────────┐
  │  Phase B1: 编写补充实验代码                    │
  │  Claude Code 为每个实验需求写代码和运行脚本     │
  │  输出 exp_*/run_experiment.sh + manifest      │
  ├──────────────────────────────────────────────┤
  │  Phase B2: 并行提交实验到 SCO                  │
  │  force_sco=True，充分利用多 GPU 并行           │
  │  保存 sco_job_id.txt → 支持断点恢复            │
  │  等待完成 → 拉取日志 → experiment_log.txt     │
  │  ★ 全部失败自动终止，不进入 Phase C            │
  └──────────────────┬───────────────────────────┘
                     ▼
  ┌──────────────────────────────────────────────┐
  │  Phase C: 用真实实验数据更新论文               │
  │  逐条修改审稿意见 → 使用实际实验数字           │
  │  不编造数据 → 重新编译 → 排版检查              │
  └──────────────────┬───────────────────────────┘
                     ▼
  ┌──────────────────────────────────────────────┐
  │  内部审稿门控（Internal Gate）                 │
  │  反复修订直到：                                │
  │  • 内部 5 位审稿平均分 ≥ 6.0                  │
  │  • 外部意见解决率 ≥ 70%                       │
  │  最多迭代 5 轮                                │
  └──────────────────┬───────────────────────────┘
                     ▼ (通过)
              重新提交 paperreview.ai
                     │
                     ▼ (循环，最多 10 次迭代)
```

**SCO 断点恢复**：系统重启后，Phase B2 自动检测 `logs/sco_job_id.txt`，查询 SCO 云端任务状态：
- SUCCEEDED → 拉取日志，跳过重交
- RUNNING → 轮询等待完成
- FAILED → 仅重交失败的实验
- 无 job_id → 重新提交

**修订检查点系统**：每个 phase 独立保存检查点（`state/<slug>/revision_checkpoint_<iter>.json`），支持任意时刻中断和恢复：
- `phase_a` / `phase_b1` / `phase_b2_submitted` / `phase_b2` / `phase_c`
- 重启后自动跳过已完成的 phase

---

## 双重质量闸门

ChenResearch 有**两层质量闸门**，分别作用于不同粒度：

### 第一层：阶段性评审门（Stage Review Gate）— 每阶段产出

```
阶段执行完成
     ↓
产出结果（文献综述 / 实验方案 / 论文草稿）
     ↓
LLM 审稿员按阶段标准打分（1-10）
     ↓
score ≥ 阈值 → ✓ 通过 → 进入下一阶段
score < 阈值 → ✗ 不通过 → 注入反馈重新执行（最多 10 次）
                     ↓
              每次重试将评审反馈拼入 Prompt
                     ↓
              达到最大重试 → ⚠ 警告后继续
```

各阶段阈值：

| 阶段 | 审稿人角色 | 评估维度数 | 通过阈值 |
|------|-----------|-----------|---------|
| literature_search | 资深文献综述审稿人 | 6 | **6.0** |
| hypothesis_generation | 资深研究假说审稿人 | 7 | **7.0**（最严） |
| experiment_design | 资深实验设计审稿人 | 8 | **6.5** |
| experiment_execution | 资深实验评审人 | 6 | **5.5**（最宽） |
| paper_writing | 资深论文写作审稿人 | 8 | **6.0** |
| revise | 资深论文修改审稿人 | 3 | **6.0** |

### 第二层：内部审稿门控（Internal Review Gate）— 提交前

提交外部审稿前，必须通过内部 5 人评审和外部意见解决率检查：

| 检查项 | 阈值 | 说明 |
|--------|------|------|
| 内部审稿平均分 | ≥ 6.0/10 | 5 位审稿人平均分 |
| 外部意见解决率 | ≥ 70% | 逐条核对上一轮外部审稿意见是否已解决 |

---

## 上下文管理策略

**每次迭代重开全新 Claude Code 会话**，通过文件传递上下文，避免历史对话积累导致的上下文爆炸：

```
Session 1: Stage 1-4（文献→实验→论文）+ 提交审稿
           → 保存 review.md + paper.tex + literature_review.md
           → start.sh 退出

Session 2: start.sh 检测到 poll_review 阶段 → 读取审稿结果
           → 判定 → 修订迭代
           → 读 3 个文件（审稿意见 + 文献综述 + 当前论文）
           → Claude Code 修订
           → 上下文只有几百行，不会积累历史

Session N: ... 直到 accept 或达到 max_iterations
```

**关键原则**：`start.sh` 是控制器（有状态），`claude -p` 是无状态的执行器。控制器负责记住"做到哪了"，执行器只负责"现在做什么"。

---

## 模块清单

### 核心控制器

| 文件 | 行数 | 职责 |
|------|------|------|
| `start.sh` | ~2200 | **主控制器** — 交互菜单 → 状态检测 → 阶段路由 → claude -p 调用 → 错误恢复 → Phase A/B/C 修订管线 → SCO 断点恢复 |
| `chenresearch.py` | ~900 | **Python CLI 备选** — `run / resume / status / list`，等价流水线逻辑 |

### 状态系统

| 文件 | 职责 |
|------|------|
| `state_manager.py` | 状态机核心 — 9 个 Stage 枚举、ResearchState/StageState/ReviewRecord 数据类、JSON 持久化、阶段评审追踪 |
| `menu.py` | 终端 ↑↓ 键交互菜单，选择已有项目或新建 |

### 学术工具

| 文件 | 职责 |
|------|------|
| `search_papers.py` | 文献检索 — arXiv（XML API + 限流重试） + Semantic Scholar + OpenAlex 三方聚合，支持 `--save-json` 元数据导出和 `--download-pdfs` PDF 下载 |
| `hypothesis_engine.py` | **ReAct 假说生成引擎** — 四阶段流程：初始分析 → 迭代检索（THOUGHT→ACT→OBSERVE）→ PDF 深度阅读 → 假说生成 |
| `internal_review.py` | 内部审稿 — 5 位审稿人并行（ThreadPoolExecutor），每位独立调用 `claude -p` |
| `review_synthesis.py` | 审稿合成 — 合并 5 位审稿人意见，计算平均分和 consensus verdict |
| `review_tools.py` | 自动化审稿检查 — AI 痕迹检测、引用覆盖、文献横向对比 |
| `revision_engine.py` | 逐章节修订循环 — backpressure + grounding，确保每轮修订有实质改进 |
| `revision_protocol.py` | 修订协议定义 — Phase A/B/C 阶段的标准化接口 |
| `literature_context.py` | 文献横向对比上下文构建 — 为修订提供文献支撑 |
| `context_compressor.py` | 上下文压缩 — 长文本摘要和关键信息提取 |
| `figure_generation.py` | 发表级图表生成 — matplotlib + booktabs，PDF 矢量输出 |
| `paperreview_api.py` | paperreview.ai 客户端 — 3 步上传（预签名 URL → S3 → 确认） + 轮询 + verdict 解析 |
| `stage_reviewer.py` | 阶段性评审 — LLM 模拟审稿员评审每个阶段产出，支持 `llm` / `human` 两种模式 |

### 基础设施

| 文件 | 职责 |
|------|------|
| `sco_runner.py` | SCO CLI 封装 + 本地执行引擎 — GPU 检测、启发式 GPU 需求判断、`run_experiment()` 统一接口（local-first，SCO 后备）、断点恢复（sco_job_id.txt） |
| `config.py` | 统一配置 — API Key、模型设置、SCO 集群参数、管道调参，全部可环境变量覆盖 |
| `exceptions.py` | 统一异常定义 — 可恢复/不可恢复错误分类 |
| `progress.py` | 进度追踪 — 实验执行、修订迭代的进度持久化 |

### Prompt 模板

| 文件 | 对应阶段 |
|------|---------|
| `prompts/literature_search.md` | Stage 1 — 文献检索（含 `{{TOPIC}}` `{{OUTPUT_DIR}}` 占位符） |
| `prompts/hypothesis_generation.md` | Stage 2 — 假说生成（含 `{{TOPIC}}` `{{LITERATURE_REVIEW}}` `{{PAPERS_JSON}}` `{{REACT_STATE}}` 注入） |
| `prompts/experiment_design.md` | Stage 3 — 实验设计（含 `{{LITERATURE_REVIEW}}` 注入） |
| `prompts/paper_writing.md` | Stage 4a — 论文撰写（含 `{{EXPERIMENT_REPORT}}` 注入） |
| `prompts/paper_revision.md` | Stage 4c — 论文修订（含 `{{REVIEWS}}` 注入） |

### LaTeX 模板

| 文件 | 说明 |
|------|------|
| `templates/aaai.tex.j2` | Jinja2 模板 — AAAI 2026 论文主文件 |
| `templates/aaai2026.sty` | AAAI 2026 官方样式文件 |
| `templates/aaai2026.bst` | AAAI 2026 BibTeX 样式文件 |

---

## 目录结构

```
ChenResearch/
├── start.sh                    # ★ 主控制器（入口）
├── install.sh                  # 一键安装脚本
│
├── chenresearch.py             # Python CLI 备选（run/resume/status/list）
├── state_manager.py            # 状态机（9 阶段枚举 + JSON 持久化）
├── search_papers.py            # 文献检索（arXiv + S2 + OpenAlex）
├── hypothesis_engine.py        # ReAct 假说生成引擎
├── stage_reviewer.py           # 阶段评审门（LLM/human 双模式）
├── internal_review.py          # 内部 5 人审稿（并行 ThreadPoolExecutor）
├── review_synthesis.py         # 审稿合成与评分
├── review_tools.py             # 自动化审稿检查（AI痕迹/引用/文献对比）
├── revision_engine.py          # 逐章节修订循环（backpressure + grounding）
├── revision_protocol.py        # Phase A/B/C 修订协议
├── literature_context.py       # 文献横向对比上下文构建
├── context_compressor.py       # 上下文压缩
├── figure_generation.py        # 发表级图表生成（matplotlib + booktabs）
├── paperreview_api.py          # paperreview.ai 客户端（3 步上传 + 轮询）
├── sco_runner.py               # SCO GPU 集群封装 + 本地执行引擎 + 断点恢复
├── config.py                   # 统一配置（全部可环境变量覆盖）
├── exceptions.py               # 统一异常定义
├── progress.py                 # 进度持久化
├── menu.py                     # ↑↓ 键终端菜单
│
├── prompts/                    # Claude Code 提示模板
│   ├── literature_search.md
│   ├── hypothesis_generation.md
│   ├── experiment_design.md
│   ├── paper_writing.md
│   └── paper_revision.md
│
├── templates/                  # AAAI 2026 LaTeX 模板
│   ├── aaai.tex.j2
│   ├── aaai2026.sty
│   └── aaai2026.bst
│
├── examples/                   # 使用示例和指南
│   ├── research_topics.md
│   ├── interaction_guide.md
│   └── ideation_frameworks.md
│
├── CLAUDE.md                   # Claude Code 工具清单（执行器视角）
├── SKILL.md                    # Claude Code 技能定义
│
├── .claude/                    # Claude Code 配置（settings.json）
├── .env                        # 环境变量（API Key 等）
│
├── state/                      # 流水线状态持久化
│   └── <topic_slug>/
│       └── state.json          # ResearchState 完整序列化
│
└── workspace/                  # 研究产物
    └── <topic>/
        ├── literature/         # literature_review.md + references.bib + papers_metadata.json
        ├── hypothesis/         # hypothesis_output.json + hypothesis_report.md + all_papers.json
        │   ├── metadata/       # 每轮检索的论文元数据 JSON
        │   ├── pdfs/           # 下载的 arXiv PDF
        │   └── react_state.json # ReAct 状态检查点（支持中断恢复）
        ├── experiment/         # experiment_plan.md + *.py + run_experiment.sh + sco_logs.txt
        │   ├── logs/          # local_run_NN.log（本地执行日志）
        │   └── revision_iter_*/  # 修订迭代实验（Phase B）
        │       ├── revision_plan.json       # Phase A 产出
        │       ├── experiment_results.json  # Phase B2 汇总
        │       └── exp_*/     # 每个补充实验
        │           ├── run_experiment.sh
        │           ├── experiment_manifest.json
        │           ├── experiment_log.txt
        │           └── logs/sco_job_id.txt  # SCO 断点恢复
        ├── paper/              # paper.tex + paper.pdf
        └── review/             # 审稿产物
            ├── round_000/      # 第 0 轮审稿
            │   ├── external.md    # paperreview.ai 结果
            │   └── internal/      # 内部 5 人审稿
            ├── round_001/      # 第 1 轮修订审稿
            ├── ...
            └── internal_gate_*/   # 内部审稿门控产物
```

---

## 配置参考

所有配置在 `config.py` 中定义默认值，通过环境变量覆盖：

### API 密钥

```bash
export ANTHROPIC_API_KEY="sk-..."           # Claude Code 执行工具（必需）
export ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic"
export CLAUDE_MODEL="deepseek-v4-pro"
export SEMANTIC_SCHOLAR_API_KEY="s2k-..."   # 文献检索
export PAPERREVIEW_EMAIL="250010008@slai.edu.cn"
export PAPERREVIEW_VENUE="AAAI"
```

### SCO 集群配置

```bash
export SCO_WORKSPACE="share-space"
export SCO_AEC2="share-cluster"
export SCO_IMAGE="registry.cn-sh-01.sensecore.cn/.../chen-mirror2:xxx"
export SCO_WORKER_SPEC="n6ls.iu.i40.4.32c512g"   # 4×N6LS-80G
export SCO_WORKER_NODES=1
export SCO_QUOTA_TYPE="reserved"
```

### 流水线参数

```bash
export CHENRESEARCH_MAX_ITERATIONS=10      # 最大修订轮次
export CHENRESEARCH_POLL_INITIAL_WAIT=300  # 审稿首次轮询前等待（秒）
export CHENRESEARCH_POLL_INTERVAL=60       # 轮询间隔（秒）
export CHENRESEARCH_POLL_MAX_WAIT=7200     # 轮询超时（秒）
export CHENRESEARCH_TARGET_VERDICT="weak accept"

# 假说生成（ReAct 引擎）
export CHENRESEARCH_HYPOTHESIS_MAX_ROUNDS=3   # ReAct 最大搜索轮次
export CHENRESEARCH_HYPOTHESIS_TOP_K_PDFS=5   # PDF 深度阅读篇数
export CHENRESEARCH_HYPOTHESIS_MAX_PAPERS=50  # 每轮检索最大论文数

# 实验执行：本地优先
export CHENRESEARCH_LOCAL_TIMEOUT=7200          # 本地执行超时（秒）
export CHENRESEARCH_LOCAL_MAX_RETRIES=3         # 本地执行最大重试次数
export CHENRESEARCH_FORCE_SCO=false             # true=强制使用 SCO 云端
```

### 阶段性评审门

```bash
export CHENRESEARCH_STAGE_REVIEW=true           # 总开关
export CHENRESEARCH_STAGE_REVIEW_MODE=llm       # llm 或 human
export CHENRESEARCH_STAGE_REVIEW_MAX_RETRIES=10 # 每阶段最大重试次数

# 各阶段通过阈值（1-10）
export CHENRESEARCH_THRESHOLD_LITERATURE=6.0
export CHENRESEARCH_THRESHOLD_HYPOTHESIS=7.0  # 假说生成（最严格）
export CHENRESEARCH_THRESHOLD_DESIGN=6.5
export CHENRESEARCH_THRESHOLD_EXECUTION=5.5
export CHENRESEARCH_THRESHOLD_WRITING=6.0
export CHENRESEARCH_THRESHOLD_REVISION=6.0
```

---

## 设计原则

1. **代码驱动，而非对话驱动** — `start.sh` 是有状态的控制器，`claude -p` 是无状态的执行器。控制器记住"做到哪了"，每次调用 Claude Code 只给一个窄任务
2. **文件传递上下文** — 所有中间产物落盘（文献综述、实验日志、论文、审稿意见），Claude Code 通过读取文件获取上下文，避免历史对话积累
3. **错误不丢项目** — 任何阶段出错都会保存状态和产物到 `workspace/`，支持人工修复后继续
4. **双重质量闸门** — 阶段级评审门（每阶段产出检查）+ 内部审稿门控（提交外部前检查），确保只把高质量论文送出去
5. **实时迭代闭环** — 外部审稿意见 → 内部修订 → 内部审稿门控 → 重新提交，形成自动改进飞轮

---

## 可用性状态

| 组件 | 状态 | 备注 |
|------|------|------|
| paperreview.ai | ✅ 2026-05-27 | 上传 / 查询均正常 |
| SCO CLI | ✅ 2026-05-27 | share-cluster 116 节点，提交成功 |
| arXiv API | ✅ | 限流自动重试（429 → 最多 3 次） |
| Semantic Scholar | ✅ | 正常工作 |
| OpenAlex API | ✅ | 正常返回 |
| 状态管理器 | ✅ | 创建 → 流转 → 持久化 → 加载 |
| 内部审稿（5 人并行） | ✅ | ThreadPoolExecutor，~3-5min |
| 阶段评审门 | ✅ | LLM 模式可用，Human 模式可用 |

---

## 依赖

| 依赖 | 用途 | 必需 |
|------|------|------|
| Python ≥ 3.10 + `requests` | 运行环境 | ✅ |
| Claude Code CLI (`claude`) | 智能任务执行、审稿 | ✅ |
| `sco` CLI | SenseCore GPU 集群操作 | 实验阶段 |
| `pdflatex` | PDF 编译 | 论文阶段 |
| `npm` | 安装 Claude Code | 首次安装 |

---

## 相关资源

- [paperreview.ai](https://paperreview.ai) — Stanford Agentic Reviewer
- [arXiv API](https://info.arxiv.org/help/api/) — 学术文献检索
- [Semantic Scholar API](https://api.semanticscholar.org/) — 文献引用数据
- [SenseCore Open Platform](https://www.sensecore.cn) — SCO GPU 集群
- [GitHub](https://github.com/2-chen/ChenResearch)
