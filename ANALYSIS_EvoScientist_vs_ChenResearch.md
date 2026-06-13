# EvoScientist vs ChenResearch — 深度对比分析报告

> 生成日期: 2026-06-13 | 分析范围: 全项目代码对比

---

## 总览

| 维度 | EvoScientist (v0.1.4) | ChenResearch | 差距评级 |
|------|----------------------|-------------|---------|
| 架构模式 | LangChain deepagents + 中间件栈 | Bash start.sh + Python 状态机 | 🔴 大 |
| 多智能体系统 | 6 个子智能体 + 异步部署 | 无（仅 claude -p 子进程调用） | 🔴 大 |
| 记忆系统 | 观察记忆 + 档案记忆 + 修剪检查点 | debug_memory + state.json | 🔴 大 |
| 错误处理 | 中间件层异常捕获 + 模型回退链 + 优雅降级 | 碰撞标记 + 断路器 + 重试 | 🟡 中 |
| 上下文管理 | 动态上下文编辑（50% 窗口触发）+ 工具选择器 | context_compressor.py（手动调用） | 🔴 大 |
| 技能/扩展系统 | 126 内置技能 + 3 层目录架构 + 技能管理器 | ~6 CLAUDE.md 技能 + 共享 Python 模块 | 🔴 大 |
| 文献检索 | Tavily 网页搜索 + 研究子智能体 | arXiv + Semantic Scholar + OpenAlex API | 🟢 互补 |
| 假设生成 | research-ideation 技能（发现+排序+建议） | hypothesis_engine.py（ReAct 循环，4 阶段） | 🟢 ChenResearch 更强 |
| 实验执行 | 后台进程管理 + 沙箱验证 | 本地优先 + SCO 云 + 调试循环 | 🟢 ChenResearch 更强 |
| 论文写作 | writing-agent 子智能体（异步） | LaTeX 模板 + revision_engine（章节修订循环） | 🟢 ChenResearch 更强 |
| 审稿系统 | paper-review 技能（自审） | 双轨审稿（外部 paperreview.ai + 5 内部审稿人并行） | 🟢 ChenResearch 更强 |
| 流水线编排 | 双循环（内循环实验 + 外循环综合） | 12 阶段线性流水线 + 阶段关卡 | 🟡 各有所长 |
| 配置系统 | 150+ 设置，4 级优先级，14+ LLM 提供商 | config.py，环境变量覆盖，单一提供商 | 🔴 大 |
| 部署与分发 | PyPI 包 + Docker + LangGraph 部署 + WebUI | 本地脚本 + start.sh | 🔴 大 |

---

## 一、架构模式 — 🔴 差距最大

### EvoScientist: 中间件栈 + 多智能体图

```
Main Agent (EvoScientist)
├── Middleware Pipeline (14 个中间件)
│   ├── ConfigurableModelMiddleware     — 会话内模型切换
│   ├── ContextEditingMiddleware        — 动态上下文编辑（50% 窗口触发）
│   ├── ModelFallbackMiddleware         — 模型回退链
│   ├── ContextOverflowMapperMiddleware — 上下文溢出预防
│   ├── ToolErrorHandlerMiddleware      — 工具异常捕获
│   ├── LLMToolSelectorMiddleware       — 每轮自适应工具选择（>26 工具时激活）
│   ├── CodeInterpreterMiddleware       — JS 沙箱
│   ├── RuntimeContextMiddleware        — 实时运行时信息注入
│   ├── EvoMemoryMiddleware             — 档案记忆注入
│   ├── EvoMemoryLifecycleMiddleware    — 后台记忆工作进程
│   ├── BackgroundExecutionMiddleware   — 操作系统进程管理
│   ├── AsyncWatcherMiddleware          — 异步任务通知
│   ├── AskUserMiddleware               — 智能体主动提问
│   └── HumanInTheLoopMiddleware        — 操作审批
├── Sub-Agents
│   ├── planner-agent     (规划 + 反思，仅 think_tool)
│   ├── research-agent    (网页检索，Tavily + think_tool)
│   ├── code-agent        (最小可复现代码实现)
│   ├── debug-agent       (根因分析 + 最小补丁)
│   ├── data-analysis-agent (异步，指标 + 图表)
│   └── writing-agent     (异步，结构化报告)
└── Backends
    ├── CustomSandboxBackend  (路径沙箱 + 命令验证)
    ├── MergedSkillsBackend   (3 层技能合并)
    └── FilesystemBackend     (记忆文件系统)
```

