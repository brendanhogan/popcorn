from __future__ import annotations

import asyncio
import mimetypes
import re
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .export import export_json, export_text
from .fetch import detect_type, fetch_content
from .llm import chat as llm_chat
from .llm import extract_text_from_image, suggest_titles, summarize
from .models import (
    BatchRequest,
    ChatRequest,
    ChatTurn,
    Entry,
    EntryPatch,
    SessionCreate,
    SessionRename,
)
from . import wiki as wiki_mod
from .storage import (
    IMAGES_DIR,
    add_entry_to_session,
    bootstrap_current_session,
    delete_entry,
    delete_session,
    get_current_session,
    list_entries_in_session,
    list_sessions,
    load_entry,
    load_session,
    new_id,
    new_session,
    save_entry,
    save_image,
    save_session,
    set_current_session_id,
)

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

app = FastAPI(title="popcorn")


@app.on_event("startup")
async def _on_startup() -> None:
    bootstrap_current_session()


_processing_sem = asyncio.Semaphore(4)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


async def _process_entry(entry_id: str) -> None:
    async with _processing_sem:
        entry = load_entry(entry_id)
        if entry is None:
            return
        try:
            entry.status = "fetching"
            save_entry(entry)

            content, error = await fetch_content(entry.url, entry.type)
            entry.fetched_content = content
            entry.fetch_error = error

            if entry.type == "twitter":
                entry.status = "ready"
                save_entry(entry)
                return

            if error or not content:
                entry.status = "error"
                save_entry(entry)
                return

            entry.status = "summarizing"
            save_entry(entry)

            summary, titles = await asyncio.gather(
                summarize(content),
                suggest_titles(content),
            )
            entry.summary = summary
            entry.title_suggestions = titles
            if titles and not entry.title:
                entry.title = titles[0]
            entry.status = "ready"
            save_entry(entry)
        except Exception as e:
            entry.status = "error"
            entry.fetch_error = entry.fetch_error or f"processing failed: {e}"
            save_entry(entry)


@app.post("/api/batch")
async def create_batch(req: BatchRequest) -> dict:
    urls = [u.strip() for u in req.urls if u.strip()]
    if not urls:
        raise HTTPException(400, "no urls provided")

    session = get_current_session()
    out = []
    for url in urls:
        entry = Entry(id=new_id(), url=url, type=detect_type(url))
        save_entry(entry)
        add_entry_to_session(entry.id, session.id)
        out.append({"id": entry.id, "url": entry.url, "type": entry.type})
        asyncio.create_task(_process_entry(entry.id))

    return {"entries": out}


@app.get("/api/entries")
async def get_entries() -> dict:
    session = get_current_session()
    entries = list_entries_in_session(session)
    return {"entries": [e.model_dump() for e in entries]}


@app.get("/api/entry/{entry_id}")
async def get_entry(entry_id: str) -> dict:
    entry = load_entry(entry_id)
    if entry is None:
        raise HTTPException(404)
    return entry.model_dump()


@app.put("/api/entry/{entry_id}")
async def update_entry(entry_id: str, patch: EntryPatch) -> dict:
    entry = load_entry(entry_id)
    if entry is None:
        raise HTTPException(404)
    data = patch.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(entry, k, v)
    save_entry(entry)
    return entry.model_dump()


class ContentBody(BaseModel):
    text: str


_IMAGE_SUFFIX = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


@app.post("/api/entry/{entry_id}/extract-image")
async def extract_image(entry_id: str, image: UploadFile = File(...)) -> dict:
    entry = load_entry(entry_id)
    if entry is None:
        raise HTTPException(404)

    media_type = image.content_type or "image/png"
    if media_type not in _IMAGE_SUFFIX:
        raise HTTPException(400, f"unsupported image type: {media_type}")

    raw = await image.read()
    if not raw:
        raise HTTPException(400, "empty image")
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, "image too large (max 10MB)")

    suffix = _IMAGE_SUFFIX[media_type]
    filename = save_image(entry_id, raw, suffix)
    entry.attached_image_filename = filename
    save_entry(entry)

    try:
        text = await extract_text_from_image(raw, media_type)
    except Exception as e:
        raise HTTPException(500, f"vision extraction failed: {e}")

    return {"text": text, "image_filename": filename}


