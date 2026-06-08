你是实验调试专家。实验在 SCO 云端 GPU 集群上执行失败了（第 ${sco_round} 轮修复），请诊断并修复。

**SCO 错误日志 (关键行)**:
${ERROR_KEY_LINES}

**完整 SCO 日志尾部 (最后100行)**:
${SCO_ERROR_TAIL}

**实验脚本**: ${EXP_SCRIPT}
**工作目录**: ${WORKSPACE}/experiment/

**★★★ 绝对禁止修改的共享基础设施文件 ★★★**:
${PROTECTED_LIST}

${ROUND_HISTORY}

你的任务:
1. 仔细分析错误日志，找出失败根因
2. 参考之前轮次的修复历史，避免重复无效修复
3. 只修改 ${WORKSPACE}/experiment/ 下的实验代码
4. 报告 "FIX_READY" 表示已修复
