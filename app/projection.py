"""2D projection of entry/concept embeddings via t-SNE.

Produces data/projection.json with one record per embedded point:

    {
      "id": "<entry_id or concept:slug>",
      "kind": "entry" | "concept",
      "x": float,           # in [-1, 1]
      "y": float,           # in [-1, 1]
      "title": str,
      "slug": str,          # wiki slug for navigation
      ...kind-specific fields...
    }

Built at the end of `build_wiki`; consumed by /api/wiki/map.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

from .embed import EMBED_PATH, load_embeddings
from .storage import DATA_DIR, list_entries_in_session, list_sessions
from .wiki import CONCEPTS_DIR

PROJECTION_PATH = DATA_DIR / "projection.json"


def _normalize(arr: np.ndarray) -> np.ndarray:
    lo, hi = float(arr.min()), float(arr.max())
    if hi <= lo:
        return np.zeros_like(arr)
    return 2.0 * (arr - lo) / (hi - lo) - 1.0


def _concept_title(slug: str) -> str:
    p = CONCEPTS_DIR / f"{slug}.md"
    if p.exists():
        first = p.read_text().split("\n", 1)[0]
        return first.lstrip("# ").strip()
    return slug


def build_projection(entry_id_to_slug: dict[str, str] | None = None) -> dict:
    """Compute the 2D projection and save to PROJECTION_PATH.

    entry_id_to_slug: mapping from build_wiki's `refs`. If None, we attempt
    to scan source-page filenames as a best-effort fallback.
    """
    store = load_embeddings()
    if store is None or len(store) < 3:
        print("not enough embeddings to project", file=sys.stderr)
        return {"points": []}

    from sklearn.manifold import TSNE

    n = len(store)
    perplexity = max(5, min(30, n // 3))
    print(f"running t-SNE on {n} vectors (perplexity={perplexity})...", file=sys.stderr)
    tsne = TSNE(
        n_components=2,
        perplexity=perplexity,
        random_state=42,
        init="pca",
        learning_rate="auto",
    )
    coords = tsne.fit_transform(store.vectors)
    xs = _normalize(coords[:, 0])
    ys = _normalize(coords[:, 1])

    # Build entry metadata lookup
    entries_by_id = {}
    for s in list_sessions():
        for e in list_entries_in_session(s):
            entries_by_id[e.id] = e

    # If no slug map provided, derive from build_wiki's slug rule
    if entry_id_to_slug is None:
        from .build_wiki import _source_slug

        taken: set[str] = set()
        entry_id_to_slug = {}
        for e in entries_by_id.values():
            slug = _source_slug(e, taken)
            taken.add(slug)
            entry_id_to_slug[e.id] = slug

    points: list[dict] = []
    for i in range(n):
        eid = str(store.ids[i])
        kind = str(store.kinds[i])
        point = {
            "id": eid,
            "kind": kind,
            "x": float(xs[i]),
            "y": float(ys[i]),
        }
        if kind == "entry":
            e = entries_by_id.get(eid)
            if e is None:
                # entry was deleted since embedding; skip
                continue
            point.update({
                "title": e.title or "(untitled)",
                "type": e.type,
                "rating": e.rating,
                "date": e.source_post_date or "",
                "private": e.private,
                "slug": entry_id_to_slug.get(eid, ""),
            })
        elif kind == "concept":
            slug = eid.split(":", 1)[1] if ":" in eid else eid
            point.update({
                "title": _concept_title(slug),
                "slug": slug,
            })
        points.append(point)

    data = {
        "points": points,
        "n_entries": sum(1 for p in points if p["kind"] == "entry"),
        "n_concepts": sum(1 for p in points if p["kind"] == "concept"),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROJECTION_PATH.write_text(json.dumps(data))
    print(f"projection saved to {PROJECTION_PATH} ({len(points)} points)", file=sys.stderr)
    return data


def load_or_build_projection() -> dict:
    """Used by the API. Rebuilds if stale relative to embeddings."""
    if PROJECTION_PATH.exists() and EMBED_PATH.exists():
        if PROJECTION_PATH.stat().st_mtime >= EMBED_PATH.stat().st_mtime:
            return json.loads(PROJECTION_PATH.read_text())
    return build_projection()


if __name__ == "__main__":
    build_projection()
