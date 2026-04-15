"""Shared embedding helpers for AI-DB tools."""

from __future__ import annotations

import math

DEFAULT_EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

_EMBED_CACHE: dict[str, object] = {}


def encode_texts(texts: list[str], model_name: str = DEFAULT_EMBED_MODEL) -> list[list[float]]:
    """Encode texts with a local sentence-transformer model."""
    if model_name not in _EMBED_CACHE:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise RuntimeError(
                "sentence-transformers is required for embedding workflows: "
                "pip install sentence-transformers"
            ) from e
        _EMBED_CACHE[model_name] = SentenceTransformer(model_name)

    model = _EMBED_CACHE[model_name]
    vecs = model.encode(texts, show_progress_bar=False)
    return [list(map(float, v)) for v in vecs]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def diverse_sample_indices(vectors: list[list[float]], k: int) -> list[int]:
    """Greedy farthest-point sampling over cosine distance."""
    if not vectors:
        return []
    k = min(k, len(vectors))
    selected = [0]
    remaining = set(range(1, len(vectors)))
    while len(selected) < k and remaining:
        best_idx = None
        best_score = -1.0
        for idx in remaining:
            sims = [cosine_similarity(vectors[idx], vectors[s]) for s in selected]
            score = 1.0 - max(sims)
            if score > best_score:
                best_score = score
                best_idx = idx
        selected.append(best_idx)
        remaining.remove(best_idx)
    return selected
