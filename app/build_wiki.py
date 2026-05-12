"""Build the wiki from current entries.

Idempotent: re-run anytime to regenerate everything from the current
state of data/entries/. Preserves data/wiki/log.md across runs.

Flow:
    1. Load all entries across all sessions.
    2. Write one source page per entry under data/wiki/sources/.
    3. Send all entries to Claude (Sonnet 4.6) and ask for 15-25 recurring
       concepts, each with a synthesis and source-id list.
    4. Write one concept page per concept under data/wiki/concepts/.
    5. Update source pages with backlinks to concepts they belong to.
    6. Write index.md.
    7. Append a build-completed line to log.md.

Usage:
    python -m app.build_wiki
    python -m app.build_wiki --no-llm     # source pages only, skip concept extraction
    python -m app.build_wiki --concepts N # ask Claude for ~N concepts (default 20)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

_ENTRY_REF_RE = re.compile(r"\bEntry\s*\[(\d+)\]\s*")

from anthropic import AsyncAnthropic

from .models import Entry
from .storage import list_entries_in_session, list_sessions, load_entry
from .wiki import (
    CONCEPTS_DIR,
    SOURCES_DIR,
    WIKI_DIR,
    append_log,
    ensure_dirs,
    fq_slug,
    slugify,
    write_page,
)

CONCEPT_MODEL = "claude-sonnet-4-6"
DEFAULT_CONCEPT_COUNT = 20

POPCORN = "🍿"


# ---------- source pages ----------

@dataclass
class SourceRef:
    entry_id: str
    slug: str
    title: str
    rating: int
    type: str
    source_post_date: str


def _source_slug(entry: Entry, taken: set[str]) -> str:
    base = slugify(entry.title) if entry.title else slugify(entry.url) or entry.id
    if base not in taken:
        return base
    # collision: append short suffix from entry id
    suffix = entry.id[:6].lower()
    return f"{base}-{suffix}"


def _format_source_md(entry: Entry, concept_slugs: list[str]) -> str:
    title = entry.title or "(untitled)"
    rating = POPCORN * entry.rating if entry.rating else "—"
    lines = [
        f"# {title}",
        "",
        f"- **Rating:** {rating}",
        f"- **Type:** {entry.type}",
        f"- **Link:** <{entry.url}>",
    ]
    if entry.source_post_date:
        lines.append(f"- **First seen:** {entry.source_post_date}")
    if entry.backfilled:
        lines.append(f"- **Backfilled:** yes")
    lines.append("")

    if entry.notes:
        lines += ["## Notes", "", entry.notes, ""]

    if entry.summary:
        lines += ["## Summary", "", entry.summary, ""]

    if concept_slugs:
        lines += ["## Concepts", ""]
        for cs in concept_slugs:
            lines.append(f"- [[concepts/{cs}]]")
        lines.append("")

    return "\n".join(lines)


def write_source_pages(entries: list[Entry]) -> dict[str, SourceRef]:
    """Returns a mapping of entry_id → SourceRef."""
    taken: set[str] = set()
    refs: dict[str, SourceRef] = {}
    for e in entries:
        slug = _source_slug(e, taken)
        taken.add(slug)
        refs[e.id] = SourceRef(
            entry_id=e.id,
            slug=slug,
            title=e.title or "(untitled)",
            rating=e.rating,
            type=e.type,
            source_post_date=e.source_post_date,
        )
        write_page("sources", slug, _format_source_md(e, concept_slugs=[]))
    return refs


# ---------- concept extraction ----------

CONCEPT_SCHEMA = {
    "type": "object",
    "properties": {
        "concepts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "synthesis": {"type": "string"},
                    "source_numbers": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["name", "description", "synthesis", "source_numbers"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["concepts"],
    "additionalProperties": False,
}


def _entries_for_prompt(entries: list[Entry]) -> str:
    blocks = []
    for i, e in enumerate(entries, 1):
        rating = POPCORN * e.rating if e.rating else "?"
        date = e.source_post_date or "—"
        header = f"[{i}] [{date} {rating} {e.type}]"
        title = f"title: {e.title or '(untitled)'}"
        body = []
        if e.notes:
            body.append(f"notes: {e.notes}")
        if e.summary:
            s = e.summary if len(e.summary) <= 600 else e.summary[:600] + "..."
            body.append(f"summary: {s}")
        block = "\n".join([header, title] + body)
        blocks.append(block)
    return "\n\n".join(blocks)


def _concept_prompt(entries: list[Entry], target_count: int) -> str:
    return f"""You are analyzing a researcher's biweekly reading list to identify recurring themes
across their entries.

Below are {len(entries)} entries from past biweekly posts. For each entry, the user provided
a title, type, rating (🍿 = how much they liked it, 0-4), and personal notes that often
reveal their perspective. Some entries have auto-generated summaries from fetched content;
many (especially tweets) have only the user's notes.

