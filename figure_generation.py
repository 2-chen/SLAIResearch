#!/usr/bin/env python3
"""
Publication-quality figure and table generation for ChenResearch.

Standards ported from chen-research-skills write-skill (figure_table_design.md):
  - Figure Contract system (conclusion → evidence → export spec)
  - Nature-grade matplotlib rcParams
  - Colorblind-safe, print-friendly color palettes
  - booktabs table style (no vertical rules, minimal horizontal rules)
  - LaTeX-ready PDF vector output + table code generation

Usage:
    from figure_generation import FigureGenerator, TableGenerator

    fg = FigureGenerator(output_dir="paper/figures")
    fg.bar_comparison(methods=["A","B","Ours"], values=[72,74,79], ...)
    fg.ablation_chart(components={...}, baseline=65.2)

    tg = TableGenerator()
    latex = tg.main_results_table(methods=[...], metrics={...})
    latex = tg.ablation_table(ablations=[...])
"""

from __future__ import annotations

import re
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("figure_generation")

# ---------------------------------------------------------------------------
# Matplotlib configuration — Nature-grade publication defaults
# ---------------------------------------------------------------------------

def configure_matplotlib() -> None:
    """Apply publication-quality matplotlib rcParams globally."""
    try:
        import matplotlib as mpl
        mpl.rcParams.update({
            "font.family": "sans-serif",
            "font.sans-serif": ["Times New Roman", "DejaVu Sans", "Arial", "sans-serif"],
            "font.size": 8,
            "axes.titlesize": 9,
            "axes.labelsize": 8,
            "legend.fontsize": 7,
            "xtick.labelsize": 7,
            "ytick.labelsize": 7,
            "svg.fonttype": "none",
            "pdf.fonttype": 42,
            "axes.spines.right": False,
            "axes.spines.top": False,
            "axes.linewidth": 0.8,
            "xtick.major.width": 0.6,
            "ytick.major.width": 0.6,
            "legend.frameon": False,
            "figure.dpi": 300,
            "savefig.dpi": 300,
            "savefig.bbox": "tight",
            "savefig.pad_inches": 0.02,
        })
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# Color palette — colorblind-safe, print-friendly (Wong 2011)
# ---------------------------------------------------------------------------

COLORS = {
    "blue":   "#0072B2",
    "orange": "#E69F00",
    "green":  "#009E73",
    "red":    "#D55E00",
    "purple": "#CC79A7",
    "brown":  "#8B6914",
    "yellow": "#F0E442",
    "grey":   "#999999",
    "black":  "#333333",
}

GAIN_COLOR = COLORS["green"]    # for improvements
DROP_COLOR = COLORS["red"]      # for degradations
NEUTRAL_COLOR = COLORS["grey"]  # for baselines
OURS_COLOR = COLORS["blue"]     # for the proposed method

# Default palette for multi-method comparison
DEFAULT_PALETTE = [
    COLORS["grey"], COLORS["orange"], COLORS["green"],
    COLORS["red"], COLORS["purple"], COLORS["brown"],
    COLORS["blue"],  # Ours (last = blue for emphasis)
]

# ---------------------------------------------------------------------------
# Figure dimensions — CCF-A standard
# ---------------------------------------------------------------------------

FIG_SINGLE_COL = (3.35, 2.0)     # ~8.5cm, for single-column venues
FIG_DOUBLE_COL = (7.0, 3.5)      # ~17.8cm, for full-width figures
FIG_SQUARE = (3.35, 3.35)        # for qualitative images
FIG_WIDE_SHORT = (7.0, 2.0)      # for wide comparison bars


# ---------------------------------------------------------------------------
# Figure Contract
# ---------------------------------------------------------------------------

