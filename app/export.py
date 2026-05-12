from __future__ import annotations

from datetime import date

from .models import Entry

POPCORN = "🍿"


def _entry_text(e: Entry) -> str:
    rating = POPCORN * max(0, e.rating)
    return "\n".join(
        [
            f"Title: {e.title or '(untitled)'}",
            f"Type: {e.type}",
            f"Link: {e.url}",
            f"Rating: {rating}",
            "Description/Notes: ",
            e.notes,
        ]
    )


def export_text(entries: list[Entry]) -> str:
    today = date.today()
    header = f"{today.month}/{today.day}/{today.year % 100:02d}"

    public = [e for e in entries if not e.private]
    private = [e for e in entries if e.private]

    parts: list[str] = [header, ""]
    for e in public:
        parts.append(_entry_text(e))
        parts.append("")

    if private:
        parts.append("=" * 32)
        parts.append("[PRIVATE — not for public post]")
        parts.append("=" * 32)
        parts.append("")
        for e in private:
            parts.append(_entry_text(e))
            parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def export_json(entries: list[Entry]) -> dict:
    return {
        "exported_at": date.today().isoformat(),
        "entries": [e.model_dump() for e in entries],
    }
