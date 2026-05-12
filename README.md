# popcorn

A local tool for turning a stream of links into (a) a curated reading-list
post and (b) a synthesized wiki of your interests, with Claude doing the
bookkeeping. Inspired by [Karpathy's LLM Wiki idea][karpathy] — popcorn is
one concrete instantiation focused on a reading-list workflow.

[karpathy]: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f

Everything runs on your machine. The only thing that leaves is per-page
text sent to the Anthropic API for summarization.

---

## What you get

**Two surfaces in the same app:**

```
http://localhost:8765/         →  ingest: paste links, rate, write notes, export
http://localhost:8765/wiki     →  browse a Claude-synthesized wiki of your corpus
```

**Ingest** (`/`)

- Paste URLs (one per line). Each becomes a card with a Claude-generated
  summary, three candidate titles you can click, and a chat panel you
  can use to ask follow-up questions about the content.
- Twitter / paywalled / JS-rendered pages can't be auto-fetched. Drop a
  screenshot onto the card and Claude vision transcribes the text — same
  pipeline runs from there.
- Rate with 🍿 (1–4), write notes, tick *private* to exclude from public
  export.
- Sessions group your batches by name. Auto-saved as you work.
- Export plain text in a per-entry block format (for your post), or full
  JSON (for downstream tooling and the wiki).

**Wiki** (`/wiki`)

Five article types, all auto-generated, all cross-linked with automatic
backlinks:

- **Source pages** — one per entry (title, rating, link, your notes, the
  Claude summary, every concept it belongs to).
- **Concept pages** — 15–25 recurring themes across your corpus,
  synthesized with quotes from *your actual notes*. The wiki describes
  your interests in your voice, not as neutral summaries.
- **Meta articles** — 4–6 cross-cutting *worldviews* tying clusters of
  concepts together. Built by clustering the concept embeddings, then
  asking Claude to name and synthesize the thesis underneath each cluster.
  Things like *"LLMs as substrates for simulated minds and societies"* or
  *"Modular, compositional intelligence over monolithic scale."*
- **Entity pages** — people, labs, companies, products, papers mentioned
  in 3+ entries. Each page tracks everything you've said about that entity.
- **Batch recaps** — one auto-generated journal-style recap per past
  session ("Persona simulation clicks; Claude Code life deepens further").

**Map** (`/wiki/map`)

- 2D t-SNE projection of every entry + concept centroid.
- Points sized by rating (🍿), colored by date (cream→terracotta for
  time-drift).
- Hover for title, click to open the entry/concept page.
- **Click empty space** → Claude reads the nearest entries and proposes
  2–3 specific papers/posts/ideas that would naturally live in that empty
  patch of idea-space. The "what's missing from your reading" inverse.

Click **Rebuild** in the topbar to regenerate after a new batch
(concepts + meta + entities + batch recaps + embeddings + projection).
~60s, ~$0.50.

---

## Setup

```bash
git clone https://github.com/<you>/popcorn
cd popcorn
cp .env.example .env                 # put your ANTHROPIC_API_KEY in
./run.sh                             # creates venv on first run
```

Opens at `http://localhost:8765` (override with `PORT=` in `.env`).

Defaults to `claude-sonnet-4-6` for live ingest. The bulk-import script
hardcodes `claude-haiku-4-5` for cost reasons. Override via env or CLI
flags.

---

## Workflow

**Day-to-day:**
1. Open the app. Your previous session is loaded automatically.
2. Click **New** to start a fresh batch, **Save as…** to name it
   (e.g. `5/26 batch`).
3. Paste URLs, hit Submit. Cards stream in as Claude summarizes each.
4. For tweets / paywalled pages: open the *Paste content* section, drop
   a screenshot (or ⌘V an image, or paste text directly). Claude
   transcribes, you edit if needed, click **Process**.
5. Pick a title, rate 🍿–🍿🍿🍿🍿, write notes, optionally mark private.
6. Use the chat panel to ask follow-up questions about any entry.
7. **Copy text** for your public post. **Download JSON** for any
   downstream tooling.

**Periodically:**

8. Click **Wiki →** in the topbar. Browse concepts, source pages,
   backlinks. Click **Rebuild** after each new batch to fold the latest
   entries into the synthesis.

---

## Backfill from existing posts

If you already have a body of curated reading-list posts (a Google Doc,
a blog, Twitter threads) you can import them in one shot:

1. Concatenate the posts into a single text file at
   `data/import/past_posts.txt`, in the format:

   ```
   M/D/YY

   Title: <your title>
   Type: <paper|twitter|other>
   Link: <url>
   Rating: 🍿🍿
   Description/Notes:
   <multi-line notes...>

   Title: <next entry>
   ...

   M/D/YY
   ...
   ```

   (The Rating field can have 0-4 🍿; spacing is tolerant.)

2. Run:

   ```bash
   .venv/bin/python -m app.import_history data/import/past_posts.txt
   ```

   Optional flags: `--dry-run` (parse only, no API calls), `--limit N`
   (process first N entries).

3. This creates one session per date header and one entry per item,
   preserving your titles/ratings/notes verbatim. URLs that can be
   fetched also get Haiku-generated summaries. Twitter URLs are
   stored with your notes as the only content (which is usually richer
   than a generic summary anyway).

4. Build the wiki on the imported corpus:

   ```bash
   .venv/bin/python -m app.build_wiki
   ```

   Or use the **Rebuild** button in the wiki UI.

---

## Architecture

```
data/
├── entries/{id}.json          # one canonical record per entry
├── images/{id}.{ext}          # attached screenshots
├── sessions/{slug}.json       # session = name + list of entry IDs
├── current.txt                # slug of the active session
├── embeddings.npz             # local sentence-transformers vectors (entries + concepts)
├── projection.json            # t-SNE 2D coords cached for the map view
└── wiki/
    ├── index.md
    ├── log.md
    ├── sources/{slug}.md      # auto-generated, one per entry
    ├── concepts/{slug}.md     # Claude-synthesized themes
    ├── meta/{slug}.md         # cross-cutting meta-syntheses
    ├── entities/{slug}.md     # people, labs, products, papers
    └── batches/{slug}.md      # per-session journal-style recaps
```

**Data flow:**

```
   [paste URLs]      [drop screenshot]
        │                  │
        ▼                  ▼
   fetch + trafilatura     Claude vision → text
        │                  │
        └──────────┬───────┘
                   ▼
       Entry (id, url, type, title, rating, notes, summary,
              fetched_content, chat_history, private,
              backfilled, source_post_date, ...)
                   │
                   ├─→ session.entry_ids
                   │
                   └─→ (on Rebuild)
                        Claude reads all entries (title + notes + summary)
                        → identifies 15-25 recurring concepts
                        → writes one .md page per concept + one per source
                        → cross-links them
```

**Two-pass concept extraction:** entries are numbered `[1]`, `[2]`, …
before being sent to Claude so the model can reliably emit which entries
belong to which concept. Synthesis is constrained to refer to sources
by short title fragments (never numeric references) so the prose reads
naturally. See `app/build_wiki.py`.

`data/` is gitignored. Safe to `rm -rf data/` to start over.

---

## Costs

Real numbers, not estimates. Anthropic pricing as of model release:

| Operation                              | Model           | Per entry / batch    |
|----------------------------------------|-----------------|----------------------|
| Live ingest (summary + 3 titles)       | Sonnet 4.6      | ~$0.02–0.05 / entry  |
| Vision OCR on a screenshot             | Sonnet 4.6      | ~$0.01 / image       |
| Chat with a page (per turn)            | Sonnet 4.6      | <$0.01 / turn        |
| Bulk import (Haiku, summary only)      | Haiku 4.5       | ~$0.005 / entry      |
| Wiki rebuild (concepts + meta + entities + batches) | Sonnet 4.6 | ~$0.50 / 170 entries |
| Embeddings + 2D projection             | Local (CPU)     | $0 (sentence-transformers + sklearn) |
| Idea discovery click                   | Sonnet 4.6      | ~$0.01 / click       |

For a heavy user (~30 entries/week + a wiki rebuild every two weeks):
roughly $2–5/month.

---

## Limits

- **Twitter / X is not auto-fetched.** The platform aggressively blocks
  scrapers and even login-cookie tricks break monthly. Use the
  screenshot workflow.
- **JS-rendered pages** (some blogs, app-like sites, dashboards) often
  fail `trafilatura`'s text extraction. The tool surfaces the failure
  and the screenshot workflow is the universal fallback.
- **Paywalls, 403s, SSL issues** are all surfaced inline with the
  underlying error; manually paste the content into the entry if you
  care about that one.
- **iframe preview is intentionally absent** — most sites set
  `X-Frame-Options: DENY` so it was uselessly blank ~80% of the time.

---

## Customize

This is a worked example, not a maintained product. Strong opinions
baked in that you'll want to override for your own use:

- **The system prompt** in `app/llm.py` references "a researcher's
  reading queue" — rephrase for your domain.
- **The export plain-text template** in `app/export.py` matches one
  specific post format. Rewrite for your shape.
- **Type detection** in `app/fetch.py` knows about arxiv and twitter.
  Add domain rules as needed.
- **The concept extraction prompt** in `app/build_wiki.py` defines
  what kind of themes Claude looks for. Tune if the auto-concepts
  feel off for your corpus.
- **Default model** is `claude-sonnet-4-6` — set `ANTHROPIC_MODEL` in
  `.env` to change. Haiku 4.5 is meaningfully cheaper and still good
  for short-form ingest.

Pattern works for many domains beyond reading lists — research notes,
job-hunt tracking, book/paper review queues, scouting reports. The
two-phase ingest-then-synthesize structure is the part that generalizes.

---

## License

MIT. Fork freely. This is a personal tool released as-is — no support
or feature requests, but PRs and forks welcome. The interesting work
isn't in this code; it's in what you put into the wiki on top.
