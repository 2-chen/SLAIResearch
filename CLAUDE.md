# SLAIResearch 工具集

你是 SLAIResearch 科研系统的执行工具。`start.sh` 是控制器，会在需要时调用你执行具体任务。每次调用只做一件事，上下文保持干净。

## 执行模式

### 实验环节 — Claude Code 全权接管
实验环节（从实验设计到结果产出）由 Claude Code 完全自主执行，不再由硬编码流程控制。

**系统提示词**: `prompts/experiment_scientist_system.md`
**任务模板**: `prompts/experiment_scientist_task.md`

控制器调用方式：
```bash
claude -p "$(python prompt_render.py prompts/experiment_scientist_task.md ...)" \
       --system-prompt prompts/experiment_scientist_system.md \
       --model deepseek-v4-pro \
       --output-format text
```

Claude Code 自主完成：环境检查 → 实验设计 → 代码编写 → 本地/SCO 执行 → 调试修复 → 评估迭代 → 结果报告。

### 文献检索（控制器直接调度）
```bash
python search_papers.py "research query" -o workspace/<topic>/literature/
```
- 自动调用 arXiv + Semantic Scholar + OpenAlex
- 可选: `--google-scholar`、`--datacite`、`--year-start/end`、`--summary`

### 引用工具（全部免费，无需 API Key）
```bash
python citation_tools.py doi-to-bibtex <DOI>            # CrossRef -> BibTeX
python citation_tools.py extract --doi <DOI>             # 结构化元数据提取
python citation_tools.py extract --arxiv <arXiv_ID>      # arXiv ID -> 元数据
python citation_tools.py verify --file <markdown_file>   # DOI 验证 + APA/Nature 引用格式化
python citation_tools.py scholar "<query>"               # Google Scholar 搜索
python citation_tools.py datacite <DOI_or_query>          # DataCite 数据集/软件 DOI 查询
```

### 实验辅助工具（Claude Code 可选调用）
- `python experiment_runner.py preflight <dir>` — 实验代码预检（语法/导入/结构）
- `python experiment_runner.py diagnose <dir>` — 增强错误诊断 + debug memory
- `python sco_runner.py run <dir>` — 程序化 SCO 作业提交（可选，推荐直接用 sco CLI）

### SCO 云端 GPU
- `sco acp jobs create` — 提交训练任务
- `sco acp jobs describe --workspace-name share-space <id>` — 查询状态
- `sco acp jobs stream-logs --workspace-name share-space <id>` — 获取日志
- 默认配置: share-cluster / 4x N6LS-80G / afs-share-01g

### 论文工具
- `pdflatex` — 编译 LaTeX
- AAAI 2026 模板: `templates/aaai.tex.j2` (Jinja2) + `templates/aaai2026.sty` + `templates/aaai2026.bst`
- 编译前必须把 `aaai2026.sty` 和 `aaai2026.bst` 复制到 paper 目录
- `python internal_review.py paper.pdf` — 内部多维度审稿（5位审稿人）

### Python 工具
- `paperreview_api.py` — paperreview.ai 上传和轮询
- `sco_runner.py` — SCO CLI 封装（Claude Code 可选调用）
- `state_manager.py` — 状态持久化
- `config.py` — 统一配置
- `review_tools.py` — 自动化审稿检查（AI痕迹、引用覆盖、文献对比）
- `revision_engine.py` — 逐章节修订循环（backpressure + grounding）
- `literature_context.py` — 文献横向对比上下文构建
- `figure_generation.py` — 发表级图表生成（matplotlib + booktabs）

## 图表标准

- 使用 `python figure_generation.py bar ...` 或直接调用 `FigureGenerator` 生成图表
- 图表输出到 `paper/figures/`，PDF 矢量格式
- 表格使用 booktabs 风格：`\toprule`/`\midrule`/`\bottomrule`，无竖线
- 通过 `FigureGenerator.figure_checklist()` 检查每张图是否符合标准

## 规则

1. **实验环节驱动方式**: Claude Code 以实验科学家系统提示词自主执行，不依赖硬编码流程
2. **如实报告** — 编造实验结果比没有结果更糟糕
3. **保存到指定路径** — 严格按照 prompt 中的文件路径保存
4. **明确报告完成** — 任务完成后说"XXX完成"
5. **受保护文件** — `sco_runner.py`、`config.py`、`workspace/.shared/` 不可修改
