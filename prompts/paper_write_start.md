你是一个学术论文撰写专家。请撰写完整的 AAAI 2026 格式论文。

研究主题: ${TOPIC}
会议: AAAI 2026

样式文件已放在 ${WORKSPACE}/paper/ 目录下（aaai2026.sty, aaai2026.bst）。
LaTeX 模板参考: templates/aaai.tex.j2

## 材料来源（必须全部读取）

1. **文献综述**: ${WORKSPACE}/literature/literature_review.md
2. **实验日志**: ${WORKSPACE}/experiment/experiment_log.md (15KB, 完整实验过程)
3. **实验数据**: ${WORKSPACE}/experiment/results/*/results.json (50+ 个独立实验的指标)
4. **实验图表**: ${WORKSPACE}/experiment/figures/ (6张PDF图表 + 1个LaTeX表格)

## 必须完成的任务

1. 撰写 LaTeX 论文，使用 \usepackage[submission]{aaai2026} 样式
2. **复制所有实验图表**: cp ${WORKSPACE}/experiment/figures/*.pdf ${WORKSPACE}/paper/figures/
3. **复制 LaTeX 表格**: cp ${WORKSPACE}/experiment/figures/table_results_cifar10.tex ${WORKSPACE}/paper/figures/
4. **论文中必须包含所有实验图表** — 6张 PDF 图和 1张 LaTeX 表全部插入正文
5. Preamble 必须包含: times, helvet, courier, natbib, caption, graphicx, booktabs, multirow (用于表格)
6. 禁止使用的包: hyperref, authblk, geometry, float, titlesec, setspace, fullpage, ulem
7. 所有数据必须来自真实实验日志和 results.json，不要编造
8. 保存到: ${WORKSPACE}/paper/paper.tex
9. 保存参考文献: ${WORKSPACE}/paper/references.bib
10. 用 pdflatex → bibtex → pdflatex × 2 编译
11. 修复所有 Overfull hbox 警告后再报告完成

**LaTeX 排版规范（必须遵守，AAAI 2026 是两栏排版）**:

**图片**:
- 栏内图片必须用 \includegraphics[width=\columnwidth]{...}
- 禁止不带 width 参数的 \includegraphics{}（原分辨率会溢出）
- 跨栏大图用 \begin{figure*}...\end{figure*} 配合 [width=\textwidth]
- 不要硬编码厘米/英寸（如 width=15cm），用 \columnwidth 或 \textwidth

**表格**:
- ≤4列的表格用 \resizebox{\columnwidth}{!}{\begin{tabular}{...}...\end{tabular}}
- 宽表用 \begin{table*}...\end{table*} 跨栏 + \resizebox{\textwidth}{!}{...}
- 禁止在 table 环境中使用没有 resizebox 包裹的宽 tabular
- booktabs 风格：\toprule / \midrule / \bottomrule，无竖线，无双重线

**公式**:
- 长公式用 \begin{aligned} 或 \begin{split} 断行，不要用单行公式溢出栏宽
- 禁止公式超出栏宽（约 3.25in / 240pt）

**通用**:
- 每个 figure/table 环境内必须有 \centering
- \textwidth 是整页宽 (~6.75in)，\columnwidth 是栏宽 (~3.25in)，在栏内用 \columnwidth
- 编译后检查 pdflatex 输出，修复所有 "Overfull \hbox" 警告
- 图表位置用 [t] (top) 或 [tb] 避免浮动到奇怪位置

完成后先编译检查无 Overfull hbox，再报告'论文撰写完成'。
