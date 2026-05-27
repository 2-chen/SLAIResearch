# ChenResearch 交互指南

启动后直接和 Agent 对话。以下展示典型交互流程。

## 场景 1: 从零开始研究

```
你: 研究 federated learning with differential privacy for medical imaging

Agent:
  ╔══════════════════════════════════════════════╗
  ║        ChenResearch — 全自动科研系统         ║
  ╚══════════════════════════════════════════════╝

  ━━━ Stage 1/4: 文献检索 ━━━
  arXiv + Semantic Scholar + OpenAlex 三方检索...
  ✓ 找到 23 篇论文
  ✓ 文献综述已保存

  ━━━ Stage 2/4: 实验设计 ━━━
  Claude Code 设计实验方案...
  ✓ 数据集: MIMIC-III, CheXpert
  ✓ 基线: DP-SGD, FedAvg, FedProx
  ✓ 指标: AUC, privacy budget ε
  ✓ 实验脚本已保存

  ━━━ Stage 3/4: SCO 云端实验 ━━━
  提交 GPU 任务... Job ID: pt-xxx
  状态: RUNNING → SUCCEEDED
  ✓ 实验完成

  ━━━ Stage 4/4: 论文撰写 ━━━
  撰写 AAAI 2026 LaTeX 论文...
  ✓ paper.tex 已保存
  ✓ PDF 编译成功

  ━━━ 提交审稿 ━━━
  ✓ paperreview.ai Token: abc123...
  ✓ 内部 5 位审稿人开始评审...

  [等待 paperreview.ai...]

  审稿结果: Borderline
  审稿意见已保存到 review/
  请运行 bash start.sh 继续修订
```

## 场景 2: 修订迭代

```
$ bash start.sh
  进行中的项目: Federated Learning with DP for Medical Imaging
  当前阶段: poll_review (第 1 轮迭代)
  ⚡ 检测到审稿意见待处理 — 将进入修订模式

你: 继续

Agent:
  ━━━ 修订迭代 #2 ━━━
  读取审稿意见...
  读取文献综述...
  读取当前论文...

  审稿意见分析:
  - 方法论专家: 6/10 — 缺少隐私预算的理论分析
  - 实验评估专家: 5/10 — 需要更多 baseline（添加强基线 X, Y）
  - 写作表达专家: 7/10 — 图表需要改进
  - 文献覆盖度专家: 5/10 — 缺少论文 A, B, C
  - 魔鬼辩护人: 4/10 — 对 ε=8 的安全性存疑

  → 补充实验：添加 baseline X, Y
  → 补充理论分析：ε 与模型效用关系
  → 添加缺失引用
  → 修订 paper.tex

  ✓ 修订完成
  ✓ 重新提交 paperreview.ai
  Token: def456...

  [等待审稿...]

  审稿结果: Weak Accept ★
```

## 场景 3: 仅做文献综述

```
你: 帮我做 literature review on diffusion models for molecular generation

Agent:
  ━━━ 文献检索 ━━━
  ✓ 找到 35 篇论文
  ✓ 文献综述已保存到 workspace/.../literature/
  
  你可以查看:
  workspace/.../literature/literature_review.md
  workspace/.../literature/references.bib
```

## Agent 理解的命令

| 你说 | Agent 做什么 |
|------|------------|
| "研究 X" | 启动完整流水线 |
| "继续" (有进行中项目) | 从上次中断处继续 |
| "只看文献" / "literature review on X" | 只做文献检索 |
| "帮我写论文" | 基于已有文献和实验数据写论文 |
| "审稿意见怎么看" | 读取并总结审稿意见 |
| "重新提交" | 重新编译 PDF 并提交审稿 |