### ChenResearch: Bash 控制器 + Python 状态机

```
start.sh (3652 行 Bash)
├── 项目菜单 + 状态检测
├── 阶段路由 (_claude_task)
├── _call_claude_with_system_prompt (PTY 包装)
├── _do_submit_review (外部 + 内部并行)
├── _do_revise_and_resubmit (A/B1/B2/C 三阶段)
├── _internal_review_gate (预提交质量门)
├── _on_error / _auto_recover_if_crashed (碰撞恢复)
└── 碰撞标记 + 恢复计数 + 断路器

chenresearch.py (1742 行 Python CLI)
├── run / resume / status / list 命令
├── stage_reviewer 集成
├── revision_engine 集成
└── ContextCompressor (中段检测)
```

### 🔧 优化建议

1. **引入中间件栈架构**：将当前分散的错误处理、上下文管理、模型选择逻辑统一为中间件栈。每个中间件单一职责，可插拔。

2. **将 Bash 逻辑迁移到 Python**：3652 行 Bash 维护成本极高。建议将 start.sh 重构为 Python 流水线引擎，使用 asyncio 替代 bash 后台进程管理。

3. **引入子智能体系统**：当前所有 LLM 调用都是通过 `claude -p` 子进程完成（无状态、无上下文继承）。引入专门的子智能体（规划、检索、编码、调试、分析、写作），每个有独立的系统提示词和工具集。

---

## 二、记忆系统 — 🔴 差距最大

### EvoScientist: 三层记忆系统

| 记忆层 | 类型 | 存储 | 生命周期 |
|--------|------|------|---------|
| **Profile Memory** | SOUL.md, USER_PROFILE.md, RESEARCH_TASTE.md, PROJECT_PROFILE.md | Markdown 文件在 `/memories/profile/` | 持久，每轮由中间件更新 |
| **Observation Memory** | 语义型（事实）/ 程序型（命令配方）/ 情景型（事件） | Markdown 文件在 `/memories/observations/` | 持久，内容寻址去重 |
| **Session State** | 对话检查点 + 线程 | SQLite + PruningCheckpointer | 修剪保留最近 1000 检查点，DeltaChannel 感知 |

**关键特性**：
- **内容寻址去重**：SHA-256 哈希 ID，相同观察幂等保存
- **认知科学启发的分类**：语义/程序/情景 + 全局/项目范围
- **后台记忆工作进程**：每轮 + 子智能体级别，异步处理完成通知
- **DeltaChannel 感知修剪**：清除旧检查点时保留快照链，支持 /resume 功能

### ChenResearch: 两套独立系统

| 系统 | 作用 | 局限性 |
|------|------|--------|
| `debug_memory.py` | 持久化错误诊断记录（JSONL），支持签名匹配 + 相似度检索 | 仅覆盖错误场景，无通用知识积累 |
| `state_manager.py` | 12 阶段流水线状态（JSON），原子写入 | 仅存储流水线进度，不存储研究知识 |

### 🔧 优化建议

1. **引入通用观察记忆系统**（借鉴 `EvoScientist/memory/observations.py`）：
   - 使用 Markdown + YAML frontmatter 存储
   - 支持 semantic/procedural/episodic 分类
   - 内容寻址去重
   - 跨项目/项目内范围

2. **引入档案记忆**：
   - `RESEARCH_TASTE.md`：记录用户的研究标准、偏好方法、质量门槛
   - `PROJECT_PROFILE.md`：记录每个研究项目的约定和上下文
   - `ERROR_PATTERNS.md`：从 debug_memory 提取通用错误模式