Your task: identify roughly {target_count} (15-25 is a good range) distinct recurring themes
that appear across these entries. For each theme:

- **name**: a short concept label (2-6 words). Be specific — not "AI" but "post-trained LLMs
  as persona simulators". Not "tools" but "agent loops with explicit tool surface".
- **description**: 1-2 sentences explaining what this theme covers and why it might matter.
- **synthesis**: 4-8 sentences synthesizing what the sources collectively show about this
  theme. Lean on the user's NOTES for the perspective layer — those reveal what they find
  interesting, skeptical, or exciting. Quote short fragments where they sharpen a point.
  When you need to refer to a specific source in prose, use a short fragment of its title
  (e.g., "the persona selection model post" or "the ciphered-reasoning paper") — never use
  bracket references like "Entry [128]" or "[N]" in the synthesis text. The reader will see
  the full citation list below the synthesis.
- **source_numbers**: the list of entry numbers (the `[N]` at the start of each entry block)
  that belong to this concept. A source can belong to multiple concepts. **Be thorough** —
  every concept should have at least 2 sources; if you can't find at least 2, the concept
  isn't really recurring and you should drop it.

Rules:
- Concepts should be substantive, not catch-alls.
- Don't force every entry into a concept — some are one-offs and that's fine.
- Prefer themes that span more than one batch (date) when possible — they're the recurring ones.
- Output via the JSON schema, no preamble.

---

Entries:

{_entries_for_prompt(entries)}
"""


async def extract_concepts(entries: list[Entry], target_count: int) -> list[dict]:
    """Returns list of concept dicts with `source_ids` (mapped from numbers)."""
    client = AsyncAnthropic()
    prompt = _concept_prompt(entries, target_count)
    print(f"  prompt is ~{len(prompt) // 4} tokens (rough)", file=sys.stderr)
    response = await client.messages.create(
        model=CONCEPT_MODEL,
        max_tokens=16000,
        messages=[{"role": "user", "content": prompt}],
        output_config={"format": {"type": "json_schema", "schema": CONCEPT_SCHEMA}},
    )
    text = next((b.text for b in response.content if b.type == "text"), "")
    data = json.loads(text)

    # Map source numbers back to entry IDs
    out: list[dict] = []
    for c in data.get("concepts", []):
        nums = c.get("source_numbers", [])
        ids: list[str] = []
        for n in nums:
            if isinstance(n, int) and 1 <= n <= len(entries):
                ids.append(entries[n - 1].id)
        c_out = {
            "name": c.get("name", ""),
            "description": c.get("description", ""),
            "synthesis": c.get("synthesis", ""),
            "source_ids": ids,
        }
        out.append(c_out)
    return out


# ---------- concept pages ----------

def _format_concept_md(name: str, description: str, synthesis: str,
                       sources: list[tuple[SourceRef, str]]) -> str:
    """sources: list of (ref, user_notes) tuples sorted by date desc."""
    # Strip leaked "Entry [N]" numeric references from the synthesis text
    cleaned_synthesis = _ENTRY_REF_RE.sub("", synthesis).strip()
    lines = [f"# {name}", "", f"*{description}*", "", "## Synthesis", "", cleaned_synthesis, ""]
    if sources:
        lines += [f"## Sources ({len(sources)})", ""]
        # group by date for readability
        for ref, notes in sources:
            rating = POPCORN * ref.rating if ref.rating else ""
            date = ref.source_post_date or "—"
            line = f"- [[sources/{ref.slug}|{ref.title}]] · {rating} · {date}"
            lines.append(line)
            if notes:
                # quote the user's note as a sub-bullet
                note_one_line = notes.replace("\n", " ").strip()
                if len(note_one_line) > 180:
                    note_one_line = note_one_line[:180] + "…"
                lines.append(f"  - *{note_one_line}*")
        lines.append("")
    return "\n".join(lines)


def write_concept_pages(
    concepts: list[dict],
    refs: dict[str, SourceRef],
    entries_by_id: dict[str, Entry],
) -> dict[str, list[str]]:
    """Write concept pages. Returns entry_id -> list of concept_slugs."""
    entry_to_concepts: dict[str, list[str]] = {}
    taken: set[str] = set()
    for c in concepts:
        name = c.get("name", "").strip()
        if not name:
            continue
        cslug = slugify(name)
        if cslug in taken:
            cslug = f"{cslug}-{len(taken)}"
        taken.add(cslug)

        source_ids = [sid for sid in c.get("source_ids", []) if sid in refs]
        if not source_ids:
            continue

        # gather (ref, notes) sorted by date desc (best-effort: lexical date sort)
        items = []
        for sid in source_ids:
            ref = refs[sid]
            entry = entries_by_id.get(sid)
            note = (entry.notes if entry else "") or ""
            items.append((ref, note))
        items.sort(key=lambda x: x[0].source_post_date, reverse=True)

        write_page(
            "concepts",
            cslug,
            _format_concept_md(name, c.get("description", ""), c.get("synthesis", ""), items),
        )

        for sid in source_ids:
            entry_to_concepts.setdefault(sid, []).append(cslug)
    return entry_to_concepts


def rewrite_source_pages_with_concepts(
    entries: list[Entry],
    refs: dict[str, SourceRef],
    entry_to_concepts: dict[str, list[str]],
) -> None:
    for e in entries:
        ref = refs[e.id]
        cslugs = entry_to_concepts.get(e.id, [])
        write_page("sources", ref.slug, _format_source_md(e, cslugs))


# ---------- index ----------

def write_index(
    entries: list[Entry],
    refs: dict[str, SourceRef],
    concept_data: list[dict],
    entry_to_concepts: dict[str, list[str]],
) -> None:
    # Concepts sorted by source count desc
    concept_rows = []
    for c in concept_data:
        cslug = slugify(c["name"])
        valid_sources = [sid for sid in c.get("source_ids", []) if sid in refs]
        if not valid_sources:
            continue
        concept_rows.append((cslug, c["name"], len(valid_sources)))
    concept_rows.sort(key=lambda r: -r[2])

    # Sessions / sources
    by_date: dict[str, list[SourceRef]] = {}
    for e in entries:
        by_date.setdefault(e.source_post_date or "(no date)", []).append(refs[e.id])

    lines = [
        "# Reading Wiki",
        "",
        f"*Generated from {len(entries)} entries across {len(by_date)} sessions. "
        f"{len(concept_rows)} concepts identified.*",
        "",
        "## Concepts",
        "",
    ]
    for cslug, name, count in concept_rows:
        lines.append(f"- [[concepts/{cslug}|{name}]] · {count} sources")
    lines.append("")

    lines.append("## Sources by batch")
    lines.append("")
    for date in sorted(by_date.keys(), reverse=True):
        lines.append(f"### {date}")
        lines.append("")
        for ref in sorted(by_date[date], key=lambda r: r.title.lower()):
            rating = POPCORN * ref.rating if ref.rating else ""
            lines.append(f"- [[sources/{ref.slug}|{ref.title}]] · {rating}")
        lines.append("")

    write_page("index", "", "\n".join(lines))


# ---------- driver ----------

def collect_entries() -> list[Entry]:
    seen: dict[str, Entry] = {}
    for s in list_sessions():
        for e in list_entries_in_session(s):
            seen[e.id] = e
    return list(seen.values())


async def build(no_llm: bool, target_count: int) -> None:
    ensure_dirs()
    # Wipe sources and concepts; preserve log.md
    if SOURCES_DIR.exists():
        shutil.rmtree(SOURCES_DIR)
    if CONCEPTS_DIR.exists():
        shutil.rmtree(CONCEPTS_DIR)
    SOURCES_DIR.mkdir(parents=True)
    CONCEPTS_DIR.mkdir(parents=True)

    entries = collect_entries()
    print(f"Collected {len(entries)} entries.", file=sys.stderr)
    if not entries:
        print("Nothing to build.", file=sys.stderr)
        return

    print(f"Writing source pages...", file=sys.stderr)
    refs = write_source_pages(entries)
    entries_by_id = {e.id: e for e in entries}

    concepts: list[dict] = []
    entry_to_concepts: dict[str, list[str]] = {}
    if not no_llm:
        print(f"Extracting concepts via {CONCEPT_MODEL}...", file=sys.stderr)
        concepts = await extract_concepts(entries, target_count)
        print(f"  got {len(concepts)} concepts", file=sys.stderr)
        for c in concepts:
            valid = [sid for sid in c.get("source_ids", []) if sid in refs]
            print(f"  - {c.get('name', '?')}  ({len(valid)} sources)", file=sys.stderr)

        print(f"Writing concept pages...", file=sys.stderr)
        entry_to_concepts = write_concept_pages(concepts, refs, entries_by_id)

        print(f"Backlinking source pages to concepts...", file=sys.stderr)
        rewrite_source_pages_with_concepts(entries, refs, entry_to_concepts)

    print(f"Writing index...", file=sys.stderr)
    write_index(entries, refs, concepts, entry_to_concepts)

    append_log(
        f"Built wiki: {len(entries)} sources, {len(concepts)} concepts "
        f"({'no-llm' if no_llm else CONCEPT_MODEL})."
    )
    print(f"\nWiki written to {WIKI_DIR}", file=sys.stderr)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--no-llm", action="store_true", help="Skip concept extraction; sources only")
    p.add_argument("--concepts", type=int, default=DEFAULT_CONCEPT_COUNT, help="Target concept count")
    args = p.parse_args()
    asyncio.run(build(args.no_llm, args.concepts))


if __name__ == "__main__":
    main()