@dataclass
class FigureContract:
    """What a figure must communicate, defined BEFORE code is written."""
    name: str = ""                          # filename stem
    core_conclusion: str = ""               # one-sentence claim
    archetype: str = "bar_comparison"       # bar_comparison, line_plot, scatter, composite
    export_format: str = "pdf"              # pdf for LaTeX
    dimensions: tuple[float, float] = field(default_factory=lambda: FIG_SINGLE_COL)
    caption: str = ""                       # self-contained LaTeX caption
    label: str = ""                         # \label{fig:xxx}


# ---------------------------------------------------------------------------
# Figure Generator
# ---------------------------------------------------------------------------

class FigureGenerator:
    """Generate publication-quality figures for LaTeX papers."""

    def __init__(self, output_dir: str | Path = "paper/figures"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        configure_matplotlib()

    # ------------------------------------------------------------------
    # Bar comparison chart
    # ------------------------------------------------------------------

    def bar_comparison(
        self,
        methods: list[str],
        values: list[float],
        *,
        errors: list[float] | None = None,
        metric_name: str = "Accuracy (%)",
        ours_idx: int = -1,
        filename: str = "main_results",
        caption: str = "",
        label: str = "fig:main_results",
        highlight_color: str = OURS_COLOR,
        neutral_color: str = NEUTRAL_COLOR,
        figsize: tuple[float, float] = FIG_SINGLE_COL,
    ) -> Path:
        """Generate a bar chart comparing methods.

        Args:
            methods: Method names (x-axis labels)
            values: Metric values (bar heights)
            errors: Optional error bar values
            metric_name: Y-axis label
            ours_idx: Index of "our" method (highlighted). -1 = last.
            filename: Output filename stem (no extension)
            caption: LaTeX caption text
            label: LaTeX \label{}
        """
        import matplotlib.pyplot as plt
        import numpy as np

        if ours_idx < 0:
            ours_idx = len(methods) - 1

        colors = [neutral_color] * len(methods)
        colors[ours_idx] = highlight_color

        fig, ax = plt.subplots(figsize=figsize)

        x = np.arange(len(methods))
        bars = ax.bar(
            x, values, color=colors, width=0.6,
            edgecolor="white", linewidth=0.5,
            yerr=errors, capsize=2, error_kw={"linewidth": 0.5},
        )

        # Value labels on top of bars
        for bar, val in zip(bars, values):
            label_y = bar.get_height() + max(values) * 0.02
            ax.text(
                bar.get_x() + bar.get_width() / 2, label_y,
                f"{val:.1f}", ha="center", va="bottom",
                fontsize=7, fontweight="bold" if val == max(values) else "normal",
            )

        ax.set_xticks(x)
        ax.set_xticklabels(methods, rotation=15, ha="right", fontsize=7)
        ax.set_ylabel(metric_name, fontsize=8)
        ax.yaxis.grid(True, alpha=0.3, linewidth=0.5)
        ax.set_axisbelow(True)
        ax.set_ylim(0, max(values) * 1.15)

        fig.tight_layout()

        pdf_path = self.output_dir / f"{filename}.pdf"
        fig.savefig(pdf_path, format="pdf", bbox_inches="tight", pad_inches=0.02)
        plt.close(fig)

        logger.info("Bar comparison → %s", pdf_path)
        return pdf_path

    # ------------------------------------------------------------------
    # Ablation chart
    # ------------------------------------------------------------------

    def ablation_chart(
        self,
        components: list[dict[str, Any]],
        *,
        baseline_value: float = 0.0,
        metric_name: str = "Accuracy (%)",
        filename: str = "ablation",
        caption: str = "",
        label: str = "fig:ablation",
        figsize: tuple[float, float] = FIG_SINGLE_COL,
    ) -> Path:
        """Generate an ablation study chart.

        Args:
            components: List of {name, value, delta} dicts,
                        e.g. [{"name": "Base", "value": 65.2},
                              {"name": "+Comp A", "value": 70.1, "delta": +4.9}, ...]
            baseline_value: Reference value for the baseline
            metric_name: Y-axis label
        """
        import matplotlib.pyplot as plt
        import numpy as np

        names = [c["name"] for c in components]
        values = [c["value"] for c in components]

        # Color: grey for baseline, progressive blue for additions
        colors = [NEUTRAL_COLOR] + [
            COLORS["blue"] if i == len(components) - 1 else
            COLORS["blue"] + f"{int(40 + i * 40):02x}"
            for i in range(len(components) - 1)
        ]
        # Actually, use a cleaner approach
        colors = [NEUTRAL_COLOR] if len(components) > 1 else []
        for i in range(1, len(components)):
            alpha = 0.5 + 0.5 * i / (len(components) - 1)
            colors.append(COLORS["blue"])
        if not colors:
            colors = [NEUTRAL_COLOR]

        fig, ax = plt.subplots(figsize=figsize)
        x = np.arange(len(names))
        bars = ax.bar(x, values, color=colors, width=0.55, edgecolor="white", linewidth=0.5)

        # Value + delta labels
        for i, (bar, comp) in enumerate(zip(bars, components)):
            val_text = f"{comp['value']:.1f}"
            if "delta" in comp and comp["delta"] != 0:
                sign = "+" if comp["delta"] > 0 else ""
                val_text += f" ({sign}{comp['delta']:.1f})"
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + max(values) * 0.02,
                val_text, ha="center", va="bottom", fontsize=6.5,
                fontweight="bold" if i == len(components) - 1 else "normal",
            )

        ax.set_xticks(x)
        ax.set_xticklabels(names, rotation=15, ha="right", fontsize=7)
        ax.set_ylabel(metric_name)
        ax.yaxis.grid(True, alpha=0.3, linewidth=0.5)
        ax.set_axisbelow(True)
        ax.set_ylim(0, max(values) * 1.18)

        fig.tight_layout()
        pdf_path = self.output_dir / f"{filename}.pdf"
        fig.savefig(pdf_path, format="pdf", bbox_inches="tight", pad_inches=0.02)
        plt.close(fig)

        logger.info("Ablation chart → %s", pdf_path)
        return pdf_path

    # ------------------------------------------------------------------
    # Line plot (training curves, trends)
    # ------------------------------------------------------------------

    def line_plot(
        self,
        curves: list[dict[str, Any]],
        *,
        x_label: str = "Epoch",
        y_label: str = "Loss",
        filename: str = "training_curves",
        figsize: tuple[float, float] = FIG_SINGLE_COL,
    ) -> Path:
        """Generate a multi-line plot (e.g., training curves).

        Args:
            curves: List of {label, x, y, color (optional)} dicts
        """
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=figsize)

        for i, curve in enumerate(curves):
            color = curve.get("color", DEFAULT_PALETTE[i % len(DEFAULT_PALETTE)])
            ax.plot(
                curve["x"], curve["y"],
                label=curve.get("label", f"Curve {i+1}"),
                color=color, linewidth=1.2, alpha=0.9,
            )

        ax.set_xlabel(x_label)
        ax.set_ylabel(y_label)
        ax.legend(fontsize=7, loc="best")
        ax.yaxis.grid(True, alpha=0.3, linewidth=0.5)
        ax.set_axisbelow(True)

        fig.tight_layout()
        pdf_path = self.output_dir / f"{filename}.pdf"
        fig.savefig(pdf_path, format="pdf", bbox_inches="tight", pad_inches=0.02)
        plt.close(fig)

        logger.info("Line plot → %s", pdf_path)
        return pdf_path

    # ------------------------------------------------------------------
    # Parameter sensitivity heatmap-style grid
    # ------------------------------------------------------------------

    def param_sensitivity(
        self,
        param1_name: str,
        param1_values: list[float],
        param2_name: str,
        param2_values: list[float],
        results_grid: list[list[float]],
        *,
        metric_name: str = "Accuracy",
        filename: str = "param_sensitivity",
        figsize: tuple[float, float] = FIG_DOUBLE_COL,
    ) -> Path:
        """Generate a parameter sensitivity grid as colored bar chart.

        Args:
            param1_name: X-axis parameter name
            param1_values: X-axis tick values
            param2_name: Legend parameter name
            param2_values: One bar group per value
            results_grid: [len(param2_values)][len(param1_values)] matrix
        """
        import matplotlib.pyplot as plt
        import numpy as np

        fig, ax = plt.subplots(figsize=figsize)

        x = np.arange(len(param1_values))
        n_bars = len(param2_values)
        width = 0.8 / n_bars

        for i, (label, results) in enumerate(zip(param2_values, results_grid)):
            offset = (i - n_bars / 2 + 0.5) * width
            ax.bar(
                x + offset, results, width,
                label=str(label), color=DEFAULT_PALETTE[i % len(DEFAULT_PALETTE)],
                edgecolor="white", linewidth=0.3,
            )

        ax.set_xticks(x)
        ax.set_xticklabels([str(v) for v in param1_values])
        ax.set_xlabel(param1_name)
        ax.set_ylabel(metric_name)
        ax.legend(title=param2_name, fontsize=7, title_fontsize=8)
        ax.yaxis.grid(True, alpha=0.3, linewidth=0.5)
        ax.set_axisbelow(True)

        fig.tight_layout()
        pdf_path = self.output_dir / f"{filename}.pdf"
        fig.savefig(pdf_path, format="pdf", bbox_inches="tight", pad_inches=0.02)
        plt.close(fig)

        return pdf_path

    # ------------------------------------------------------------------
    # LaTeX integration helpers
    # ------------------------------------------------------------------

    @staticmethod
    def includegraphics(filename: str, width: str = r"\linewidth",
                        caption: str = "", label: str = "") -> str:
        """Generate a LaTeX figure environment block."""
        lines = [r"\begin{figure}[t]", r"  \centering"]
        lines.append(f"  \\includegraphics[width={width}]{{figures/{filename}}}")
        if caption:
            lines.append(f"  \\caption{{{caption}}}")
        if label:
            lines.append(f"  \\label{{{label}}}")
        lines.append(r"\end{figure}")
        return "\n".join(lines)

    @staticmethod
    def figure_checklist(fig: FigureContract) -> list[str]:
        """Check a figure contract against publication standards."""
        issues: list[str] = []
        if not fig.core_conclusion:
            issues.append("Missing core conclusion (one-sentence claim)")
        if not fig.caption:
            issues.append("Missing self-contained caption")
        if not fig.label:
            issues.append("Missing \\label{}")
        if fig.export_format != "pdf":
            issues.append(f"Expected PDF format, got {fig.export_format}")
        return issues