3. **升级状态管理**：
   - 从单 JSON 文件改为 SQLite（借鉴 PruningCheckpointer）
   - 支持多线程/多进程并发访问
   - 引入自动修剪（防止无界增长）

---

## 三、上下文管理 — 🔴 差距最大

### EvoScientist: 多层级自动化

```
上下文编辑（50% 窗口触发）
  ↓
保留最近 5 个工具调用（排除 think_tool）
  ↓
工具选择器（>26 工具时激活，辅助模型选择相关工具）
  ↓
上下文溢出映射（85%+ 窗口触发摘要）
  ↓
运行时上下文注入（日期、cwd、项目路径，每轮注入）
```

### ChenResearch: 手动 + 被动

```
context_compressor.py（手动调用）
  ├── 软压缩（迭代摘要）
  ├── 硬压缩（完整恢复包）
  └── 中段检测（9 种材料类扫描）
```

ChenResearch 的核心设计理念是"每次调用都是新上下文"（通过 `claude -p`），这自然避免了上下文溢出，但也损失了跨调用的学习能力。

### 🔧 优化建议

1. **为长时间运行的智能体引入动态上下文编辑**：
   - 当上下文达到模型窗口的 50% 时自动触发工具调用清除
   - 保留最近 N 个关键工具调用
   - 排除 think_tool / 反思类调用

2. **引入自适应工具选择**：
   - 当工具数量 > 阈值时，用辅助模型预选相关工具
   - 始终保留核心工具（如 think_tool, task）

3. **添加运行时上下文注入**：
   - 每轮自动注入：当前日期、工作目录、项目路径、流水线阶段
   - 减少 LLM 对环境的猜测性错误

4. **保留 context_compressor 但增强自动化**：
   - 添加自动触发机制（检测到上下文使用率 > 阈值时自动压缩）
   - 支持增量合并（而非每次都重新生成完整文档）

---

## 四、错误处理与弹性 — 🟡 中等差距

### EvoScientist

| 机制 | 实现 |
|------|------|
| **ToolErrorHandlerMiddleware** | 捕获所有工具执行异常，返回 `ToolMessage(status="error")` + 完整回溯 |
| **ModelFallbackMiddleware** | 模型失败时按配置的回退链切换（跨提供商） |
| **上下文长度 / 格式错误分类** | 某些错误立即中止回退链（上下文溢出），认证错误可回退（另一提供商可能有有效凭证） |
| **沙箱超时恢复** | 退出码 124 → 建议更大超时或后台执行 |
| **优雅降级** | 工具选择器失败 → 使用所有工具；MCP 加载失败 → 跳过该服务器 |

### ChenResearch

| 机制 | 实现 |
|------|------|
| **碰撞恢复** (`_on_exit`) | EXIT 陷阱检测非零退出码 → Claude 诊断 → 自动重试（max 3） |
| **碰撞标记** (`.crash_markers/`) | 跨会话碰撞检测 |
| **SCO 调试循环** (`run_with_debug_loop()`) | 匹配错误 → FixDatabase → 应用修复 → 重新提交（max 20 轮） |
| **断路器** | 受保护文件的 MD5 校验，未授权修改 → 从 .cr_backup 恢复 |
| **阶段关卡重试** | 每阶段输出被审查，失败 → 注入反馈 → 重试（max 10） |
| **arXiv 429 重试** | 3 次，5s/10s/15s 回退 |

### 🔧 优化建议

1. **引入模型回退链**（借鉴 `ModelFallbackMiddleware`）：
   - 支持配置多个模型提供商（Anthropic, OpenAI, DeepSeek, 本地 Ollama）
   - 智能判断哪些错误可回退（认证错误可切换，上下文溢出不可切换）
   - 通过专用命令 `/model-fallback add` 动态调整回退链

2. **增强工具调用错误处理**：
   - 当前 `claude -p` 子进程失败时只有重试，没有结构化错误信息
   - 建议在 Python 层包装所有工具调用，返回结构化的错误消息（tool_name + traceback + recovery_hints）

