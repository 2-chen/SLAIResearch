A professional system architecture flowchart diagram for "SLAIResearch", an automated AI research pipeline system. The diagram should use a clean, modern tech style with dark background (deep navy #1a1a2e) and neon-accented connecting lines in cyan (#00d4ff) and green (#00ff88).

The flowchart is organized into 4 horizontal layers from top to bottom:

**LAYER 1 — ENTRY POINTS (top row, 2 boxes):**
- Left box: "start.sh (Shell Controller)" with subtitle "Interactive Menu → Stage Router → Error Recovery"
- Right box: "slairesearch.py (Python CLI)" with subtitle "run / resume / status / list"
- Both connect downward to Layer 2 via cyan arrows.

**LAYER 2 — CORE PIPELINE (center, 7 sequential boxes connected horizontally by arrows):**
Box 1: "① Literature Search" — arXiv + Semantic Scholar + OpenAlex
Box 2: "② Hypothesis Gen" — ReAct Engine (Thought→Act→Observe) + PDF Deep-Read
Box 3: "③ Experiment Design" — Claude Code Scientist (Python template renderer)
Box 4: "④ Environment Prep" — Wheels/Models/Datasets (manifest.json priority)
Box 5: "⑤ Experiment Exec" — Local GPU First → SCO Fallback → Auto-Fix Loop (≤20 rounds)
Box 6: "⑥ Paper Writing" — LaTeX (AAAI 2026) → Compile → PDF
Box 7: "⑦ Submit Review" — paperreview.ai Upload + Poll

**LAYER 3 — REVIEW & REVISION LOOP (below the pipeline, forming a feedback cycle):**
A diamond decision node: "Verdict?" with branches:
- Right branch (green): "accept / weak accept" → terminal "DONE ✓"
- Down branch (red): "reject / borderline / weak reject" → flows into a vertical sequence of 4 boxes:

Sub-box A: "Calibration Engine ★" — "Compare internal vs external reviews → Analyze gaps → Generate reviewer_pool.json → Dynamic reviewers loaded next round"

Sub-box B: "Phase A: Review Analysis" — "Extract issue_tracker.md (EXP/TXT/CIT/FIG/THY) → EXP_COUNT cross-validation (tracker as authority)"

Sub-box C: "Phase B: Supplementary Experiments" — "Write code → SCO parallel submit → Auto-fix loop → .experiments_checked conditional persistence"

Sub-box D: "Phase C: Paper Update" — "Real results from results.json → No fabrication → Recompile LaTeX"

Below these, a large box spanning the width: "Internal Review Gate (≤5 iterations)" containing:
- Left side: "5 Fixed Reviewers + N Dynamic Reviewers (from calibration)"
- Center: "Disk Evidence Verification: scan results.json → enforce ≤50% resolution rate for text-only fixes"
- Right side: "Gate Checks: internal score ≥ 6.0/10 AND external issue resolution ≥ 70%"

A feedback arrow loops from the Gate back up to "Submit Review" (Layer 2 Box 7).

**LAYER 4 — INFRASTRUCTURE & MODULES (bottom row, showing the shared library):**
Smaller boxes connected horizontally:
"SCO Runner (GPU cluster + local executor)" | "State Manager (JSON persistence)" | "paperreview API (6-layer verdict extraction)" | "Review Tools (AI detection, citation coverage)" | "Figure Generation (matplotlib + booktabs)" | "Revision Engine (backpressure + grounding)"

**ADDITIONAL ELEMENTS:**
- A small legend in the bottom-right corner: "★ = New in v2.4" | "Solid line = data flow" | "Dashed line = control flow"
- Title at the top in large font: "SLAIResearch — Automated AI Research Pipeline" with subtitle "v2.4 | From Research Idea to Publication-Ready Paper"
- Use isometric-style rounded rectangles with subtle glow effects.
- Color coding: Blue boxes = controller/entry, Cyan boxes = pipeline stages, Green boxes = success/terminal, Red/Amber boxes = revision/repair, Purple boxes = infrastructure.
- The feedback loop from Review Gate back to Submit should be clearly visible as a curved arrow on the right side.