@app.get("/api/entry/{entry_id}/image")
async def get_image(entry_id: str):
    entry = load_entry(entry_id)
    if entry is None or not entry.attached_image_filename:
        raise HTTPException(404)
    path = IMAGES_DIR / entry.attached_image_filename
    if not path.exists():
        raise HTTPException(404)
    media_type, _ = mimetypes.guess_type(path.name)
    return FileResponse(path, media_type=media_type or "application/octet-stream")


@app.post("/api/entry/{entry_id}/content")
async def set_content(entry_id: str, body: ContentBody) -> dict:
    entry = load_entry(entry_id)
    if entry is None:
        raise HTTPException(404)
    content = body.text.strip()
    if not content:
        raise HTTPException(400, "empty content")

    entry.fetched_content = content
    entry.fetch_error = ""
    entry.chat_history = []  # old chat referred to old content
    entry.content_source = "image" if entry.attached_image_filename else "paste"
    entry.status = "summarizing"
    save_entry(entry)

    try:
        summary, titles = await asyncio.gather(
            summarize(content),
            suggest_titles(content),
        )
        entry.summary = summary
        entry.title_suggestions = titles
        if titles and not entry.title:
            entry.title = titles[0]
        entry.status = "ready"
    except Exception as e:
        entry.status = "error"
        entry.fetch_error = f"summarization failed: {e}"

    save_entry(entry)
    return entry.model_dump()


@app.post("/api/entry/{entry_id}/chat")
async def chat_entry(entry_id: str, req: ChatRequest) -> dict:
    entry = load_entry(entry_id)
    if entry is None:
        raise HTTPException(404)
    if entry.type == "twitter" or not entry.fetched_content:
        raise HTTPException(400, "no content available to chat with")

    reply = await llm_chat(entry.fetched_content, entry.chat_history, req.message)
    entry.chat_history.append(ChatTurn(role="user", content=req.message))
    entry.chat_history.append(ChatTurn(role="assistant", content=reply))
    save_entry(entry)
    return {
        "reply": reply,
        "chat_history": [t.model_dump() for t in entry.chat_history],
    }


@app.delete("/api/entries")
async def clear_entries() -> dict:
    session = get_current_session()
    for eid in list(session.entry_ids):
        delete_entry(eid)
    session.entry_ids = []
    save_session(session)
    return {"status": "ok"}


# ---------- sessions ----------


def _session_summary(s) -> dict:
    return {
        "id": s.id,
        "name": s.name,
        "entry_count": len(s.entry_ids),
        "created_at": s.created_at,
        "updated_at": s.updated_at,
    }


@app.get("/api/sessions")
async def list_sessions_route() -> dict:
    current = get_current_session()
    return {
        "sessions": [_session_summary(s) for s in list_sessions()],
        "current_id": current.id,
    }


@app.get("/api/sessions/current")
async def current_session_route() -> dict:
    return _session_summary(get_current_session())


@app.post("/api/sessions")
async def create_session_route(body: SessionCreate) -> dict:
    s = new_session(body.name or None)
    set_current_session_id(s.id)
    return _session_summary(s)


@app.put("/api/sessions/{session_id}")
async def rename_session_route(session_id: str, body: SessionRename) -> dict:
    s = load_session(session_id)
    if s is None:
        raise HTTPException(404)
    s.name = body.name.strip() or "Untitled"
    save_session(s)
    return _session_summary(s)


@app.delete("/api/sessions/{session_id}")
async def delete_session_route(session_id: str) -> dict:
    s = load_session(session_id)
    if s is None:
        raise HTTPException(404)
    delete_session(session_id)
    # If we deleted the current one, fall back to most recent or a fresh "Untitled"
    bootstrap_current_session()
    return {"status": "ok"}


@app.post("/api/sessions/{session_id}/open")
async def open_session_route(session_id: str) -> dict:
    s = load_session(session_id)
    if s is None:
        raise HTTPException(404)
    set_current_session_id(session_id)
    return _session_summary(s)


# ---------- wiki ----------