3. **引入更智能的回退策略**：
   - 碰撞恢复目前是"全部恢复或全部失败"，应该支持部分恢复
   - 区分可恢复错误（网络超时、API 限流）和不可恢复错误（语法错误、配置缺失）

4. **增强 FixDatabase**：
   - 当前只有 1 个内置修复（opencv-typing-bug）
   - 添加自动学习机制：成功修复后自动注册到 FixDatabase
   - 支持跨项目修复共享

---

## 五、技能/扩展系统 — 🔴 差距最大

### EvoScientist: 126 内置技能 + 3 层架构

```
技能目录优先级（高 → 低）：
  1. workspace/skills/          # 项目本地，可写（覆盖优先级最高）
  2. ~/.evoscientist/skills/    # 全局用户，只读
  3. EvoScientist/skills/       # 内置 126 技能，只读

技能结构：
  skill-name/
  ├── SKILL.md       # 自文档化的工作流指令（AI 可读）
  └── (可选) scripts/  # 支持脚本

内置技能分类：
  - 研究生涯: autoresearch, research-ideation, experiment-pipeline, 
              paper-planning, paper-writing, paper-review, paper-rebuttal
  - 文献: literature-review, research-lookup, citation-management
  - 训练: axolotl, llama-factory, unsloth, deepspeed, moe-training,
          grpo-rl-training, verl-rl-training, openrlhf-training,
          distributed-llm-pretraining-torchtitan
  - 推理: serving-llms-vllm, sglang, llama-cpp, tensorrt-llm
  - 量化: awq-quantization, gptq, hqq-quantization, gguf-quantization, bitsandbytes
  - 评估: evaluating-llms-harness, evaluating-code-models
  - 微调: peft-fine-tuning, fine-tuning-with-trl
  - 多模态: stable-diffusion, whisper, clip, blip-2, llava
  - 知识: knowledge-distillation, model-merging, model-pruning
  - 可解释性: sparse-autoencoder-training, transformer-lens-interpretability,
              nnsight-remote, pyvene-interventions
  - 可观测性: weights-and-biases, tensorboard, mlflow, langsmith, phoenix
  - 元技能: find-skills, skill-creator
```

### ChenResearch: CLAUDE.md 中声明的 ~6 技能 + 共享 Python 模块

当前技能体系：
- `search-skill` — 文献检索
- `experiment-skill` — 实验执行
- `sco-skill` — SCO 远程 GPU
- `write-skill` — 论文写作
- `review-skill` — 论文审稿
- `autoresearch` — 全流水线编排
- `hypothesis-skill` — 假设生成
- `vpn-skill` — VPN/代理
- `evolution-skill` — 技能自进化

**差距**：
- 只有 9 个技能 vs EvoScientist 的 126 个
- 缺少 117 个领域专项技能（训练/推理/量化/评估/微调/多模态/可解释性/可观测性）
- 技能通过 CLAUDE.md 声明而非 SKILL.md 文件，不支持 3 层覆盖
- 无 skill-creator 元技能（无法自动创建新技能）

### 🔧 优化建议

1. **建立 3 层技能目录架构**：
   - 项目本地 (`workspace/skills/`) > 全局 (`~/.chenresearch/skills/`) > 内置
   - 每技能一个目录，包含 SKILL.md + 可选脚本

2. **批量导入高价值技能**：
   - **立即导入（训练）**：axolotl, llama-factory, unsloth, deepspeed, moe-training
   - **立即导入（推理）**：serving-llms-vllm, sglang, tensorrt-llm
   - **立即导入（评估）**：evaluating-llms-harness, evaluating-code-models
   - **立即导入（微调）**：peft-fine-tuning, fine-tuning-with-trl
   - **立即导入（量化）**：awq-quantization, gguf-quantization, bitsandbytes
   - **立即导入（可观测性）**：weights-and-biases, tensorboard
   - **逐步导入**：其余 100+ 技能按需添加

3. **添加 skill-creator 元技能**：
   - 让 AI 能在实验过程中自动创建和注册新技能
   - 从成功的工作流中提取可复用模式

