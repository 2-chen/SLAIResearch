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

CRITICAL REQUIREMENTS:
- Every factual claim must have a citation
- All figures/tables must be properly labeled and referenced
- The paper must compile without errors
- Use proper academic writing style — clear, precise, objective
- Do NOT fabricate results — only report what was actually found in the experiments
- If experiments haven't been run yet, clearly mark placeholder sections with [EXPERIMENTAL RESULTS PENDING]
