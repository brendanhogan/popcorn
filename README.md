# popcorn

A local tool for turning a pile of links into a curated reading-list post,
with the help of Claude. Paste URLs, get summaries and title suggestions,
rate with 🍿 (1–4), chat with each page to dig deeper, then export the
whole thing as plain text (for your post) and JSON (for whatever comes
next).

## Why

Inspired by [Karpathy's LLM Wiki idea][karpathy]. I do a biweekly reading
roundup — papers, tweets, blogs — as a forcing function to keep up with
ML research. The bookkeeping (read → summarize → rank → write notes →
format) was the bottleneck. This is the ingestion layer: it turns a
batch of links into structured entries fast enough that I can do a full
biweekly batch in one sitting. The wiki / clustering / idea-discovery
layer on top of the JSON output is the next thing.

[karpathy]: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f

## What it does

- **Batch ingest.** Paste URLs (one per line). Each becomes a card with
  a Claude-generated summary, 3 candidate titles, and a panel to chat
  with the content.
- **Twitter/X workaround.** Tweets aren't fetchable (login + anti-bot),
  so each tweet entry has a paste box: drag a screenshot in, Claude
  reads the text via vision, and you process it like any other entry.
  Same flow works for paywalled articles, PDFs, screenshots — anything.
- **Sessions.** Each weekly batch is a named session. Auto-saved as
  you work. Open old sessions from the dropdown to resume.
- **Two exports.** Plain text in your post format (date header, per-entry
  Title/Type/Link/Rating/Notes; private entries grouped separately) and
  full JSON for downstream tools.
- **Private flag.** Tick "private" on entries you want to keep but not
  publish — they show in a clearly-marked section in the text export
  and stay in the JSON. The flag is local; nothing ever leaves your
  machine except the per-page Claude API calls.

## Setup

```bash
cp .env.example .env
# put your ANTHROPIC_API_KEY in .env
./run.sh
```

First run creates a venv and installs deps. Opens at
`http://localhost:8765` (override with `PORT=` in `.env`).

Requires an Anthropic API key. Defaults to `claude-sonnet-4-6`; override
via `ANTHROPIC_MODEL` in `.env`.

## Workflow

1. Open the app. Previous session is loaded automatically.
2. **New** button to start a fresh batch; **Save as…** to name it
   (e.g., `5/26 batch`).
3. Paste URLs, hit Submit. Cards stream in as Claude summarizes each.
4. For tweets / paywalled pages: open the "Paste content" section,
   drop a screenshot (or paste with ⌘V, or paste text directly).
   Claude transcribes the image, you edit if needed, click Process.
5. Pick a title (LLM suggests three — click a chip or type your own),
   rate 🍿–🍿🍿🍿🍿, write notes, optionally mark private.
6. Use the chat panel to ask follow-up questions about any entry.
7. **Copy text** when you're ready to paste into your post.
   **Download JSON** for the future wiki layer.

## Storage

Everything lives under `data/` (gitignored):

- `data/entries/{id}.json` — one file per entry
- `data/images/{id}.{ext}` — attached screenshots
- `data/sessions/{slug}.json` — session metadata (name + entry IDs)
- `data/current.txt` — slug of the active session

Safe to delete `data/` to reset everything.

## A note on scope

This is a personal tool I'm sharing as-is — fork it, hack on it, no
support or feature requests. The interesting design question (per
Karpathy's framing) is what wiki/synthesis/idea-discovery layer you
build _on top_ of an ingest pipeline like this; that part is yours
to figure out for your domain.
