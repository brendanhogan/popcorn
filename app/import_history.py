"""Bulk import past biweekly reading-list posts into the database.

Parses a text file of past posts in the format used by the user:

    M/D/YY
    Title: ...
    Type: ...
    Link: <url>
    Rating: 🍿 (1-4 popcorns)
    Description/Notes:
    <multi-line notes>

    Title: ...
    ...

Creates one session per date header. For each entry, preserves the
user's title / rating / notes verbatim; for non-Twitter URLs, fetches
the page content and generates a Claude (Haiku 4.5) summary.

Usage:
    python -m app.import_history data/import/past_posts.txt
    python -m app.import_history data/import/past_posts.txt --dry-run
    python -m app.import_history data/import/past_posts.txt --limit 5
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .fetch import detect_type, fetch_content
from .llm import summarize
from .models import Entry, Session
from .storage import new_id, save_entry, save_session

HAIKU_MODEL = "claude-haiku-4-5"
CONCURRENCY = 10


@dataclass
class ParsedEntry:
    title: str
    user_type: str
    url: str
    rating: int
    notes: str


@dataclass
class ParsedBatch:
    date: str
    entries: list[ParsedEntry] = field(default_factory=list)


# ---------- parsing ----------

_DATE_RE = re.compile(r"^\s*(\d+/\d+/\d+)\s*$")


def _parse_rating(s: str) -> int:
    return min(4, s.count("🍿"))


def _parse_entry_block(block: str) -> ParsedEntry | None:
    title = user_type = url = rating_str = ""
    notes_lines: list[str] = []
    in_notes = False

    for line in block.splitlines():
        stripped = line.rstrip()
        if in_notes:
            notes_lines.append(stripped)
            continue
        if stripped.startswith("Title:"):
            title = stripped[len("Title:"):].strip()
        elif stripped.startswith("Type:"):
            user_type = stripped[len("Type:"):].strip()
        elif stripped.startswith("Link:"):
            url = stripped[len("Link:"):].strip()
        elif stripped.startswith("Rating:"):
            rating_str = stripped[len("Rating:"):].lstrip(": ").rstrip()
        elif stripped.startswith("Description/Notes:"):
            in_notes = True

    notes = "\n".join(notes_lines).strip()
    if not url:
        return None
    return ParsedEntry(
        title=title,
        user_type=user_type,
        url=url,
        rating=_parse_rating(rating_str),
        notes=notes,
    )


def parse_file(text: str) -> list[ParsedBatch]:
    batches: list[ParsedBatch] = []
    current_date: str | None = None
    current_section: list[str] = []

    def flush() -> None:
        if current_date is None:
            return
        section_text = "\n".join(current_section)
        blocks = re.split(r"(?m)^(?=Title: )", section_text)
        b = ParsedBatch(date=current_date)
        for block in blocks:
            if not block.strip().startswith("Title:"):
                continue
            e = _parse_entry_block(block)
            if e is not None:
                b.entries.append(e)
        if b.entries:
            batches.append(b)

    for line in text.splitlines():
        m = _DATE_RE.match(line)
        if m:
            flush()
            current_date = m.group(1)
            current_section = []
        else:
            current_section.append(line)
    flush()
    return batches


def _session_name(date: str) -> str:
    return f"Past · {date}"


# ---------- processing ----------

async def _make_entry(
    parsed: ParsedEntry,
    batch_date: str,
    sem: asyncio.Semaphore,
    *,
    do_llm: bool,
) -> Entry:
    async with sem:
        url_type = detect_type(parsed.url)
        entry = Entry(
            id=new_id(),
            url=parsed.url,
            type=url_type,
            status="ready",
            title=parsed.title,
            rating=parsed.rating,
            notes=parsed.notes,
            backfilled=True,
            source_post_date=batch_date,
        )
        if url_type == "twitter":
            return entry

        try:
            content, error = await fetch_content(parsed.url, url_type)
        except Exception as e:
            entry.fetch_error = f"fetch crashed: {e}"
            return entry

        entry.fetched_content = content
        entry.fetch_error = error
        if not content or not do_llm:
            return entry

        try:
            entry.summary = await summarize(content, model=HAIKU_MODEL)
        except Exception as e:
            entry.fetch_error = f"summarize failed: {e}"
        return entry


# ---------- run ----------

async def run(file_path: Path, *, dry_run: bool, limit: int | None) -> None:
    text = file_path.read_text()
    batches = parse_file(text)
    total = sum(len(b.entries) for b in batches)

    print(f"Parsed {len(batches)} batches, {total} entries:", file=sys.stderr)
    for b in batches:
        twitter = sum(1 for e in b.entries if detect_type(e.url) == "twitter")
        fetchable = len(b.entries) - twitter
        print(
            f"  {b.date}: {len(b.entries)} entries ({twitter} twitter / {fetchable} fetchable)",
            file=sys.stderr,
        )

    if limit is not None:
        kept = 0
        for b in batches:
            if kept >= limit:
                b.entries = []
            elif kept + len(b.entries) > limit:
                b.entries = b.entries[: limit - kept]
                kept = limit
            else:
                kept += len(b.entries)
        batches = [b for b in batches if b.entries]
        total = sum(len(b.entries) for b in batches)
        print(f"\nLimited to first {total} entries.", file=sys.stderr)

    if dry_run:
        print("\n--- DRY RUN (no fetches, no LLM, no saves) ---", file=sys.stderr)
        print("\nSample entries from first batch:", file=sys.stderr)
        for e in batches[0].entries[:3] if batches else []:
            print(f"\n  type={detect_type(e.url):8s}  rating={e.rating}", file=sys.stderr)
            print(f"  title: {e.title}", file=sys.stderr)
            print(f"  url:   {e.url}", file=sys.stderr)
            print(f"  notes: {e.notes[:120]}{'...' if len(e.notes) > 120 else ''}", file=sys.stderr)
        return

    sem = asyncio.Semaphore(CONCURRENCY)
    print(f"\nProcessing with concurrency={CONCURRENCY}, model={HAIKU_MODEL}", file=sys.stderr)

    for b in batches:
        session = Session(id=new_id(), name=_session_name(b.date), entry_ids=[])
        save_session(session)
        print(f"\n[{b.date}] session={session.id} ({len(b.entries)} entries)", file=sys.stderr)

        tasks = [_make_entry(e, b.date, sem, do_llm=True) for e in b.entries]
        done = 0
        for coro in asyncio.as_completed(tasks):
            entry = await coro
            save_entry(entry)
            session.entry_ids.append(entry.id)
            done += 1
            tag = "tweet" if entry.type == "twitter" else ("ok" if entry.summary else "no-content")
            label = (entry.title or entry.url)[:60]
            print(f"  [{done:3d}/{len(b.entries)}] {tag:10s} {label}", file=sys.stderr)

        save_session(session)
        print(f"  done: {len(session.entry_ids)} entries saved", file=sys.stderr)

    print("\nAll batches imported. Open the app and use the dropdown to switch into them.", file=sys.stderr)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("file", type=Path, help="Path to past_posts.txt")
    p.add_argument("--dry-run", action="store_true", help="Parse only; no fetches, no LLM, no saves")
    p.add_argument("--limit", type=int, default=None, help="Process only the first N entries")
    args = p.parse_args()
    asyncio.run(run(args.file, dry_run=args.dry_run, limit=args.limit))


if __name__ == "__main__":
    main()