4. **技能版本管理**：
   - 添加 SKILL.md 中的版本字段
   - 支持技能更新检测和升级

---

## 六、模型与提供商支持 — 🔴 差距最大

### EvoScientist: 200+ 模型，15+ 提供商

支持：Anthropic, OpenAI, Google, MiniMax, NVIDIA, SiliconFlow, OpenRouter, ZhipuAI, Volcengine, DashScope, DeepSeek, Moonshot, Kimi, Ollama, 自定义 OpenAI/Anthropic 兼容

**关键特性**：
- 自动配置 thinking/reasoning 参数（按提供商）
- 第三方提供商的 content format 修补
- 辅助模型支持（后台任务、工具选择器使用独立模型）
- 会话内模型切换（`/model` 命令）
- 上下文窗口自动检测（`get_context_window(model)`）

### ChenResearch: 单一提供商（DeepSeek v4-pro）

当前硬编码在 `start.sh` 和 `.claude/settings.json` 中：
```bash
--model deepseek-v4-pro
```

### 🔧 优化建议

1. **添加模型抽象层**：
   - 支持多提供商（至少 Anthropic + OpenAI + DeepSeek + 本地 Ollama）
   - 按任务选择模型（编程用 Sonnet/Opus，写作用 Opus，检索用 Haiku）

2. **引入辅助模型概念**：
   - 后台任务（记忆写入、工具选择、阶段审查）使用更便宜的模型
   - 主要研究任务使用最强模型
   - 节省成本 ≥ 50%

3. **支持会话内模型切换**：
   - 实验阶段可用便宜模型高频迭代
   - 写作阶段切换到最强模型

---

## 七、子智能体系统 — 🔴 差距最大

### EvoScientist: 6 个专门化子智能体

| 子智能体 | 工具集 | 模式 | 定义方式 |
|---------|-------|------|---------|
| **planner-agent** | 仅 think_tool | 同步 | planner.yaml（PLAN + REFLECTION 模式） |
| **research-agent** | Tavily + think_tool | 同步 | research.yaml（网页检索 + 反思循环） |
| **code-agent** | 完整编码工具 | 同步 | code.yaml（最小可复现代码） |
| **debug-agent** | 完整编码工具 | 同步 | debug.yaml（根因分析 + 最小补丁） |
| **data-analysis-agent** | 完整分析工具 | **异步** (LangGraph 部署) | data_analysis.yaml |
| **writing-agent** | 文件编辑工具 | **异步** (LangGraph 部署) | writing.yaml |

**关键特性**：
- YAML 定义文件（prompt + tools + skills + model + async 标志）
- 异步子智能体通过 LangGraph dev HTTP API 调用
- AsyncWatcherMiddleware 处理完成通知
- 计划与执行分离（planner 不能执行代码）

### ChenResearch: 无子智能体系统

当前所有任务通过 `claude -p "$(python prompt_render.py ...)"` 执行，每次都是全新的无状态调用。实验科学家模式（`experiment_scientist_system.md`）将规划、编码、调试、分析全压缩到一个 439 行的系统提示词中。

### 🔧 优化建议

1. **引入专门化的子智能体**：
   - **planner-agent**：仅负责实验规划和反思（不执行代码）
   - **research-agent**：专门的文献检索智能体（网页搜索 + PDF 阅读）
   - **code-agent**：最小可复现的实验代码实现
   - **debug-agent**：根因分析 + 最小修复
   - **data-analysis-agent**：异步运行，产出指标 + 图表
   - **writing-agent**：异步运行，产出结构化报告

2. **子智能体定义使用 YAML/JSON 格式**：
   - 统一的结构化定义（prompt + tools + skills + constraints）
   - 支持 `async: true` 标志（长时间运行的智能体部署为后台任务）

3. **计划与执行严格分离**：
   - planner-agent 工具集仅 `think_tool`，不能执行代码
   - 避免"边做边想"导致的计划漂移

