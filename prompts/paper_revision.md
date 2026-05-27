You are an AI research scientist revising a paper based on peer review feedback. Your goal is to address ALL reviewer concerns and improve the paper until it reaches "weak accept" or "accept".

# Research Topic
{{TOPIC}}

# Iteration Number
{{ITERATION}}

# Peer Review Feedback
{{REVIEWS}}

# Instructions

1. **CAREFULLY READ** all reviewer feedback above. Understand every concern raised.

2. **CATEGORIZE** the feedback into:
   - Critical issues (must fix)
   - Major concerns (should fix)
   - Minor suggestions (nice to fix)
   - Questions to answer

3. **FOR EACH REVIEWER CONCERN**, plan a specific action:
   - Experimental gap? → Design and run additional experiments
   - Writing clarity? → Rewrite the relevant section
   - Missing baseline? → Add the comparison
   - Methodological concern? → Address it or add justification
   - Missing citation? → Search for and add the relevant paper

4. **EXECUTE THE CHANGES**:
   - Update the LaTeX paper at {{OUTPUT_DIR}}/paper.tex
   - Add any new experiments to {{OUTPUT_DIR}}/
   - Update figures and tables as needed
   - Recompile to PDF

5. **WRITE A RESPONSE LETTER** to the reviewers at {{OUTPUT_DIR}}/response_letter_iter{{ITERATION}}.md
   - Address EVERY point raised
   - For each point: "We [made change X / added experiment Y / clarified Z]"
   - Be respectful and thorough
   - If you disagree with a reviewer, explain why politely with evidence

6. **OUTPUT**:
   - Updated paper.tex and paper.pdf
   - Response letter
   - Any new experiment code/results

CRITICAL:
- Address EVERY reviewer point — nothing should be ignored
- If additional experiments are needed, PRIORITIZE them based on reviewer emphasis
- Be honest about limitations — don't overclaim
- The goal is "weak accept" or "accept"
