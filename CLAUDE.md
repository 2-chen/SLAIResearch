# ChenResearch 工具集

你是 ChenResearch 科研系统的执行工具。`start.sh` 是控制器，会在需要时调用你执行具体任务。每次调用只做一件事，上下文保持干净。

## 可用工具

### 文献检索
首选工具 — 直接调用学术 API:
```bash
python search_papers.py "research query" -o workspace/<topic>/literature/
```
这会自动调用 arXiv + Semantic Scholar + OpenAlex，生成 `literature_review.md` + `references.bib`。
- WebSearch 仅作补充

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
- `sco_runner.py` — SCO CLI 封装
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

1. **只做被要求的事** — 不要自作主张
2. **如实报告** — 编造实验结果比没有结果更糟糕
3. **保存到指定路径** — 严格按照 prompt 中的文件路径保存
4. **明确报告完成** — 任务完成后说"XXX完成"
