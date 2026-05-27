# ChenResearch — 全自动科研系统

一键启动交互式科研 Agent。代码驱动流水线，Claude Code 作为执行工具。文献检索 → 实验设计 → SCO 云端 GPU 执行 → LaTeX 论文 → 内部审稿 + paperreview.ai 外部审稿 → 修订迭代 → accept。

**核心设计：`start.sh` 是控制器，Claude Code 是执行工具。每次审稿迭代重开新会话，上下文不爆炸。**

## 快速开始

```bash
cd /data/PaperBot/ChenResearch
bash install.sh    # 安装 Python 依赖 + Claude Code + LaTeX
bash start.sh      # 首次：配置 API Key → 启动 Agent
                   # 之后：直接启动 Agent
```

启动后直接对话，无需 Python 命令行：

```
你: 研究多智能体强化学习在机器人协作中的应用

Agent: ━━━ Stage 1/4: 文献检索 ━━━
       arXiv + Semantic Scholar + OpenAlex 三方检索...
       ✓ 找到 23 篇论文
       ━━━ Stage 2/4: 实验设计 ━━━
       ...
```

## 系统架构

```
start.sh（控制器，代码驱动）
  │
  ├─ 检查 state/ → 确定当前阶段
  │
  ├─ Stage 1: python search_papers.py → arXiv/S2/OpenAlex 三方检索
  │           + claude -p 补充分析
  │
  ├─ Stage 2: claude -p "设计实验方案"（窄任务，上下文干净）
  │
  ├─ Stage 3: sco acp jobs create → SenseCore 4×N6LS-80G
  │           轮询状态 → 获取日志
  │
  ├─ Stage 4: claude -p "撰写 AAAI LaTeX 论文并编译 PDF"
  │
  ├─ Stage 5: python submit_paper() → paperreview.ai
  │
  ├─ Stage 6: ┌─ python internal_review.py（5位审稿人并行）
  │           │  方法论专家 + 实验评估 + 写作表达
  │           │  + 文献覆盖度 + 魔鬼辩护人
  │           │
  │           └─ python poll_review()（paperreview.ai 外部审稿）
  │
  ├─ 判断: accept/weak accept? → ✅ 完成
  │       其他 → 保存审稿 → exit → 用户重开 start.sh
  │
  └─ 修订迭代（全新 Claude Code 会话）:
       读审稿意见 + 文献综述 + 当前论文
       → claude -p "修订论文" → 重新提交 → 循环
```

## 关键模块

| 文件 | 用途 |
|------|------|
| `start.sh` | **控制器** — 状态检测 → 阶段调度 → `claude -p` 执行 |
| `search_papers.py` | 文献检索 — arXiv + Semantic Scholar + OpenAlex 三方 API |
| `internal_review.py` | 内部审稿 — 5 位审稿人并行，与 paperreview.ai 同步运行 |
| `paperreview_api.py` | paperreview.ai — 3 步上传 + 轮询 + verdict 解析 |
| `sco_runner.py` | SCO CLI — SenseCore GPU 任务提交和监控 |
| `state_manager.py` | 状态持久化 — JSON 文件，支持中断恢复 |
| `config.py` | 统一配置 — 所有 API Key 和环境变量 |

## 内部审稿系统

5 位审稿人并行运行，利用等待 paperreview.ai 的时间：

| 审稿人 | 关注维度 |
|--------|---------|
| 方法论专家 | 技术正确性、创新性、可复现性 |
| 实验评估专家 | 数据集、基线、指标、统计显著性、消融实验 |
| 写作表达专家 | 结构、清晰度、图表质量、AI 写作检测 |
| 文献覆盖度专家 | 相关工作覆盖、缺失引用、定位准确性 |
| 魔鬼辩护人 | 过度声称、隐藏假设、替代解释、数据泄漏 |

每位审稿人独立评分（1-10），输出 PROBLEM→IMPACT→FIX 格式的详细修改意见。

## 双轨审稿

```
提交 PDF 到 paperreview.ai
        ↓
┌─ 内部审稿（后台并行）──┐   ┌─ 外部审稿 ──────────────┐
│ 5 位审稿人 × claude -p │   │ 5min 后每 60s 轮询      │
│ ~3-5 分钟完成          │   │ 10-30 分钟获取结果       │
└─ internal_review_*.md ─┘   └─ review_iter*.md ───────┘
        ↓                            ↓
        └──── 合并两份审稿 → 判断 verdict ────┘
```

## 上下文管理

每次审稿迭代重开全新 Claude Code 会话，通过文件传递上下文：

```
Session 1: Stage 1-4（文献→实验→论文）+ 提交审稿
           → 保存 review.md + paper.tex + literature_review.md
           → 关闭会话

Session 2: 读 3 个文件 → 修订论文 → 重新提交
           → 上下文只有几百行，不会积累历史对话

Session N: ...直到 accept
```

## 目录结构

```
├── start.sh                  # 控制器（入口）
├── install.sh                # 一键安装
├── search_papers.py          # 学术文献检索
├── internal_review.py        # 内部多维度审稿
├── paperreview_api.py        # paperreview.ai 客户端
├── sco_runner.py             # SCO CLI 封装
├── state_manager.py          # 状态管理
├── config.py                 # 统一配置
├── chenresearch.py           # 备用：Python CLI 模式
├── CLAUDE.md                 # Claude Code 工具清单
├── SKILL.md                  # Claude Code 技能定义
│
├── prompts/                  # Claude 提示模板
├── templates/                # LaTeX 模板
├── .claude/                  # Claude Code 配置
│
├── state/                    # 流水线状态
└── workspace/                # 研究输出
    └── <topic>/
        ├── literature/       # literature_review.md + references.bib
        ├── experiment/       # experiment_plan.md + 代码 + sco_logs.txt
        ├── paper/            # paper.tex + paper.pdf + response_letter
        └── review/           # internal_review_*.md + review_iter*.md
```

## 配置

所有配置在 `config.py`，环境变量覆盖：

```bash
# Claude Code（执行工具）
export CLAUDE_MODEL=deepseek-v4-pro
export CLAUDE_API_KEY=sk-...
export CLAUDE_BASE_URL=https://api.deepseek.com/anthropic

# 学术 API
export SEMANTIC_SCHOLAR_API_KEY=s2k-...

# paperreview.ai
export PAPERREVIEW_EMAIL=250010008@slai.edu.cn
export PAPERREVIEW_VENUE=AAAI

# 流水线参数
export CHENRESEARCH_MAX_ITERATIONS=10
export CHENRESEARCH_POLL_INTERVAL=60
```

## 可用性验证（2026-05-27 实测通过）

| 组件 | 测试结果 |
|------|---------|
| paperreview.ai | 上传 ✓ / 查询 ✓ |
| SCO CLI | 集群 share-cluster (116 节点) ✓ / 提交 SUCCEEDED ✓ |
| arXiv API | 限流自动重试 ✓ |
| Semantic Scholar | 正常工作 ✓ |
| OpenAlex API | 正常返回 ✓ |
| 状态管理器 | 创建→流转→持久化→加载 ✓ |

## 依赖

| 依赖 | 用途 | 必需 |
|------|------|------|
| Python ≥ 3.10 + `requests` | 运行环境 | 是 |
| Claude Code CLI | 智能任务执行 | 是 |
| `sco` CLI | SenseCore GPU 实验 | 实验阶段 |
| `pdflatex` / `tectonic` | PDF 编译 | 论文撰写阶段 |

## 相关资源

- [paperreview.ai](https://paperreview.ai) — Stanford Agentic Reviewer
- [GitHub](https://github.com/2-chen/ChenResearch)
