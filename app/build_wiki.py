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
    BATCHES_DIR,
    CONCEPTS_DIR,
    ENTITIES_DIR,
    META_DIR,
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


# ---------- meta articles (clusters of concepts) ----------

META_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "description": {"type": "string"},
        "synthesis": {"type": "string"},
    },
    "required": ["name", "description", "synthesis"],
    "additionalProperties": False,
}


def cluster_concepts(concepts: list[dict], n_clusters: int = 5) -> list[list[int]]:
    """Cluster the given concepts using their stored embeddings.

    Returns a list of clusters, each being a list of indices into `concepts`.
    Only includes clusters with at least 2 concepts (singletons are dropped).
    """
    from sklearn.cluster import AgglomerativeClustering

    from .embed import load_embeddings

    store = load_embeddings()
    if store is None:
        return []

    concept_store = store.of_kind("concept")
    if len(concept_store) == 0:
        return []

    # Match each concept (by slug) to its vector
    vectors: list = []
    valid_indices: list[int] = []
    for i, c in enumerate(concepts):
        cslug = slugify(c.get("name", ""))
        idx = concept_store.index_by_id(f"concept:{cslug}")
        if idx is not None:
            vectors.append(concept_store.vectors[idx])
            valid_indices.append(i)

    if len(vectors) < 2:
        return []

    import numpy as np
    X = np.stack(vectors)
    n_clusters_eff = min(n_clusters, len(vectors) - 1)
    labels = AgglomerativeClustering(n_clusters=n_clusters_eff, linkage="average").fit_predict(X)

    clusters: dict[int, list[int]] = {}
    for local_i, label in enumerate(labels):
        clusters.setdefault(int(label), []).append(valid_indices[local_i])

    return [members for members in clusters.values() if len(members) >= 2]


def _meta_prompt(cluster_concepts: list[dict]) -> str:
    sections = []
    for c in cluster_concepts:
        cname = c.get("name", "")
        desc = c.get("description", "")
        synth = c.get("synthesis", "")
        # take first paragraph of synthesis to keep prompt compact
        first_para = synth.split("\n\n")[0] if synth else ""
        sections.append(f"### {cname}\n{desc}\n\n{first_para}")

    bodies = "\n\n".join(sections)
    return f"""You're looking at a cluster of concept pages from a researcher's personal wiki.
These concepts cluster together in semantic embedding space — they share underlying ideas.

Your task: identify the cross-cutting *worldview* or *thesis* that ties them together.
This is one level of abstraction up from the individual concepts.

Output via the JSON schema:
- **name**: a short label (3-9 words) for the meta-theme. Should feel like a *worldview*
  or *thesis*, not a category. Good example: "LLMs as found objects, not engineered
  systems." Bad example: "Topics about LLMs."
- **description**: 1-2 sentences naming what unifies these concepts and why it matters.
- **synthesis**: 4-7 sentences threading the concepts together into a coherent thread.
  Refer to concepts by their names (e.g. "the persona-selection-model thread") — never
  use bracket references. End with the underlying question or takeaway.

Cluster concepts:

{bodies}
"""


async def extract_meta_article(cluster_concepts: list[dict]) -> dict | None:
    if not cluster_concepts:
        return None
    client = AsyncAnthropic()
    prompt = _meta_prompt(cluster_concepts)
    try:
        response = await client.messages.create(
            model=CONCEPT_MODEL,
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": META_SCHEMA}},
        )
        text = next((b.text for b in response.content if b.type == "text"), "")
        return json.loads(text)
    except Exception as e:
        print(f"  meta extraction failed for cluster: {e}", file=sys.stderr)
        return None


def _format_meta_md(meta: dict, cluster_concepts: list[dict]) -> str:
    cleaned_synth = _ENTRY_REF_RE.sub("", meta.get("synthesis", "")).strip()
    lines = [
        f"# {meta.get('name', '(untitled meta)')}",
        "",
        f"*{meta.get('description', '')}*",
        "",
        "## The thread",
        "",
        cleaned_synth,
        "",
        "## Concepts in this thread",
        "",
    ]
    for c in cluster_concepts:
        cslug = slugify(c.get("name", ""))
        lines.append(f"- [[concepts/{cslug}|{c.get('name', '')}]]")
    lines.append("")
    return "\n".join(lines)


