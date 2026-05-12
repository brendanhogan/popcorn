"""Local embeddings for entries and concept pages.

Uses sentence-transformers (all-MiniLM-L6-v2 by default, 384 dims, CPU-fine).
Stores at data/embeddings.npz with parallel arrays:
    ids:     ['<entry_id>', ..., 'concept:<slug>', ...]
    vectors: float32 (N, dim), L2-normalized
    kinds:   ['entry', ..., 'concept', ...]

Usage:
    python -m app.embed
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .models import Entry
from .storage import DATA_DIR, list_entries_in_session, list_sessions
from .wiki import CONCEPTS_DIR, WIKI_LINK_RE

EMBED_PATH = DATA_DIR / "embeddings.npz"
DEFAULT_MODEL = os.environ.get("EMBED_MODEL", "all-MiniLM-L6-v2")


@dataclass
class EmbeddingStore:
    ids: np.ndarray       # (N,) str
    vectors: np.ndarray   # (N, dim) float32, L2-normalized
    kinds: np.ndarray     # (N,) str — 'entry' or 'concept'

    def __len__(self) -> int:
        return len(self.ids)

    def index_by_id(self, target_id: str) -> int | None:
        matches = np.where(self.ids == target_id)[0]
        return int(matches[0]) if len(matches) else None

    def of_kind(self, kind: str) -> "EmbeddingStore":
        mask = self.kinds == kind
        return EmbeddingStore(
            ids=self.ids[mask],
            vectors=self.vectors[mask],
            kinds=self.kinds[mask],
        )


def entry_text(e: Entry) -> str:
    parts: list[str] = []
    if e.title:
        parts.append(f"Title: {e.title}")
    if e.notes:
        parts.append(f"Notes: {e.notes}")
    if e.summary:
        s = e.summary if len(e.summary) <= 1500 else e.summary[:1500]
        parts.append(f"Summary: {s}")
    return "\n".join(parts) or e.url or "(empty)"


def concept_text(path: Path) -> str:
    raw = path.read_text()
    # strip [[wiki-link]] syntax (keep the label, drop the link wrapping)
    return WIKI_LINK_RE.sub(
        lambda m: (m.group(2) or m.group(1).rsplit("/", 1)[-1]),
        raw,
    )


def _collect_entries() -> list[Entry]:
    seen: dict[str, Entry] = {}
    for s in list_sessions():
        for e in list_entries_in_session(s):
            seen[e.id] = e
    return list(seen.values())


def build_embeddings(model_name: str = DEFAULT_MODEL) -> EmbeddingStore:
    """Embed all current entries and concept pages. Returns the store."""
    # Import here to keep the module importable without torch loaded
    from sentence_transformers import SentenceTransformer

    entries = _collect_entries()
    print(f"Embedding {len(entries)} entries with {model_name}...", file=sys.stderr)
    model = SentenceTransformer(model_name)
    entry_texts = [entry_text(e) for e in entries]
    entry_vecs = (
        model.encode(
            entry_texts,
            batch_size=32,
            show_progress_bar=True,
            normalize_embeddings=True,
        )
        if entries
        else np.zeros((0, model.get_sentence_embedding_dimension()), dtype=np.float32)
    )

    concept_paths = sorted(CONCEPTS_DIR.glob("*.md")) if CONCEPTS_DIR.exists() else []
    print(f"Embedding {len(concept_paths)} concept pages...", file=sys.stderr)
    concept_texts = [concept_text(p) for p in concept_paths]
    concept_vecs = (
        model.encode(
            concept_texts,
            batch_size=8,
            show_progress_bar=True,
            normalize_embeddings=True,
        )
        if concept_paths
        else np.zeros((0, model.get_sentence_embedding_dimension()), dtype=np.float32)
    )

    ids = np.array([e.id for e in entries] + [f"concept:{p.stem}" for p in concept_paths])
    vectors = np.concatenate([entry_vecs, concept_vecs], axis=0).astype(np.float32)
    kinds = np.array(["entry"] * len(entries) + ["concept"] * len(concept_paths))

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    np.savez(EMBED_PATH, ids=ids, vectors=vectors, kinds=kinds)
    print(f"Saved {len(ids)} embeddings ({vectors.shape}) to {EMBED_PATH}", file=sys.stderr)

    return EmbeddingStore(ids=ids, vectors=vectors, kinds=kinds)


def load_embeddings() -> EmbeddingStore | None:
    if not EMBED_PATH.exists():
        return None
    data = np.load(EMBED_PATH, allow_pickle=False)
    return EmbeddingStore(
        ids=data["ids"],
        vectors=data["vectors"].astype(np.float32),
        kinds=data["kinds"],
    )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--model", default=DEFAULT_MODEL, help="Sentence-transformers model name")
    args = p.parse_args()
    build_embeddings(model_name=args.model)


if __name__ == "__main__":
    main()