4. **异步子智能体用于 I/O 密集型任务**：
   - 论文写作、数据分析、文献检索可并行执行
   - 使用回调/通知机制处理完成事件

---

## 八、流水线编排 — 🟡 各有所长

### EvoScientist: 双循环架构

```
内循环 (实验节奏, 分钟~小时)
  Pick hypothesis → Write protocol → Commit → Run experiment → 
  Measure → Record → Learn → Repeat

外循环 (综合节奏, 每 5-10 实验)
  Review results → Find patterns → Update findings.md → 
  Generate new hypotheses → Decide direction (DEEPEN/BROADEN/PIVOT/CONCLUDE)
```

**关键特性**：
- Git 预注册：协议提交必须先于结果提交
- 确认性 vs 探索性分析区分
- findings.md 作为跨会话记忆
- 通过 `/loop 10m` 维持智能体连续性

### ChenResearch: 12 阶段线性流水线 + 阶段关卡

```
LITERATURE → HYPOTHESIS → BASELINE → EXPERIMENT_DESIGN → 
ENV_PREP → EXPERIMENT_EXECUTION → PAPER_WRITING → 
SUBMIT_REVIEW → POLL_REVIEW → REVISE → RESUBMIT → DONE
```

**关键特性**：
- 每阶段有独立的审查关卡（pass threshold 5.5-7.0）
- 双轨审稿（外部 paperreview.ai + 内部 5 人并行）
- 三阶段修订（A: 分析 → B1: 补充实验代码 → B2: SCO 并行提交 → C: 论文更新）
- 碰撞恢复 + 断路器

### 🔧 优化建议

1. **引入双循环架构作为可选模式**：
   - 内循环：快速实验迭代（适合需要大量实验验证的假设）
   - 外循环：定期综合结果、调整研究方向
   - 保留线性流水线作为"单次研究"模式

2. **引入 Git 预注册机制**：
   - 实验协议提交 → 执行 → 结果提交
   - 确保实验可复现性和时间戳证明

3. **添加 findings.md 作为跨阶段记忆**：
   - 每个阶段的关键发现写入 findings.md
   - 后续阶段（尤其是假设生成和论文写作）读取 findings.md
   - 避免信息在阶段间丢失

4. **让阶段流水线可配置**：
   - 当前阶段顺序硬编码在 `_first_pending_stage()` 中
   - 支持跳过阶段（如跳过文献检索直接从假设开始）
   - 支持阶段并行（如文献检索和基线获取可并行）

---

## 九、具体差距与优先级矩阵

| 优化项 | 影响 | 难度 | 优先级 |
|--------|------|------|--------|
| 引入子智能体系统（planner + code + debug） | 🔴 极高 | 高 | **P0 — 立即** |
| 建立 3 层技能架构 + 批量导入领域技能 | 🔴 极高 | 中 | **P0 — 立即** |
| 引入观察记忆系统（内容寻址，跨实验学习） | 🔴 极高 | 中 | **P0 — 立即** |
| 引入模型回退链 + 多提供商支持 | 🟡 高 | 低 | **P1 — 本月** |
| 动态上下文编辑（50% 窗口触发 + 工具选择器） | 🟡 高 | 中 | **P1 — 本月** |
| 将 start.sh 迁移到 Python 流水线引擎 | 🟡 高 | 高 | **P1 — 本月** |
| 引入辅助模型（后台任务低成本模型） | 🟢 中 | 低 | **P2 — 下月** |
| 双循环架构（内循环实验 + 外循环综合） | 🟢 中 | 中 | **P2 — 下月** |
| Git 预注册机制 | 🟢 中 | 低 | **P2 — 下月** |
| 配置系统升级（150+ 设置，4 级优先级） | 🟢 低 | 中 | **P3 — 择机** |
| 异步子智能体部署（LangGraph dev） | 🟢 低 | 高 | **P3 — 择机** |
| SQLite 状态管理（替代单 JSON 文件） | 🟢 低 | 中 | **P3 — 择机** |
| CLI/TUI 交互界面 | 🟢 低 | 高 | **P3 — 择机** |

---