async def build_meta_articles(concepts: list[dict]) -> list[dict]:
    """Returns list of meta dicts written. Empty if embeddings unavailable."""
    clusters = cluster_concepts(concepts, n_clusters=5)
    if not clusters:
        print("  no clusters formed (need embeddings + at least 2 concepts)", file=sys.stderr)
        return []

    print(f"  formed {len(clusters)} concept clusters", file=sys.stderr)
    tasks = [extract_meta_article([concepts[i] for i in members]) for members in clusters]
    metas = await asyncio.gather(*tasks)

    written: list[dict] = []
    taken: set[str] = set()
    for meta, members in zip(metas, clusters):
        if meta is None:
            continue
        name = meta.get("name", "").strip()
        if not name:
            continue
        mslug = slugify(name)
        if mslug in taken:
            mslug = f"{mslug}-{len(taken)}"
        taken.add(mslug)
        cluster_concepts_list = [concepts[i] for i in members]
        write_page("meta", mslug, _format_meta_md(meta, cluster_concepts_list))
        written.append({"slug": mslug, **meta, "concept_count": len(members)})
        print(f"  - {name}  ({len(members)} concepts)", file=sys.stderr)
    return written


# ---------- entity articles ----------

ENTITY_SCHEMA = {
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "kind": {"type": "string"},
                    "description": {"type": "string"},
                    "synthesis": {"type": "string"},
                    "source_numbers": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["name", "kind", "description", "synthesis", "source_numbers"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["entities"],
    "additionalProperties": False,
}


def _entity_prompt(entries: list[Entry]) -> str:
    return f"""You are extracting named entities from a researcher's biweekly reading list.

Below are {len(entries)} entries, each numbered [N]. Identify named entities — people,
research labs, companies, products, or specific famous papers/projects — that appear
in **at least 3 entries**. For each, return:

- **name**: the entity's name as commonly referred to (e.g., "Stefano Ermon", "Anthropic",
  "Claude Code", "AlphaZero", "Karpathy")
- **kind**: one of: `person`, `lab`, `company`, `product`, `paper`
- **description**: one sentence locating who/what this is
- **synthesis**: 3-6 sentences summarizing what the user has said about this entity across
  the entries — quote short fragments of their notes where they reveal opinion or interest.
  Refer to entries by short title fragments (e.g., "in the Mercury 2 launch entry"), never
  by bracket references like "Entry [N]".
- **source_numbers**: the [N] numbers of every entry that mentions this entity

Rules:
- Skip generic concepts ("LLMs", "agents", "RL") — only NAMED entities
- Skip entities that appear in only 1-2 entries
- If an entity appears under variant names (e.g. "Andrej Karpathy" / "Karpathy" / "AK"),
  treat them as one
- Output via the JSON schema, no preamble

---

Entries:

{_entries_for_prompt(entries)}
"""


async def extract_entities(entries: list[Entry]) -> list[dict]:
    """Returns list of entity dicts with `source_ids` (mapped from numbers)."""
    client = AsyncAnthropic()
    prompt = _entity_prompt(entries)
    print(f"  entity prompt ~{len(prompt) // 4} tokens", file=sys.stderr)
    response = await client.messages.create(
        model=CONCEPT_MODEL,
        max_tokens=16000,
        messages=[{"role": "user", "content": prompt}],
        output_config={"format": {"type": "json_schema", "schema": ENTITY_SCHEMA}},
    )
    text = next((b.text for b in response.content if b.type == "text"), "")
    data = json.loads(text)

    out: list[dict] = []
    for e in data.get("entities", []):
        nums = e.get("source_numbers", [])
        ids: list[str] = []
        for n in nums:
            if isinstance(n, int) and 1 <= n <= len(entries):
                ids.append(entries[n - 1].id)
        if len(ids) < 3:
            continue
        out.append({
            "name": e.get("name", ""),
            "kind": e.get("kind", "other"),
            "description": e.get("description", ""),
            "synthesis": e.get("synthesis", ""),
            "source_ids": ids,
        })
    return out


def _format_entity_md(entity: dict, refs: dict[str, SourceRef]) -> str:
    name = entity.get("name", "(unnamed)")
    kind = entity.get("kind", "")
    description = entity.get("description", "")
    synthesis = _ENTRY_REF_RE.sub("", entity.get("synthesis", "")).strip()

    lines = [
        f"# {name}",
        "",
        f"*{kind}{(' · ' + description) if description else ''}*",
        "",
        "## What the user has said",
        "",
        synthesis,
        "",
        f"## Mentions ({len(entity['source_ids'])})",
        "",
    ]
    for sid in entity["source_ids"]:
        ref = refs.get(sid)
        if not ref:
            continue
        rating = POPCORN * ref.rating if ref.rating else ""
        lines.append(f"- [[sources/{ref.slug}|{ref.title}]] · {rating} · {ref.source_post_date or '—'}")
    lines.append("")
    return "\n".join(lines)


def write_entity_pages(entities: list[dict], refs: dict[str, SourceRef]) -> list[dict]:
    written: list[dict] = []
    taken: set[str] = set()
    for e in entities:
        name = e.get("name", "").strip()
        if not name or len(e.get("source_ids", [])) < 3:
            continue
        eslug = slugify(name)
        if eslug in taken:
            eslug = f"{eslug}-{len(taken)}"
        taken.add(eslug)
        write_page("entities", eslug, _format_entity_md(e, refs))
        written.append({"slug": eslug, "name": name, "kind": e.get("kind", ""), "mentions": len(e["source_ids"])})
    return written


# ---------- batch recap articles ----------

BATCH_RECAP_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "recap": {"type": "string"},
    },
    "required": ["headline", "recap"],
    "additionalProperties": False,
}


