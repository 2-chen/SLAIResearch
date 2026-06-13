# 实验任务

你收到一个研究实验任务。请按照你的实验科学家系统提示词，自主完成完整流程：理解假设 → 设计实验 → 检查环境 → 编写代码 → 执行 → 调试 → 评估 → 报告。

## 输入文件

请先阅读以下文件：

- **研究假设**: `${HYPOTHESIS_FILE}`
- **文献综述**: `${LITERATURE_FILE}`
${BASELINE_SECTION}

## 输出目录

所有产物放在: `${OUTPUT_DIR}`

请确保该目录存在，并在其中创建标准的子目录结构（logs/, checkpoints/, results/, figures/）。

## SCO 云端 GPU 配置

如果本地无 GPU 或实验需要，可使用以下 SCO 配置提交云端作业：

| 参数 | 值 |
|------|-----|
| Workspace | `${SCO_WORKSPACE}` |
| AEC2 (集群) | `${SCO_AEC2}` |
| 容器镜像 | `${SCO_IMAGE}` |
| 存储挂载 | `${SCO_STORAGE_MOUNT}` |

Worker Spec 按 GPU 数量:
| GPU 数 | Spec |
|--------|------|
| 1 | `${SCO_WORKER_SPEC_1GPU}` |
| 2 | `${SCO_WORKER_SPEC_2GPU}` |
| 4 | `${SCO_WORKER_SPEC_4GPU}` |

## 计算预算

**最大 GPU-hours: ${MAX_GPU_HOURS}**

即 `gpu_count × estimated_runtime_hours ≤ ${MAX_GPU_HOURS}`。如有超出风险，优先减少 GPU 数量或数据规模。

## 共享缓存

以下缓存目录在 SCO 容器中可用：
- **模型缓存**: workspace/.shared/cache/models/（HuggingFace 等）
- **数据集**: workspace/.shared/cache/datasets/（常用数据集 tar.gz）
- **Wheels**: workspace/.shared/cache/wheels/（预下载的 pip 包）

运行前先检查缓存中是否已有需要的资源。

## 调试配置

- 最大调试轮数: ${MAX_DEBUG_ROUNDS}
- 本地执行超时: ${LOCAL_TIMEOUT}s
- 本地最大重试: ${LOCAL_MAX_RETRIES}

## 受保护文件

以下文件**绝不修改**，如需修改先报告：
- `sco_runner.py`
- `config.py`
- `workspace/.shared/` 下的所有文件
- `install.sh`
- `.chenresearch_protected` 中列出的所有文件

## 开始

现在开始你的实验工作。记住：先读输入 → 检查环境 → 写计划 → 小规模试探 → 全量运行 → 报告结果。
