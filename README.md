# ChenResearch — 全自动科研系统

一个精简的自动化科研流水线系统。核心架构是 **项目编排器 + 工具链**：`chenresearch.py` 作为主控制器，Claude Code 作为项目的执行工具（负责文献检索、实验设计、LaTeX 论文撰写与修改），paperreview.ai（Stanford ML Group, Andrew Ng）提供同行评审反馈，SCO CLI 执行云端 GPU 实验。系统持续循环，直到审稿结果为 "Weak Accept" 或 "Accept"。

**关键设计：Claude Code 是本项目的执行工具，而非控制器。** 项目通过 `install.sh` 自动安装 Claude Code，并通过 `config.py` 统一管理其配置（模型: deepseek-v4-pro）。

## 目录

- [系统架构](#系统架构)
- [快速开始](#快速开始)
- [两种运行模式](#两种运行模式)
- [流水线阶段详解](#流水线阶段详解)
- [迭代循环逻辑](#迭代循环逻辑)
- [状态管理与断点续传](#状态管理与断点续传)
- [SCO 云端实验执行](#sco-云端实验执行)
- [paperreview.ai 审稿平台](#paperreviewai-审稿平台)
- [目录结构](#目录结构)
- [配置与自定义](#配置与自定义)
- [验证测试](#验证测试)
- [常见问题](#常见问题)
- [依赖项](#依赖项)

---

## 系统架构

```
用户输入: "Federated Learning with Differential Privacy for Medical Imaging"
                              ↓
┌──────────────────────────────────────────────────────────────────────┐
│                     chenresearch.py (编排器)                          │
│                                                                      │
│  ┌─ 第一阶段 (执行一次) ──────────────────────────────────────────┐  │
│  │  Stage 1  文献检索      Claude Code + arXiv/OpenAlex/S2       │  │
│  │  Stage 2  实验设计      Claude Code 设计方案 + 编写代码       │  │
│  │  Stage 3  实验执行      SCO CLI → SenseCore GPU 集群          │  │
│  │  Stage 4  论文撰写      Claude Code → LaTeX → PDF             │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                              ↓                                       │
│  ┌─ 迭代循环 ──────────────────────────────────────────────────┐    │
│  │  Stage 5  提交审稿      paperreview.ai API 上传 PDF          │    │
│  │  Stage 6  轮询审稿      等待 5min → 每 60s 查询结果          │    │
│  │           判断结果      提取 verdict                          │    │
│  │           ├─ Accept / Weak Accept → ✅ 完成                   │    │
│  │           └─ 其他 → Stage 7 修订                              │    │
│  │  Stage 7  论文修订      Claude Code 读审稿 → 改论文          │    │
│  │  Stage 8  重新提交      上传修订版 → 回到 Stage 6            │    │
│  └──────────────────────────────────────────────────────────────┘    │
│                                                                      │
│  最多迭代 10 次 (可配置)，每次迭代都会保存完整的审稿记录              │
└──────────────────────────────────────────────────────────────────────┘
```

### 核心设计原则

1. **力求精简** — 整个系统 ~1500 行 Python，无复杂框架依赖
2. **Claude Code 是执行工具** — 项目控制 Claude Code（通过 `claude -p`），而非反过来
3. **一键安装** — `bash install.sh` 自动安装 Python 依赖 + Claude Code + LaTeX
4. **REST API 优先** — paperreview.ai 提供完整的 HTTP API，无需浏览器自动化
5. **断点续传** — 所有状态持久化到 JSON 文件，可随时中断和恢复
6. **统一配置** — `config.py` 管理所有 API Key 和参数，环境变量可覆盖

---

## 快速开始

### 安装

```bash
# 进入项目目录
cd /data/PaperBot/ChenResearch

# 一键安装所有依赖（Python + Claude Code + LaTeX）
bash install.sh
```

`install.sh` 会自动完成：
1. 安装 Python 依赖 (`requests`)
2. 安装并配置 Claude Code CLI，写入项目级配置到 `.claude/settings.json`
   - 模型: `deepseek-v4-pro`
   - API Key: `sk-5d8ed00d568645efb4f6a544160b3849`
   - Base URL: `https://api.deepseek.com/anthropic`
3. 检查并安装 LaTeX 编译环境 (`pdflatex` 或 `tectonic`)
4. 验证所有组件可用

### 运行

```bash
# 启动完整流水线
python chenresearch.py run "Multi-Agent Reinforcement Learning for Robot Collaboration"

# 查看进度
python chenresearch.py status

# 中断后恢复
python chenresearch.py resume "multi_agent_reinforcement_learning..."

# 列出所有研究项目
python chenresearch.py list
```

---

## 运行模式

### 项目编排模式（核心设计）

本项目作为控制器，Claude Code 作为执行工具。运行 `python chenresearch.py run "topic"` 时：

1. 项目编排器在每个智能任务阶段调用 `claude -p`（headless 模式）
2. Claude Code 接收 prompt 并完成任务，返回结果
3. 编排器继续执行下一步（SCO 提交、paperreview API 调用、轮询等）
4. 整个过程中，编排器始终保持控制权

```bash
python chenresearch.py run "Your Research Topic"
```

### Claude Code 技能模式

在 Claude Code 会话中也可调用 `/chenresearch` 技能。Claude Code 按照 SKILL.md 定义逐步执行流水线，使用 Python 工具函数处理 API 调用和状态管理。

```
用户: /chenresearch run "Your Research Topic"
```

此模式适合交互式研究（可中途调整方向）。

---

## 流水线阶段详解

### Stage 1: 文献检索 (Literature Search)

**执行者**: Claude Code  
**输入**: 研究主题  
**输出**: `literature/literature_review.md` + `references.bib`

Claude Code 通过以下数据源检索文献：
- **arXiv API** — 预印本搜索（`search_arxiv` MCP tool）
- **OpenAlex** — 全文搜索 + 引用扩展（`search_openalex`, `enrich_citation_counts`）
- **Semantic Scholar** — 语义搜索（`search_semantic_scholar`）
- **Papers With Code** — 任务和 SOTA 查询（`search_pwc_tasks`）

输出包含：
```
literature/
├── literature_review.md    # 文献综述（研究概览、关键论文、方法汇总、研究空白、建议方向）
└── references.bib          # BibTeX 格式的参考文献
```

### Stage 2: 实验设计 (Experiment Design)

**执行者**: Claude Code  
**输入**: 文献综述  
**输出**: `experiment/experiment_plan.md` + 可执行代码

Claude Code 设计完整的实验方案：
1. 研究问题与假设
2. 方法/算法/模型详细描述
3. 数据集选择与预处理
4. 基线方法列表
5. 评估指标（主指标 + 辅助指标）
6. 实验配置（硬件、依赖、超参）
7. 消融实验设计
8. 统计显著性方案

输出包含可运行的 Python 代码和 `run_experiment.sh` 执行脚本。

### Stage 3: 实验执行 (Experiment Execution)

**执行者**: SCO CLI → SenseCore 云端 GPU  
**输入**: `run_experiment.sh`  
**输出**: 实验日志 + 结果

默认 SCO 配置（来自 `/data/sco-skill`，用户 `2chen`）：

| 参数 | 值 |
|------|-----|
| `--workspace-name` | `share-space` |
| `--aec2-name` | `share-cluster` |
| `--container-image-url` | `registry.cn-sh-01.sensecore.cn/ccr-zhicheng-02/chen-mirror2:2chen-mini-20260410132739` |
| `--worker-spec` | `n6ls.iu.i40.4.32c512g` (4× N6LS-80G, 32 vCPU, 512GB) |
| `--storage-mount` | `01995892-d478-76d8-aec7-13fd8284477e:/data:/250010008` |
| `--worker-nodes` | 1 |
| `--quota-type` | `reserved` |

编排器自动轮询任务状态直到完成，然后获取日志。

### Stage 4: 论文撰写 (Paper Writing)

**执行者**: Claude Code  
**输入**: 文献综述 + 实验报告  
**输出**: `paper/paper.tex` + `paper.pdf`

Claude Code 按照 AAAI 格式撰写完整论文：
1. Title（标题）
2. Abstract（摘要，150-250 词）
3. Introduction（引言）
4. Related Work（相关工作）
5. Method（方法）
6. Experimental Setup（实验设置）
7. Results（结果）
8. Discussion（讨论）
9. Conclusion（结论）

使用 `pdflatex` 编译为 PDF。

### Stage 5-8: 审稿迭代循环

详见[迭代循环逻辑](#迭代循环逻辑)。

---

## 迭代循环逻辑

```
iteration = 0
while iteration < max_iterations (默认 10):
    │
    ├─ Stage 5: 上传 PDF 到 paperreview.ai
    │     ├─ POST /api/get-upload-url  (获取预签名 S3 URL)
    │     ├─ POST S3 presigned URL     (上传 PDF 文件)
    │     └─ POST /api/confirm-upload  (确认提交, 返回 token)
    │
    ├─ Stage 6: 等待审稿结果
    │     ├─ 静候 5 分钟 (给服务器处理时间)
    │     └─ 每 60 秒轮询 GET /api/review/{token}
    │          ├─ HTTP 202 → 继续等待
    │          └─ HTTP 200 → 获取审稿 JSON
    │
    ├─ 提取 verdict
    │     ├─ "accept"      → ✅ DONE
    │     ├─ "weak accept" → ✅ DONE
    │     ├─ "borderline"  → 继续修订
    │     └─ "reject"      → 继续修订
    │
    └─ Stage 7-8: 修订 + 重新提交
          ├─ Claude Code 仔细阅读每一条审稿意见
          ├─ 补充缺失的实验（如果需要）
          ├─ 重写/修改相关章节
          ├─ 撰写 response letter
          ├─ 重新编译 PDF
          └─ iteration += 1 → 回到 Stage 5
```

### 审稿结果解析

系统从 paperreview.ai 返回的 JSON 中自动提取 verdict：

```python
def extract_verdict(review: dict) -> str:
    # 直接字段: recommendation, verdict, decision
    # 文本解析: "Recommendation: Accept" / "Recommendation: Weak Accept" / ...
    # 返回: "accept" / "weak accept" / "borderline" / "reject" / "unknown"
```

### 审稿结果格式化

审稿 JSON 自动转换为结构化的 Markdown 文件，保存在 `review/review_iter{NN}.md`，包含：
- Summary（总结）
- Strengths（优点）
- Weaknesses（缺点）
- Detailed Comments（详细评论）
- Questions for Authors（致作者问题）
- Overall Assessment（总体评价）
- Parsed Verdict（解析后的结果）

---

## 状态管理与断点续传

系统采用 JSON 文件持久化所有状态，支持随时中断和恢复。

### 状态文件结构

```json
{
  "topic": "Federated Learning with Differential Privacy",
  "topic_slug": "federated_learning_with_differential_privacy_a1b2c3",
  "stage": "poll_review",
  "iteration": 2,
  "max_iterations": 10,
  "target_verdict": "weak accept",
  "email": "250010008@slai.edu.cn",
  "venue": "AAAI",
  "created_at": "2026-05-27T10:30:00+00:00",
  "updated_at": "2026-05-27T14:20:00+00:00",
  "work_dir": "/data/PaperBot/ChenResearch/workspace/federated_learning...",
  "literature_dir": ".../literature",
  "experiment_dir": ".../experiment",
  "paper_dir": ".../paper",
  "review_dir": ".../review",
  "stages": {
    "literature_search": {
      "status": "completed",
      "started_at": "2026-05-27T10:30:00+00:00",
      "completed_at": "2026-05-27T10:45:00+00:00",
      "meta": { "papers_found": 23 }
    },
    "experiment_design": { "status": "completed", ... },
    "experiment_execution": {
      "status": "completed",
      "meta": { "job_id": "acp-abc123", "job_status": "SUCCEEDED" }
    },
    "paper_writing": { "status": "completed", ... },
    "submit_review": { "status": "completed", ... },
    "poll_review": { "status": "in_progress", ... }
  },
  "reviews": [
    {
      "iteration": 0,
      "token": "abc123...",
      "verdict": "reject",
      "review_md_path": ".../review/review_iter00.md"
    },
    {
      "iteration": 1,
      "token": "def456...",
      "verdict": "borderline",
      "review_md_path": ".../review/review_iter01.md"
    }
  ]
}
```

### 断点续传

任何时候中断（Ctrl+C、网络断开、超时），直接运行 `resume` 即可从上次中断的阶段继续：

```bash
python chenresearch.py resume "federated_learning_with_differential_privacy_a1b2c3"
```

系统会：
- 跳过已完成的阶段
- 从当前 `stage` 字段指示的阶段开始
- 保留所有历史审稿记录
- 继续迭代计数器

---

## 文件结构

```
ChenResearch/                        # 项目根目录
│
├── chenresearch.py                  # 主编排器 (CLI 入口, 流水线状态机, 迭代循环)
├── config.py                        # 统一配置 (API Keys, 模型, SCO, 轮询参数)
├── paperreview_api.py               # paperreview.ai REST API 客户端
├── sco_runner.py                    # SCO CLI 封装 (提交/等待/日志)
├── state_manager.py                 # JSON 状态持久化 (断点续传)
├── install.sh                       # 一键安装脚本
├── SKILL.md                         # Claude Code 技能定义
├── README.md                        # 项目文档
│
├── .claude/                         # Claude Code 配置
│   └── settings.json                # deepseek-v4-pro 模型配置
│
├── prompts/                         # Claude Code 提示模板
│   ├── literature_search.md
│   ├── experiment_design.md
│   ├── paper_writing.md
│   └── paper_revision.md
│
├── templates/
│   └── aaai.tex.j2                  # AAAI 会议 LaTeX 模板
│
├── state/                           # 状态文件存储
│   └── <topic_slug>/
│       └── state.json
│
└── workspace/                       # 研究工作目录
    └── <topic>/
        ├── literature/
        │   ├── literature_review.md
        │   └── references.bib
        ├── experiment/
        │   ├── experiment_plan.md
        │   ├── run_experiment.sh
        │   ├── main_experiment.py
        │   └── sco_logs.txt
        ├── paper/
        │   ├── paper.tex
        │   ├── paper.pdf
        │   └── response_letter_iter01.md
        └── review/
            ├── review_iter00.md
            └── review_iter01.md
```

---

## 代码结构详解

### chenresearch.py — 主控模块 (~490 行)

- **CLI 层**: `cmd_run()`, `cmd_resume()`, `cmd_status()`, `cmd_list()`
- **流水线编排**: `_run_pipeline()` — 阶段状态机 + 迭代循环
- **阶段实现**: 8 个 `_do_*` 函数，每个负责一个阶段的具体逻辑
- **Claude Code 调用**: `_run_claude_task()` — 尝试 `claude -p` headless 模式，失败时保存 prompt 供手动执行

### paperreview_api.py — 审稿平台客户端 (~250 行)

关键函数：
```python
def submit_paper(pdf_path, email="250010008@slai.edu.cn", venue="AAAI") -> str
    """3步上传: 获取预签名URL → 上传S3 → 确认提交. 返回 review token."""

def poll_review(token, initial_wait=300, interval=60, max_wait=7200) -> dict
    """等待 initial_wait 秒后每 interval 秒轮询. 返回审稿数据."""

def get_review(token) -> dict | None
    """单次查询. None = 仍在处理, dict = 审稿完成."""

def extract_verdict(review: dict) -> str
    """从审稿JSON中提取 verdict: accept/weak accept/borderline/reject/unknown."""

def review_to_markdown(review: dict) -> str
    """将审稿JSON转换为结构化 Markdown."""
```

paperreview.ai API 接口：
```
POST   /api/get-upload-url      body: {filename, venue}          → {presigned_url, s3_key, ...}
POST   {presigned_s3_url}       multipart: fields + file         → HTTP 200/204
POST   /api/confirm-upload      form: {s3_key, venue, email}     → {success, token}
GET    /api/review/{token}      —                                → 202 (处理中) / 200 (完成)
```

### state_manager.py — 状态管理 (~230 行)

关键类型：
```python
class Stage(str, Enum):
    LITERATURE_SEARCH, EXPERIMENT_DESIGN, EXPERIMENT_EXECUTION,
    PAPER_WRITING, SUBMIT_REVIEW, POLL_REVIEW, REVISE, RESUBMIT,
    DONE, FAILED

class StageState:    # 每个阶段的状态
    status: str       # pending / in_progress / completed / failed
    started_at, completed_at, error, meta

class ResearchState: # 完整研究项目状态
    topic, stage, iteration, max_iterations, target_verdict
    stages: dict[str, StageState]
    reviews: list[dict]
    email, venue, work_dir, ...

class StateManager:
    create(topic, work_dir) -> ResearchState
    load(topic_or_slug) -> ResearchState
    save(state)
    start_stage(state, stage) / complete_stage(state, stage)
    fail_stage(state, stage, error)
    add_review(state, record)
```

### sco_runner.py — 云端实验管理 (~220 行)

关键配置和函数：
```python
@dataclass
class SCOConfig:
    workspace: str = "share-space"
    aec2: str = "share-cluster"
    image: str = "registry.cn-sh-01.sensecore.cn/ccr-zhicheng-02/chen-mirror2:..."
    worker_spec: str = "n6ls.iu.i40.4.32c512g"          # 4× N6LS-80G
    storage_mount: str = "01995892-d478-76d8-aec7-..."
    worker_nodes: int = 1

def submit_job(script_path, job_name, ...) -> SCOJob
def wait_for_job(job_id, poll_interval=60, max_wait=86400) -> SCOJob
def stream_logs(job_id, output_path=None) -> str
def list_jobs(limit=20) -> list[dict]
```

---

## paperreview.ai 审稿平台

**网址**: https://paperreview.ai  
**开发方**: Stanford Machine Learning Group (Andrew Ng)  
**费用**: 免费  
**审稿格式**: multi-perspective AI review (Summary, Strengths, Weaknesses, Detailed Comments, Questions, Overall Assessment)

### 关键特性

- **完全 REST API** — 无需浏览器自动化，纯 HTTP 调用
- **Token 机制** — 每次提交返回唯一 token，通过 token 查询审稿结果
- **异步处理** — 提交后需等待（通常 10-30 分钟），系统自动发送邮件通知
- **支持多会议** — AAAI, NeurIPS, ICML, ICLR, CVPR, ACL 等
- **审稿结构** — 7 个部分的结构化审稿报告

### 数据流程图

```
chenresearch
    │
    ├──(1)──→ paperreview.ai ──→ presigned S3 URL + s3_key
    │
    ├──(2)──→ AWS S3 (直传 PDF)
    │
    ├──(3)──→ paperreview.ai ──→ token (保存到 state.json)
    │
    ├──(4)──→ paperreview.ai ──→ 202 (处理中, 每60s重试)
    │         paperreview.ai ──→ 200 + 审稿JSON (处理完成)
    │
    └──(5)──→ 解析 verdict, 保存 review markdown, 决定继续或结束
```

---

## 配置与自定义

所有配置集中在 `config.py`，同时支持环境变量覆盖：

### Claude Code（执行工具）

```bash
export CLAUDE_MODEL=deepseek-v4-pro
export CLAUDE_API_KEY=sk-5d8ed00d568645efb4f6a544160b3849
export CLAUDE_BASE_URL=https://api.deepseek.com/anthropic
```

### API Keys

```bash
export SEMANTIC_SCHOLAR_API_KEY=s2k-TxOJNhO0O615j3huoEbRfhfIUfnzoXLE2V9ZfEaq
export PAPERREVIEW_EMAIL=250010008@slai.edu.cn
export PAPERREVIEW_VENUE=AAAI
```

### 调整迭代参数

```bash
export CHENRESEARCH_MAX_ITERATIONS=10
export CHENRESEARCH_POLL_INITIAL_WAIT=300   # 提交后等待 (秒)
export CHENRESEARCH_POLL_INTERVAL=60        # 轮询间隔 (秒)
export CHENRESEARCH_POLL_MAX_WAIT=7200      # 最大等待 (秒)
export CHENRESEARCH_TARGET_VERDICT="weak accept"
```

---

## 可用性验证

以下测试于 2026-05-27 实际执行通过：

### paperreview.ai

| 测试项 | 结果 |
|--------|------|
| PDF 上传 (3-step API) | **PASS** — 正常获取 token |
| 审稿查询 `GET /api/review/{token}` | **PASS** — 202 处理中 / 200 完成 |
| 无效 token 处理 | **PASS** — HTTP 404 + 错误描述 |

```bash
cd /data/PaperBot/ChenResearch
python paperreview_api.py
# 预期: Upload OK — token: xxx...
```

### SCO CLI (SenseCore)

| 测试项 | 结果 |
|--------|------|
| `sco` 命令 | **PASS** — 用户 `250010008`，zone `cn-sh-01g` |
| AEC2 集群查询 | **PASS** — `share-cluster` 活跃 (116 节点) |
| AFS 存储查询 | **PASS** — `afs-share-01g` (1735TB) |
| ACP 任务提交 | **PASS** — `pt-ume6sefc` SUCCEEDED |
| 实际运行 | **PASS** — 4× N6LS-80G，提交后 <1 分钟完成 |

```bash
# 提交一个 smoke test 任务
sco acp jobs create \
  --workspace-name share-space --aec2-name share-cluster \
  --job-name chenresearch-smoke \
  --container-image-url registry.cn-sh-01.sensecore.cn/ccr-zhicheng-02/chen-mirror2:2chen-mini-20260410132739 \
  --training-framework pytorch --worker-nodes 1 \
  --worker-spec n6ls.iu.i40.4.32c512g \
  --storage-mount 01995892-d478-76d8-aec7-13fd8284477e:/data:/250010008 \
  --command "echo 'Hello from ChenResearch'; nvidia-smi"
```

### 核心模块

```bash
# 语法检查
python -m py_compile chenresearch.py config.py paperreview_api.py sco_runner.py state_manager.py

# 状态管理器
python -c "
from state_manager import StateManager, Stage
sm = StateManager('state')
state = sm.create('smoke test', work_dir='/tmp/smoke')
state = sm.start_stage(state, Stage.LITERATURE_SEARCH)
state = sm.complete_stage(state, Stage.LITERATURE_SEARCH, {'papers': 10})
reloaded = sm.load(state.topic_slug)
assert reloaded.stages['literature_search'].status == 'completed'
print('PASS')
"
```

---

## 常见问题

### Q: 没有 SCO CLI 怎么办？

实验执行阶段会跳过或报错。可以在 `chenresearch.py` 中将 `_do_experiment_execution` 改为 mock 模式，或直接在本地执行实验脚本。

### Q: claude CLI 不可用？

这是正常情况。当作为 Claude Code 技能运行时，Claude Code 本身就是执行环境。Python 脚本只在机械性任务（API 调用、轮询）时被调用。

### Q: 审稿超时怎么办？

系统会保存 token 并提示手动查看。访问 `https://paperreview.ai/review?token={your_token}` 即可查看审稿结果。也可以在 `paperreview_api.py` 中增加 `max_wait` 参数。

### Q: 如何手动继续审稿流程？

```python
from paperreview_api import poll_review, review_to_markdown
token = "your-saved-token"
review = poll_review(token, initial_wait=0)  # 不等待, 直接开始轮询
print(review_to_markdown(review))
```

### Q: 可以同时运行多个研究项目吗？

可以。每个研究项目有独立的 `state/<slug>/state.json` 和 `workspace/<topic>/` 目录，互不干扰。

---

## 依赖项

| 依赖 | 用途 | 必需 |
|------|------|------|
| Python ≥ 3.11 | 运行环境 | 是 |
| `requests` | HTTP 客户端 (paperreview API) | 是 |
| `sco` CLI | SenseCore 云端 GPU 实验 | 实验阶段 |
| `claude` CLI | headless 模式智能任务 | 独立脚本模式 |
| `pdflatex` / `tectonic` | LaTeX PDF 编译 | 论文撰写阶段 |
| NanoResearch-slai MCP | 文献检索工具 | 文献检索阶段 |

安装全部依赖：
```bash
pip install requests
# SCO CLI 需在 SenseCore 环境中安装配置
# Claude Code 通过 /plugin 安装
# pdflatex: apt install texlive-latex-base 或 brew install tectonic
```

---

## 相关资源

- [paperreview.ai](https://paperreview.ai) — Stanford Agentic Reviewer
- [SCO CLI 文档](/data/sco-skill/skills/sco-control/SKILL.md) — SenseCore 云端操作
- [NanoResearch-slai](/data/PaperBot/NanoResearch-slai) — MCP 文献检索工具
- [paperfactory](/data/paperfactory/RESEARCH_SYSTEM.md) — 早期研究自动化参考
- [审稿样本](/data/paperreview-ai-review-invalid-gain-detection.md) — paperreview.ai 审稿输出范例

---

## License

MIT
