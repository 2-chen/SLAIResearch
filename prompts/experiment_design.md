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
     - Sets up the environment (conda or system Python)
     - **Installs dependencies using the shared offline-first module**: add the following lines at the top of the script (after `set -euo pipefail`):
       ```bash
       PROJECT_DIR="$(dirname "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)")"
       source /data/AutoResearch/ChenResearch/workspace/.shared/install_deps.sh
       ```
       Then call `install_dependencies` to install all packages. This module automatically:
       * Checks shared wheel cache at `/data/AutoResearch/ChenResearch/workspace/.shared/wheels/`
       * Checks project-local wheel cache at `${PROJECT_DIR}/wheels/`
       * Installs offline if wheels exist, falls back to network with auto-VPN if needed
       * Pre-download wheels: `bash /data/AutoResearch/ChenResearch/workspace/.shared/download_wheels.sh`
     - Runs all experiments
     - Saves results to a structured output directory

10. **Expected Outcomes**: What results would support/reject the hypothesis?

Save all experiment code to: {{OUTPUT_DIR}}/
Save the experiment plan to: {{OUTPUT_DIR}}/experiment_plan.md

IMPORTANT: The shell script must be self-contained and runnable with `bash run_experiment.sh`.
The Python code should be well-structured, include error handling, and save all results.