## 十、ChenResearch 优于 EvoScientist 的方面（保持优势）

以下领域 ChenResearch 已经领先或持平，建议保持并继续增强：

1. **假设生成引擎** (`hypothesis_engine.py`)：4 阶段 ReAct + PDF 深度阅读 + 证据提取，比 EvoScientist 的 research-ideation 技能更系统化

2. **实验执行** (`sco_runner.py`)：本地优先 + SCO 云 + 调试循环 + 断路器，比 EvoScientist 的后台进程管理更适合 GPU 集群场景

3. **论文写作** (`revision_engine.py`)：章节级修订循环 + 回压 + 元精炼 + 收敛检测 + 接地保护，比 EvoScientist 的 writing-agent 更精细

4. **审稿系统** (`internal_review.py` + `review_synthesis.py`)：双轨审稿（外部 + 5 内部并行） + 跨源综合，比 EvoScientist 的 paper-review 技能更全面

5. **FixDatabase** (`fix_db.py`) + **DebugMemory** (`debug_memory.py`)：已知错误模式匹配 + 持久化诊断记录，EvoScientist 没有等效功能

6. **受保护文件断路器**：防止 LLM 损坏关键基础设施，EvoScientist 没有等效安全机制

7. **SCO 检查点恢复**：跨会话恢复 SCO 作业状态，EvoScientist 的后台进程管理不支持跨会话

8. **PTY 包装器** (`claude_pty.py`)：消除 Node.js 管道缓冲，EvoScientist 使用 LangGraph 自有流式处理

---

## 十一、实施路线图建议

### 第 1 阶段（1-2 周）：补齐基础设施
1. 引入子智能体系统（最小可行：planner + code + debug）
2. 建立 3 层技能目录架构
3. 从 EvoScientist 导入 20-30 个核心领域技能（训练/推理/评估/微调/量化）

### 第 2 阶段（2-4 周）：增强记忆与弹性
4. 引入观察记忆系统（Markdown + YAML frontmatter + 内容寻址）
5. 引入档案记忆（RESEARCH_TASTE.md, PROJECT_PROFILE.md）
6. 实现模型回退链 + 多提供商支持
7. 引入动态上下文编辑

### 第 3 阶段（4-6 周）：升级编排与运维
8. 将 start.sh 核心逻辑迁移到 Python
9. 引入辅助模型概念
10. 实现双循环实验架构（可选模式）
11. 配置系统重构（多提供商 + 环境变量映射）
12. 阶段流水线可配置化

### 第 4 阶段（6-8 周）：高级特性
13. 异步子智能体部署
14. SQLite 状态管理
15. CLI/TUI 交互界面
16. 技能自动发现与注册（skill-creator）
17. Git 预注册机制

---

## 附录：文件对照表

| EvoScientist 文件 | ChenResearch 对应 | 状态 |
|-------------------|-------------------|------|
| `EvoScientist/EvoScientist.py` | `start.sh` + `chenresearch.py` | 需重构 |
| `middleware/context_editing.py` | `context_compressor.py`（手动） | 需增强 |
| `middleware/tool_error_handler.py` | 分散在 start.sh 中 | 需新增 |
| `middleware/model_fallback.py` | 无 | 需新增 |
| `middleware/tool_selector.py` | 无 | 需新增 |
| `memory/observations.py` | `debug_memory.py`（仅错误） | 需大幅增强 |
| `sessions.py` (PruningCheckpointer) | `state_manager.py`（JSON） | 需升级 |
| `backends.py` (Sandbox + Skills) | `sco_runner.py`（沙箱部分） | 需增强 |
| `prompts.py` | `prompts/` 目录 | 需模块化 |
| `config/settings.py` | `config.py` | 需扩展 |
| `subagents/*.yaml` | 无 | 需新增 |
| `skills/` (126 技能) | CLAUDE.md 中的 9 技能 | 需大幅扩展 |
| `cli/` (33 文件) | `menu.py`（126 行） | 需新增 |
| `llm/models.py` | `.claude/settings.json` | 需新增 |
