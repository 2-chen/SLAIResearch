#!/usr/bin/env python3
"""
Independent literature search tool — directly calls academic APIs.
Usage: python search_papers.py "query" -o output_dir/
"""

import sys
import json
import time
import logging
from pathlib import Path
from datetime import datetime

import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("search")

from config import SEMANTIC_SCHOLAR_API_KEY

# ---------------------------------------------------------------------------
# arXiv API (free, no key)
# ---------------------------------------------------------------------------

def search_arxiv(query: str, max_results: int = 20) -> list[dict]:
    """Search arXiv via official API. Returns list of paper dicts."""
    url = "https://export.arxiv.org/api/query"
    params = {
        "search_query": f"all:{query}",
        "start": 0,
        "max_results": max_results,
        "sortBy": "relevance",
        "sortOrder": "descending",
    }
    logger.info("arXiv: searching %d papers for '%s' ...", max_results, query[:60])
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=30)
            if resp.status_code == 429:
                wait = 5 * (attempt + 1)
                logger.warning("arXiv rate limited, retrying in %ds ...", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            break
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(3)

    import xml.etree.ElementTree as ET
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }
    root = ET.fromstring(resp.text)
    papers = []
    for entry in root.findall("atom:entry", ns):
        papers.append({
            "source": "arxiv",
            "title": " ".join((entry.find("atom:title", ns).text or "").split()),
            "authors": [a.find("atom:name", ns).text for a in entry.findall("atom:author", ns)],
            "year": entry.find("atom:published", ns).text[:4] if entry.find("atom:published", ns) is not None else "",
            "abstract": " ".join((entry.find("atom:summary", ns).text or "").split()),
            "url": entry.find("atom:id", ns).text or "",
            "arxiv_id": entry.find("atom:id", ns).text.split("/")[-1] if entry.find("atom:id", ns) is not None else "",
            "category": entry.find("arxiv:primary_category", ns).get("term", "") if entry.find("arxiv:primary_category", ns) is not None else "",
        })
    return papers


# ---------------------------------------------------------------------------
# Semantic Scholar API (key required for higher rate limit)
# ---------------------------------------------------------------------------

def search_semantic_scholar(query: str, max_results: int = 20) -> list[dict]:
    """Search Semantic Scholar. Falls back gracefully if key is invalid."""
    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    headers = {}
    if SEMANTIC_SCHOLAR_API_KEY and "s2k-" in SEMANTIC_SCHOLAR_API_KEY:
        headers["x-api-key"] = SEMANTIC_SCHOLAR_API_KEY

    params = {
        "query": query,
        "limit": min(max_results, 100),
        "fields": "title,authors,year,abstract,url,externalIds,citationCount,venue",
    }
    logger.info("Semantic Scholar: searching %d papers for '%s' ...", max_results, query[:60])
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        if resp.status_code == 429:
            logger.warning("Semantic Scholar rate limited, skipping")
            return []
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Semantic Scholar error: %s", e)
        return []

    data = resp.json()
    papers = []
    for p in data.get("data", []):
        papers.append({
            "source": "semantic_scholar",
            "title": p.get("title", ""),
            "authors": [a.get("name", "") for a in p.get("authors", [])],
            "year": str(p.get("year", "")),
            "abstract": p.get("abstract", ""),
            "url": p.get("url", ""),
            "arxiv_id": p.get("externalIds", {}).get("ArXiv", ""),
            "citations": p.get("citationCount", 0),
            "venue": p.get("venue", ""),
        })
    return papers


# ---------------------------------------------------------------------------
# OpenAlex API (free, no key)
# ---------------------------------------------------------------------------

def search_openalex(query: str, max_results: int = 20) -> list[dict]:
    """Search OpenAlex works. Polite to the API with rate limiting."""
    url = "https://api.openalex.org/works"
    params = {
        "search": query,
        "per_page": min(max_results, 200),
        "sort": "cited_by_count:desc",
    }
    logger.info("OpenAlex: searching %d papers for '%s' ...", max_results, query[:60])
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("OpenAlex error: %s", e)
        return []

    data = resp.json()
    papers = []
    for p in data.get("results", []):
        # Reconstruct abstract from inverted index
        abstract = ""
        if p.get("abstract_inverted_index"):
            idx = p["abstract_inverted_index"]
            words = sorted([(pos, word) for word, positions in idx.items() for pos in positions])
            abstract = " ".join(w for _, w in words)

        papers.append({
            "source": "openalex",
            "title": p.get("title", ""),
            "authors": [a.get("author", {}).get("display_name", "") for a in p.get("authorships", [])],
            "year": str(p.get("publication_year", "")),
            "abstract": abstract,
            "url": p.get("doi", ""),
            "arxiv_id": p.get("primary_location", {}).get("landing_page_url", "").split("/")[-1] if p.get("primary_location") else "",
            "citations": p.get("cited_by_count", 0),
            "venue": p.get("primary_location", {}).get("source", {}).get("display_name", ""),
        })
    return papers


