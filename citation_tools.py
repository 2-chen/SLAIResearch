#!/usr/bin/env python3
"""
Citation tools — DOI/BibTeX conversion, metadata extraction, citation verification,
Google Scholar search, and DataCite dataset/software lookup.

All APIs are FREE — no API keys required.

Usage:
    python citation_tools.py doi-to-bibtex 10.1038/s41586-021-03819-2
    python citation_tools.py extract --doi 10.1038/s41586-021-03819-2
    python citation_tools.py extract --arxiv 2101.12345
    python citation_tools.py verify --file literature_review.md
    python citation_tools.py scholar "attention mechanisms" --limit 20
    python citation_tools.py datacite 10.5281/zenodo.10481703
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional, Dict, List, Tuple
from urllib.parse import urlparse

import requests

from config import (
    CROSSREF_RATE_LIMIT_DELAY,
    SCHOLARLY_MAX_RESULTS,
    DATACITE_RATE_LIMIT_DELAY,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("citation_tools")

# ---------------------------------------------------------------------------
# Optional dependency: scholarly (Google Scholar scraping)
# ---------------------------------------------------------------------------
try:
    import scholarly

    SCHOLARLY_AVAILABLE = True
except ImportError:
    scholarly = None  # type: ignore[assignment]
    SCHOLARLY_AVAILABLE = False
    logger.warning(
        "scholarly package not installed. Google Scholar search disabled. "
        "Install with: pip install scholarly"
    )


# ============================================================================
# Class 1: DOIConverter — CrossRef DOI → BibTeX
# ============================================================================

class DOIConverter:
    """Convert DOIs to BibTeX entries using CrossRef content negotiation.
    Free, no API key required."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "SLAIResearch/1.0 (Citation Tools; mailto:research@example.com)"
        })

    def clean_doi(self, doi: str) -> str:
        """Strip URL prefixes and whitespace from a DOI."""
        doi = doi.strip()
        for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
            if doi.startswith(prefix):
                doi = doi.replace(prefix, "", 1)
                break
        return doi

    def doi_to_bibtex(self, doi: str) -> Optional[str]:
        """Convert a single DOI to BibTeX via CrossRef content negotiation.

        Returns BibTeX string or None on failure."""
        doi = self.clean_doi(doi)
        url = f"https://doi.org/{doi}"
        headers = {
            "Accept": "application/x-bibtex",
            "User-Agent": "SLAIResearch/1.0 (Citation Tools)",
        }
        try:
            resp = self.session.get(url, headers=headers, timeout=15)
            if resp.status_code == 200:
                bibtex = resp.text.strip()
                # CrossRef sometimes returns @data type for datasets
                if bibtex.startswith("@data{"):
                    bibtex = bibtex.replace("@data{", "@misc{", 1)
                return bibtex
            elif resp.status_code == 404:
                logger.warning("DOI not found: %s", doi)
                return None
            elif resp.status_code == 429:
                logger.warning("CrossRef rate limited, retrying in 2s...")
                time.sleep(2)
                return self.doi_to_bibtex(doi)  # single retry
            else:
                logger.warning(
                    "CrossRef returned status %d for DOI: %s", resp.status_code, doi
                )
                return None
        except requests.exceptions.Timeout:
            logger.warning("Request timeout for DOI: %s", doi)
            return None
        except requests.exceptions.RequestException as e:
            logger.warning("Request failed for DOI %s: %s", doi, e)
            return None

    def convert_multiple(
        self, dois: List[str], delay: float | None = None
    ) -> List[str]:
        """Convert multiple DOIs to BibTeX with rate limiting.

        Args:
            dois: List of DOIs to convert.
            delay: Seconds between requests. Defaults to CROSSREF_RATE_LIMIT_DELAY.

        Returns:
            List of BibTeX strings (failed conversions excluded).
        """
        if delay is None:
            delay = CROSSREF_RATE_LIMIT_DELAY
        entries = []
        for i, doi in enumerate(dois):
            logger.info("Converting DOI %d/%d: %s", i + 1, len(dois), doi)
            bibtex = self.doi_to_bibtex(doi)
            if bibtex:
                entries.append(bibtex)
            if i < len(dois) - 1:
                time.sleep(delay)
        return entries

    def _get_crossref_metadata(self, doi: str) -> Optional[Dict]:
        """Fetch structured metadata from CrossRef API for a DOI."""
        doi = self.clean_doi(doi)
        url = f"https://api.crossref.org/works/{doi}"
        try:
            resp = self.session.get(url, timeout=15)
            if resp.status_code != 200:
                return None
            data = resp.json()
            message = data.get("message", {})
            return {
                "title": (message.get("title") or [""])[0],
                "authors": self._format_authors(message.get("author", [])),
                "year": self._extract_year(message),
                "journal": (message.get("container-title") or [""])[0]
                if message.get("container-title")
                else "",
                "volume": str(message.get("volume", "")),
                "pages": message.get("page", ""),
                "publisher": message.get("publisher", ""),
                "doi": doi,
            }
        except Exception as e:
            logger.debug("CrossRef metadata error for %s: %s", doi, e)
            return None

    @staticmethod
    def _format_authors(authors: List[Dict]) -> str:
        """Format author list as 'Last, First and Last, First'."""
        if not authors:
            return ""
        formatted = []
        for a in authors:
            given = a.get("given", "")
            family = a.get("family", "")
            if family:
                formatted.append(f"{family}, {given}" if given else family)
        return " and ".join(formatted)

    @staticmethod
    def _extract_year(message: Dict) -> str:
        """Extract publication year from CrossRef message."""
        for key in ("published-print", "published-online", "issued"):
            date_parts = message.get(key, {}).get("date-parts", [[]])
            if date_parts and date_parts[0]:
                return str(date_parts[0][0])
        return ""


