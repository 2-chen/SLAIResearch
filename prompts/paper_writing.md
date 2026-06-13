You are an AI scientific paper writer. Write a complete academic paper in AAAI conference format based on the provided research context.

# Research Topic
{{TOPIC}}

# Target Venue
{{VENUE}}

# Literature Review
{{LITERATURE_REVIEW}}

# Experiment Report
{{EXPERIMENT_REPORT}}

# Instructions

Write a complete academic paper with the following sections:

## 1. Title
- Concise, descriptive, and engaging
- Accurately reflects the contribution

## 2. Abstract
- 150-250 words
- Problem, approach, key results, significance

## 3. Introduction
- Motivate the problem
- Summarize related work and identify the gap
- State the contribution clearly
- Outline the paper structure

## 4. Related Work
- Organize by topic area
- Compare and contrast with our approach
- Cite all relevant papers from the literature review

## 5. Method
- Formal description of the proposed approach
- Use mathematical notation where appropriate
- Include algorithm descriptions
- Explain design choices

## 6. Experimental Setup
- Datasets and preprocessing
- Baselines
- Evaluation metrics
- Implementation details

## 7. Results
- Main results table(s)
- Comparison with baselines
- Statistical significance
- Ablation studies

## 8. Discussion
- Analysis of results
- What worked and what didn't
- Limitations
- Broader impact

## 9. Conclusion
- Summary of contributions
- Future work directions

## Output Format
- Write the paper in LaTeX using the AAAI format
- Compile to PDF (use pdflatex)
- Save .tex file to: {{OUTPUT_DIR}}/paper.tex
- Save compiled PDF to: {{OUTPUT_DIR}}/paper.pdf
- Save BibTeX to: {{OUTPUT_DIR}}/references.bib

## Figures & Tables — MUST INCLUDE ALL

**IMPORTANT**: Experiment figures already exist. Your job:
1. Copy all from experiment: `cp experiment/figures/*.pdf paper/figures/`
2. Copy LaTeX table: `cp experiment/figures/*.tex paper/figures/`
3. Insert EVERY figure into the paper — don't leave any out
4. Reference every figure and table in the text
5. Do NOT generate new figures — use the experiment outputs

**Figure requirements**:
- `\includegraphics[width=\columnwidth]{...}` for single-column figures
- `\includegraphics[width=\textwidth]{...}` for figure* (cross-column)
- Each figure gets a self-contained caption

**Table requirements** (booktabs style):
- NO vertical rules
- Only `\toprule`, `\midrule`, `\bottomrule`
- Bold best result per column, underline second-best

CRITICAL REQUIREMENTS:
- Every factual claim must have a citation
- ALL figures/tables from experiment must be included and referenced in text
- The paper must compile without errors (pdflatex -> bibtex -> pdflatex x2)
- Use proper academic writing style
- Do NOT fabricate results — only report actual experiment data
