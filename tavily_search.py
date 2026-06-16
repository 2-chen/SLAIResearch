#!/usr/bin/env python3
"""
Tavily web search integration for SLAIResearch.

Used in literature search and revision stages to find:
- Recent papers and preprints (arxiv.org, openreview.net)
- Benchmark leaderboards and SOTA results
- Related code repositories (github.com)
- Supplementary materials and datasets

API docs: https://docs.tavily.com/

Usage:
    python tavily_search.py "query" --max-results 10 --format markdown
    python tavily_search.py "query" --search-depth advanced --include-raw
"""

import json
import os
import sys
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))


class TavilySearch:
    """Minimal Tavily API client. Uses requests to keep deps light."""

    BASE_URL = "https://api.tavily.com/search"

    def __init__(self, api_key: str = ""):
        self.api_key = api_key or os.environ.get("TAVILY_API_KEY", "")
        if not self.api_key:
            from config import TAVILY_API_KEY
            self.api_key = TAVILY_API_KEY

    def search(
        self,
        query: str,
        max_results: int = 10,
        search_depth: str = "basic",
        include_domains: Optional[list[str]] = None,
        exclude_domains: Optional[list[str]] = None,
        include_raw_content: bool = False,
    ) -> dict:
        """Execute a Tavily search and return structured results."""
        import requests

        payload = {
            "api_key": self.api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": search_depth,
            "include_raw_content": include_raw_content,
        }
        if include_domains:
            payload["include_domains"] = include_domains
        if exclude_domains:
            payload["exclude_domains"] = exclude_domains

        resp = requests.post(self.BASE_URL, json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def search_academic(
        self,
        query: str,
        max_results: int = 10,
    ) -> dict:
        """Academic-focused search — prioritizes arxiv, openreview, semanticscholar."""
        return self.search(
            query=query,
            max_results=max_results,
            search_depth="advanced",
            include_domains=[
                "arxiv.org",
                "openreview.net",
                "semanticscholar.org",
                "paperswithcode.com",
                "github.com",
            ],
            include_raw_content=False,
        )

    def search_related_work(
        self,
        topic: str,
        max_results: int = 15,
    ) -> dict:
        """Broad search for related work, code, and datasets."""
        return self.search(
            query=f"{topic} research paper benchmark state of the art",
            max_results=max_results,
            search_depth="advanced",
            include_raw_content=False,
        )


def format_markdown(results: dict, title: str = "Tavily Web Search") -> str:
    """Format search results as markdown."""
    lines = [f"## {title}", "", f"**Query**: {results.get('query', '')}", ""]
    answer = results.get("answer")
    if answer:
        lines.append(f"**AI Answer**: {answer}")
        lines.append("")

    for i, r in enumerate(results.get("results", []), 1):
        url = r.get("url", "")
        title_text = r.get("title", "Untitled")
        content = r.get("content", "")
        score = r.get("score", 0)
        lines.append(f"{i}. **[{title_text}]({url})** (score: {score:.2f})")
        if content:
            lines.append(f"   {content[:300]}")
        lines.append("")

    return "\n".join(lines)


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Tavily web search for SLAIResearch"
    )
    parser.add_argument("query", help="Search query")
    parser.add_argument(
        "--max-results", type=int, default=10, help="Max results (default 10)"
    )
    parser.add_argument(
        "--search-depth",
        choices=["basic", "advanced"],
        default="advanced",
        help="Search depth (default: advanced)",
    )
    parser.add_argument(
        "--format",
        choices=["json", "markdown"],
        default="markdown",
        help="Output format",
    )
    parser.add_argument(
        "--output", "-o", help="Output file path (prints to stdout if not set)"
    )
    parser.add_argument(
        "--mode",
        choices=["general", "academic", "related-work"],
        default="general",
        help="Search mode: general, academic (arxiv/openreview), related-work (broad)",
    )
    parser.add_argument(
        "--include-raw",
        action="store_true",
        help="Include raw page content",
    )
    parser.add_argument(
        "--topic",
        help="Research topic (used in related-work mode for context)",
    )

    args = parser.parse_args()

    searcher = TavilySearch()
    if not searcher.api_key:
        print("ERROR: TAVILY_API_KEY not set in environment or config.py", file=sys.stderr)
        sys.exit(1)

    try:
        if args.mode == "academic":
            results = searcher.search_academic(args.query, args.max_results)
        elif args.mode == "related-work":
            results = searcher.search_related_work(
                args.topic or args.query, args.max_results
            )
        else:
            results = searcher.search(
                query=args.query,
                max_results=args.max_results,
                search_depth=args.search_depth,
                include_raw_content=args.include_raw,
            )
    except Exception as e:
        print(f"Tavily search failed: {e}", file=sys.stderr)
        sys.exit(1)

    if args.format == "json":
        output = json.dumps(results, indent=2, ensure_ascii=False)
    else:
        output = format_markdown(results)

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(output)
        print(f"Results saved to {args.output}")
    else:
        print(output)


if __name__ == "__main__":
    main()
