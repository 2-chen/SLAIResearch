你是一个机器学习研究员。请根据审稿意见中要求的补充实验编写完整的实验代码。

研究主题: ${TOPIC}
实验需求: ${REVISION_PLAN_JSON}

请阅读以下文件：
1. 当前实验代码: ${WORKSPACE}/experiment/ (了解现有代码结构)
2. 当前论文: ${WORKSPACE}/paper/paper.tex
3. 修订计划: ${REVISION_EXP_DIR}/revision_plan.json

**任务**：
为每个补充实验编写代码和运行脚本。对于 revision_plan.json 中的每个实验：

1. 在 ${REVISION_EXP_DIR}/<exp_id>/ 下创建独立的实验子目录
2. 编写 Python 实验代码
3. 编写 run_experiment.sh（必须包含：环境设置、依赖安装、实验执行）
4. 创建 experiment_manifest.json：{"gpu_count": N, "estimated_runtime_hours": H}
   - gpu_count 根据实验实际需要填写（默认 1）
   - gpu_count × estimated_runtime_hours ≤ 32

**run_experiment.sh 规范**：
- 读取 $GPU_COUNT 环境变量设置 CUDA_VISIBLE_DEVICES
- 必须包含 pip install 需要的依赖
- 结果保存到当前目录下的 results/ 子目录
- 用 echo "EXPERIMENT_DONE" 标记完成

完成后报告 'PHASE_B1_DONE'。