# ---------------------------------------------------------------------------
# Table Generator
# ---------------------------------------------------------------------------

class TableGenerator:
    """Generate publication-quality LaTeX tables in booktabs style."""

    @staticmethod
    def _booktabs_preamble() -> str:
        return r"\usepackage{booktabs}"

    # ------------------------------------------------------------------
    # Main results comparison table
    # ------------------------------------------------------------------

    @staticmethod
    def main_results_table(
        methods: list[str],
        metrics: dict[str, list[float]],
        *,
        method_years: dict[str, str] | None = None,
        ours_name: str = "Ours",
        caption: str = "Comparison with state-of-the-art methods.",
        label: str = "tab:main_results",
        metric_directions: dict[str, str] | None = None,
        extra_info: dict[str, list[Any]] | None = None,
    ) -> str:
        """Generate a booktabs main results table.

        Args:
            methods: ['Method A', 'Method B', 'Ours (full)']
            metrics: {'Accuracy': [72.3, 74.1, 78.9], 'F1': [68.1, 70.3, 74.2]}
            method_years: {'Method A': '2023', ...} for year annotations
            ours_name: Prefix for "our" method rows
            metric_directions: {'Accuracy': r'$\\uparrow$', ...}
            extra_info: {'Params (M)': [45, 52, 35], ...}
        """
        if metric_directions is None:
            metric_directions = {k: r"$\uparrow$" for k in metrics}
        if extra_info is None:
            extra_info = {}

        # Determine best per column for bold
        all_cols: dict[str, list[float]] = {**metrics}
        for k, v in extra_info.items():
            if all(isinstance(x, (int, float)) for x in v):
                all_cols[k] = [float(x) for x in v]

        n_methods = len(methods)
        metric_names = list(metrics.keys())
        extra_names = list(extra_info.keys())
        all_headers = metric_names + extra_names
        n_cols = 1 + len(all_headers)

        # Build header
        col_spec = "@{}l" + " c" * len(all_headers) + "@{}"
        header_parts = ["Method"]
        for h in all_headers:
            direction = metric_directions.get(h, "")
            header_parts.append(f"{h} {direction}" if direction else h)

        lines = [
            r"\begin{table}[t]",
            r"  \centering",
            f"  \\caption{{{caption}}}",
            f"  \\label{{{label}}}",
            f"  \\begin{{tabular}}{{{col_spec}}}",
            r"    \toprule",
            "    " + " & ".join(header_parts) + r" \\",
            r"    \midrule",
        ]

        # Find best per metric column
        best_idx: dict[str, int] = {}
        second_idx: dict[str, int] = {}
        for col_name, vals in all_cols.items():
            sorted_idx = sorted(range(len(vals)),
                                key=lambda i: vals[i],
                                reverse=("down" not in (metric_directions.get(col_name, "") or "")))
            if len(sorted_idx) >= 1:
                best_idx[col_name] = sorted_idx[0]
            if len(sorted_idx) >= 2:
                second_idx[col_name] = sorted_idx[1]

        # Build rows
        for i, method in enumerate(methods):
            year_suffix = f" ({method_years[method]})" if method_years and method in method_years else ""
            is_ours = ours_name.lower() in method.lower() or "our" in method.lower()

            row_parts: list[str] = []
            if is_ours:
                row_parts.append(f"    \\textbf{{{method}{year_suffix}}}")
            else:
                row_parts.append(f"    {method}{year_suffix}")

            for col_name in all_headers:
                if col_name in metrics:
                    val = metrics[col_name][i]
                elif col_name in extra_info:
                    val = extra_info[col_name][i]
                else:
                    val = "—"

                if isinstance(val, float):
                    val_str = f"{val:.1f}"
                elif isinstance(val, str):
                    val_str = val
                else:
                    val_str = str(val)

                # Bold best, underline second
                if col_name in best_idx and i == best_idx[col_name]:
                    val_str = f"\\textbf{{{val_str}}}"
                elif col_name in second_idx and i == second_idx[col_name]:
                    val_str = f"\\underline{{{val_str}}}"

                row_parts.append(val_str)

            lines.append("    " + " & ".join(row_parts) + r" \\")

        lines.extend([
            r"    \bottomrule",
            r"  \end{tabular}",
            r"\end{table}",
        ])

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Ablation table
    # ------------------------------------------------------------------

    @staticmethod
    def ablation_table(
        ablations: list[dict[str, Any]],
        *,
        baseline_name: str = "Baseline (no components)",
        caption: str = "Ablation study on model components.",
        label: str = "tab:ablation",
        metric_names: list[str] | None = None,
    ) -> str:
        """Generate a booktabs ablation study table.

        Args:
            ablations: List of {name, metrics: {metric_name: value}, ...}
                       e.g. [{"name": "Baseline", "Accuracy": 65.2, "F1": 62.1},
                             {"name": "+Comp A", "Accuracy": 70.1, "F1": 67.8, "delta_Accuracy": 4.9}]
            baseline_name: Label for the baseline row
            metric_names: Manual metric name list (auto-detected if None)
        """
        if metric_names is None:
            # Auto-detect from first ablation entry
            metric_names = [
                k for k in ablations[0]
                if k != "name" and not k.startswith("delta_")
            ]

        all_headers = list(metric_names)
        # Add delta columns if any ablation has deltas
        has_deltas = any(
            f"delta_{m}" in a
            for a in ablations
            for m in metric_names
        )
        if has_deltas:
            all_headers.append(r"$\Delta$")

        n_cols = 1 + len(all_headers)
        col_spec = "@{}l" + " c" * len(all_headers) + "@{}"

        lines = [
            r"\begin{table}[t]",
            r"  \centering",
            f"  \\caption{{{caption}}}",
            f"  \\label{{{label}}}",
            f"  \\begin{{tabular}}{{{col_spec}}}",
            r"    \toprule",
            "    Configuration & " + " & ".join(all_headers) + r" \\",
            r"    \midrule",
        ]

        for i, abl in enumerate(ablations):
            name = abl["name"]
            is_final = i == len(ablations) - 1

            if is_final:
                row = f"    \\textbf{{{name}}}"
            elif i == 0:
                row = f"    \\textit{{{name}}}"
            else:
                row = f"    {name}"

            for m in metric_names:
                val = abl.get(m, "—")
                if isinstance(val, float):
                    row += f" & {val:.1f}"
                else:
                    row += f" & {val}"
                if is_final and isinstance(val, (int, float)):
                    row = row[:-len(str(val))] + f"\\textbf{{{val:.1f}}}" if isinstance(val, float) else row

            if has_deltas:
                # Show primary metric delta or cumulative
                primary_delta = abl.get(f"delta_{metric_names[0]}", None)
                if primary_delta is not None:
                    sign = "+" if primary_delta > 0 else ""
                    row += f" & {sign}{primary_delta:.1f}"
                else:
                    row += " & —"

            row += r" \\"
            lines.append(row)

            # Add space before the full model
            if i == len(ablations) - 2:
                lines.append(r"    \addlinespace")

        lines.extend([
            r"    \bottomrule",
            r"  \end{tabular}",
            r"\end{table}",
        ])

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Quick helpers
    # ------------------------------------------------------------------

    @staticmethod
    def metric_arrow(metric_name: str) -> str:
        """Return the appropriate arrow direction for a metric."""
        higher_better = {
            "accuracy", "f1", "precision", "recall", "auc", "bleu", "rouge",
            "map", "ndcg", "mrr", "r2",
        }
        name_lower = re.sub(r'[^a-z]', '', metric_name.lower())
        if name_lower in higher_better:
            return r"$\uparrow$"
        if any(w in name_lower for w in ("loss", "error", "perplexity", "time")):
            return r"$\downarrow$"
        return r"$\uparrow$"

    @staticmethod
    def format_value(val: float, precision: int = 1) -> str:
        """Format a numeric value for table display."""
        if val >= 100:
            return f"{val:.0f}"
        if val >= 10:
            return f"{val:.1f}"
        if val < 0.01:
            return f"{val:.4f}"
        return f"{val:.{precision}f}"

    @staticmethod
    def table_checklist(latex_code: str) -> list[str]:
        """Check a LaTeX table against booktabs standards."""
        issues: list[str] = []
        if r"\toprule" not in latex_code:
            issues.append("Missing \\toprule")
        if r"\bottomrule" not in latex_code:
            issues.append("Missing \\bottomrule")
        if "|" in re.findall(r'\\begin\{tabular\}\{([^}]+)\}', latex_code)[0] if re.findall(r'\\begin\{tabular\}\{([^}]+)\}', latex_code) else False:
            issues.append("Vertical rules detected — booktabs forbids them")
        if r"\resizebox" in latex_code:
            issues.append("\\resizebox used — restructure table instead")
        if r"\caption{" not in latex_code:
            issues.append("Missing \\caption")
        if r"\label{" not in latex_code:
            issues.append("Missing \\label")
        return issues