# ---------------------------------------------------------------------------
# Merge & deduplicate
# ---------------------------------------------------------------------------

def _title_key(title: str) -> str:
    return " ".join(title.lower().split())[:80]


def merge_results(all_papers: list[dict]) -> list[dict]:
    """Deduplicate by title, prefer entries with abstracts."""
    seen: dict[str, dict] = {}
    for p in sorted(all_papers, key=lambda x: len(x.get("abstract") or "")):
        key = _title_key(p["title"])
        if key not in seen:
            seen[key] = p
    # Sort by citations desc
    return sorted(seen.values(), key=lambda x: x.get("citations") or 0, reverse=True)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def format_markdown(papers: list[dict], query: str) -> str:
    """Format merged results as a Markdown literature review."""
    lines = [
        f"# Literature Review: {query}",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"Papers found: {len(papers)} (sources: arXiv, Semantic Scholar, OpenAlex)",
        "",
        "---",
        "",
        "## Summary of Papers",
        "",
    ]
    for i, p in enumerate(papers, 1):
        title = p["title"]
        authors = ", ".join(p["authors"][:5])
        if len(p["authors"]) > 5:
            authors += " et al."
        year = p.get("year", "?")
        venue = p.get("venue", "")
        citations = p.get("citations", 0)
        source = p["source"]
        abstract = p.get("abstract", "")[:500]

        lines.append(f"### {i}. {title}")
        lines.append(f"**{authors}** — {year} | {venue} | cited {citations}× | {source}")
        lines.append("")
        if abstract:
            lines.append(f"{abstract}")
            lines.append("")
        url = p.get("url", "")
        if url:
            lines.append(f"[Link]({url})")
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def format_bibtex(papers: list[dict]) -> str:
    """Generate BibTeX entries for all papers."""
    entries = []
    for i, p in enumerate(papers, 1):
        key = f"ref{i}"
        title = p["title"].replace("{", "\\{").replace("}", "\\}")
        authors = " and ".join(p["authors"][:5])
        year = p.get("year", "????")
        arxiv_id = p.get("arxiv_id", "")

        if arxiv_id:
            entries.append(
                f"@misc{{{key},\n"
                f"  title = {{{title}}},\n"
                f"  author = {{{authors}}},\n"
                f"  year = {{{year}}},\n"
                f"  eprint = {{{arxiv_id}}},\n"
                f"  archivePrefix = {{arXiv}},\n"
                f"}}"
            )
        else:
            entries.append(
                f"@misc{{{key},\n"
                f"  title = {{{title}}},\n"
                f"  author = {{{authors}}},\n"
                f"  year = {{{year}}},\n"
                f"}}"
            )
    return "\n\n".join(entries)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Search academic papers across multiple sources")
    parser.add_argument("query", help="Search query")
    parser.add_argument("-n", "--num", type=int, default=20, help="Max results per source")
    parser.add_argument("-o", "--output", default=".", help="Output directory")
    parser.add_argument("--no-arxiv", action="store_true")
    parser.add_argument("--no-s2", action="store_true")
    parser.add_argument("--no-openalex", action="store_true")
    args = parser.parse_args()

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_papers = []

    if not args.no_arxiv:
        try:
            all_papers.extend(search_arxiv(args.query, args.num))
        except Exception as e:
            logger.error("arXiv failed: %s", e)

    if not args.no_s2:
        try:
            all_papers.extend(search_semantic_scholar(args.query, args.num))
        except Exception as e:
            logger.error("Semantic Scholar failed: %s", e)

    if not args.no_openalex:
        try:
            all_papers.extend(search_openalex(args.query, args.num))
        except Exception as e:
            logger.error("OpenAlex failed: %s", e)

    if not all_papers:
        logger.error("No results from any source. Try a different query.")
        sys.exit(1)

    merged = merge_results(all_papers)
    logger.info("Total unique papers: %d", len(merged))

    # Write markdown review
    md = format_markdown(merged, args.query)
    md_path = out_dir / "literature_review.md"
    md_path.write_text(md)
    logger.info("Literature review: %s", md_path)

    # Write BibTeX
    bib = format_bibtex(merged)
    bib_path = out_dir / "references.bib"
    bib_path.write_text(bib)
    logger.info("BibTeX: %s", bib_path)

    # Print summary to stdout
    print(f"\nFound {len(merged)} unique papers across arXiv, Semantic Scholar, OpenAlex\n")
    for i, p in enumerate(merged[:10], 1):
        print(f"  {i}. {p['title'][:80]}")
        print(f"     {p.get('year','?')} | {p['source']} | cited {p.get('citations',0)}×")
    print(f"\nFull review: {md_path}")


if __name__ == "__main__":
    main()
