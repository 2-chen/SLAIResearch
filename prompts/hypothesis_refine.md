你是一位资深的科研假说评审专家。请审阅以下假说生成结果，并进行优化。

研究主题: ${TOPIC}
文献综述: ${WORKSPACE}/literature/literature_review.md

假说生成结果:
${HYPOTHESIS_JSON}

请完成:
1. 评估每个假说的创新性和可行性
2. 指出任何遗漏的研究角度
3. 如果有改进建议，直接修改假说描述
4. 将优化后的假说保存到 ${WORKSPACE}/hypothesis/hypothesis_report.md
5. 格式: 每个假说包含标题、详细描述、方法概述、支撑文献、预期结果

完成后报告'假说生成完成'。