# ============================================================================
# Class 2: MetadataExtractor — DOI/arXiv ID → structured metadata + BibTeX
# ============================================================================

class MetadataExtractor:
    """Extract structured citation metadata from DOI or arXiv ID.

    Uses CrossRef API for DOIs and arXiv API for arXiv IDs.
    Both are FREE — no API keys required.
    PubMed/PMID support intentionally excluded (biomedical focus).
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "SLAIResearch/1.0 (Metadata Extractor)"
        })

    def identify_type(self, identifier: str) -> Tuple[str, str]:
        """Identify the type of a research identifier.

        Returns (type, cleaned_identifier) where type is one of:
        'doi', 'arxiv', 'url', or 'unknown'.
        """
        identifier = identifier.strip()

        # URL
        if identifier.startswith(("http://", "https://")):
            return self._parse_url(identifier)

        # DOI: starts with 10.
        if re.match(r"^10\.\d{4,}/", identifier):
            return ("doi", identifier)

        # arXiv ID: YYMM.NNNNN or YYMM.NNNNNvN
        if re.match(r"^\d{4}\.\d{4,}(v\d+)?$", identifier):
            return ("arxiv", identifier)
        # arXiv: prefix
        if identifier.startswith("arXiv:"):
            return ("arxiv", identifier.replace("arXiv:", ""))

        return ("unknown", identifier)

    @staticmethod
    def _parse_url(url: str) -> Tuple[str, str]:
        """Extract identifier type and value from a URL."""
        parsed = urlparse(url)

        # DOI URL
        if "doi.org" in parsed.netloc:
            doi = parsed.path.lstrip("/")
            return ("doi", doi)

        # arXiv URL
        if "arxiv.org" in parsed.netloc:
            m = re.search(r"/abs/(\d{4}\.\d{4,})", parsed.path)
            if m:
                return ("arxiv", m.group(1))

        # Try to find a DOI anywhere in the URL
        m = re.search(r"10\.\d{4,}/[^\s]+", url)
        if m:
            return ("doi", m.group())

        return ("url", url)

    def extract_from_doi(self, doi: str) -> Optional[Dict]:
        """Extract structured metadata from a DOI via CrossRef API."""
        # Strip prefix
        doi = re.sub(r"^(https?://doi\.org/|doi:)", "", doi.strip())
        url = f"https://api.crossref.org/works/{doi}"

        try:
            resp = self.session.get(url, timeout=15)
            if resp.status_code != 200:
                logger.warning("CrossRef returned %d for DOI: %s", resp.status_code, doi)
                return None

            data = resp.json()
            message = data.get("message", {})

            return {
                "type": "doi",
                "entry_type": self._crossref_type_to_bibtex(message.get("type")),
                "doi": doi,
                "title": (message.get("title") or [""])[0],
                "authors": self._format_authors_crossref(message.get("author", [])),
                "year": self._extract_year_crossref(message),
                "journal": (message.get("container-title") or [""])[0]
                if message.get("container-title")
                else "",
                "volume": str(message.get("volume", ""))
                if message.get("volume")
                else "",
                "issue": str(message.get("issue", ""))
                if message.get("issue")
                else "",
                "pages": message.get("page", ""),
                "publisher": message.get("publisher", ""),
                "url": f"https://doi.org/{doi}",
            }
        except Exception as e:
            logger.warning("Error extracting from DOI %s: %s", doi, e)
            return None

    def extract_from_arxiv(self, arxiv_id: str) -> Optional[Dict]:
        """Extract metadata from an arXiv ID via the arXiv Atom API."""
        arxiv_id = arxiv_id.strip()
        if arxiv_id.startswith("arXiv:"):
            arxiv_id = arxiv_id.replace("arXiv:", "")

        url = "http://export.arxiv.org/api/query"
        params = {"id_list": arxiv_id, "max_results": 1}

        try:
            resp = self.session.get(url, params=params, timeout=15)
            if resp.status_code != 200:
                logger.warning("arXiv returned %d for: %s", resp.status_code, arxiv_id)
                return None

            ns = {
                "atom": "http://www.w3.org/2005/Atom",
                "arxiv": "http://arxiv.org/schemas/atom",
            }
            root = ET.fromstring(resp.content)
            entry = root.find("atom:entry", ns)
            if entry is None:
                logger.warning("No entry found for arXiv ID: %s", arxiv_id)
                return None

            # DOI (if published version exists)
            doi_elem = entry.find("arxiv:doi", ns)
            doi = doi_elem.text if doi_elem is not None else None

            # Journal reference
            journal_ref_elem = entry.find("arxiv:journal_ref", ns)
            journal_ref = journal_ref_elem.text if journal_ref_elem is not None else None

            # Authors
            authors = []
            for author in entry.findall("atom:author", ns):
                name = author.findtext("atom:name", "", ns)
                if name:
                    authors.append(name)

            # Publication date
            published = entry.findtext("atom:published", "", ns)
            year = published[:4] if published else ""

            return {
                "type": "arxiv",
                "entry_type": "article" if doi else "misc",
                "arxiv_id": arxiv_id,
                "title": (
                    entry.findtext("atom:title", "", ns).strip().replace("\n", " ")
                ),
                "authors": " and ".join(authors),
                "year": year,
                "doi": doi,
                "journal_ref": journal_ref,
                "abstract": (
                    entry.findtext("atom:summary", "", ns)
                    .strip()
                    .replace("\n", " ")
                ),
                "url": f"https://arxiv.org/abs/{arxiv_id}",
            }
        except Exception as e:
            logger.warning("Error from arXiv %s: %s", arxiv_id, e)
            return None

    def metadata_to_bibtex(
        self, metadata: Dict, citation_key: Optional[str] = None
    ) -> str:
        """Convert a metadata dict to a formatted BibTeX entry."""
        if not citation_key:
            citation_key = self._generate_citation_key(metadata)

        entry_type = metadata.get("entry_type", "misc")
        lines = [f"@{entry_type}{{{citation_key},"]

        if metadata.get("authors"):
            lines.append(f'  author  = {{{metadata["authors"]}}},')

        if metadata.get("title"):
            title = self._protect_title(metadata["title"])
            lines.append(f"  title   = {{{title}}},")

        if entry_type == "article" and metadata.get("journal"):
            lines.append(f'  journal = {{{metadata["journal"]}}},')
        elif entry_type == "misc" and metadata.get("type") == "arxiv":
            lines.append("  howpublished = {arXiv},")

        if metadata.get("year"):
            lines.append(f'  year    = {{{metadata["year"]}}},')

        if metadata.get("volume"):
            lines.append(f'  volume  = {{{metadata["volume"]}}},')

        if metadata.get("issue"):
            lines.append(f'  number  = {{{metadata["issue"]}}},')

        if metadata.get("pages"):
            pages = metadata["pages"].replace("-", "--")
            lines.append(f"  pages   = {{{pages}}},")

        if metadata.get("doi"):
            lines.append(f'  doi     = {{{metadata["doi"]}}},')
        elif metadata.get("url"):
            lines.append(f'  url     = {{{metadata["url"]}}},')

        if metadata.get("type") == "arxiv" and not metadata.get("doi"):
            lines.append("  note    = {Preprint},")

        # Remove trailing comma from last field line
        if lines[-1].endswith(","):
            lines[-1] = lines[-1][:-1]

        lines.append("}")
        return "\n".join(lines)

    def extract(self, identifier: str) -> Optional[str]:
        """Auto-detect identifier type and return BibTeX."""
        id_type, clean_id = self.identify_type(identifier)
        logger.info("Identified as %s: %s", id_type, clean_id)

        metadata = None
        if id_type == "doi":
            metadata = self.extract_from_doi(clean_id)
        elif id_type == "arxiv":
            metadata = self.extract_from_arxiv(clean_id)
        else:
            logger.warning("Unknown identifier type: %s", identifier)
            return None

        if metadata:
            return self.metadata_to_bibtex(metadata)
        return None

    # ---- helpers ----

    @staticmethod
    def _crossref_type_to_bibtex(crossref_type: str) -> str:
        mapping = {
            "journal-article": "article",
            "book": "book",
            "book-chapter": "incollection",
            "proceedings-article": "inproceedings",
            "posted-content": "misc",
            "dataset": "misc",
            "report": "techreport",
            "book-section": "incollection",
            "book-part": "incollection",
        }
        return mapping.get(crossref_type, "misc")

    @staticmethod
    def _format_authors_crossref(authors: List[Dict]) -> str:
        if not authors:
            return ""
        formatted = []
        for a in authors:
            given = a.get("given", "")
            family = a.get("family", "")
            if family:
                formatted.append(f"{family}, {given}" if given else family)
        return " and ".join(formatted)

    @staticmethod
    def _extract_year_crossref(message: Dict) -> str:
        for key in ("published-print", "published-online", "issued"):
            date_parts = message.get(key, {}).get("date-parts", [[]])
            if date_parts and date_parts[0]:
                return str(date_parts[0][0])
        return ""

    @staticmethod
    def _generate_citation_key(metadata: Dict) -> str:
        authors = metadata.get("authors", "")
        if authors:
            first_author = authors.split(" and ")[0]
            last_name = (
                first_author.split(",")[0].strip()
                if "," in first_author
                else (first_author.split()[-1] if first_author else "Unknown")
            )
        else:
            last_name = "Unknown"
        last_name = re.sub(r"[^a-zA-Z]", "", last_name)

        year = (metadata.get("year") or "XXXX").strip()
        title = metadata.get("title", "")
        words = re.findall(r"\b[a-zA-Z]{4,}\b", title)
        keyword = words[0].lower() if words else "paper"

        return f"{last_name}{year}{keyword}"

    @staticmethod
    def _protect_title(title: str) -> str:
        """Wrap known acronyms/proper nouns in braces for BibTeX capitalisation."""
        protected = [
            "DNA", "RNA", "CRISPR", "COVID", "HIV", "AIDS", "AlphaFold",
            "Python", "AI", "ML", "GPU", "CPU", "USA", "UK", "EU",
            "BERT", "GPT", "CNN", "RNN", "LSTM", "GAN", "RL", "NLP",
        ]
        for word in protected:
            title = re.sub(rf"\b{word}\b", f"{{{word}}}", title, flags=re.IGNORECASE)
        return title


# ============================================================================
# Class 3: CitationVerifier — DOI extraction, validation, formatting
# ============================================================================

class CitationVerifier:
    """Verify DOIs in text, validate them, and format citations in APA/Nature style.

    Uses CrossRef and DOI Handle APIs — free, no API keys required."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "SLAIResearch/1.0 (Citation Verifier)"
        })

    @staticmethod
    def extract_dois(text: str) -> List[str]:
        """Extract all DOI-like strings from text."""
        return re.findall(r"10\.\d{4,}/[^\s\]\)\"\']+", text)

    def verify_doi(self, doi: str) -> Tuple[bool, Dict]:
        """Verify a DOI exists and retrieve its metadata.

        Returns (is_valid, metadata_dict)."""
        doi = doi.strip().rstrip(".")
        # First check via CrossRef
        metadata = self._get_crossref_metadata(doi)
        if metadata:
            return True, metadata
        # Fallback: Handle API
        return self._check_handle(doi)

    def _get_crossref_metadata(self, doi: str) -> Dict:
        """Fetch metadata from CrossRef API."""
        try:
            resp = self.session.get(
                f"https://api.crossref.org/works/{doi}", timeout=10
            )
            if resp.status_code != 200:
                return {}
            data = resp.json()
            message = data.get("message", {})
            return {
                "title": (message.get("title") or [""])[0],
                "authors": self._format_authors_short(message.get("author", [])),
                "year": self._extract_year(message),
                "journal": (message.get("container-title") or [""])[0]
                if message.get("container-title")
                else "",
                "volume": message.get("volume", ""),
                "pages": message.get("page", ""),
                "doi": doi,
            }
        except Exception as e:
            return {"error": str(e)}

    def _check_handle(self, doi: str) -> Tuple[bool, Dict]:
        """Fallback check via DOI Handle API."""
        try:
            resp = self.session.get(
                f"https://doi.org/api/handles/{doi}", timeout=10
            )
            return (resp.status_code == 200, {})
        except Exception:
            return (False, {})

    def verify_url(self, url: str) -> Tuple[bool, int]:
        """Check if a URL is accessible. Returns (is_accessible, status_code)."""
        try:
            resp = self.session.head(url, timeout=10, allow_redirects=True)
            return (resp.status_code < 400, resp.status_code)
        except Exception:
            return (False, 0)

    def verify_citations_in_file(self, filepath: str) -> Dict:
        """Extract and verify all DOIs in a markdown/text file.

        Returns a verification report dict."""
        path = Path(filepath)
        if not path.exists():
            return {"error": f"File not found: {filepath}", "total_dois": 0}

        content = path.read_text(encoding="utf-8")
        dois = self.extract_dois(content)

        report: Dict = {
            "file": str(path),
            "total_dois": len(dois),
            "verified": [],
            "failed": [],
            "metadata": {},
        }

        for doi in dois:
            logger.info("Verifying DOI: %s", doi)
            is_valid, metadata = self.verify_doi(doi)
            if is_valid:
                report["verified"].append(doi)
                report["metadata"][doi] = metadata
            else:
                report["failed"].append(doi)
            time.sleep(0.5)  # Polite rate limiting

        return report

    @staticmethod
    def _format_authors_short(authors: List[Dict]) -> str:
        """Format up to 3 authors as 'Last, X.' with et al. for more."""
        if not authors:
            return ""
        formatted = []
        for a in authors[:3]:
            given = a.get("given", "")
            family = a.get("family", "")
            if family:
                formatted.append(
                    f"{family}, {given[0]}." if given else family
                )
        if len(authors) > 3:
            formatted.append("et al.")
        return ", ".join(formatted)

    @staticmethod
    def _extract_year(message: Dict) -> str:
        for key in ("published-print", "published-online", "issued"):
            date_parts = message.get(key, {}).get("date-parts", [[]])
            if date_parts and date_parts[0]:
                return str(date_parts[0][0])
        return ""

    @staticmethod
    def format_citation_apa(metadata: Dict) -> str:
        """Format a citation in APA style (7th ed.)."""
        authors = metadata.get("authors", "")
        year = metadata.get("year", "n.d.")
        title = metadata.get("title", "")
        journal = metadata.get("journal", "")
        volume = metadata.get("volume", "")
        pages = metadata.get("pages", "")
        doi = metadata.get("doi", "")

        cite = f"{authors} ({year}). {title}."
        if journal:
            cite += f" *{journal}*"
        if volume:
            cite += f", *{volume}*"
        if pages:
            cite += f", {pages}"
        if doi:
            cite += f". https://doi.org/{doi}"
        return cite

    @staticmethod
    def format_citation_nature(metadata: Dict) -> str:
        """Format a citation in Nature style."""
        authors = metadata.get("authors", "")
        title = metadata.get("title", "")
        journal = metadata.get("journal", "")
        volume = metadata.get("volume", "")
        pages = metadata.get("pages", "")
        year = metadata.get("year", "")

        cite = f"{authors} {title}."
        if journal:
            cite += f" *{journal}*"
        if volume:
            cite += f" **{volume}**"
        if pages:
            cite += f", {pages}"
        if year:
            cite += f" ({year})"
        return cite


