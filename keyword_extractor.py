#!/usr/bin/env python3
"""
研究主题 → 搜索关键词提取器。

将自然语言研究主题拆解为多组搜索关键词，每组从不同角度检索，
合并去重后获得更全面、更相关的文献覆盖。

用法:
    python keyword_extractor.py "研究主题" --groups 3
    输出 JSON:
    {
      "groups": [
        ["keyword1", "keyword2", ...],
        ["keyword3", "keyword4", ...],
      ]
    }
"""

import os
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))


def extract_keywords_llm(topic: str, num_groups: int = 3) -> list[list[str]]:
    """用 LLM 提取搜索关键词组。每组从不同角度搜索。"""
    from config import CLAUDE_CMD, CLAUDE_MODEL
    import subprocess

    prompt = f"""Analyze this research topic and generate {num_groups} groups of search keywords.

Research topic: "{topic}"

Rules:
1. Group 1: Core method/technique keywords (e.g., "randomized smoothing", "certified robustness")
2. Group 2: Application domain and related methods (e.g., "image classification defense", "adversarial examples")
3. Group 3: Broader/alternative approaches (e.g., "provable defense", "robustness guarantees", "verification")

Each group should have 2-4 concise keyword phrases (5 words max each).
These are for academic paper search (arXiv, Semantic Scholar) — short keywords work better than long sentences.

Output ONLY valid JSON:
{{
  "groups": [
    ["keyword1", "keyword2", "keyword3"],
    ["keyword4", "keyword5"],
    ["keyword6", "keyword7", "keyword8"]
  ]
}}"""

    try:
        result = subprocess.run(
            [CLAUDE_CMD, "-p", "--model", CLAUDE_MODEL, "--output-format", "text"],
            input=prompt, capture_output=True, text=True, timeout=30,
            cwd=str(PROJECT_ROOT), env={**os.environ},
        )
        # Extract JSON from output
        text = result.stdout
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end]).get("groups", [])
    except Exception as e:
        print(f"LLM keyword extraction failed: {e}", file=sys.stderr)

    return []


def extract_keywords_simple(topic: str, num_groups: int = 3) -> list[list[str]]:
    """简单规则提取关键词（LLM 不可用时的 fallback）。"""
    import re

    # Remove common stop words and punctuation
    clean = re.sub(r'[^\w\s]', ' ', topic.lower())
    words = clean.split()

    # Filter out stop words
    stop_words = {
        'a', 'an', 'the', 'for', 'of', 'in', 'on', 'to', 'with', 'and',
        'or', 'by', 'via', 'through', 'from', 'using', 'improving',
        'enhancing', 'towards', 'novel', 'new', 'efficient', 'effective',
        'the', 'is', 'are', 'be', 'been', 'being',
    }
    content_words = [w for w in words if w not in stop_words and len(w) > 2]

    if not content_words:
        return [[topic]]

    # Group 1: first 1/3 of content words (core concepts)
    n = max(1, len(content_words) // num_groups)
    groups = []
    for i in range(num_groups):
        group_words = content_words[i * n:(i + 1) * n]
        if group_words:
            # Create 2-3 keyword phrases from this group
            phrases = [
                " ".join(group_words),
                " ".join(group_words[:2]) if len(group_words) >= 2 else group_words[0],
            ]
            groups.append(phrases)

    return groups if groups else [[topic]]


def extract(topic: str, num_groups: int = 3, use_llm: bool = True) -> list[list[str]]:
    """提取搜索关键词组。优先用 LLM，失败则用规则 fallback。"""
    if use_llm:
        groups = extract_keywords_llm(topic, num_groups)
        if groups and len(groups) >= 2:
            return groups

    # Fallback: simple rule-based extraction
    groups = extract_keywords_simple(topic, num_groups)

    # Always include the original topic as the last group
    if topic not in [" ".join(g) for g in groups]:
        groups.append([topic[:200]])

    return groups


def main():
    import argparse
    parser = argparse.ArgumentParser(description="研究主题 → 搜索关键词提取")
    parser.add_argument("topic", help="研究主题（自然语言）")
    parser.add_argument("--groups", type=int, default=3, help="关键词组数 (默认 3)")
    parser.add_argument("--no-llm", action="store_true", help="不用 LLM，只用规则")
    args = parser.parse_args()

    groups = extract(args.topic, args.groups, not args.no_llm)

    result = {"topic": args.topic, "groups": groups}
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
