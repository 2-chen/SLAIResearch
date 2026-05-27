"""
paperreview.ai API client — 3-step upload + review polling.
No browser needed; the site exposes clean REST endpoints.
"""

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
    email: str = "250010008@slai.edu.cn",
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

    logger.info("Step 1/3: requesting presigned upload URL …")
    url_data = _get_upload_url(pdf_path.name, venue, timeout=timeout)

    logger.info("Step 2/3: uploading to S3 …")
    _upload_to_s3(url_data, pdf_path, timeout=timeout)

    logger.info("Step 3/3: confirming upload …")
    token = _confirm_upload(url_data["s3_key"], venue, email, timeout=timeout)

    logger.info(f"Submission complete. Token = {token}")
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
    Looks in: recommendation → overall_assessment → verdict → (walk sections).
    Returns lowercased string, e.g. 'accept', 'weak accept', 'reject'.
    """
    # Direct fields
    for key in ("recommendation", "verdict", "decision"):
        val = review.get(key)
        if val and isinstance(val, str):
            return val.strip().lower()

    # overall_assessment section text
    oa = review.get("overall_assessment") or review.get("sections", {}).get(
        "overall_assessment"
    )
    if oa and isinstance(oa, str):
        oa_lower = oa.lower()
        if "recommendation: accept" in oa_lower or "**recommendation: accept**" in oa_lower:
            return "accept"
        if "recommendation: weak accept" in oa_lower or "recommendation: borderline" in oa_lower:
            return "weak accept" if "weak" in oa_lower else "borderline"
        if "recommendation: reject" in oa_lower:
            return "reject"

    return "unknown"


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
    for section_name in (
        "summary",
        "strengths",
        "weaknesses",
        "detailed_comments",
        "questions",
        "overall_assessment",
    ):
        content = sections.get(section_name)
        if content:
            heading = section_name.replace("_", " ").title()
            lines.append(f"## {heading}")
            lines.append("")
            lines.append(content if isinstance(content, str) else str(content))
            lines.append("")

    verdict = extract_verdict(review)
    lines.append(f"**Parsed Verdict**: `{verdict}`")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_upload_url(filename: str, venue: str, timeout: int) -> dict:
    resp = requests.post(
        f"{BASE_URL}/api/get-upload-url",
        json={"filename": filename, "venue": venue},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"get-upload-url failed: {data}")
    return data


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
    resp = requests.post(
        f"{BASE_URL}/api/confirm-upload",
        data={"s3_key": s3_key, "venue": venue, "email": email},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"confirm-upload failed: {data}")
    return data["token"]


# ---------------------------------------------------------------------------
# Quick self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    print("=== paperreview API smoke test ===")

    # Create a tiny valid PDF
    test_pdf = Path("/tmp/_test_chenresearch.pdf")
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
        print(f"Upload OK — token: {token}")
        print("(Use this token to check results later at paperreview.ai/review)")
    except Exception as e:
        print(f"Upload failed: {e}")

    test_pdf.unlink(missing_ok=True)