# ============================================================================
# Class 4: GoogleScholarSearch — search via scholarly package
# ============================================================================

class GoogleScholarSearch:
    """Search Google Scholar using the `scholarly` Python package.

    Free, no API key required. Uses web scraping — may be rate-limited.
    Falls back gracefully if scholarly is not installed."""

    def __init__(self):
        self._available = SCHOLARLY_AVAILABLE
        if not self._available:
            logger.warning(
                "scholarly not installed. Google Scholar search disabled. "
                "Install: pip install scholarly"
            )

    def search(
        self,
        query: str,
        max_results: int = 50,
        year_start: Optional[int] = None,
        year_end: Optional[int] = None,
    ) -> List[Dict]:
        """Search Google Scholar and return structured paper dicts.

        Args:
            query: Search query string.
            max_results: Maximum number of results (capped by SCHOLARLY_MAX_RESULTS).
            year_start: Optional minimum publication year.
            year_end: Optional maximum publication year.

        Returns:
            List of paper dicts with keys: title, authors, year, abstract,
            citations, url, venue, source.
        """
        if not self._available:
            return []

        max_results = min(max_results, SCHOLARLY_MAX_RESULTS)
        papers = []

        try:
            search_query = scholarly.search_pubs(query)
            count = 0
            for pub in search_query:
                if count >= max_results:
                    break
                try:
                    paper = self._parse_pub(pub)
                    if paper:
                        # Year filter
                        year_str = paper.get("year", "")
                        if year_start or year_end:
                            try:
                                y = int(year_str) if year_str else 0
                                if year_start and y < year_start:
                                    continue
                                if year_end and y > year_end:
                                    continue
                            except (ValueError, TypeError):
                                pass
                        papers.append(paper)
                        count += 1
                except Exception as e:
                    logger.debug("Error parsing scholar result: %s", e)
                    continue

                # Polite delay to avoid blocking
                time.sleep(2 + (hashlib.md5(str(count).encode()).digest()[0] % 30) / 10)

        except Exception as e:
            logger.warning("Google Scholar search error: %s", e)

        logger.info("Google Scholar: found %d results for '%s'", len(papers), query[:60])
        return papers

    @staticmethod
    def _parse_pub(pub) -> Optional[Dict]:
        """Parse a scholarly publication object into a standard dict."""
        bib = pub.get("bib", {})
        title = bib.get("title", "").strip()
        if not title:
            return None

        authors = bib.get("author", [])
        if isinstance(authors, list):
            author_str = ", ".join(authors)
        else:
            author_str = str(authors) if authors else ""

        year = bib.get("pub_year", "")
        if year:
            year = str(year)

        return {
            "source": "google_scholar",
            "title": title,
            "authors": author_str,
            "year": year,
            "abstract": bib.get("abstract", ""),
            "citations": bib.get("num_citations", 0) or 0,
            "url": bib.get("url", "") or bib.get("eprint_url", "") or "",
            "venue": bib.get("venue", "") or bib.get("journal", "") or "",
        }

    @staticmethod
    def metadata_to_bibtex(metadata: Dict, cite_key: str = "ref") -> str:
        """Generate a BibTeX entry from Google Scholar metadata."""
        title = (metadata.get("title") or "").replace("{", "\\{").replace("}", "\\}")
        authors = (metadata.get("authors") or "Unknown").replace(" and ", " and ")
        year = metadata.get("year", "") or "????"
        url = metadata.get("url", "")

        lines = [
            f"@misc{{{cite_key},",
            f"  title = {{{title}}},",
            f"  author = {{{authors}}},",
            f"  year = {{{year}}},",
        ]
        if url:
            lines.append(f"  url = {{{url}}},")
        lines.append("}")
        return "\n".join(lines)


