"""Per-user SQLite storage for the web app.

Everything is keyed by the connected Gmail address so multiple users stay
isolated. Three tables:

- ``users``            -- one row per account: the OAuth token JSON used to call
                          Gmail on the user's behalf.
- ``user_settings``    -- the user's category list and chosen auto-reply
                          category.
- ``email_embeddings`` -- per-email metadata (sender, subject, a short snippet,
                          date) plus a semantic embedding vector, powering the
                          "search by meaning" feature.

Security note: the stored OAuth tokens grant access to the user's Gmail, and the
saved email metadata is personal data. Both are written to ``app.db`` in
plaintext, so that file is sensitive -- it is gitignored and must never be
committed or shared, should have restricted filesystem permissions, and for a
real deployment ought to be encrypted at rest (encrypted volume or
column-level encryption).
"""

import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "app.db"

# Fallback category configuration used when a user has not saved their own yet.
DEFAULT_CATEGORIES = [
    "Needs Action",
    "FAQ",
    "Red Flag",
    "Low Priority",
    "Spam/Newsletter",
]
DEFAULT_FAQ_CATEGORY = "FAQ"

# A category that is always present and cannot be removed by the user: it is the
# catch-all where emails that do not clearly fit any other category land, which
# also guarantees the classifier never has to invent a new label.
FIXED_CATEGORY = "Low Priority"


def ensure_fixed_category(categories):
    """Return ``categories`` guaranteed to contain FIXED_CATEGORY (appended if
    missing). Comparison is case-insensitive; the canonical name is used."""
    if not any(
        isinstance(c, str) and c.strip().lower() == FIXED_CATEGORY.lower()
        for c in categories
    ):
        return list(categories) + [FIXED_CATEGORY]
    return list(categories)


def _connect():
    return sqlite3.connect(DB_PATH)


def init_db():
    """Create the users and user_settings tables if they do not exist yet."""
    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY,
                token_json TEXT NOT NULL,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                email TEXT PRIMARY KEY,
                categories_json TEXT NOT NULL,
                faq_category TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS email_embeddings (
                user_email TEXT,
                gmail_id TEXT,
                thread_id TEXT,
                sender TEXT,
                subject TEXT,
                snippet TEXT,
                date TEXT,
                embedding TEXT,
                PRIMARY KEY (user_email, gmail_id)
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def save_user_token(email, token_json):
    """Insert or update the stored token for a user (upsert by email)."""
    updated_at = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO users (email, token_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                token_json = excluded.token_json,
                updated_at = excluded.updated_at
            """,
            (email, token_json, updated_at),
        )
        conn.commit()
    finally:
        conn.close()


def get_user_token(email):
    """Return the stored token JSON for a user, or ``None`` if not found."""
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT token_json FROM users WHERE email = ?", (email,)
        )
        row = cur.fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def save_user_settings(email, categories, faq_category):
    """Insert or update a user's category settings (upsert by email).

    ``categories`` is a list of strings, stored as JSON. ``faq_category`` is the
    category that triggers auto-reply drafting, or ``None`` for no drafting.
    """
    updated_at = datetime.now(timezone.utc).isoformat()
    # The fixed catch-all category is always kept, even if the caller omits it.
    categories_json = json.dumps(ensure_fixed_category(categories))
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO user_settings (email, categories_json, faq_category, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                categories_json = excluded.categories_json,
                faq_category = excluded.faq_category,
                updated_at = excluded.updated_at
            """,
            (email, categories_json, faq_category, updated_at),
        )
        conn.commit()
    finally:
        conn.close()


def get_user_settings(email):
    """Return a user's category settings, or sensible defaults if none saved.

    Shape: {"categories": [str, ...], "faq_category": str | None}.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT categories_json, faq_category FROM user_settings WHERE email = ?",
            (email,),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return {
            "categories": list(DEFAULT_CATEGORIES),
            "faq_category": DEFAULT_FAQ_CATEGORY,
        }

    try:
        categories = json.loads(row[0])
    except (TypeError, ValueError):
        categories = None

    if not isinstance(categories, list) or not categories:
        categories = list(DEFAULT_CATEGORIES)

    return {
        "categories": ensure_fixed_category(categories),
        "faq_category": row[1],
    }


def save_email_embedding(
    user_email, gmail_id, thread_id, sender, subject, snippet, date, embedding
):
    """Insert or update a stored email embedding (upsert by user + gmail_id).

    ``embedding`` is a list of floats, stored JSON-encoded.
    """
    embedding_json = json.dumps(embedding)
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO email_embeddings
                (user_email, gmail_id, thread_id, sender, subject, snippet, date, embedding)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_email, gmail_id) DO UPDATE SET
                thread_id = excluded.thread_id,
                sender = excluded.sender,
                subject = excluded.subject,
                snippet = excluded.snippet,
                date = excluded.date,
                embedding = excluded.embedding
            """,
            (
                user_email,
                gmail_id,
                thread_id,
                sender,
                subject,
                snippet,
                date,
                embedding_json,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_embeddings_for_user(user_email):
    """Return all stored email embeddings for a user.

    Each item is a dict with user_email, gmail_id, thread_id, sender, subject,
    snippet, date, and embedding (decoded from JSON into a list of floats).
    """
    conn = _connect()
    try:
        cur = conn.execute(
            """
            SELECT user_email, gmail_id, thread_id, sender, subject, snippet,
                   date, embedding
            FROM email_embeddings
            WHERE user_email = ?
            """,
            (user_email,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    results = []
    for row in rows:
        try:
            embedding = json.loads(row[7])
        except (TypeError, ValueError):
            embedding = []
        results.append(
            {
                "user_email": row[0],
                "gmail_id": row[1],
                "thread_id": row[2],
                "sender": row[3],
                "subject": row[4],
                "snippet": row[5],
                "date": row[6],
                "embedding": embedding,
            }
        )
    return results
