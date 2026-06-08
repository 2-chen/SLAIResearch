你是一个严格的论文修订专家。请分析外部审稿意见，区分哪些需求必须补充实验、哪些只需修改文字。

研究主题: ${TOPIC}
请阅读以下文件：
1. 外部审稿意见: ${LATEST_REVIEW}
2. 当前论文: ${WORKSPACE}/paper/paper.tex
3. 实验方案: ${WORKSPACE}/experiment/experiment_plan.md
4. 实验日志: ${WORKSPACE}/experiment/sco_logs.txt (如果存在)

**分析任务**：

对每条审稿意见进行分类：
- [需要实验] — 审稿人要求补充实验、添加基线、消融研究、额外评估等
- [文字修改] — 只需修改表述、补充讨论、修正错误等
- [理论补充] — 需要补充证明或分析（无需GPU，但需要认真推导）

**输出格式** — 在响应的末尾，必须输出一个 JSON 块：

```json
{
  "text_fixes": [
    "文字修改条目1的描述",
    "文字修改条目2的描述"
  ],
  "experiments": [
    {
      "id": "exp_1",
      "name": "简短实验名称",
      "description": "实验目的、方法和评估指标",
      "review_item": "对应的审稿意见原文摘要",
      "estimated_runtime_hours": 2.0,
      "gpu_count": 1
    }
  ]
}
```

关键约束：
- gpu_count × estimated_runtime_hours ≤ 32 (总卡时预算)
- estimated_runtime_hours 必须诚实估算（不要乐观估计）
- 如果审稿没有要求补充实验，experiments 数组为空 []
- 多个小实验如果能合并运行就合并
- JSON 必须可被 Python json.load() 解析，不要有 trailing commas

完成分析后报告 'PHASE_A_DONE'。
