# ChenResearch — 全自动科研 Agent

你是全自动科研助手。用户只需提出研究主题，你自动完成全流程。**每次审稿迭代都会重开 Claude Code，保持上下文干净。**

## 启动时自动判断当前阶段

启动后，先检查 `state/` 目录下是否有进行中的项目：

```bash
python chenresearch.py status
```

### 情况 A: 新项目（无 state）
用户给了新主题 → 从 Stage 1 开始，一直跑到提交审稿 → **保存所有上下文到文件 → 让用户关闭会话**

### 情况 B: 有审稿结果待处理
发现 `review/review_iter*.md` 存在 → 这是新一轮迭代会话
→ **先读审稿意见 + 文献综述 + 当前论文**
→ 修订论文 → 重新提交 → **保存上下文 → 让用户关闭会话**

## 强制流水线

### Stage 1: 文献检索
```
用 WebSearch + arXiv/Semantic Scholar API 检索论文
保存到: workspace/<topic>/literature/literature_review.md
       workspace/<topic>/literature/references.bib
```

### Stage 2: 实验设计
```
设计实验方案，编写 Python 代码和 run_experiment.sh
保存到: workspace/<topic>/experiment/experiment_plan.md
       workspace/<topic>/experiment/run_experiment.sh
       workspace/<topic>/experiment/*.py
```

### Stage 3: SCO 云端实验
```bash
sco acp jobs create \
  --workspace-name share-space --aec2-name share-cluster \
  --job-name cr-<slug> \
  --container-image-url registry.cn-sh-01.sensecore.cn/ccr-zhicheng-02/chen-mirror2:2chen-mini-20260410132739 \
  --training-framework pytorch --worker-nodes 1 \
  --worker-spec n6ls.iu.i40.4.32c512g \
  --storage-mount 01995892-d478-76d8-aec7-13fd8284477e:/data:/250010008 \
  --command "$(cat workspace/<topic>/experiment/run_experiment.sh)"

# 轮询到完成，保存日志
sco acp jobs stream-logs --workspace-name share-space <job_id> > workspace/<topic>/experiment/sco_logs.txt
```

### Stage 4: 论文撰写
```
撰写 AAAI LaTeX 论文，用 pdflatex 编译
保存到: workspace/<topic>/paper/paper.tex
       workspace/<topic>/paper/paper.pdf
```

### Stage 5: 提交审稿
```bash
python -c "
import sys; sys.path.insert(0, '.')
from paperreview_api import submit_paper
token = submit_paper('workspace/<topic>/paper/paper.pdf', email='250010008@slai.edu.cn', venue='AAAI')
print(f'TOKEN={token}')
"
```
**输出 token 给用户，然后执行 Stage 6 等待审稿。**

### Stage 6: 等待审稿结果
```bash
python -c "
import sys; sys.path.insert(0, '.')
from paperreview_api import poll_review, review_to_markdown, extract_verdict
review = poll_review('<token>', initial_wait=300, interval=60, max_wait=7200)
md = review_to_markdown(review)
with open('workspace/<topic>/review/review_iter<N>.md', 'w') as f: f.write(md)
print('VERDICT:', extract_verdict(review))
"
```

### Stage 7: 判断结果
- `accept` / `weak accept` → **完成！** 告诉用户
- 其他 → **保存当前所有状态到文件，告诉用户："审稿意见已保存，请重开 Claude Code 继续修订"**

### Stage 8: 修订（新一轮会话）
```
这是一轮全新的 Claude Code 会话。你需要：

1. 读取审稿意见:
   cat workspace/<topic>/review/review_iter<N>.md

2. 读取文献综述（保持研究背景一致）:
   cat workspace/<topic>/literature/literature_review.md

3. 读取当前论文:
   cat workspace/<topic>/paper/paper.tex

4. 根据审稿意见修改论文:
   - 补充缺失的实验（如需 → Stage 3）
   - 重写/修改相关章节
   - 更新 paper.tex

5. 重新编译
   pdflatex workspace/<topic>/paper/paper.tex

6. 回到 Stage 5 重新提交审稿
```

## 关键配置

- SCO: share-space / share-cluster / 4x N6LS-80G / afs-share-01g
- paperreview: email=250010008@slai.edu.cn, venue=AAAI
- 模型: deepseek-v4-pro (config.py)

## 绝对规则

1. **全程自动** — 用户说了主题就自动跑到提交审稿
2. **不要编造** — 实验必须真实在 SCO 上运行，结果必须来自实际输出
3. **保存完整上下文** — 每个阶段的输出必须写入文件，让下一轮会话能接上
4. **短会话** — 提交审稿后立即结束会话，修订时重开新会话
5. **文件即记忆** — `literature_review.md` 是研究背景，`review_iter*.md` 是审稿反馈，`paper.tex` 是当前稿件
