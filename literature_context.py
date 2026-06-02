#!/usr/bin/env python3
"""
Literature context builder for literature-grounded paper review.

Reads a workspace's literature/ directory (literature_review.md, references.bib,
experiment_plan.md, experiment results) and builds structured context blocks
that can be injected into reviewer prompts. This enables reviewers to do
cross-comparison rather than reviewing the paper in isolation.

Capabilities:
  1. Baseline completeness check — paper baselines vs literature-standard baselines
  2. SOTA comparison — paper's claimed performance vs literature-reported benchmarks
  3. Related-work coverage — papers cited vs papers that SHOULD be cited
  4. Literature landscape summary — what the field looks like for reviewer context
  5. Key paper deep-dive — extracted claims/limitations from top cited papers

Usage:
    from literature_context import LiteratureContext

    lc = LiteratureContext(workspace_dir="workspace/my_topic")
    context_blocks = lc.build_context()
    # Inject context_blocks into reviewer prompts
"""

from __future__ import annotations

import re
import json
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("literature_context")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PaperEntry:
    """A single paper from the literature review."""
    title: str = ""
    authors: str = ""
    year: str = ""
    venue: str = ""
    citations: int = 0
    source: str = ""
    abstract: str = ""
    relevance: str = "medium"  # high/medium/low for the review topic


@dataclass
class LiteratureLandscape:
    """Structured view of the research landscape."""
    papers: list[PaperEntry] = field(default_factory=list)
    total_papers: int = 0
    key_methods: list[str] = field(default_factory=list)
    key_datasets: list[str] = field(default_factory=list)
    key_metrics: list[str] = field(default_factory=list)
    standard_baselines: list[str] = field(default_factory=list)
    top_cited: list[PaperEntry] = field(default_factory=list)  # top 5 by citations


# ---------------------------------------------------------------------------
# Literature Context Builder
# ---------------------------------------------------------------------------

