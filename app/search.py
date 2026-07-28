"""Semantic (meaning-based) email search using local embeddings.

Embeddings are produced by a small local fastembed model and stored per-user in
SQLite. At a few hundred emails per user, a brute-force cosine-similarity scan
is fast and needs no vector database.
"""

import logging

import numpy as np

from app.db import get_embeddings_for_user

logger = logging.getLogger(__name__)

# Small, fast, good-quality general-purpose embedding model.
_MODEL_NAME = "BAAI/bge-small-en-v1.5"

# Cap input length before embedding to keep things fast and bounded.
_MAX_CHARS = 2000

# Lazily-created singleton embedding model. Loading it downloads/initializes the
# model on first use, so we do it once and reuse it.
_model = None


def _get_model():
    """Return the cached fastembed model, loading it on first use."""
    global _model
    if _model is None:
        from fastembed import TextEmbedding

        _model = TextEmbedding(model_name=_MODEL_NAME)
    return _model


def embed_text(text):
    """Return the embedding vector for a string as a list of floats."""
    text = (text or "")[:_MAX_CHARS]
    model = _get_model()
    # fastembed's embed() yields one numpy array per input string.
    vector = next(iter(model.embed([text])))
    return [float(x) for x in vector]


def cosine_similarity(a, b):
    """Cosine similarity of two vectors; 0.0 if either has zero magnitude."""
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


def search_emails(user_email, query, top_k=10):
    """Return the top_k stored emails for a user ranked by similarity to query.

    Each result is a dict with gmail_id, thread_id, sender, subject, snippet,
    date, and score. Returns [] if the user has no stored embeddings yet, or if
    embedding the query fails.
    """
    rows = get_embeddings_for_user(user_email)
    if not rows:
        return []

    try:
        query_vec = embed_text(query)
    except Exception as exc:  # noqa: BLE001 - degrade gracefully on model errors
        logger.warning("Failed to embed search query: %s", exc)
        return []

    scored = []
    for row in rows:
        embedding = row.get("embedding")
        if not embedding:
            continue
        scored.append(
            {
                "gmail_id": row.get("gmail_id"),
                "thread_id": row.get("thread_id"),
                "sender": row.get("sender"),
                "subject": row.get("subject"),
                "snippet": row.get("snippet"),
                "date": row.get("date"),
                "score": cosine_similarity(query_vec, embedding),
            }
        )

    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[: max(0, top_k)]