# ---------------------------------------------------------------------------
# Paper figure planner — figure/table inventory
# ---------------------------------------------------------------------------

@dataclass
class PaperFigurePlan:
    """Complete figure/table plan for a paper."""
    figures: list[FigureContract] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def standard_plan(cls) -> "PaperFigurePlan":
        """Return the standard CCF-A paper figure/table plan."""
        return cls(
            figures=[
                FigureContract(
                    name="method_overview",
                    core_conclusion="Our method's architecture and data flow",
                    archetype="schematic_led_composite",
                    dimensions=FIG_DOUBLE_COL,
                    export_format="pdf",
                    caption="Overview of the proposed method. (a) ... (b) ... (c) ...",
                    label="fig:overview",
                ),
                FigureContract(
                    name="main_results",
                    core_conclusion="Our method outperforms baselines on primary metrics",
                    archetype="bar_comparison",
                    dimensions=FIG_SINGLE_COL,
                    export_format="pdf",
                    caption="Comparison with state-of-the-art methods on the benchmark dataset.",
                    label="fig:main_results",
                ),
                FigureContract(
                    name="ablation",
                    core_conclusion="Each component contributes measurably to performance",
                    archetype="bar_comparison",
                    dimensions=FIG_SINGLE_COL,
                    export_format="pdf",
                    caption="Ablation study showing per-component contribution.",
                    label="fig:ablation",
                ),
            ],
            tables=[
                {
                    "type": "main_results",
                    "caption": "Comparison with state-of-the-art methods.",
                    "label": "tab:main_results",
                },
                {
                    "type": "ablation",
                    "caption": "Ablation study on model components.",
                    "label": "tab:ablation",
                },
            ],
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="Generate publication-quality figures and tables",
    )
    sub = parser.add_subparsers(dest="command")

    # bar
    p = sub.add_parser("bar", help="Generate bar comparison chart")
    p.add_argument("--output", "-o", default="paper/figures", help="Output dir")
    p.add_argument("--methods", nargs="+", required=True, help="Method names")
    p.add_argument("--values", nargs="+", type=float, required=True, help="Metric values")
    p.add_argument("--metric", default="Accuracy (%)", help="Metric name")
    p.add_argument("--filename", default="main_results", help="Output filename stem")

    # table
    p = sub.add_parser("table", help="Generate main results table")
    p.add_argument("--methods", nargs="+", required=True, help="Method names")
    p.add_argument("--output", default="paper/tables/main_comparison.tex", help="Output file")

    # plan
    p = sub.add_parser("plan", help="Show standard figure/table plan")
    p.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    if args.command == "bar":
        fg = FigureGenerator(args.output)
        path = fg.bar_comparison(
            methods=args.methods,
            values=args.values,
            metric_name=args.metric,
            filename=args.filename,
        )
        print(f"Figure saved to: {path}")

    elif args.command == "table":
        # Quick demo table
        tg = TableGenerator()
        table = tg.main_results_table(
            methods=args.methods,
            metrics={"Accuracy": [72.3, 74.1, 78.9]},
        )
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(table)
        print(f"Table saved to: {args.output}")

    elif args.command == "plan":
        plan = PaperFigurePlan.standard_plan()
        if args.json:
            import dataclasses, json
            print(json.dumps(dataclasses.asdict(plan), indent=2, ensure_ascii=False))
        else:
            print("## Standard CCF-A Paper Figure/Table Plan\n")
            print("### Figures")
            for f in plan.figures:
                print(f"- **{f.name}**: {f.core_conclusion}")
                print(f"  Archetype: {f.archetype}, Size: {f.dimensions}")
                print(f"  Label: {f.label}")
            print("\n### Tables")
            for t in plan.tables:
                print(f"- **{t['type']}**: {t['caption']} ({t['label']})")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
