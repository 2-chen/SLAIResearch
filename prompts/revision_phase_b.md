你是一个机器学习研究员。请根据审稿意见中要求的补充实验编写完整的实验代码。

研究主题: ${TOPIC}
实验需求: ${REVISION_PLAN_JSON}

请阅读以下文件：
1. 当前实验代码: ${WORKSPACE}/experiment/ (了解现有代码结构)
2. 当前论文: ${WORKSPACE}/paper/paper.tex
3. 修订计划: ${REVISION_EXP_DIR}/revision_plan.json

## 共享基础设施（必须使用绝对路径，不可用相对路径拼接）

- 全局共享缓存: /data/AutoResearch/ChenResearch/workspace/.shared/cache/
  ├── models/google-t5--t5-base/   (T5-base 模型)
  ├── models/google-t5--t5-large/  (T5-large 模型)
  ├── datasets_cache.tar.gz        (所有15个数据集, 468MB)
  └── wheels/                      (Python wheel 包, 100+ 个)
- 预装包路径: /data/AutoResearch/ChenResearch/env/site-packages/ (datasets, accelerate, sklearn 等)
- SCO 容器无网络，禁止 pip install 从网络下载，必须使用离线 wheel 缓存或 site-packages
- 数据集: 从 tarball 解压到 ~/.cache/huggingface/ (AFS 不支持 flock，必须解压到本地 ext4)

**任务**：
为每个补充实验编写代码和运行脚本。对于 revision_plan.json 中的每个实验：

1. 在 ${REVISION_EXP_DIR}/<exp_id>/ 下创建独立的实验子目录
2. 编写 Python 实验代码
3. 编写 run_experiment.sh
4. 创建 experiment_manifest.json：{"gpu_count": N, "estimated_runtime_hours": H}

**run_experiment.sh 规范（关键）**：
- 第一行: GLOBAL_SHARED="/data/AutoResearch/ChenResearch/workspace/.shared" （绝对路径，不可用 ROOT_DIR 拼接）
- 设置 PYTHONPATH 包含共享 site-packages: export PYTHONPATH="${GLOBAL_SHARED}/../env/site-packages:${PYTHONPATH:-}"
- 数据集: tar -xzf ${GLOBAL_SHARED}/cache/datasets_cache.tar.gz -C ~/.cache/huggingface/
- 模型: 使用 ${GLOBAL_SHARED}/cache/models/google-t5--t5-large/ 等绝对路径
- 依赖安装: 离线优先 — pip install --no-index --find-links ${GLOBAL_SHARED}/cache/wheels/ <packages>
- 不需要的包(torch, transformers, numpy)从容器的系统路径加载，不要重复安装
- 读取 $GPU_COUNT 设置 CUDA_VISIBLE_DEVICES
- **复制主实验的 Python 模块**: cp ${主实验目录}/*.py ${SCRIPT_DIR}/ (models.py, data_utils.py 等)
- 必要时在 shell 脚本开头设置 PYTHONPATH 包含主实验目录
- 结果保存到 ${SCRIPT_DIR}/results/
- 用 echo "EXPERIMENT_DONE" 标记完成

完成后报告 'PHASE_B1_DONE'。
