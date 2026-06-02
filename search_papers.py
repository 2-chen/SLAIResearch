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
import re
import hashlib
import urllib.parse

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("search")

from config import SEMANTIC_SCHOLAR_API_KEY

# ---------------------------------------------------------------------------
# Paper ID extraction (for dedup + PDF download)
# ---------------------------------------------------------------------------

def _extract_arxiv_id(url_or_text: str) -> str:
    """Extract arXiv ID from a URL or raw text. Returns empty string if none."""
    if not url_or_text:
        return ""
    # Patterns: arxiv.org/abs/2101.12345v2, arxiv:2101.12345
    for pat in [r'arxiv\.org/abs/([a-z0-9\-\.]+)', r'arxiv:([a-z0-9\-\.]+)',
                r'abs/([a-z0-9\-\.]+)']:
        m = re.search(pat, url_or_text, re.IGNORECASE)
        if m:
            return m.group(1).replace('v2', '').replace('v1', '')
    return ""


def _is_valid_arxiv_id(aid: str) -> bool:
    """Check if a string looks like a real arXiv ID (YYMM.NNNNN format)."""
    if not aid:
        return False
    return bool(re.match(r'^\d{4}\.\d{4,}(v\d+)?$', aid))


def _looks_like_acl_id(aid: str) -> bool:
    """Check if an ID looks like ACL Anthology: P19-1285 or 2024.acl-long.123."""
    if not aid:
        return False
    return bool(re.match(r'^[A-Z]\d{2}-\d{4,}$', aid)) or \
           bool(re.match(r'^\d{4}\.[a-z][a-z-]*\d*\.\d+$', aid))


def _paper_fingerprint(p: dict) -> str:
    """Deterministic paper ID: arxiv_id if available, otherwise title hash."""
    aid = p.get("arxiv_id", "")
    if aid:
        return f"arxiv:{aid}"
    title = " ".join((p.get("title") or "").lower().split())[:100]
    return f"title:{hashlib.md5(title.encode()).hexdigest()[:12]}"

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
        "fields": "title,authors,year,abstract,url,externalIds,citationCount,venue,openAccessPdf",
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
            "authors": [(a or {}).get("name", "") for a in (p.get("authors") or [])],
            "year": str(p.get("year", "")),
            "abstract": p.get("abstract", ""),
            "url": p.get("url", ""),
            "arxiv_id": p.get("externalIds", {}).get("ArXiv", ""),
            "open_access_pdf_url": (p.get("openAccessPdf") or {}).get("url", ""),
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

        # Only set arxiv_id if it matches arXiv format (avoid DOI suffixes etc.)
        raw_id = ((p.get("primary_location") or {}).get("landing_page_url") or "").split("/")[-1] if p.get("primary_location") else ""
        arxiv_id = raw_id if _is_valid_arxiv_id(raw_id) else ""

        # Also try to extract arXiv ID from open access URL (common for arXiv-hosted papers)
        oa_url = (p.get("open_access") or {}).get("oa_url", "")
        if not arxiv_id and oa_url and "arxiv.org" in oa_url:
            arxiv_id = _extract_arxiv_id(oa_url)

        papers.append({
            "source": "openalex",
            "title": p.get("title", ""),
            "authors": [(a.get("author") or {}).get("display_name", "") for a in (p.get("authorships") or [])],
            "year": str(p.get("publication_year", "")),
            "abstract": abstract,
            "url": p.get("doi", ""),
            "arxiv_id": arxiv_id,
            "open_access_pdf_url": oa_url,
            "citations": p.get("cited_by_count") or 0,
            "venue": ((p.get("primary_location") or {}).get("source") or {}).get("display_name", ""),
        })
    return papers


# ---------------------------------------------------------------------------
# JSON metadata export (for hypothesis engine integration)
# ---------------------------------------------------------------------------

def papers_to_json(papers: list[dict], output_path: Path) -> Path:
    """Serialize full paper metadata to structured JSON."""
    enriched = []
    for p in papers:
        arxiv_id = p.get("arxiv_id", "") or _extract_arxiv_id(p.get("url", ""))
        # Only keep if it looks like a real arXiv ID
        if arxiv_id and not _is_valid_arxiv_id(arxiv_id):
            arxiv_id = ""
        enriched.append({
            "title": p.get("title", ""),
            "authors": p.get("authors", []),
            "year": str(p.get("year", "")),
            "abstract": p.get("abstract", ""),
            "url": p.get("url", ""),
            "arxiv_id": arxiv_id,
            "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else "",
            "open_access_pdf_url": p.get("open_access_pdf_url", ""),
            "source": p.get("source", ""),
            "venue": p.get("venue", ""),
            "citations": p.get("citations") or 0,
            "category": p.get("category", ""),
            "paper_id": _paper_fingerprint(p),
        })
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(enriched, indent=2, ensure_ascii=False))
    logger.info("Paper metadata JSON → %s (%d papers)", output_path, len(enriched))
    return output_path


def load_papers_json(json_path: Path) -> list[dict]:
    """Load paper metadata from a JSON file."""
    return json.loads(Path(json_path).read_text())