def _batch_prompt(session_name: str, session_entries: list[Entry]) -> str:
    blocks = []
    for i, e in enumerate(session_entries, 1):
        rating = POPCORN * e.rating if e.rating else "?"
        head = f"[{i}] {rating} {e.type} · {e.title or '(untitled)'}"
        notes = f"notes: {e.notes}" if e.notes else "(no notes)"
        blocks.append(f"{head}\n{notes}")
    body = "\n\n".join(blocks)
    return f"""You're writing a one-paragraph journal-style recap of a single biweekly batch
of reading from a researcher named the user. Session: "{session_name}", {len(session_entries)} entries.

Read the entries and write:

- **headline**: a short (4-10 word) headline capturing the dominant character of this
  batch. Example: "Persona simulation finally clicks; Claude Code week continues."
- **recap**: 4-7 sentences in a journal voice. What themes dominated? What got the
  highest ratings? What did the user seem excited about, what were they skeptical of?
  Quote short fragments from their notes where they sharpen a point. Refer to entries
  by short title fragments — never by bracket references. Make it feel like a
  retrospective the user might have written themselves.

Output via the JSON schema, no preamble.

---

Entries in this batch:

{body}
"""


async def make_batch_recap(session_name: str, session_entries: list[Entry]) -> dict | None:
    if not session_entries:
        return None
    client = AsyncAnthropic()
    try:
        response = await client.messages.create(
            model=CONCEPT_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": _batch_prompt(session_name, session_entries)}],
            output_config={"format": {"type": "json_schema", "schema": BATCH_RECAP_SCHEMA}},
        )
        text = next((b.text for b in response.content if b.type == "text"), "")
        return json.loads(text)
    except Exception as e:
        print(f"  recap failed for {session_name}: {e}", file=sys.stderr)
        return None


def _format_batch_md(session_name: str, recap: dict, session_entries: list[Entry],
                     refs: dict[str, SourceRef]) -> str:
    headline = recap.get("headline", "")
    body = _ENTRY_REF_RE.sub("", recap.get("recap", "")).strip()
    lines = [
        f"# {session_name}",
        "",
        f"*{headline}*" if headline else "",
        "",
        "## Recap",
        "",
        body,
        "",
        f"## Entries ({len(session_entries)})",
        "",
    ]
    # sort by rating desc, then title
    sorted_es = sorted(session_entries, key=lambda e: (-e.rating, (e.title or "").lower()))
    for e in sorted_es:
        ref = refs.get(e.id)
        if not ref:
            continue
        rating = POPCORN * e.rating if e.rating else ""
        lines.append(f"- [[sources/{ref.slug}|{ref.title}]] · {rating}")
    lines.append("")
    return "\n".join(lines)


async def build_batch_recaps(refs: dict[str, SourceRef]) -> list[dict]:
    from .storage import list_sessions

    sessions = list_sessions()
    if not sessions:
        return []

    # Build (session, entries) pairs
    pairs: list[tuple] = []
    for s in sessions:
        es = [e for eid in s.entry_ids if (e := load_entry(eid))]
        if es:
            pairs.append((s, es))

    print(f"  generating recaps for {len(pairs)} sessions...", file=sys.stderr)
    recap_tasks = [make_batch_recap(s.name, es) for s, es in pairs]
    recaps = await asyncio.gather(*recap_tasks)

    written: list[dict] = []
    taken: set[str] = set()
    for (s, es), recap in zip(pairs, recaps):
        if recap is None:
            continue
        bslug = slugify(s.name)
        if bslug in taken:
            bslug = f"{bslug}-{len(taken)}"
        taken.add(bslug)
        write_page("batches", bslug, _format_batch_md(s.name, recap, es, refs))
        written.append({
            "slug": bslug,
            "name": s.name,
            "headline": recap.get("headline", ""),
            "entry_count": len(es),
        })
        print(f"  - {s.name}: {recap.get('headline', '')[:60]}", file=sys.stderr)
    return written


# ---------- index ----------

