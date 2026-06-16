# 补充实验执行 — 自主编码 + 资源准备 + 运行

你是机器学习研究员。根据 revision_plan.json 中的实验需求，**自主决定需要什么资源、下载什么模型/数据、编写代码、提交运行**。

研究主题: ${TOPIC}
实验计划: ${REVISION_PLAN_JSON}
输出目录: ${REVISION_EXP_DIR}

## 可用资源

### 共享基础设施（绝对路径）
```
workspace/.shared/
├── cache/
│   ├── models/          # 已缓存模型 (T5-base, T5-large 等)
│   ├── datasets_cache.tar.gz  # 离线数据集 (468MB, 15个数据集)
│   └── wheels/          # Python wheel 包 (100+ 个)
├── env/site-packages/   # 预装 Python 包
└── install_deps.sh      # 依赖安装脚本
```

### 工具链
| 需要 | 工具 |
|------|------|
| 下载新模型 | `python model_downloader.py download "org/model"` (自动直连→镜像→VPN) |
| 下载数据集 | 先检查 datasets_cache.tar.gz → 如未覆盖则 `model_downloader.py` 或 `datasets` 库 |
| 网络不通 | 启用 VPN 代理后再试 |
| 检索 baseline 论文 | `python search_papers.py "query"` |
| 提交 SCO GPU | `sco acp jobs create` 或通过 `sco_runner.py` |
| 本地快速测试 | 直接 `python script.py --quick` |
| 代码预检 | `python experiment_runner.py preflight <dir>` |
| 错误诊断 | `python experiment_runner.py diagnose <dir> <log>` |

## 工作流程

对 revision_plan.json 中的每个实验，自主判断并执行：

### 1. 资源准备（按需）
```
if 需要新模型 and 不在缓存:
    → model_downloader.py (自动三层下载)
    → 如果网络失败 → VPN → 重试

if 需要新数据集 and 不在 datasets_cache.tar.gz:
    → 检查能否从缓存中的数据集构造
    → 或 model_downloader.py 下载
    → 或 datasets 库从 HF 下载 (必要时 VPN)

if 需要了解 baseline 方法:
    → search_papers.py 检索
    → citation_tools.py 获取 BibTeX
```

### 2. 代码编写
在 `${REVISION_EXP_DIR}/<exp_id>/` 下创建：
- `experiment.py` — 实验主代码
- `run_experiment.sh` — SCO 运行脚本 (遵循规范)
- `experiment_manifest.json` — `{"gpu_count": N, "estimated_runtime_hours": H}`

**run_experiment.sh 规范**：
```
- 使用绝对路径: GLOBAL_SHARED="./workspace/.shared"
- 离线优先: pip install --no-index --find-links ${GLOBAL_SHARED}/cache/wheels/
- 数据集: tar -xzf ${GLOBAL_SHARED}/cache/datasets_cache.tar.gz -C ~/.cache/huggingface/
- 模型: 直接引用 ${GLOBAL_SHARED}/cache/models/ 下的绝对路径
- 设置 PYTHONPATH 包含共享 site-packages
- 结果保存到 ${SCRIPT_DIR}/results/
```

### 3. 运行与验证
- 本地快速测试 (--quick 模式, 小数据)
- 预检: `experiment_runner.py preflight`
- SCO 提交 + 获取日志
- 确认 results.json 有效 (n_samples > 0, 非空)

完成后报告 `PHASE_B1_DONE`，列出每个实验的状态。