@app.get("/wiki")
@app.get("/wiki/{path:path}")
async def wiki_view(path: str = "") -> FileResponse:
    return FileResponse(STATIC / "wiki.html")


@app.get("/api/wiki/index")
async def wiki_index() -> dict:
    pages = wiki_mod.list_pages()
    concepts: list[dict] = []
    for slug in pages["concepts"]:
        text = wiki_mod.read_page("concepts", slug) or ""
        title = wiki_mod.page_title(text, fallback=slug)
        source_count = len(wiki_mod.WIKI_LINK_RE.findall(text)) - 1  # rough: subtract self-link if any
        # better: count "Sources (N)" header
        m = re.search(r"## Sources \((\d+)\)", text)
        if m:
            source_count = int(m.group(1))
        concepts.append({"slug": slug, "title": title, "source_count": source_count})
    concepts.sort(key=lambda c: -c["source_count"])

    sources: list[dict] = []
    for slug in pages["sources"]:
        text = wiki_mod.read_page("sources", slug) or ""
        title = wiki_mod.page_title(text, fallback=slug)
        date_match = re.search(r"\*\*First seen:\*\*\s*(\S+)", text)
        rating_match = re.search(r"\*\*Rating:\*\*\s*(\S+)", text)
        sources.append({
            "slug": slug,
            "title": title,
            "date": date_match.group(1) if date_match else "",
            "rating_str": rating_match.group(1) if rating_match else "",
        })
    sources.sort(key=lambda s: s["date"], reverse=True)

    has_index = (wiki_mod.INDEX_PATH).exists()
    return {
        "has_index": has_index,
        "concepts": concepts,
        "sources": sources,
        "counts": {
            "concepts": len(pages["concepts"]),
            "sources": len(pages["sources"]),
        },
    }


@app.get("/api/wiki/page")
async def wiki_page(path: str) -> dict:
    resolved = wiki_mod.resolve_fq(path)
    if resolved is None:
        raise HTTPException(404, f"unknown page path: {path}")
    kind, slug = resolved
    text = wiki_mod.read_page(kind, slug)
    if text is None:
        raise HTTPException(404, f"page not found: {path}")
    title = wiki_mod.page_title(text, fallback=slug or kind)
    html = wiki_mod.render_to_html(text)
    backlinks_raw = wiki_mod.find_backlinks(path)
    backlinks = []
    for bk_kind, bk_slug in backlinks_raw:
        bk_text = wiki_mod.read_page(bk_kind, bk_slug) or ""
        bk_title = wiki_mod.page_title(bk_text, fallback=bk_slug or bk_kind)
        backlinks.append({
            "path": wiki_mod.fq_slug(bk_kind, bk_slug),
            "title": bk_title,
            "kind": bk_kind,
        })
    return {
        "path": path,
        "kind": kind,
        "title": title,
        "html": html,
        "markdown": text,
        "backlinks": backlinks,
    }


_wiki_build_state = {"running": False, "started_at": "", "last_finished_at": "", "last_error": ""}


@app.post("/api/wiki/build")
async def wiki_build() -> dict:
    if _wiki_build_state["running"]:
        return {"status": "already_running", **_wiki_build_state}

    async def runner():
        from datetime import datetime, timezone
        from .build_wiki import build
        _wiki_build_state["running"] = True
        _wiki_build_state["started_at"] = datetime.now(timezone.utc).isoformat()
        _wiki_build_state["last_error"] = ""
        try:
            await build(no_llm=False, target_count=20)
        except Exception as e:
            _wiki_build_state["last_error"] = f"{type(e).__name__}: {e}"
        finally:
            _wiki_build_state["running"] = False
            _wiki_build_state["last_finished_at"] = datetime.now(timezone.utc).isoformat()

    asyncio.create_task(runner())
    return {"status": "started", **_wiki_build_state}


@app.get("/api/wiki/build/status")
async def wiki_build_status() -> dict:
    return dict(_wiki_build_state)


@app.get("/api/export/text", response_class=PlainTextResponse)
async def export_text_route() -> str:
    session = get_current_session()
    return export_text(list_entries_in_session(session))


@app.get("/api/export/json")
async def export_json_route() -> dict:
    session = get_current_session()
    return export_json(list_entries_in_session(session))