def _download_url(url: str, pdf_path: Path, timeout: int, source: str = "") -> Path | None:
    """Download from a URL to pdf_path. Verifies PDF header. Returns path or None."""
    logger.info("Downloading PDF (%s): %s", source, url)
    try:
        resp = requests.get(url, timeout=timeout, stream=True)
        if resp.status_code != 200:
            logger.warning("PDF download failed (%d) [%s]: %s", resp.status_code, source, url)
            return None
        with pdf_path.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        # Verify it's a real PDF
        if pdf_path.stat().st_size < 100:
            logger.warning("Downloaded file too small (%d bytes), deleting", pdf_path.stat().st_size)
            pdf_path.unlink()
            return None
        with pdf_path.open("rb") as f:
            if f.read(5) != b'%PDF-':
                logger.warning("Downloaded file is not a PDF, deleting")
                pdf_path.unlink()
                return None
        logger.info("PDF saved: %s (%d bytes)", pdf_path.name, pdf_path.stat().st_size)
        return pdf_path
    except requests.RequestException as e:
        logger.warning("PDF download error [%s]: %s", source, e)
        return None


def download_paper_pdf(paper: dict, output_dir: Path, timeout: int = 60) -> Path | None:
    """Download paper PDF, trying multiple sources in order:
    1. arXiv (most reliable, cleanest LaTeX-sourced text)
    2. ACL Anthology (fully open access)
    3. Open access URL from Semantic Scholar / OpenAlex

    Returns path to downloaded PDF, or None if all sources fail.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Use paper fingerprint for filename (handles non-arXiv papers)
    safe_name = paper.get("paper_id", _paper_fingerprint(paper)).replace(":", "_").replace("/", "_")
    pdf_path = output_dir / f"{safe_name}.pdf"

    if pdf_path.exists():
        logger.info("PDF already downloaded: %s", pdf_path.name)
        return pdf_path

    aid = paper.get("arxiv_id", "")

    # Source 1: arXiv
    if _is_valid_arxiv_id(aid):
        result = _download_url(f"https://arxiv.org/pdf/{aid}.pdf", pdf_path, timeout, source="arXiv")
        if result:
            return result

    # Source 2: ACL Anthology (e.g. P19-1285, 2024.acl-long.123)
    if _looks_like_acl_id(aid):
        result = _download_url(f"https://aclanthology.org/{aid}.pdf", pdf_path, timeout, source="ACL")
        if result:
            return result

    # Source 3: Open access URL from Semantic Scholar / OpenAlex
    oa_url = paper.get("open_access_pdf_url", "")
    if oa_url:
        result = _download_url(oa_url, pdf_path, timeout, source="open access")
        if result:
            return result

    # Source 4: Try DOI-based Unpaywall / open access
    doi = paper.get("url", "")
    if doi and doi.startswith("https://doi.org/"):
        result = _download_url(doi, pdf_path, timeout, source="DOI")
        if result:
            return result

    logger.info("No downloadable source found for: %s", paper.get("title", "?")[:80])
    return None


def download_pdf(arxiv_id: str, output_dir: Path, timeout: int = 60) -> Path | None:
    """Download a paper PDF from arXiv by its ID. (Backward-compatible wrapper.)
    Prefer download_paper_pdf() for multi-source support."""
    return download_paper_pdf({"arxiv_id": arxiv_id}, output_dir, timeout)


def download_top_pdfs(papers: list[dict], output_dir: Path, top_k: int = 5) -> list[Path]:
    """Download PDFs for the top-K papers, trying multiple sources per paper.
    Prioritizes papers with known downloadable sources, then by citations."""
    downloaded = []

    def _source_score(p: dict) -> tuple:
        """Higher score = more likely to download successfully."""
        aid = p.get("arxiv_id", "")
        if _is_valid_arxiv_id(aid):
            return (3, p.get("citations") or 0)
        if _looks_like_acl_id(aid):
            return (2, p.get("citations") or 0)
        if p.get("open_access_pdf_url"):
            return (1, p.get("citations") or 0)
        return (0, p.get("citations") or 0)

    candidates = sorted(papers, key=_source_score, reverse=True)

    for p in candidates[:top_k]:
        path = download_paper_pdf(p, output_dir)
        if path:
            downloaded.append(path)
        time.sleep(1)  # Be polite to servers
    return downloaded


def extract_pdf_text(pdf_path: Path) -> str:
    """Extract text from a PDF file using pdftotext. Returns empty string on failure."""
    import subprocess
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), "-"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
        return ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        logger.warning("pdftotext not available, cannot extract text from %s", pdf_path.name)
        return ""


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
        abstract = (p.get("abstract") or "")[:500]

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
    parser.add_argument("--save-json", default=None, help="Save paper metadata as JSON to this path")
    parser.add_argument("--download-pdfs", default=None, help="Download top-N PDFs to this directory (N from --num)")
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

    # Save structured JSON metadata
    if args.save_json:
        papers_to_json(merged, Path(args.save_json))

    # Download PDFs for top-N papers
    if args.download_pdfs:
        pdf_dir = Path(args.download_pdfs)
        n_pdfs = min(args.num, 10)  # Cap at 10 to be polite
        pdfs = download_top_pdfs(merged, pdf_dir, top_k=n_pdfs)
        logger.info("Downloaded %d PDFs to %s", len(pdfs), pdf_dir)

    # Print summary to stdout
    print(f"\nFound {len(merged)} unique papers across arXiv, Semantic Scholar, OpenAlex\n")
    for i, p in enumerate(merged[:10], 1):
        print(f"  {i}. {p['title'][:80]}")
        print(f"     {p.get('year','?')} | {p['source']} | cited {p.get('citations',0)}×")
    print(f"\nFull review: {md_path}")


if __name__ == "__main__":
    main()
