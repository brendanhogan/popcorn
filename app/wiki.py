"""Wiki storage + markdown rendering helpers.

Wiki files live under data/wiki/:
    index.md
    log.md
    sources/{slug}.md     # one per Entry
    concepts/{slug}.md    # LLM-synthesized theme pages

Markdown supports a [[wiki-link]] syntax that we rewrite to internal links
when rendering to HTML.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import markdown as md_lib

from .storage import DATA_DIR

WIKI_DIR = DATA_DIR / "wiki"
SOURCES_DIR = WIKI_DIR / "sources"
CONCEPTS_DIR = WIKI_DIR / "concepts"
META_DIR = WIKI_DIR / "meta"
ENTITIES_DIR = WIKI_DIR / "entities"
BATCHES_DIR = WIKI_DIR / "batches"
INDEX_PATH = WIKI_DIR / "index.md"
LOG_PATH = WIKI_DIR / "log.md"

WIKI_LINK_RE = re.compile(r"\[\[([^\[\]\|]+)(?:\|([^\[\]]+))?\]\]")


_KIND_DIRS: dict[str, Path] = {
    "sources": SOURCES_DIR,
    "concepts": CONCEPTS_DIR,
    "meta": META_DIR,
    "entities": ENTITIES_DIR,
    "batches": BATCHES_DIR,
}


def ensure_dirs() -> None:
    for d in _KIND_DIRS.values():
        d.mkdir(parents=True, exist_ok=True)


def slugify(text: str, *, max_len: int = 80) -> str:
    """Stable, URL-safe slug. Strips accents, lowercases, replaces non-word with -."""
    if not text:
        return "untitled"
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    text = re.sub(r"[-\s]+", "-", text)
    return text[:max_len] or "untitled"


# ---------- page paths and routing ----------

def page_path(kind: str, slug: str) -> Path:
    """kind: any of 'index', 'log', 'sources', 'concepts', 'meta', 'entities', 'batches'."""
    if kind == "index":
        return INDEX_PATH
    if kind == "log":
        return LOG_PATH
    if kind in _KIND_DIRS:
        return _KIND_DIRS[kind] / f"{slug}.md"
    raise ValueError(f"unknown page kind: {kind}")


def fq_slug(kind: str, slug: str) -> str:
    """Fully-qualified slug used in URLs and [[links]]: e.g. 'concepts/agent-loops'."""
    if kind in ("index", "log"):
        return kind
    return f"{kind}/{slug}"


def resolve_fq(fq: str) -> tuple[str, str] | None:
    """Inverse of fq_slug. Returns (kind, slug) or None for unknown."""
    if fq in ("index", "log"):
        return (fq, "")
    if "/" in fq:
        kind, slug = fq.split("/", 1)
        if kind in _KIND_DIRS:
            return (kind, slug)
    return None


# ---------- reading ----------

def list_pages() -> dict[str, list[str]]:
    """Returns a dict mapping kind → sorted slugs."""
    ensure_dirs()
    return {kind: sorted(p.stem for p in d.glob("*.md")) for kind, d in _KIND_DIRS.items()}


def read_page(kind: str, slug: str = "") -> str | None:
    p = page_path(kind, slug)
    if not p.exists():
        return None
    return p.read_text()


def write_page(kind: str, slug: str, content: str) -> None:
    ensure_dirs()
    page_path(kind, slug).write_text(content)


# ---------- rendering ----------

def _build_known_slugs() -> set[str]:
    pages = list_pages()
    known: set[str] = {"index", "log"}
    for kind, slugs in pages.items():
        for s in slugs:
            known.add(f"{kind}/{s}")
    return known


def _rewrite_links(text: str, known: set[str]) -> str:
    """Convert [[fq-slug]] or [[fq-slug|label]] to <a> tags. Mark unknown as broken."""

    def sub(m: re.Match[str]) -> str:
        target = m.group(1).strip()
        label = (m.group(2) or target.rsplit("/", 1)[-1]).strip()
        if target in known:
            return f'<a href="/wiki/{target}" class="wiki-link">{label}</a>'
        return f'<span class="wiki-link broken" title="No page named {target}">{label}</span>'

    return WIKI_LINK_RE.sub(sub, text)


def render_to_html(markdown_text: str) -> str:
    known = _build_known_slugs()
    rewritten = _rewrite_links(markdown_text, known)
    return md_lib.markdown(
        rewritten,
        extensions=["fenced_code", "tables", "sane_lists"],
        output_format="html5",
    )


def find_backlinks(target_fq: str) -> list[tuple[str, str]]:
    """Find all pages that link to target_fq. Returns list of (kind, slug)."""
    ensure_dirs()
    backlinks: list[tuple[str, str]] = []
    for kind, dir_path in _KIND_DIRS.items():
        for p in dir_path.glob("*.md"):
            text = p.read_text()
            for m in WIKI_LINK_RE.finditer(text):
                if m.group(1).strip() == target_fq:
                    backlinks.append((kind, p.stem))
                    break
    for kind, path in (("index", INDEX_PATH), ("log", LOG_PATH)):
        if path.exists():
            text = path.read_text()
            for m in WIKI_LINK_RE.finditer(text):
                if m.group(1).strip() == target_fq:
                    backlinks.append((kind, ""))
                    break
    return backlinks


# ---------- page-title extraction ----------

_HEADING_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def page_title(markdown_text: str, fallback: str = "") -> str:
    m = _HEADING_RE.search(markdown_text)
    if m:
        return m.group(1).strip()
    return fallback


# ---------- log ----------

def append_log(message: str) -> None:
    ensure_dirs()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    line = f"- **{ts}** — {message}\n"
    if not LOG_PATH.exists():
        LOG_PATH.write_text("# Log\n\nChronological record of wiki builds and updates.\n\n")
    with LOG_PATH.open("a") as f:
        f.write(line)
