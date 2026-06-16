你是一个机器学习研究员。请基于文献综述${HYPOTHESIS_CLAUSE}设计实验方案。

研究主题: ${TOPIC}
文献综述: ${WORKSPACE}/literature/literature_review.md
${HYPOTHESIS_LINE}
${BASELINE_CONTEXT}

## 共享基础设施（所有路径必须用绝对路径，不可用相对路径拼接 ROOT_DIR）
- 全局共享缓存: workspace/.shared/cache/
- 预装包路径: env/site-packages/ (datasets, accelerate, sklearn 可从此加载)
- 模型下载: source workspace/.shared/download_model.sh (已内置三层回退)
- SCO 容器无网络，pip install 必须用 --no-index --find-links workspace/.shared/cache/wheels/
- 数据集: tar -xzf workspace/.shared/cache/datasets_cache.tar.gz -C ~/.cache/huggingface/

请完成以下任务：
1. ${DESIGN_STEP_1}
2. 设计完整的实验方案，包括：研究问题和假设、方法/模型详细描述、数据集选择、基线方法、评估指标、实验配置（超参、硬件）、消融实验设计
3. 编写可执行的 Python 实验代码
4. 编写 run_experiment.sh（包含环境设置、依赖安装、实验执行的全部命令）
5. 保存实验方案到: ${WORKSPACE}/experiment/experiment_plan.md
6. 保存代码和脚本到: ${WORKSPACE}/experiment/
7. 创建 experiment_manifest.json，声明以下字段：
   - gpu_count：需要的 GPU 数量
     - 你有 4 张 N6LS-80G GPU，目标是充分利用它们压缩端到端实验时间
     - 并行策略（按优先级，应逐项考虑）：
       a) 任务级并行：不同 baseline / 不同消融实验分配到不同 GPU 同时跑，各自设置 CUDA_VISIBLE_DEVICES
       b) 数据并行：训练用 DataParallel 或 DistributedDataParallel 加速单任务
       c) 推理并行：认证/评估阶段多 GPU 分片处理不同数据子集
     - gpu_count 默认填 4；仅当实验完全无法并行时才填 1
   - estimated_runtime_hours：预估总运行时间（小时）
     - 单任务算力预算上限为 32 卡时（gpu_count × estimated_runtime_hours ≤ 32）
     - 例如 4 卡 × 8h = 32 卡时（OK），4 卡 × 12h = 48 卡时（超预算会被拒绝）
     - 诚实估算整体实验时间；如果超预算，减少实验规模或增加并行度
     - 示例：{"gpu_count": 4, "estimated_runtime_hours": 6.0}
8. run_experiment.sh 规范：
   - 读取 $GPU_COUNT 环境变量，据此动态设置 CUDA_VISIBLE_DEVICES（不要硬编码为 0）
   - GPU_COUNT>=2 时，独立子任务必须并行启动（后台进程 + wait），不能串行逐个跑
   - 训练脚本内部使用 DataParallel 时，传入可见 GPU 数量
   - 如需下载预训练模型，使用 source ${WORKSPACE}/../../workspace/.shared/download_model.sh 提供的 download_hf_model 函数

重要：只做实验设计，不要做其他事情。完成后明确报告'实验设计完成'。
