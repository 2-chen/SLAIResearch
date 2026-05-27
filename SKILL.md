---
name: chenresearch
description: Automated research pipeline — the project orchestrates Claude Code as its execution tool. Claude Code handles literature search, experiment design, LaTeX writing, and revision. paperreview.ai provides peer review. SCO CLI runs cloud GPU experiments. Iterates until accept.
argument-hint: "<topic> [--resume] [--status]"
---

# ChenResearch — Automated Research Pipeline

A lean, iterative research system. **The project owns Claude Code** — Claude Code is an execution tool, not the controller. The project orchestrator (`chenresearch.py`) runs the pipeline, calls `claude -p` for intelligent tasks, and manages the iteration loop with paperreview.ai.

## When to Use

- "Write a paper on X" — full pipeline from topic to AAAI-format paper
- "Do a literature review on X" — just the lit search phase
- "Help me research X" — step-by-step guided research
- Triggers: `chenresearch`, `auto research`, `自动科研`, `paper pipeline`, `写论文`

## Architecture

```
┌────────────────────────────────────────────┐
│        chenresearch.py (controller)         │
│                                              │
│  ┌─ Tools called by the project ──────────┐ │
│  │  Claude Code  → literature, design,    │ │
│  │                  writing, revision      │ │
│  │  SCO CLI      → cloud GPU experiments  │ │
│  │  paperreview  → upload, poll, parse    │ │
│  │  pdflatex     → PDF compilation        │ │
│  └────────────────────────────────────────┘ │
└────────────────────────────────────────────┘
```

## Quick Start

```bash
# Install everything (Python deps + Claude Code + LaTeX)
bash install.sh

# Run the pipeline
python chenresearch.py run "Federated Learning with Differential Privacy for Medical Imaging"

# Resume from saved state
python chenresearch.py resume "federated_learning_with_differential_privacy"

# Check status
python chenresearch.py status
python chenresearch.py list
```

## Pipeline Stages

| Stage | What Happens | Tool |
|-------|-------------|------|
| 1. Literature Search | Search arXiv, Semantic Scholar, OpenAlex | Claude Code |
| 2. Experiment Design | Design methodology, baselines, metrics, write code | Claude Code |
| 3. Experiment Execution | Run on SenseCore GPU cluster (4× N6LS-80G) | SCO CLI |
| 4. Paper Writing | Write AAAI LaTeX, compile to PDF | Claude Code + pdflatex |
| 5. Submit Review | Upload PDF to paperreview.ai (AAAI venue) | paperreview API |
| 6. Poll Review | Wait 5min, poll every 60s until ready | paperreview API |
| 7. Revise | Address all reviewer concerns, add experiments | Claude Code |
| 8. Resubmit | Upload revised PDF → back to stage 6 | paperreview API |

Loop continues until verdict is "accept" or "weak accept", or max_iterations (default 10).

## Configuration

All settings in `config.py`, overridable via environment variables:

```bash
# Claude Code model (execution tool)
export CLAUDE_MODEL=deepseek-v4-pro
export CLAUDE_API_KEY=sk-...
export CLAUDE_BASE_URL=https://api.deepseek.com/anthropic

# Semantic Scholar
export SEMANTIC_SCHOLAR_API_KEY=s2k-...

# paperreview.ai
export PAPERREVIEW_EMAIL=250010008@slai.edu.cn
export PAPERREVIEW_VENUE=AAAI

# Pipeline tuning
export CHENRESEARCH_MAX_ITERATIONS=10
export CHENRESEARCH_POLL_INTERVAL=60
```
