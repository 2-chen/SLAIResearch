You are an AI research scientist designing experiments. Based on the literature review below, design a rigorous experimental plan.

# Research Topic
{{TOPIC}}

# Literature Review Summary
{{LITERATURE_REVIEW}}

# Instructions

Design a complete experimental plan that:

1. **Research Question**: Clearly state the hypothesis or research question.

2. **Methodology**: Describe the proposed method/algorithm/model in detail, including:
   - Architecture design
   - Key innovations over prior work
   - Theoretical justification (if applicable)

3. **Datasets**: Specify which datasets to use, why, and how to preprocess them.

4. **Baselines**: List all baseline methods to compare against, with justification for each.

5. **Evaluation Metrics**: Define clear, quantitative metrics. Include both primary and secondary metrics.

6. **Experimental Setup**:
   - Hardware requirements
   - Software dependencies
   - Hyperparameter settings
   - Training details (epochs, batch size, optimizer, etc.)

7. **Ablation Studies**: What components should be ablated to understand their contribution?

8. **Statistical Rigor**: How to ensure results are statistically significant (e.g., multiple seeds, error bars, significance tests).

9. **Implementation Plan**:
   - Write a complete, runnable Python training script
   - Write a shell script `run_experiment.sh` that:
     - Uses the shared dependency module (works in local and SCO mode):
       ```bash
       PROJECT_DIR="$(dirname "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)")"
       source /data/AutoResearch/ChenResearch/workspace/.shared/install_deps.sh
       install_dependencies
       ```
       This module automatically:
       * **SCO mode (CHENRESEARCH=1)**: Skips all pip install, only sets PYTHONPATH
         to pre-configured site-packages. Zero network, zero install in container.
       * **Local mode**: Installs offline from shared/project wheel caches,
         falls back to network with auto-VPN if needed.
     - **Environment separation rule**: Before submitting to SCO, pre-install missing
       packages locally:
       ```bash
       bash /data/AutoResearch/ChenResearch/workspace/.shared/prepare_env.sh --from-imports experiment.py
       ```
       This installs to `/data/AutoResearch/ChenResearch/env/site-packages/` which
       is mounted into the SCO container. No pip/conda/network in the SCO task itself.
     - Runs all experiments and saves results to a structured output directory

10. **Expected Outcomes**: What results would support/reject the hypothesis?

Save all experiment code to: {{OUTPUT_DIR}}/
Save the experiment plan to: {{OUTPUT_DIR}}/experiment_plan.md

11. **GPU Requirements**:
   - Create a file `experiment_manifest.json` in the output directory:
     ```json
     {"gpu_count": 1}
     ```
     Set `gpu_count` to the number of GPUs your experiment actually needs:
     * 1 for most single-GPU training/evaluation experiments
     * 2-4 for large-scale distributed training
   - **IMPORTANT**: Your `run_experiment.sh` MUST read the `GPU_COUNT` environment variable and adapt:
     * If `GPU_COUNT` >= 2, wrap the model with `torch.nn.DataParallel` to utilize all GPUs
     * Set `CUDA_VISIBLE_DEVICES` to `0,1,...,GPU_COUNT-1`
     * Example snippet for your Python training script:
       ```python
       import os, torch
       gpu_count = int(os.environ.get("GPU_COUNT", "1"))
       device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
       model = model.to(device)
       if gpu_count > 1 and torch.cuda.device_count() >= gpu_count:
           model = torch.nn.DataParallel(model, device_ids=list(range(gpu_count)))
       ```

IMPORTANT: The shell script must be self-contained and runnable with `bash run_experiment.sh`.
The Python code should be well-structured, include error handling, and save all results.