class LiteratureContext:
    """Build structured literature context from a research workspace."""

    def __init__(self, workspace_dir: str | Path):
        self.workspace = Path(workspace_dir)
        self.literature_dir = self.workspace / "literature"
        self.experiment_dir = self.workspace / "experiment"

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def build_context(self) -> dict[str, str]:
        """Build all literature context blocks for review.

        Returns a dict of context blocks keyed by category:
          - "landscape": research landscape summary
          - "top_papers": key papers with claims and limitations
          - "baselines": standard baselines from literature
          - "sota": SOTA benchmarks from literature
          - "coverage": citation coverage analysis
        """
        landscape = self._parse_literature()
        if not landscape or not landscape.papers:
            logger.warning("No literature found — review will be ungrounded")
            return {}

        blocks: dict[str, str] = {}

        # 1. Landscape summary
        blocks["landscape"] = self._build_landscape_block(landscape)

        # 2. Top papers deep-dive
        blocks["top_papers"] = self._build_top_papers_block(landscape)

        # 3. Standard baselines from literature
        blocks["baselines"] = self._build_baselines_block(landscape)

        # 4. SOTA benchmarks
        blocks["sota"] = self._build_sota_block(landscape)

        # 5. Coverage analysis
        blocks["coverage"] = self._build_coverage_block(landscape)

        return blocks

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_literature(self) -> LiteratureLandscape | None:
        """Parse literature_review.md into a structured landscape."""
        lit_file = self.literature_dir / "literature_review.md"
        if not lit_file.exists():
            return None

        text = lit_file.read_text()
        landscape = LiteratureLandscape()

        # Parse individual papers (delimited by ---)
        paper_blocks = text.split("\n---\n")
        for block in paper_blocks:
            paper = self._parse_paper_block(block)
            if paper and paper.title:
                landscape.papers.append(paper)

        landscape.total_papers = len(landscape.papers)

        # Extract key methods, datasets, metrics
        landscape.key_methods = self._extract_key_entities(text, "method")
        landscape.key_datasets = self._extract_key_entities(text, "dataset")
        landscape.key_metrics = self._extract_key_entities(text, "metric")
        landscape.standard_baselines = self._extract_baselines(text)

        # Top cited papers
        landscape.top_cited = sorted(
            landscape.papers,
            key=lambda p: p.citations,
            reverse=True,
        )[:5]

        return landscape

    def _parse_paper_block(self, block: str) -> PaperEntry | None:
        """Parse a single paper block from literature_review.md."""
        lines = block.strip().split("\n")
        if len(lines) < 2:
            return None

        # Title line: "### N. Title"
        title_line = ""
        for line in lines:
            if re.match(r'^###\s+\d+\.', line):
                title_line = line
                break
        if not title_line:
            return None

        title = re.sub(r'^###\s+\d+\.\s*', '', title_line).strip()

        # Author/venue line: "**Authors** — Year | Venue | cited N× | source"
        meta_line = ""
        for line in lines:
            if "**" in line and ("cited" in line.lower() or "|" in line):
                meta_line = line
                break

        authors = ""
        year = ""
        venue = ""
        citations = 0
        source = ""

        if meta_line:
            # Extract authors (bold text before —)
            author_match = re.match(r'\*\*(.+?)\*\*', meta_line)
            if author_match:
                authors = author_match.group(1).strip()

            # Extract year
            year_match = re.search(r'\b(20\d{2})\b', meta_line)
            if year_match:
                year = year_match.group(1)

            # Extract venue (text between year and | or ,)
            if year:
                rest = meta_line.split(year, 1)[-1]
                venue_match = re.search(r'[\|,]\s*(.+?)(?:\||cited|$)', rest)
                if not venue_match:
                    venue_match = re.match(r'\s*(.+?)(?:\||cited|$)', rest.strip())
                if venue_match:
                    venue = venue_match.group(1).strip()

            # Extract citation count
            cite_match = re.search(r'cited\s+(\d[\d,]*)\s*×', meta_line, re.IGNORECASE)
            if cite_match:
                citations = int(cite_match.group(1).replace(",", ""))

            # Extract source
            src_match = re.search(r'\|\s*(\w+)\s*$', meta_line)
            if src_match:
                source = src_match.group(1)

        # Extract abstract (the body text after meta line)
        abstract_lines = []
        in_body = False
        for line in lines:
            if line == meta_line:
                in_body = True
                continue
            if in_body:
                if line.startswith("[Link]") or line.startswith("---"):
                    break
                if line.strip():
                    abstract_lines.append(line.strip())
        abstract = " ".join(abstract_lines)[:500]

        return PaperEntry(
            title=title,
            authors=authors,
            year=year,
            venue=venue,
            citations=citations,
            source=source,
            abstract=abstract,
        )

    def _extract_key_entities(self, text: str, entity_type: str) -> list[str]:
        """Extract key methods/datasets/metrics mentioned across the literature."""
        patterns = {
            "method": [
                # Method names often appear in titles/abstracts as proper nouns
                r'\b(?:Transformer|BERT|GPT|LLaMA|T5|RoBERTa|Diffusion|GAN|VAE|'
                r'RLHF|PPO|LoRA|Adapter|Prompt\s*Tuning|Fine-tuning|'
                r'Chain.of.Thought|RAG|Retrieval.Augmented)\b',
                r'\b(?:Attention|Self-Attention|Cross-Attention|'
                r'Contrastive\s*Learning|Meta-Learning|Transfer\s*Learning|'
                r'Data\s*Augmentation|Knowledge\s*Distillation|'
                r'Reinforcement\s*Learning|Federated\s*Learning)\b',
            ],
            "dataset": [
                r'\b(?:GLUE|SuperGLUE|SQuAD|MNLI|QQP|RTE|WNLI|'
                r'ImageNet|CIFAR-10|CIFAR-100|COCO|'
                r'WikiText|PTB|BookCorpus|CommonCrawl|C4|The\s*Pile)\b',
                r'\b(?:arXiv|PubMed|\w+Bench(?:mark)?|Kaggle)\b',
            ],
            "metric": [
                r'\b(?:Accuracy|F1|BLEU|ROUGE|METEOR|Perplexity|'
                r'Exact\s*Match|MRR|MAP|NDCG|AUC|MSE|MAE|'
                r'Cross.entropy|Log.likelihood)\b',
                r'\b(?:\d+(?:\.\d+)?%)\b',  # percentage values
            ],
        }

        patterns_for_type = patterns.get(entity_type, [])
        found: set[str] = set()
        for pat in patterns_for_type:
            for m in re.finditer(pat, text, re.IGNORECASE):
                found.add(m.group(0))

        return sorted(found)[:15]

    def _extract_baselines(self, text: str) -> list[str]:
        """Extract standard baseline methods mentioned in the literature."""
        baselines: set[str] = set()

        # Common baseline patterns in literature
        baseline_patterns = [
            # Compare/outperform patterns
            r'(?:compared?\s*(?:to|with|against)|outperforms?|beats?|'
            r'superior\s*to|better\s*than)\s+([A-Z][A-Za-z\s]+?)(?:[,.;]|on\s+the|by\s+|in\s+terms)',
            # "X achieves Y%, followed by Z"
            r'(?:followed\s*by|ahead\s*of|next\s*best)\s+([A-Z][A-Za-z\s]+?)(?:[,.;]|with|at|$)',
            # Explicit baseline mentions
            r'\b(?:baselines?|compared?\s*methods?)\s*(?:include|are|:)\s*([^.]+)',
            # Standard method names as baselines
            r'\b(?:Logistic\s*Regression|SVM|Random\s*Forest|XGBoost|LightGBM|'
            r'k-NN|Naive\s*Bayes|Decision\s*Tree|MLP|CNN|RNN|LSTM|GRU|'
            r'BiLSTM|TextCNN|FastText|Word2Vec|GloVe|TF-IDF|BM25)\b',
        ]

        for pat in baseline_patterns:
            for m in re.finditer(pat, text, re.IGNORECASE):
                candidate = m.group(0).strip()[:100]
                if len(candidate) > 3:
                    baselines.add(candidate)

        return sorted(baselines)[:15]

    # ------------------------------------------------------------------
    # Context block builders
    # ------------------------------------------------------------------

    def _build_landscape_block(self, landscape: LiteratureLandscape) -> str:
        """Build the research landscape summary block for reviewer prompts."""
        lines = [
            "\n## 📚 Literature Landscape (for cross-comparison)",
            "",
            f"**Papers reviewed**: {landscape.total_papers}",
            f"**Top venues represented**: "
            + ", ".join(
                sorted(set(p.venue for p in landscape.papers if p.venue))[:8]
            ),
            "",
        ]

        if landscape.key_methods:
            lines.append(f"**Key methods in the field**: {', '.join(landscape.key_methods[:12])}")
            lines.append("")

        if landscape.key_datasets:
            lines.append(f"**Standard datasets**: {', '.join(landscape.key_datasets[:8])}")
            lines.append("")

        if landscape.key_metrics:
            lines.append(f"**Standard evaluation metrics**: {', '.join(landscape.key_metrics[:8])}")
            lines.append("")

        if landscape.standard_baselines:
            lines.append(f"**Standard baselines used in literature**: "
                        f"{', '.join(landscape.standard_baselines[:10])}")
            lines.append("")

        lines.append(
            "**Reviewer instruction**: Use the above to cross-check: "
            "(1) Does the paper use standard datasets/metrics? "
            "(2) Are all standard baselines included? "
            "(3) Is the paper's method positioned correctly within this landscape?"
        )

        return "\n".join(lines)

    def _build_top_papers_block(self, landscape: LiteratureLandscape) -> str:
        """Build the key papers reference block."""
        if not landscape.top_cited:
            return ""

        lines = [
            "\n## 📖 Key Papers in This Area (for cross-reference)",
            "",
            "When reviewing, cross-check the paper against these works:",
            "",
        ]

        for i, p in enumerate(landscape.top_cited, 1):
            lines.append(f"### {i}. {p.title[:120]}")
            lines.append(f"**Authors**: {p.authors} ({p.year}) | "
                        f"**Cited**: {p.citations}× | **Venue**: {p.venue}")
            if p.abstract:
                lines.append(f"**Summary**: {p.abstract[:300]}...")
            lines.append("")

        lines.append(
            "**Reviewer instruction**: (1) Does the paper adequately cite and "
            "discuss these key works? (2) Does the paper's claimed contribution "
            "genuinely advance beyond what these papers already achieved?"
        )

        return "\n".join(lines)

    def _build_baselines_block(self, landscape: LiteratureLandscape) -> str:
        """Build the baselines completeness check block."""
        baseline_list = landscape.standard_baselines
        if not baseline_list:
            return ""

        lines = [
            "\n## 🎯 Baseline Completeness Check",
            "",
            "The following baselines are commonly used in this research area. "
            "The paper under review should include comparisons against most of these.",
            "",
        ]

        for i, bl in enumerate(baseline_list[:12], 1):
            lines.append(f"{i}. {bl}")

        lines.append("")
        lines.append(
            "**Reviewer instruction**: Check the paper's Experiments section. "
            "Which of these standard baselines are MISSING? Flag missing baselines "
            "as a weakness. If the paper introduces a novel baseline, verify it "
            "is fairly compared."
        )

        return "\n".join(lines)

    def _build_sota_block(self, landscape: LiteratureLandscape) -> str:
        """Build the SOTA comparison block."""
        lines = [
            "\n## 🏆 State-of-the-Art Context",
            "",
            f"This research area has {landscape.total_papers} recent papers. "
            f"The most cited work ({landscape.top_cited[0].citations}× citations "
            f"if available) establishes the current benchmark.",
            "",
        ]

        # Extract any numeric claims from top papers
        sota_claims: list[str] = []
        for p in landscape.top_cited:
            # Look for percentage/score claims in the abstract
            for m in re.finditer(
                r'(?:achieves?|obtains?|reports?|scores?|reaches?)\s+'
                r'(\d+(?:\.\d+)?%?\s*(?:accuracy|F1|BLEU|ROUGE|precision|recall|AUC)?)',
                p.abstract, re.IGNORECASE,
            ):
                sota_claims.append(f"- {p.title[:60]}... → {m.group(0)}")
                break

        if sota_claims:
            lines.append("**Literature-reported benchmark results**:")
            for claim in sota_claims[:5]:
                lines.append(claim)
            lines.append("")

        lines.append(
            "**Reviewer instruction**: Cross-check the paper's reported results "
            "against these literature benchmarks. (1) Are the paper's numbers "
            "in the same range? (2) If the paper claims SOTA, does it convincingly "
            "beat these reported numbers? (3) Are the same datasets/metrics used?"
        )

        return "\n".join(lines)

    def _build_coverage_block(self, landscape: LiteratureLandscape) -> str:
        """Build the citation coverage analysis block."""
        lines = [
            "\n## 📋 Related Work Coverage Checklist",
            "",
            "The paper's Related Work section should discuss most of these topics. "
            "Use this checklist during review:",
            "",
        ]

        # Categorize papers by rough topic
        topics: dict[str, list[str]] = {}
        for p in landscape.papers:
            # Extract key topic words from title
            words = set(re.findall(r'\b[A-Z][a-z]{3,}(?:\s+[A-Z][a-z]{3,})?\b', p.title))
            topic_key = " & ".join(sorted(words)[:2]) if words else "Other"
            if topic_key not in topics:
                topics[topic_key] = []
            if len(topics[topic_key]) < 3:
                topics[topic_key].append(f"- {p.title[:80]} ({p.year})")

        for topic, papers in list(topics.items())[:8]:
            lines.append(f"**{topic}**:")
            lines.extend(papers)
            lines.append("")

        lines.append(
            "**Reviewer instruction**: (1) Does the paper's Related Work cover "
            "these topic areas? (2) Are there important papers or entire topic "
            "areas missing? (3) Is the paper positioned accurately against each "
            "line of work?"
        )

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Format for injection into reviewer prompts
    # ------------------------------------------------------------------

    def format_for_reviewer(
        self,
        reviewer_role: str,
        blocks: dict[str, str] | None = None,
    ) -> str:
        """Format literature context for a specific reviewer role.

        Different reviewers get different context emphasis:
        - Methodology → landscape + top_papers
        - Experiments → baselines + sota
        - Related Work → coverage + top_papers
        - Devil's Advocate → sota + top_papers (for overclaim detection)
        - Clarity → landscape (for field knowledge)
        """
        if blocks is None:
            blocks = self.build_context()
        if not blocks:
            return ""

        role_lower = reviewer_role.lower()
        parts: list[str] = []

        # Everyone gets the landscape
        if "landscape" in blocks:
            parts.append(blocks["landscape"])

        # Role-specific emphasis
        if "method" in role_lower or "methodology" in role_lower:
            if "top_papers" in blocks:
                parts.append(blocks["top_papers"])

        elif "experiment" in role_lower:
            if "baselines" in blocks:
                parts.append(blocks["baselines"])
            if "sota" in blocks:
                parts.append(blocks["sota"])

        elif "related" in role_lower or "literature" in role_lower:
            if "coverage" in blocks:
                parts.append(blocks["coverage"])
            if "top_papers" in blocks:
                parts.append(blocks["top_papers"])

        elif "devil" in role_lower or "advocate" in role_lower or "critical" in role_lower:
            if "sota" in blocks:
                parts.append(blocks["sota"])
            if "top_papers" in blocks:
                parts.append(blocks["top_papers"])

        elif "clarity" in role_lower or "writing" in role_lower:
            # Lightweight context — just key papers for field terminology
            if "top_papers" in blocks:
                parts.append(blocks["top_papers"])

        else:
            # Unknown role — give everything
            parts.extend(blocks.values())

        return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="Build literature context for paper review",
    )
    parser.add_argument("workspace", help="Path to workspace directory")
    parser.add_argument("--role", default="all", help="Reviewer role for context filtering")
    parser.add_argument("--json", action="store_true", help="Output structured JSON")
    args = parser.parse_args()

    lc = LiteratureContext(args.workspace)
    blocks = lc.build_context()

    if args.json:
        print(json.dumps(blocks, indent=2, ensure_ascii=False))
    else:
        formatted = lc.format_for_reviewer(args.role, blocks)
        print(formatted)

    if not blocks:
        print("\n⚠️  No literature found — review will be ungrounded.")


if __name__ == "__main__":
    main()