# ============================================================================
# Class 5: DataCiteClient — dataset/software DOI lookup
# ============================================================================

class DataCiteClient:
    """Look up datasets, software, and other research outputs via DataCite API.

    DataCite is the DOI registration agency for research data.
    API is FREE — no key required.  https://api.datacite.org"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "SLAIResearch/1.0 (DataCite Client; mailto:research@example.com)"
        })

    def lookup_doi(self, doi: str) -> Optional[Dict]:
        """Look up a single DataCite DOI and return structured metadata."""
        doi = doi.strip()
        if doi.startswith("https://doi.org/"):
            doi = doi.replace("https://doi.org/", "")
        elif doi.startswith("doi:"):
            doi = doi.replace("doi:", "")

        url = f"https://api.datacite.org/dois/{doi}"
        try:
            resp = self.session.get(url, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                attrs = data.get("data", {}).get("attributes", {})
                return self._parse_attributes(attrs, doi)
            elif resp.status_code == 404:
                logger.warning("DataCite DOI not found: %s", doi)
                return None
            else:
                logger.warning("DataCite returned %d for: %s", resp.status_code, doi)
                return None
        except Exception as e:
            logger.warning("DataCite error for %s: %s", doi, e)
            return None

    def search(
        self, query: str, max_results: int = 20
    ) -> List[Dict]:
        """Search DataCite for datasets and software matching the query.

        Args:
            query: Search string.
            max_results: Maximum number of results.

        Returns:
            List of metadata dicts.
        """
        url = "https://api.datacite.org/dois"
        params = {
            "query": query,
            "page[size]": min(max_results, 100),
        }
        try:
            resp = self.session.get(url, params=params, timeout=15)
            if resp.status_code != 200:
                logger.warning("DataCite search returned %d", resp.status_code)
                return []

            data = resp.json()
            results = []
            for item in data.get("data", []):
                attrs = item.get("attributes", {})
                doi = attrs.get("doi", "")
                results.append(self._parse_attributes(attrs, doi))
                time.sleep(DATACITE_RATE_LIMIT_DELAY)

            logger.info("DataCite: found %d results for '%s'", len(results), query[:60])
            return results
        except Exception as e:
            logger.warning("DataCite search error: %s", e)
            return []

    @staticmethod
    def _parse_attributes(attrs: Dict, doi: str) -> Dict:
        """Parse DataCite attributes into a standard metadata dict."""
        creators = []
        for c in attrs.get("creators", []):
            name = c.get("name", "")
            if not name:
                given = c.get("givenName", "")
                family = c.get("familyName", "")
                name = f"{family}, {given}" if family else given
            creators.append(name)

        titles = attrs.get("titles", [{}])
        title = titles[0].get("title", "") if titles else ""

        descriptions = attrs.get("descriptions", [{}])
        description = descriptions[0].get("description", "") if descriptions else ""

        return {
            "source": "datacite",
            "doi": doi,
            "title": title,
            "creators": creators,
            "authors": " and ".join(creators),
            "publisher": attrs.get("publisher", ""),
            "publicationYear": attrs.get("publicationYear", ""),
            "year": str(attrs.get("publicationYear", "")),
            "resourceType": attrs.get("types", {}).get("resourceTypeGeneral", ""),
            "description": description,
            "abstract": description,  # for compatibility with search_papers format
            "url": f"https://doi.org/{doi}",
            "citations": 0,
        }

    @staticmethod
    def format_markdown(results: List[Dict]) -> str:
        """Format DataCite results as markdown."""
        lines = [
            "# DataCite Search Results",
            "",
            f"**Total Results**: {len(results)}",
            "",
        ]
        for i, r in enumerate(results, 1):
            lines.append(f"## {i}. {r.get('title', 'Untitled')}")
            lines.append("")
            lines.append(
                f"**Creators**: {', '.join(r.get('creators', [])[:5])}"
            )
            lines.append(f"**Year**: {r.get('publicationYear', 'N/A')}")
            lines.append(f"**Type**: {r.get('resourceType', 'Unknown')}")
            lines.append(f"**Publisher**: {r.get('publisher', 'Unknown')}")
            if r.get("description"):
                desc = r["description"][:400]
                lines.append(f"**Description**: {desc}")
            lines.append(f"**DOI**: [{r.get('doi', '')}](https://doi.org/{r.get('doi', '')})")
            lines.append("")
            lines.append("---")
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def format_bibtex(metadata: Dict) -> str:
        """Generate a BibTeX entry for a DataCite record."""
        doi = metadata.get("doi", "")
        cite_key = f"dataset_{doi.replace('/', '_').replace('.', '_')[:30]}" if doi else "dataset"
        title = (metadata.get("title") or "Untitled").replace("{", "\\{").replace("}", "\\}")
        authors = metadata.get("authors", "") or "Unknown"
        year = metadata.get("year", "") or "????"
        publisher = metadata.get("publisher", "")

        lines = [
            f"@misc{{{cite_key},",
            f"  title = {{{title}}},",
            f"  author = {{{authors}}},",
            f"  year = {{{year}}},",
        ]
        if doi:
            lines.append(f"  doi = {{{doi}}},")
        if publisher:
            lines.append(f"  publisher = {{{publisher}}},")
        res_type = metadata.get("resourceType", "")
        if res_type:
            lines.append(f"  note = {{{res_type}}},")
        lines.append("}")
        return "\n".join(lines)


# ============================================================================
# CLI dispatch
# ============================================================================

def _cmd_doi_to_bibtex(args: argparse.Namespace) -> None:
    converter = DOIConverter()
    if args.input_file:
        with open(args.input_file, "r", encoding="utf-8") as f:
            dois = [line.strip() for line in f if line.strip()]
    else:
        dois = args.dois

    if not dois:
        print("Error: No DOIs provided.", file=sys.stderr)
        sys.exit(1)

    if len(dois) == 1:
        bibtex = converter.doi_to_bibtex(dois[0])
        if bibtex:
            entries = [bibtex]
        else:
            sys.exit(1)
    else:
        entries = converter.convert_multiple(dois, delay=args.delay)

    if not entries:
        print("Error: No successful conversions.", file=sys.stderr)
        sys.exit(1)

    output = "\n\n".join(entries) + "\n"
    if args.output:
        Path(args.output).write_text(output)
        logger.info("Wrote %d BibTeX entries → %s", len(entries), args.output)
    else:
        print(output)


def _cmd_extract(args: argparse.Namespace) -> None:
    extractor = MetadataExtractor()
    identifiers = []
    if args.doi:
        identifiers.append(args.doi)
    if args.arxiv:
        identifiers.append(args.arxiv)
    if args.input_file:
        with open(args.input_file, "r", encoding="utf-8") as f:
            identifiers.extend(line.strip() for line in f if line.strip())

    if not identifiers:
        print("Error: No identifiers provided.", file=sys.stderr)
        sys.exit(1)

    entries = []
    for ident in identifiers:
        bibtex = extractor.extract(ident)
        if bibtex:
            entries.append(bibtex)
        time.sleep(CROSSREF_RATE_LIMIT_DELAY)

    if not entries:
        print("Error: No successful extractions.", file=sys.stderr)
        sys.exit(1)

    output = "\n\n".join(entries) + "\n"
    if args.output:
        Path(args.output).write_text(output)
        logger.info("Wrote %d entries → %s", len(entries), args.output)
    else:
        print(output)


def _cmd_verify(args: argparse.Namespace) -> None:
    verifier = CitationVerifier()
    report = verifier.verify_citations_in_file(args.file)

    print(f"\n{'='*60}")
    print("CITATION VERIFICATION REPORT")
    print(f"{'='*60}")
    print(f"File:    {report.get('file', args.file)}")
    print(f"DOIs:    {report['total_dois']} found")
    print(f"Passed:  {len(report['verified'])}")
    print(f"Failed:  {len(report['failed'])}")

    if report.get("failed"):
        print("\nFailed DOIs:")
        for doi in report["failed"]:
            print(f"  ✗ {doi}")

    if report.get("metadata"):
        print(f"\nVerified Citations ({args.style.upper()} format):\n")
        for doi, meta in report["metadata"].items():
            if args.style == "apa":
                cite = verifier.format_citation_apa(meta)
            else:
                cite = verifier.format_citation_nature(meta)
            print(f"  {cite}\n")

    if args.output:
        out_path = Path(args.output)
        out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
        logger.info("Report saved → %s", out_path)


def _cmd_scholar(args: argparse.Namespace) -> None:
    if not SCHOLARLY_AVAILABLE:
        print("Error: scholarly package not installed. Run: pip install scholarly",
              file=sys.stderr)
        sys.exit(1)

    searcher = GoogleScholarSearch()
    results = searcher.search(
        args.query,
        max_results=args.limit,
        year_start=args.year_start,
        year_end=args.year_end,
    )

    if args.format == "bibtex":
        for i, r in enumerate(results, 1):
            print(searcher.metadata_to_bibtex(r, cite_key=f"scholar{i}"))
            print()
    elif args.format == "json":
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        for i, r in enumerate(results, 1):
            print(f"{i}. {r['title']}")
            print(f"   {r['authors']} ({r['year']}) — cited {r['citations']}×")
            if r.get("abstract"):
                print(f"   {r['abstract'][:200]}...")
            print()

    if args.output:
        out = "\n".join(
            f"{i}. {r['title']} — {r['authors']} ({r['year']})"
            for i, r in enumerate(results, 1)
        )
        Path(args.output).write_text(out)
        logger.info("Results saved → %s", args.output)


def _cmd_datacite(args: argparse.Namespace) -> None:
    client = DataCiteClient()

    if args.doi:
        result = client.lookup_doi(args.doi)
        if result:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"DOI not found: {args.doi}", file=sys.stderr)
            sys.exit(1)
    elif args.query:
        results = client.search(args.query, max_results=args.limit)
        if args.format == "markdown":
            print(client.format_markdown(results))
        elif args.format == "bibtex":
            for r in results:
                print(client.format_bibtex(r))
                print()
        elif args.format == "json":
            print(json.dumps(results, indent=2, ensure_ascii=False))
        else:
            for i, r in enumerate(results, 1):
                print(f"{i}. [{r.get('resourceType', '?')}] {r.get('title', 'Untitled')}")
                print(f"   DOI: {r.get('doi', '?')}  |  {r.get('publicationYear', '?')}")
                print()
    else:
        print("Error: Provide --doi or --query.", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Citation tools — DOI/BibTeX conversion, metadata extraction, "
                    "citation verification, Google Scholar search, DataCite lookup.",
    )
    sub = parser.add_subparsers(dest="command", help="Sub-command")

    # --- doi-to-bibtex ---
    p_doi = sub.add_parser("doi-to-bibtex", help="Convert DOIs to BibTeX via CrossRef")
    p_doi.add_argument("dois", nargs="*", help="DOI(s) to convert")
    p_doi.add_argument("-i", "--input-file", help="File with DOIs (one per line)")
    p_doi.add_argument("-o", "--output", help="Output file (default: stdout)")
    p_doi.add_argument("--delay", type=float, default=CROSSREF_RATE_LIMIT_DELAY,
                       help=f"Delay between requests in seconds (default: {CROSSREF_RATE_LIMIT_DELAY})")

    # --- extract ---
    p_ext = sub.add_parser("extract", help="Extract metadata from DOI/arXiv ID")
    p_ext.add_argument("--doi", help="DOI to extract")
    p_ext.add_argument("--arxiv", help="arXiv ID to extract")
    p_ext.add_argument("-i", "--input-file", help="File with identifiers (one per line)")
    p_ext.add_argument("-o", "--output", help="Output file for BibTeX")

    # --- verify ---
    p_ver = sub.add_parser("verify", help="Verify citations in a markdown file")
    p_ver.add_argument("-f", "--file", required=True, help="Markdown/text file to scan")
    p_ver.add_argument("-o", "--output", help="Save JSON report to file")
    p_ver.add_argument("--style", choices=["apa", "nature"], default="apa",
                       help="Citation style (default: apa)")

    # --- scholar ---
    p_sch = sub.add_parser("scholar", help="Search Google Scholar")
    p_sch.add_argument("query", help="Search query")
    p_sch.add_argument("--limit", type=int, default=20, help="Max results")
    p_sch.add_argument("--year-start", type=int, help="Min publication year")
    p_sch.add_argument("--year-end", type=int, help="Max publication year")
    p_sch.add_argument("--format", choices=["text", "json", "bibtex"], default="text")
    p_sch.add_argument("-o", "--output", help="Save results to file")

    # --- datacite ---
    p_data = sub.add_parser("datacite", help="Search or lookup DataCite DOIs")
    p_data.add_argument("doi_or_query", nargs="?", help="DOI to look up, or search query with --query")
    p_data.add_argument("--doi", dest="doi", help="DOI to look up")
    p_data.add_argument("--query", dest="query", help="Search query")
    p_data.add_argument("--limit", type=int, default=20, help="Max search results")
    p_data.add_argument("--format", choices=["text", "json", "bibtex", "markdown"],
                        default="text")

    args = parser.parse_args()

    if args.command == "doi-to-bibtex":
        _cmd_doi_to_bibtex(args)
    elif args.command == "extract":
        _cmd_extract(args)
    elif args.command == "verify":
        _cmd_verify(args)
    elif args.command == "scholar":
        _cmd_scholar(args)
    elif args.command == "datacite":
        # Handle positional arg routing
        if not args.doi and not args.query:
            given = args.doi_or_query
            if given and given.startswith("10."):
                args.doi = given
            elif given:
                args.query = given
        _cmd_datacite(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
