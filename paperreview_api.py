"""
paperreview.ai API client — 3-step upload + review polling.
No browser needed; the site exposes clean REST endpoints.
"""

import re
import time
import json
import logging
import requests
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_URL = "https://paperreview.ai"

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def submit_paper(
    pdf_path: str | Path,
    email: str = "",
    venue: str = "AAAI",
    timeout: int = 300,
) -> str:
    """
    3-step upload to paperreview.ai.  Returns the *review token* (str).

    Steps
    -----
    1. POST /api/get-upload-url  → presigned S3 URL + fields + s3_key
    2. POST <presigned_url>       → upload file directly to S3
    3. POST /api/confirm-upload   → finalise; server returns token
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    if not email:
        raise ValueError("email is required for paperreview.ai submission")

    logger.info("Step 1/3: requesting presigned upload URL …")
    url_data = _get_upload_url(pdf_path.name, venue, timeout=timeout)

    logger.info("Step 2/3: uploading to S3 …")
    _upload_to_s3(url_data, pdf_path, timeout=timeout)

    logger.info("Step 3/3: confirming upload …")
    token = _confirm_upload(url_data["s3_key"], venue, email, timeout=timeout)

    logger.info("Submission complete. Token received.")
    return token


def get_review(token: str, timeout: int = 30) -> dict | None:
    """
    Single query to GET /api/review/{token}.
    Returns review dict if ready (200), None if still processing (202),
    raises on other errors.
    """
    url = f"{BASE_URL}/api/review/{token}"
    resp = requests.get(url, timeout=timeout)
    if resp.status_code == 202:
        return None  # still processing
    resp.raise_for_status()
    return resp.json()


def poll_review(
    token: str,
    initial_wait: int = 300,
    interval: int = 60,
    max_wait: int = 7200,
) -> dict:
    """
    Wait *initial_wait* seconds, then poll every *interval* seconds.
    Returns the review dict once ready.
    Raises TimeoutError if *max_wait* is exceeded.
    """
    logger.info(
        f"Waiting {initial_wait}s before first poll (poll interval={interval}s, max_wait={max_wait}s) …"
    )
    time.sleep(initial_wait)

    deadline = time.time() + max_wait
    attempt = 0

    while time.time() < deadline:
        attempt += 1
        remaining = max(0, int(deadline - time.time()))
        logger.info(
            f"Poll attempt {attempt} … (elapsed ~{attempt * interval + initial_wait}s, remaining {remaining}s)"
        )

        try:
            data = get_review(token)
        except requests.RequestException as exc:
            logger.warning(f"Poll {attempt} failed with HTTP error: {exc}; retrying …")
            time.sleep(interval)
            continue

        if data is not None:
            logger.info("Review ready!")
            return data

        time.sleep(interval)

    raise TimeoutError(
        f"Review not ready after {max_wait + initial_wait}s (max_wait={max_wait})"
    )


def extract_verdict(review: dict) -> str:
    """
    Extract the review verdict from the response.

    Resolution order (most reliable first):
      1. Direct top-level fields: recommendation, verdict, decision
      2. sections.overall_assessment — parse recommendation patterns
      3. Semantic analysis of strengths vs weaknesses text
      4. Raw JSON walk — find any dict key matching 'verdict' / 'decision' / 'recommendation'
      5. Ratio heuristic: count strengths vs weaknesses bullet points

    Returns lowercased string, e.g. 'accept', 'weak accept', 'reject'.
    Never returns 'unknown' unless the review is completely empty.
    """
    # ── 1. Direct top-level fields ──
    for key in ("recommendation", "verdict", "decision"):
        val = review.get(key)
        if val and isinstance(val, str):
            v = val.strip().lower()
            if v in ("accept", "weak accept", "weak reject", "reject", "borderline"):
                return v
            # Try to extract from free-text recommendation
            parsed = _parse_verdict_from_text(v)
            if parsed != "unknown":
                return parsed

    # ── 2. overall_assessment section (top-level or nested in sections) ──
    oa = review.get("overall_assessment")
    if not oa:
        oa = review.get("sections", {}).get("overall_assessment")
    if oa:
        if isinstance(oa, str):
            parsed = _parse_verdict_from_text(oa)
            if parsed != "unknown":
                return parsed
        elif isinstance(oa, dict):
            # overall_assessment might be a dict with a 'recommendation' sub-key
            for k in ("recommendation", "verdict", "decision"):
                v = oa.get(k)
                if v and isinstance(v, str):
                    parsed = _parse_verdict_from_text(v)
                    if parsed != "unknown":
                        return parsed

    # ── 3. Semantic analysis of strengths vs weaknesses ──
    sections = review.get("sections", {})
    strengths_text = sections.get("strengths", "") or ""
    weaknesses_text = sections.get("weaknesses", "") or ""
    summary_text = sections.get("summary", "") or ""

    all_text = f"{summary_text}\n{strengths_text}\n{weaknesses_text}"

    # Count bullet points / numbered items in strengths vs weaknesses
    strength_items = _count_items(strengths_text)
    weakness_items = _count_items(weaknesses_text)

    # Search for verdict-signalling phrases in the full text
    text_lower = all_text.lower()

    # Strong signals
    if re.search(r'\baccept\b.*\brecommend\b|\brecommend\b.*\baccept\b', text_lower):
        if "weak" in text_lower.split("accept")[0][-50:]:
            return "weak accept"
        return "accept"

    if re.search(r'\breject\b.*\brecommend\b|\brecommend\b.*\breject\b', text_lower):
        return "reject"

    # Weak signals
    for phrase, verdict in _VERDICT_PHRASES:
        if phrase in text_lower:
            return verdict

    # ── 4. Walk the entire JSON tree for any nested verdict field ──
    found = _walk_for_verdict(review)
    if found:
        return found

    # ── 5. Ratio heuristic ──
    if strength_items > 0 and weakness_items > 0:
        ratio = strength_items / max(weakness_items, 1)
        if ratio >= 2.0:
            return "weak accept"  # Many more strengths than weaknesses
        elif ratio <= 0.5:
            return "weak reject"  # Many more weaknesses than strengths

    # ── 6. Fallback: count explicit "strength"/"weakness" phrases ──
    strength_indicators = len(re.findall(
        r'strength|novel|contribution|solid|well.*(written|motivated|designed)|'
        r'clear|thorough|comprehensive|promising|interesting',
        strengths_text, re.I
    ))
    weakness_indicators = len(re.findall(
        r'weakness|concern|missing|lack|insufficient|limited|unclear|'
        r'not (clear|shown|evaluated|compared|reported|validated)|'
        r'cannot|fails|does not|incomplete',
        weaknesses_text, re.I
    ))

    if strength_indicators > weakness_indicators * 2:
        return "weak accept"
    elif weakness_indicators > strength_indicators * 2:
        return "weak reject"

    # Still unknown → check if there's any content at all
    if len(all_text.strip()) > 100:
        # If strengths exist but weaknesses are minimal → likely positive
        if len(strengths_text.strip()) > 200 and len(weaknesses_text.strip()) < 100:
            return "weak accept"
        # If weaknesses dominate in length → likely needs revision
        if len(weaknesses_text.strip()) > len(strengths_text.strip()) * 2:
            return "weak reject"
        return "borderline"

    return "unknown"


# ── Verdict parsing helpers ──

# Phrases that map to verdicts, ordered by specificity (most specific first)
_VERDICT_PHRASES: list[tuple[str, str]] = [
    ("strong accept", "accept"),
    ("clear accept", "accept"),
    ("definitely accept", "accept"),
    ("weak accept", "weak accept"),
    ("borderline accept", "weak accept"),
    ("borderline", "borderline"),
    ("weak reject", "weak reject"),
    ("borderline reject", "weak reject"),
    ("strong reject", "reject"),
    ("clear reject", "reject"),
    ("definitely reject", "reject"),
    ("below bar", "reject"),
    ("not ready for publication", "reject"),
    ("major revision", "weak reject"),
    ("minor revision", "weak accept"),
    ("ready for publication", "accept"),
    ("publishable", "accept"),
    ("substantial revision", "weak reject"),
    ("not suitable", "reject"),
    ("insufficient contribution", "reject"),
    ("the paper should be accepted", "accept"),
    ("i recommend acceptance", "accept"),
    ("i recommend rejection", "reject"),
    ("cannot recommend acceptance", "reject"),
]


def _parse_verdict_from_text(text: str) -> str:
    """Extract a verdict from free-form text."""
    if not text:
        return "unknown"
    text_lower = text.lower()

    # Try explicit "Recommendation: X" or "Verdict: X" patterns
    m = re.search(
        r'(?:recommendation|verdict|decision|rating)\s*[:=]\s*'
        r'(accept|weak accept|weak reject|reject|borderline)',
        text_lower, re.I,
    )
    if m:
        return m.group(1).strip().lower()

    # Check known phrases
    for phrase, verdict in _VERDICT_PHRASES:
        if phrase in text_lower:
            return verdict

    return "unknown"


def _count_items(text: str) -> int:
    """Count bullet points, numbered items, or list entries in review text."""
    # Match markdown bullets: "- ", "* ", "+ ", or numbered "1. "
    items = re.findall(r'^\s*(?:[-*+]|\d+[.)])\s+\S', text, re.MULTILINE)
    # Also count paragraphs under "Strengths"/"Weaknesses" headings as fallback
    if not items:
        # Split by double newlines and count substantive paragraphs
        paras = [p.strip() for p in text.split('\n\n') if len(p.strip()) > 50]
        return len(paras)
    return len(items)


def _walk_for_verdict(obj, depth: int = 0) -> str | None:
    """Recursively walk a JSON object looking for verdict-like key-value pairs."""
    if depth > 6:  # Safety limit
        return None
    if isinstance(obj, dict):
        for key, val in obj.items():
            key_lower = key.lower()
            if key_lower in ("verdict", "recommendation", "decision", "rating"):
                if isinstance(val, str):
                    parsed = _parse_verdict_from_text(val)
                    if parsed != "unknown":
                        return parsed
            elif isinstance(val, (dict, list)):
                result = _walk_for_verdict(val, depth + 1)
                if result:
                    return result
    elif isinstance(obj, list):
        for item in obj[:10]:  # Check first 10 items
            if isinstance(item, (dict, list)):
                result = _walk_for_verdict(item, depth + 1)
                if result:
                    return result
    return None


def extract_review_key_feedback(review: dict) -> tuple[str, dict[str, bool]]:
    """Extract actionable review sections for revision guidance.

    Returns ``(feedback_md, found_map)`` where *feedback_md* is a focused
    Markdown string and *found_map* records which sections were present.

    Sections extracted (in priority order):
    - Weaknesses
    - Detailed Comments
    - Questions for Authors
    - Overall Assessment

    If all four sections are missing, *feedback_md* falls back to the full
    ``review_to_markdown()`` output.
    """
    sections = review.get("sections", {})
    source = {**review, **sections}

    section_map = [
        ("weaknesses", "Weaknesses"),
        ("detailed_comments", "Detailed Comments"),
        ("questions", "Questions for Authors"),
        ("overall_assessment", "Overall Assessment"),
    ]

    found: dict[str, bool] = {}
    parts: list[str] = []

    for key, heading in section_map:
        content = source.get(key)
        if not content:
            found[key] = False
            continue

        # Check if content is non-empty
        has_content = False
        if isinstance(content, str) and content.strip():
            has_content = True
        elif isinstance(content, (list, dict)) and len(content) > 0:
            has_content = True

        found[key] = has_content
        if not has_content:
            continue

        parts.append(f"## {heading}")
        if isinstance(content, dict):
            for k, v in content.items():
                parts.append(f"- **{k.replace('_', ' ').title()}**: {v}")
        elif isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for item in content:
                parts.append(f"- {item}")
        parts.append("")

    any_found = any(found.values())

    if any_found:
        return "\n".join(parts), found

    # Fallback: use full review
    logger.warning(
        "No key sections (weaknesses/detailed_comments/questions/overall_assessment) "
        "found in review — falling back to full review content"
    )
    return review_to_markdown(review), found


def review_to_markdown(review: dict) -> str:
    """Convert review JSON to a clean Markdown string."""
    title = review.get("title", "Untitled")
    venue = review.get("venue", "N/A")
    submission_date = review.get("submission_date", "N/A")

    lines = [
        f"# Stanford Agentic Reviewer — Review Report",
        f"",
        f"**Paper**: {title}",
        f"**Venue**: {venue}",
        f"**Submitted**: {submission_date}",
        f"",
        "---",
        "",
    ]

    sections = review.get("sections", {})

    # Also check top-level keys that might be sections not nested under "sections"
    _TOP_LEVEL_SECTIONS = (
        "summary", "strengths", "weaknesses", "detailed_comments",
        "questions", "overall_assessment",
    )
    for section_name in _TOP_LEVEL_SECTIONS:
        # Prefer nested sections, fall back to top-level
        content = sections.get(section_name) or review.get(section_name)
        if content:
            heading = section_name.replace("_", " ").title()
            lines.append(f"## {heading}")
            lines.append("")
            if isinstance(content, dict):
                # If content is a dict (e.g. overall_assessment with sub-keys), flatten it
                for k, v in content.items():
                    lines.append(f"**{k.replace('_', ' ').title()}**: {v}")
                    lines.append("")
            elif isinstance(content, str):
                lines.append(content)
            elif isinstance(content, list):
                for item in content:
                    lines.append(f"- {item}")
            lines.append("")

    verdict = extract_verdict(review)
    lines.append(f"**Parsed Verdict**: `{verdict}`")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_upload_url(filename: str, venue: str, timeout: int) -> dict:
    import time as _t
    for attempt in range(3):
        resp = requests.post(
            f"{BASE_URL}/api/get-upload-url",
            json={"filename": filename, "venue": venue},
            timeout=timeout,
        )
        if resp.status_code == 429:
            wait = 10 * (attempt + 1)
            logger.warning("Rate limited (429), retrying in %ds …", wait)
            _t.sleep(wait)
            continue
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"get-upload-url failed: {data}")
        return data
    resp.raise_for_status()  # Re-raise last 429 after exhausting retries
    return {}  # unreachable


def _upload_to_s3(url_data: dict, pdf_path: Path, timeout: int) -> None:
    presigned_url = url_data["presigned_url"]
    presigned_fields = url_data.get("presigned_fields", {})

    with pdf_path.open("rb") as fh:
        files_payload = {"file": (pdf_path.name, fh, "application/pdf")}
        resp = requests.post(
            presigned_url,
            data=presigned_fields,
            files=files_payload,
            timeout=timeout,
        )
    if not resp.ok:
        raise RuntimeError(
            f"S3 upload failed: HTTP {resp.status_code} — {resp.text[:500]}"
        )


def _confirm_upload(s3_key: str, venue: str, email: str, timeout: int) -> str:
    import time as _t
    for attempt in range(3):
        resp = requests.post(
            f"{BASE_URL}/api/confirm-upload",
            data={"s3_key": s3_key, "venue": venue, "email": email},
            timeout=timeout,
        )
        if resp.status_code == 429:
            wait = 10 * (attempt + 1)
            logger.warning("Rate limited (429), retrying in %ds …", wait)
            _t.sleep(wait)
            continue
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise RuntimeError(f"confirm-upload failed: {data}")
        return data["token"]
    resp.raise_for_status()
    return ""  # unreachable


# ---------------------------------------------------------------------------
# Quick self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    print("=== paperreview API smoke test ===")

    # Create a tiny valid PDF
    test_pdf = Path("/tmp/_test_slairesearch.pdf")
    test_pdf.write_bytes(
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R>>endobj\n"
        b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n"
        b"0000000058 00000 n \n0000000115 00000 n \n"
        b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n190\n%%EOF"
    )

    try:
        token = submit_paper(str(test_pdf), venue="AAAI")
        print("Upload OK — token received")
        print("(Check results later at paperreview.ai/review)")
    except Exception as e:
        print(f"Upload failed: {e}")

    test_pdf.unlink(missing_ok=True)
