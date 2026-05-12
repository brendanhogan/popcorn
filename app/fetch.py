from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx
import trafilatura

from .models import EntryType

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
MAX_CONTENT_CHARS = 120_000


def detect_type(url: str) -> EntryType:
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return "other"
    host = host.lower().lstrip("www.")
    if host in {"x.com", "twitter.com", "mobile.twitter.com", "nitter.net"} or host.endswith(".twitter.com"):
        return "twitter"
    if host == "arxiv.org" or host.endswith(".arxiv.org"):
        return "paper"
    return "other"


def normalize_arxiv(url: str) -> str:
    # /pdf/2510.09714(v2)(.pdf) -> /abs/2510.09714
    m = re.match(r"^(https?://arxiv\.org)/pdf/([^/?#]+?)(?:v\d+)?(?:\.pdf)?(?:[?#].*)?$", url)
    if m:
        return f"{m.group(1)}/abs/{m.group(2)}"
    return url


async def fetch_content(url: str, type_: EntryType) -> tuple[str, str]:
    """Returns (cleaned_text, error). Empty error means success."""
    if type_ == "twitter":
        return "", "twitter: scraping skipped"

    target = normalize_arxiv(url) if type_ == "paper" else url

    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            follow_redirects=True,
        ) as client:
            resp = await client.get(target)
            resp.raise_for_status()
            html = resp.text
    except httpx.HTTPError as e:
        return "", f"fetch failed: {e}"
    except Exception as e:
        return "", f"fetch failed: {e}"

    extracted = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=True,
        favor_recall=True,
    )
    if not extracted:
        return "", "extraction produced no text (page may be JS-rendered or blocked)"

    if len(extracted) > MAX_CONTENT_CHARS:
        extracted = extracted[:MAX_CONTENT_CHARS] + "\n\n[...truncated...]"

    return extracted, ""