def write_index(
    entries: list[Entry],
    refs: dict[str, SourceRef],
    concept_data: list[dict],
    entry_to_concepts: dict[str, list[str]],
    metas: list[dict] | None = None,
    entities: list[dict] | None = None,
    batches: list[dict] | None = None,
) -> None:
    metas = metas or []
    entities = entities or []
    batches = batches or []
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
        f"{len(concept_rows)} concepts, {len(metas)} meta articles.*",
        "",
    ]

    if metas:
        lines += ["## Meta articles", "", "*Cross-cutting threads tying multiple concepts together.*", ""]
        for m in metas:
            lines.append(f"- [[meta/{m['slug']}|{m.get('name', '')}]] · {m.get('concept_count', 0)} concepts")
        lines.append("")

    lines += ["## Concepts", ""]
    for cslug, name, count in concept_rows:
        lines.append(f"- [[concepts/{cslug}|{name}]] · {count} sources")
    lines.append("")

    if entities:
        lines += ["## Entities", "", "*People, labs, companies, products mentioned 3+ times.*", ""]
        sorted_entities = sorted(entities, key=lambda e: -e.get("mentions", 0))
        for e in sorted_entities:
            kind_tag = f"_{e.get('kind', '')}_" if e.get("kind") else ""
            lines.append(f"- [[entities/{e['slug']}|{e['name']}]] {kind_tag} · {e['mentions']} mentions")
        lines.append("")

    if batches:
        lines += ["## Batch recaps", "", "*One auto-generated recap per past session.*", ""]
        for b in batches:
            line = f"- [[batches/{b['slug']}|{b['name']}]]"
            if b.get("headline"):
                line += f" — *{b['headline']}*"
            lines.append(line)
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


async def build(no_llm: bool, target_count: int, *, skip_embed: bool = False,
                skip_meta: bool = False, skip_entities: bool = False,
                skip_batches: bool = False) -> None:
    ensure_dirs()
    # Wipe regenerated dirs; preserve log.md
    for d in (SOURCES_DIR, CONCEPTS_DIR, META_DIR, ENTITIES_DIR, BATCHES_DIR):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)

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
    metas: list[dict] = []

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

        if not skip_embed:
            print(f"Embedding entries and concepts...", file=sys.stderr)
            from .embed import build_embeddings
            build_embeddings()

        if not skip_meta and not skip_embed:
            print(f"Generating meta articles from concept clusters...", file=sys.stderr)
            metas = await build_meta_articles(concepts)

    entities_written: list[dict] = []
    if not no_llm and not skip_entities:
        print(f"Extracting entities via {CONCEPT_MODEL}...", file=sys.stderr)
        entities = await extract_entities(entries)
        print(f"  got {len(entities)} entities (≥3 mentions)", file=sys.stderr)
        entities_written = write_entity_pages(entities, refs)
        for ew in entities_written:
            print(f"  - {ew['name']} ({ew['kind']}, {ew['mentions']} mentions)", file=sys.stderr)

    batches_written: list[dict] = []
    if not no_llm and not skip_batches:
        print(f"Generating batch recaps...", file=sys.stderr)
        batches_written = await build_batch_recaps(refs)

    # 2D projection over the freshly-built embeddings
    if not skip_embed:
        print(f"Building 2D projection...", file=sys.stderr)
        from .projection import build_projection
        slug_map = {eid: ref.slug for eid, ref in refs.items()}
        build_projection(entry_id_to_slug=slug_map)

    print(f"Writing index...", file=sys.stderr)
    write_index(entries, refs, concepts, entry_to_concepts, metas, entities_written, batches_written)

    append_log(
        f"Built wiki: {len(entries)} sources, {len(concepts)} concepts, "
        f"{len(metas)} meta, {len(entities_written)} entities, "
        f"{len(batches_written)} batch recaps "
        f"({'no-llm' if no_llm else CONCEPT_MODEL})."
    )
    print(f"\nWiki written to {WIKI_DIR}", file=sys.stderr)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--no-llm", action="store_true", help="Skip concept extraction; sources only")
    p.add_argument("--concepts", type=int, default=DEFAULT_CONCEPT_COUNT, help="Target concept count")
    p.add_argument("--no-embed", action="store_true", help="Skip embedding step")
    p.add_argument("--no-meta", action="store_true", help="Skip meta article generation")
    p.add_argument("--no-entities", action="store_true", help="Skip entity extraction")
    p.add_argument("--no-batches", action="store_true", help="Skip batch recap generation")
    args = p.parse_args()
    asyncio.run(build(
        args.no_llm, args.concepts,
        skip_embed=args.no_embed,
        skip_meta=args.no_meta,
        skip_entities=args.no_entities,
        skip_batches=args.no_batches,
    ))


if __name__ == "__main__":
    main()
