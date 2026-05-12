from __future__ import annotations

import json
import secrets
from pathlib import Path
from threading import Lock

from .models import Entry, Session

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ENTRIES_DIR = DATA_DIR / "entries"
IMAGES_DIR = DATA_DIR / "images"
SESSIONS_DIR = DATA_DIR / "sessions"
CURRENT_PATH = DATA_DIR / "current.txt"
LEGACY_INDEX_PATH = DATA_DIR / "index.json"

# kept for backwards-compatible imports from main.py (no longer the source of truth)
INDEX_PATH = LEGACY_INDEX_PATH

_lock = Lock()


def _ensure_dirs() -> None:
    ENTRIES_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def new_id() -> str:
    return secrets.token_urlsafe(8)


# ---------- entries ----------

def _entry_path(entry_id: str) -> Path:
    return ENTRIES_DIR / f"{entry_id}.json"


def save_entry(entry: Entry) -> None:
    _ensure_dirs()
    entry.touch()
    with _lock:
        _entry_path(entry.id).write_text(entry.model_dump_json(indent=2))


def load_entry(entry_id: str) -> Entry | None:
    p = _entry_path(entry_id)
    if not p.exists():
        return None
    return Entry.model_validate_json(p.read_text())


def delete_entry(entry_id: str) -> None:
    p = _entry_path(entry_id)
    if not p.exists():
        return
    try:
        e = Entry.model_validate_json(p.read_text())
        if e.attached_image_filename:
            img = IMAGES_DIR / e.attached_image_filename
            if img.exists():
                img.unlink()
    except Exception:
        pass
    p.unlink()


def save_image(entry_id: str, content: bytes, suffix: str) -> str:
    _ensure_dirs()
    filename = f"{entry_id}{suffix}"
    (IMAGES_DIR / filename).write_bytes(content)
    return filename


# ---------- sessions ----------

def _session_path(slug: str) -> Path:
    return SESSIONS_DIR / f"{slug}.json"


def load_session(slug: str) -> Session | None:
    p = _session_path(slug)
    if not p.exists():
        return None
    return Session.model_validate_json(p.read_text())


def save_session(session: Session) -> None:
    _ensure_dirs()
    session.touch()
    with _lock:
        _session_path(session.id).write_text(session.model_dump_json(indent=2))


def list_sessions() -> list[Session]:
    _ensure_dirs()
    sessions: list[Session] = []
    paths = list(SESSIONS_DIR.glob("*.json"))
    paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for p in paths:
        try:
            sessions.append(Session.model_validate_json(p.read_text()))
        except Exception:
            continue
    return sessions


def delete_session(slug: str) -> None:
    session = load_session(slug)
    if session is None:
        return
    for eid in session.entry_ids:
        delete_entry(eid)
    _session_path(slug).unlink(missing_ok=True)


def get_current_session_id() -> str:
    if CURRENT_PATH.exists():
        return CURRENT_PATH.read_text().strip()
    return ""


def set_current_session_id(slug: str) -> None:
    _ensure_dirs()
    CURRENT_PATH.write_text(slug)


def new_session(name: str | None = None) -> Session:
    sid = new_id()
    s = Session(id=sid, name=name or "Untitled", entry_ids=[])
    save_session(s)
    return s


def add_entry_to_session(entry_id: str, session_id: str) -> None:
    s = load_session(session_id)
    if s is None:
        return
    if entry_id not in s.entry_ids:
        s.entry_ids.append(entry_id)
        save_session(s)


def list_entries_in_session(session: Session) -> list[Entry]:
    out: list[Entry] = []
    for eid in session.entry_ids:
        e = load_entry(eid)
        if e is not None:
            out.append(e)
    return out


def bootstrap_current_session() -> Session:
    """Return the active session, creating/migrating one if needed."""
    _ensure_dirs()
    current_id = get_current_session_id()
    if current_id:
        s = load_session(current_id)
        if s is not None:
            return s

    # No valid current pointer; fall back to most recent session
    sessions = list_sessions()
    if sessions:
        set_current_session_id(sessions[0].id)
        return sessions[0]

    # No sessions exist; migrate from legacy index.json if it has any entries
    if LEGACY_INDEX_PATH.exists():
        try:
            ids = json.loads(LEGACY_INDEX_PATH.read_text())
            if isinstance(ids, list) and ids:
                s = new_session("Imported")
                s.entry_ids = [str(i) for i in ids]
                save_session(s)
                set_current_session_id(s.id)
                return s
        except Exception:
            pass

    s = new_session("Untitled")
    set_current_session_id(s.id)
    return s


def get_current_session() -> Session:
    return bootstrap_current_session()
