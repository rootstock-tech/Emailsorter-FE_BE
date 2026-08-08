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

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(
    os.getenv("APP_DB_PATH", str(Path(__file__).resolve().parent.parent / "app.db"))
).expanduser()

# Fallback category configuration used when a user has not saved their own yet.
DEFAULT_CATEGORIES = [
    "Needs Action",
    "FAQ",
    "Red Flag",
    "Others",
    "Spam/Newsletter",
]
DEFAULT_FAQ_CATEGORY = "FAQ"

# Consistent LLM decisions for the same sender/domain+category before it is
# promoted to an auto-applied learned rule (so a one-off does not create a rule).
LEARNED_RULE_THRESHOLD = 3

PUBLIC_MAIL_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "yahoo.com",
    "yahoo.co.in",
    "icloud.com",
    "proton.me",
    "protonmail.com",
    "aol.com",
}

# A category that is always present and cannot be removed by the user: it is the
# catch-all where emails that do not clearly fit any other category land, which
# also guarantees the classifier never has to invent a new label.
FIXED_CATEGORY = "Others"


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
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB_PATH, timeout=30)


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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS learned_rules (
                user_email TEXT,
                match_type TEXT,
                match_value TEXT,
                category TEXT,
                hits INTEGER NOT NULL DEFAULT 0,
                last_seen TEXT,
                active INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_email, match_type, match_value, category)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gmail_watch (
                email TEXT PRIMARY KEY,
                history_id TEXT,
                expiration TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS auto_triage (
                email TEXT PRIMARY KEY,
                interval_minutes INTEGER NOT NULL,
                updated_at TEXT
            )
            """
        )
        # Per-email priority score + reason from the latest run, powering the
        # "Priority inbox" view (most important mail first, with a why).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS email_priority (
                user_email TEXT,
                gmail_id TEXT,
                thread_id TEXT,
                sender TEXT,
                subject TEXT,
                category TEXT,
                score INTEGER NOT NULL DEFAULT 0,
                reason TEXT,
                date TEXT,
                created_at TEXT,
                PRIMARY KEY (user_email, gmail_id)
            )
            """
        )
        # A row per triage run so its label changes can be undone as a unit.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS triage_runs (
                run_id TEXT PRIMARY KEY,
                user_email TEXT,
                created_at TEXT,
                undone INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # Every label applied during a run, so Undo can remove exactly those.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS triage_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                user_email TEXT,
                gmail_id TEXT,
                label_id TEXT,
                label_name TEXT,
                created_at TEXT
            )
            """
        )
        # A deadline extracted from a mail, so the user can be reminded before it.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deadlines (
                user_email TEXT,
                gmail_id TEXT,
                thread_id TEXT,
                subject TEXT,
                sender TEXT,
                due_date TEXT,
                description TEXT,
                created_at TEXT,
                PRIMARY KEY (user_email, gmail_id)
            )
            """
        )
        # People the user actually converses with. Remembered so their mail is
        # never treated as spam and we do not have to re-query Gmail each run.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS known_contacts (
                user_email TEXT,
                address TEXT,
                reason TEXT,
                updated_at TEXT,
                PRIMARY KEY (user_email, address)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reminder_state (
                user_email TEXT,
                gmail_id TEXT,
                reminder_kind TEXT,
                reminded_at TEXT,
                reminder_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_email, gmail_id, reminder_kind)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                user_email TEXT,
                thread_id TEXT,
                participant TEXT,
                status TEXT NOT NULL DEFAULT 'observed',
                last_category TEXT,
                last_interaction TEXT,
                PRIMARY KEY (user_email, thread_id)
            )
            """
        )
        # Catch-all decisions must remain eligible for later reclassification.
        conn.execute(
            "UPDATE learned_rules SET active = 0 WHERE lower(category) = lower(?)",
            (FIXED_CATEGORY,),
        )
        learned_cols = [r[1] for r in conn.execute("PRAGMA table_info(learned_rules)")]
        for column, definition in (
            ("keyword_signature", "TEXT"),
            ("forced_category", "TEXT"),
            ("old_category", "TEXT"),
            ("weight", "REAL NOT NULL DEFAULT 1"),
            ("updated_at", "TEXT"),
        ):
            if column not in learned_cols:
                conn.execute(f"ALTER TABLE learned_rules ADD COLUMN {column} {definition}")
        # Older databases predate per-category prompts; add the column on upgrade.
        existing_cols = [r[1] for r in conn.execute("PRAGMA table_info(user_settings)")]
        if "category_prompts_json" not in existing_cols:
            conn.execute(
                "ALTER TABLE user_settings ADD COLUMN category_prompts_json TEXT"
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


def list_user_emails():
    """Return every connected account's email (used to renew Gmail watches)."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT email FROM users").fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def save_watch_state(email, history_id, expiration):
    """Upsert a user's Gmail push watch state (history id + expiration ms)."""
    updated_at = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO gmail_watch (email, history_id, expiration, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                history_id = excluded.history_id,
                expiration = excluded.expiration,
                updated_at = excluded.updated_at
            """,
            (email, str(history_id) if history_id is not None else None,
             str(expiration) if expiration is not None else None, updated_at),
        )
        conn.commit()
    finally:
        conn.close()


def get_watch_state(email):
    """Return {'history_id', 'expiration'} for a user, or None if not watched."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT history_id, expiration FROM gmail_watch WHERE email = ?",
            (email,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {"history_id": row[0], "expiration": row[1]}


def set_auto_triage(email, interval_minutes):
    """Enable recurring auto-triage every ``interval_minutes`` for a user.

    Passing ``None`` or a value <= 0 turns it off (removes the row).
    """
    conn = _connect()
    try:
        if not interval_minutes or interval_minutes <= 0:
            conn.execute("DELETE FROM auto_triage WHERE email = ?", (email,))
        else:
            updated_at = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                INSERT INTO auto_triage (email, interval_minutes, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    interval_minutes = excluded.interval_minutes,
                    updated_at = excluded.updated_at
                """,
                (email, int(interval_minutes), updated_at),
            )
        conn.commit()
    finally:
        conn.close()


def get_auto_triage(email):
    """Return a user's recurring interval in minutes, or None if disabled."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT interval_minutes FROM auto_triage WHERE email = ?", (email,)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def list_auto_triage():
    """Return [(email, interval_minutes)] for every user with auto-triage on."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT email, interval_minutes FROM auto_triage"
        ).fetchall()
    finally:
        conn.close()
    return [(r[0], r[1]) for r in rows]


def save_user_settings(email, categories, faq_category, category_prompts=None):
    """Insert or update a user's category settings (upsert by email).

    ``categories`` is a list of strings, stored as JSON. ``faq_category`` is the
    category that triggers auto-reply drafting, or ``None`` for no drafting.
    ``category_prompts`` is an optional {name: description} mapping so a user can
    describe what belongs in each (often client-specific) category; the
    classifier uses these descriptions instead of guessing from the name alone.
    """
    updated_at = datetime.now(timezone.utc).isoformat()
    # The fixed catch-all category is always kept, even if the caller omits it.
    categories_json = json.dumps(ensure_fixed_category(categories))
    category_prompts_json = json.dumps(category_prompts or {})
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO user_settings (email, categories_json, faq_category, category_prompts_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                categories_json = excluded.categories_json,
                faq_category = excluded.faq_category,
                category_prompts_json = excluded.category_prompts_json,
                updated_at = excluded.updated_at
            """,
            (email, categories_json, faq_category, category_prompts_json, updated_at),
        )
        conn.commit()
    finally:
        conn.close()


def get_user_settings(email):
    """Return a user's category settings, or sensible defaults if none saved.

    Shape: {"categories": [str, ...], "faq_category": str | None,
    "category_prompts": {name: description}}.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT categories_json, faq_category, category_prompts_json FROM user_settings WHERE email = ?",
            (email,),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return {
            "categories": list(DEFAULT_CATEGORIES),
            "faq_category": DEFAULT_FAQ_CATEGORY,
            "category_prompts": {},
        }

    try:
        categories = json.loads(row[0])
    except (TypeError, ValueError):
        categories = None

    if not isinstance(categories, list) or not categories:
        categories = list(DEFAULT_CATEGORIES)

    try:
        category_prompts = json.loads(row[2]) if row[2] else {}
    except (TypeError, ValueError):
        category_prompts = {}
    if not isinstance(category_prompts, dict):
        category_prompts = {}

    return {
        "categories": ensure_fixed_category(categories),
        "faq_category": row[1],
        "category_prompts": category_prompts,
    }


# --- Self-learning rules ---------------------------------------------------
#
# The classifier records every LLM decision as an observation keyed by the
# email's sender address and its domain. Once the same sender (or domain) has
# been classified into the same category LEARNED_RULE_THRESHOLD times, that
# mapping becomes an "active" rule and future matching mail is sorted
# deterministically -- no LLM call. Rules stay transparent: they can be listed,
# toggled, or deleted by the user.


def _sender_address(raw_sender):
    """Extract a bare lowercased email address from a raw From header.

    Handles 'Name <a@b.com>' and 'a@b.com'; returns '' if none found.
    """
    if not raw_sender:
        return ""
    text = str(raw_sender)
    start = text.rfind("<")
    end = text.rfind(">")
    if start != -1 and end != -1 and end > start:
        text = text[start + 1 : end]
    text = text.strip().lower()
    return text if "@" in text else ""


def _sender_domain(address):
    """Return the domain part of an email address, or '' if not parseable."""
    if "@" not in address:
        return ""
    return address.split("@", 1)[1].strip()


def _learnable_domain(address):
    domain = _sender_domain(address)
    return "" if domain in PUBLIC_MAIL_DOMAINS else domain


_SIGNATURE_STOP_WORDS = {
    "about", "after", "before", "from", "have", "into", "mail", "please",
    "re", "regarding", "that", "the", "this", "with", "your",
}


def _keyword_signature(subject):
    words = re.findall(r"[a-z0-9]{3,}", (subject or "").lower())
    useful = sorted({word for word in words if word not in _SIGNATURE_STOP_WORDS})
    return " ".join(useful[:8])


def record_llm_decision(user_email, raw_sender, category, threshold=LEARNED_RULE_THRESHOLD):
    """Record one LLM classification as a learning observation.

    Increments the hit count for (sender, category) and (domain, category), then
    promotes the dominant category to the active rule for that sender/domain once
    it reaches ``threshold`` hits. Each sender/domain has at most one active rule.
    """
    address = _sender_address(raw_sender)
    if (
        not address
        or not category
        or str(category).strip().lower() == FIXED_CATEGORY.lower()
    ):
        return
    domain = _learnable_domain(address)
    updated_at = datetime.now(timezone.utc).isoformat()

    targets = [("sender", address)]
    if domain:
        targets.append(("domain", domain))

    conn = _connect()
    try:
        for match_type, match_value in targets:
            conn.execute(
                """
                INSERT INTO learned_rules (user_email, match_type, match_value, category, hits, last_seen, active)
                VALUES (?, ?, ?, ?, 1, ?, 0)
                ON CONFLICT(user_email, match_type, match_value, category) DO UPDATE SET
                    hits = hits + 1,
                    last_seen = excluded.last_seen
                """,
                (user_email, match_type, match_value, category, updated_at),
            )
            # Re-derive the active rule: the highest-hit category, if it meets the
            # threshold. Deactivate the rest so only one rule applies per value.
            rows = conn.execute(
                "SELECT category, hits FROM learned_rules WHERE user_email = ? AND match_type = ? AND match_value = ?",
                (user_email, match_type, match_value),
            ).fetchall()
            top_category, top_hits = None, 0
            for cat, hits in rows:
                if hits > top_hits:
                    top_hits, top_category = hits, cat
            for cat, _ in rows:
                active = 1 if (cat == top_category and top_hits >= threshold) else 0
                conn.execute(
                    "UPDATE learned_rules SET active = ? WHERE user_email = ? AND match_type = ? AND match_value = ? AND category = ?",
                    (active, user_email, match_type, match_value, cat),
                )
        conn.commit()
    finally:
        conn.close()


def record_user_correction(user_email, raw_sender, category, subject="", old_category=None):
    """Record a manual relabel as an immediate, authoritative learned rule.

    Unlike record_llm_decision (which needs repeated agreement to promote a
    rule), a human correction is trusted at once: the sender-level rule for
    ``category`` is activated right away and every other category for that
    sender is deactivated, so the next mail from them is sorted the user's way.
    Domain-level observations are still counted but not force-activated, since
    one sender's correction should not override a whole domain.
    """
    address = _sender_address(raw_sender)
    if not address or not category:
        return
    domain = _learnable_domain(address)
    updated_at = datetime.now(timezone.utc).isoformat()
    signature = _keyword_signature(subject)

    conn = _connect()
    try:
        # Bump the corrected (sender, category) well past the promotion
        # threshold so it stays dominant even against past LLM observations.
        conn.execute(
            """
            INSERT INTO learned_rules (user_email, match_type, match_value, category, hits, last_seen, active)
            VALUES (?, 'sender', ?, ?, ?, ?, 1)
            ON CONFLICT(user_email, match_type, match_value, category) DO UPDATE SET
                hits = hits + ?,
                last_seen = excluded.last_seen,
                active = 1,
                keyword_signature = excluded.keyword_signature,
                forced_category = excluded.forced_category,
                old_category = excluded.old_category,
                weight = learned_rules.weight + 3,
                updated_at = excluded.updated_at
            """,
            (user_email, address, category, LEARNED_RULE_THRESHOLD, updated_at, LEARNED_RULE_THRESHOLD),
        )
        conn.execute(
            """
            UPDATE learned_rules
            SET keyword_signature = ?, forced_category = ?, old_category = ?,
                weight = MAX(weight, 3), updated_at = ?
            WHERE user_email = ? AND match_type = 'sender' AND match_value = ? AND category = ?
            """,
            (signature, category, old_category, updated_at, user_email, address, category),
        )
        # Only one active rule per sender: turn off any other category.
        conn.execute(
            "UPDATE learned_rules SET active = 0 WHERE user_email = ? AND match_type = 'sender' AND match_value = ? AND category != ?",
            (user_email, address, category),
        )
        # Count the domain observation too, but leave promotion to the usual path.
        if domain:
            conn.execute(
                """
                INSERT INTO learned_rules (user_email, match_type, match_value, category, hits, last_seen, active)
                VALUES (?, 'domain', ?, ?, 1, ?, 1)
                ON CONFLICT(user_email, match_type, match_value, category) DO UPDATE SET
                    hits = hits + 1,
                    last_seen = excluded.last_seen,
                    keyword_signature = excluded.keyword_signature,
                    forced_category = excluded.forced_category,
                    old_category = excluded.old_category,
                    weight = learned_rules.weight + 1,
                    updated_at = excluded.updated_at
                """,
                (user_email, domain, category, updated_at),
            )
            conn.execute(
                """
                UPDATE learned_rules
                SET keyword_signature = ?, forced_category = ?, old_category = ?,
                    weight = MAX(weight, 1), updated_at = ?, active = 1
                WHERE user_email = ? AND match_type = 'domain' AND match_value = ? AND category = ?
                """,
                (signature, category, old_category, updated_at, user_email, domain, category),
            )
        conn.commit()
    finally:
        conn.close()


def get_active_rules(user_email):
    """Return active learned rules for fast lookup during classification.

    Shape: {"sender": {address: category}, "domain": {domain: category}}.
    """
    result = {
        "sender": {},
        "domain": {},
        "weighted": [],
        "context": [],
        "disabled_senders": set(),
    }
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT match_type, match_value, category FROM learned_rules
            WHERE user_email = ? AND active = 1
              AND (forced_category IS NULL OR match_type = 'sender')
            """,
            (user_email,),
        ).fetchall()
        weighted = conn.execute(
            """
            SELECT match_type, match_value, category, keyword_signature, weight, updated_at
            FROM learned_rules
            WHERE user_email = ? AND forced_category IS NOT NULL AND active = 1
            ORDER BY weight DESC, updated_at DESC
            LIMIT 50
            """,
            (user_email,),
        ).fetchall()
        disabled_senders = conn.execute(
            """
            SELECT DISTINCT match_value FROM learned_rules
            WHERE user_email = ? AND match_type = 'sender'
              AND forced_category IS NOT NULL AND active = 0
            """,
            (user_email,),
        ).fetchall()
    finally:
        conn.close()
    for match_type, match_value, category in rows:
        if match_type in result:
            result[match_type][match_value] = category
    result["disabled_senders"] = {row[0] for row in disabled_senders}
    for match_type, match_value, category, signature, weight, updated_at in weighted:
        result["weighted"].append(
            {
                "match_type": match_type,
                "match_value": match_value,
                "category": category,
                "keyword_signature": signature or "",
                "weight": float(weight or 1),
                "updated_at": updated_at,
            }
        )
        if len(result["context"]) < 12:
            result["context"].append(
                f"The user corrected {match_type} {match_value} to {category}"
                + (f" for subjects about {signature}" if signature else "")
            )
    return result


def match_weighted_rule(active_rules, email, minimum_score=40):
    """Return a correction category when sender/domain/subject evidence is strong."""
    address = _sender_address(email.get("sender"))
    if not address:
        return None
    if address in active_rules.get("disabled_senders", set()):
        return None
    domain = _learnable_domain(address)
    subject_words = set(_keyword_signature(email.get("subject")).split())
    now = datetime.now(timezone.utc)
    scores = {}
    for rule in active_rules.get("weighted", []):
        value = rule.get("match_value")
        match_type = rule.get("match_type")
        if match_type == "sender" and value != address:
            continue
        if match_type == "domain" and value != domain:
            continue
        weight = max(1.0, float(rule.get("weight") or 1))
        score = (100 if match_type == "sender" else 20) * weight
        signature = set((rule.get("keyword_signature") or "").split())
        if signature and subject_words:
            score += 30 * weight * len(signature & subject_words) / len(signature)
        try:
            age_days = max(0, (now - datetime.fromisoformat(rule["updated_at"])).days)
            score += max(0, 10 - age_days / 30)
        except (KeyError, TypeError, ValueError):
            pass
        category = rule.get("category")
        scores[category] = scores.get(category, 0) + score
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    if not ranked or ranked[0][1] < minimum_score:
        return None
    if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < 15:
        return None
    return ranked[0][0]


def match_learned_rule(active_rules, raw_sender):
    """Return the category for an email's sender via active rules, else None.

    Sender-level rules take priority over domain-level (more specific first).
    """
    address = _sender_address(raw_sender)
    if not address:
        return None
    sender_cat = active_rules.get("sender", {}).get(address)
    if sender_cat:
        return sender_cat
    return active_rules.get("domain", {}).get(_learnable_domain(address))


def list_learned_rules(user_email):
    """Return all learned rules for a user (for display/management)."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT match_type, match_value, category, hits, active, last_seen "
            "FROM learned_rules WHERE user_email = ? ORDER BY active DESC, hits DESC",
            (user_email,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "match_type": r[0],
            "match_value": r[1],
            "category": r[2],
            "hits": r[3],
            "active": bool(r[4]),
            "last_seen": r[5],
        }
        for r in rows
    ]


def reminder_due(user_email, gmail_id, reminder_kind, min_hours=24, max_count=3, now=None):
    """Return True when a reminder has not been sent too recently/often."""
    now = now or datetime.now(timezone.utc)
    conn = _connect()
    try:
        row = conn.execute(
            """
            SELECT reminded_at, reminder_count FROM reminder_state
            WHERE user_email = ? AND gmail_id = ? AND reminder_kind = ?
            """,
            (user_email, gmail_id, reminder_kind),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return True
    reminded_at, count = row
    if int(count or 0) >= max_count:
        return False
    try:
        previous = datetime.fromisoformat(reminded_at)
    except (TypeError, ValueError):
        return True
    return (now - previous).total_seconds() >= min_hours * 3600


def mark_reminded(user_email, gmail_id, reminder_kind, now=None):
    """Record a successfully sent reminder and increment its count."""
    reminded_at = (now or datetime.now(timezone.utc)).isoformat()
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO reminder_state
                (user_email, gmail_id, reminder_kind, reminded_at, reminder_count)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(user_email, gmail_id, reminder_kind) DO UPDATE SET
                reminded_at = excluded.reminded_at,
                reminder_count = reminder_state.reminder_count + 1
            """,
            (user_email, gmail_id, reminder_kind, reminded_at),
        )
        conn.commit()
    finally:
        conn.close()


def get_reminder_state(user_email, gmail_id, reminder_kind):
    """Return reminder metadata for tests/status, or None when never sent."""
    conn = _connect()
    try:
        row = conn.execute(
            """
            SELECT reminded_at, reminder_count FROM reminder_state
            WHERE user_email = ? AND gmail_id = ? AND reminder_kind = ?
            """,
            (user_email, gmail_id, reminder_kind),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {"reminded_at": row[0], "reminder_count": int(row[1])}


def remember_conversation(
    user_email,
    thread_id,
    participant,
    status="observed",
    category=None,
    interacted_at=None,
):
    """Upsert thread context while preserving an already-active status."""
    if not user_email or not thread_id:
        return
    address = _sender_address(participant) or (participant or "").strip().lower()
    timestamp = interacted_at or datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO conversations
                (user_email, thread_id, participant, status, last_category, last_interaction)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_email, thread_id) DO UPDATE SET
                participant = excluded.participant,
                status = CASE
                    WHEN conversations.status = 'active' THEN 'active'
                    ELSE excluded.status
                END,
                last_category = COALESCE(excluded.last_category, conversations.last_category),
                last_interaction = excluded.last_interaction
            """,
            (user_email, thread_id, address, status, category, timestamp),
        )
        conn.commit()
    finally:
        conn.close()


def get_conversation(user_email, thread_id):
    """Return persisted context for one Gmail thread, or None."""
    if not user_email or not thread_id:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            """
            SELECT participant, status, last_category, last_interaction
            FROM conversations WHERE user_email = ? AND thread_id = ?
            """,
            (user_email, thread_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "participant": row[0],
        "status": row[1],
        "last_category": row[2],
        "last_interaction": row[3],
    }


def set_rule_active(user_email, match_type, match_value, category, active):
    """Manually enable/disable a learned rule (user override)."""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE learned_rules SET active = ? WHERE user_email = ? AND match_type = ? AND match_value = ? AND category = ?",
            (1 if active else 0, user_email, match_type, match_value, category),
        )
        conn.commit()
    finally:
        conn.close()


def delete_learned_rule(user_email, match_type, match_value, category):
    """Delete a learned rule and its observation count for a user."""
    conn = _connect()
    try:
        conn.execute(
            "DELETE FROM learned_rules WHERE user_email = ? AND match_type = ? AND match_value = ? AND category = ?",
            (user_email, match_type, match_value, category),
        )
        conn.commit()
    finally:
        conn.close()


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


def has_embeddings(user_email):
    """Return True if the user has at least one stored (searchable) email."""
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT 1 FROM email_embeddings WHERE user_email = ? LIMIT 1",
            (user_email,),
        )
        return cur.fetchone() is not None
    finally:
        conn.close()


# --- Priority inbox ---------------------------------------------------------


def save_priority(user_email, gmail_id, thread_id, sender, subject, category, score, reason, date):
    """Upsert a per-email priority score + reason (from the latest run)."""
    created_at = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO email_priority
                (user_email, gmail_id, thread_id, sender, subject, category, score, reason, date, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_email, gmail_id) DO UPDATE SET
                thread_id = excluded.thread_id,
                sender = excluded.sender,
                subject = excluded.subject,
                category = excluded.category,
                score = excluded.score,
                reason = excluded.reason,
                date = excluded.date,
                created_at = excluded.created_at
            """,
            (user_email, gmail_id, thread_id, sender, subject, category, int(score), reason, date, created_at),
        )
        conn.commit()
    finally:
        conn.close()


def top_priority(user_email, limit=15):
    """Return the user's highest-scoring emails, most important first."""
    conn = _connect()
    try:
        cur = conn.execute(
            """
            SELECT gmail_id, thread_id, sender, subject, category, score, reason, date
            FROM email_priority
            WHERE user_email = ?
            ORDER BY score DESC, created_at DESC
            LIMIT ?
            """,
            (user_email, int(limit)),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    return [
        {
            "gmail_id": r[0],
            "thread_id": r[1],
            "sender": r[2],
            "subject": r[3],
            "category": r[4],
            "score": r[5],
            "reason": r[6],
            "date": r[7],
        }
        for r in rows
    ]


def clear_priority(user_email):
    """Remove all stored priority rows for a user (called before a fresh run)."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM email_priority WHERE user_email = ?", (user_email,))
        conn.commit()
    finally:
        conn.close()


def delete_priority(user_email, gmail_ids):
    """Remove specific priority rows (e.g. after their labels are undone)."""
    if not gmail_ids:
        return
    conn = _connect()
    try:
        conn.executemany(
            "DELETE FROM email_priority WHERE user_email = ? AND gmail_id = ?",
            [(user_email, gid) for gid in gmail_ids],
        )
        conn.commit()
    finally:
        conn.close()


# --- Deadlines --------------------------------------------------------------


def save_deadline(user_email, gmail_id, thread_id, subject, sender, due_date, description):
    """Upsert a deadline extracted from a mail (one per gmail_id)."""
    created_at = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO deadlines
                (user_email, gmail_id, thread_id, subject, sender, due_date, description, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_email, gmail_id) DO UPDATE SET
                thread_id = excluded.thread_id,
                subject = excluded.subject,
                sender = excluded.sender,
                due_date = excluded.due_date,
                description = excluded.description,
                created_at = excluded.created_at
            """,
            (user_email, gmail_id, thread_id, subject, sender, due_date, description, created_at),
        )
        conn.commit()
    finally:
        conn.close()


def delete_deadline(user_email, gmail_id):
    """Remove a deadline that is no longer appropriate for a message."""
    conn = _connect()
    try:
        conn.execute(
            "DELETE FROM deadlines WHERE user_email = ? AND gmail_id = ?",
            (user_email, gmail_id),
        )
        conn.commit()
    finally:
        conn.close()


def upcoming_deadlines(user_email, on_or_before=None, limit=20):
    """Return the user's deadlines due today or later, soonest first.

    ``on_or_before`` (an ISO "YYYY-MM-DD" string) caps how far ahead to look;
    when omitted, all future deadlines are returned.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    conn = _connect()
    try:
        if on_or_before:
            rows = conn.execute(
                """
                                SELECT d.gmail_id, d.thread_id, d.subject, d.sender, d.due_date, d.description
                                FROM deadlines d
                                LEFT JOIN email_priority p
                                    ON p.user_email = d.user_email AND p.gmail_id = d.gmail_id
                                WHERE d.user_email = ? AND d.due_date >= ? AND d.due_date <= ?
                                    AND lower(COALESCE(p.category, '')) NOT LIKE '%spam%'
                                    AND lower(COALESCE(p.category, '')) NOT LIKE '%newsletter%'
                                    AND lower(COALESCE(p.category, '')) NOT LIKE '%promo%'
                                ORDER BY d.due_date ASC LIMIT ?
                """,
                (user_email, today, on_or_before, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                                SELECT d.gmail_id, d.thread_id, d.subject, d.sender, d.due_date, d.description
                                FROM deadlines d
                                LEFT JOIN email_priority p
                                    ON p.user_email = d.user_email AND p.gmail_id = d.gmail_id
                                WHERE d.user_email = ? AND d.due_date >= ?
                                    AND lower(COALESCE(p.category, '')) NOT LIKE '%spam%'
                                    AND lower(COALESCE(p.category, '')) NOT LIKE '%newsletter%'
                                    AND lower(COALESCE(p.category, '')) NOT LIKE '%promo%'
                                ORDER BY d.due_date ASC LIMIT ?
                """,
                (user_email, today, limit),
            ).fetchall()
    finally:
        conn.close()
    return [
        {
            "gmail_id": r[0],
            "thread_id": r[1],
            "subject": r[2],
            "sender": r[3],
            "due_date": r[4],
            "description": r[5],
        }
        for r in rows
    ]


# --- Contact memory (people the user converses with) -----------------------


def remember_contact(user_email, raw_sender, reason="conversation"):
    """Remember a sender as someone the user talks to (never spam).

    Keyed by the bare email address so it is stable across display-name changes.
    Best-effort: a blank/unparseable sender is ignored.
    """
    address = _sender_address(raw_sender)
    if not address:
        return
    updated_at = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO known_contacts (user_email, address, reason, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_email, address) DO UPDATE SET
                reason = excluded.reason,
                updated_at = excluded.updated_at
            """,
            (user_email, address, reason, updated_at),
        )
        conn.commit()
    finally:
        conn.close()


def known_contacts(user_email):
    """Return the set of remembered contact addresses for fast lookup."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT address FROM known_contacts WHERE user_email = ?",
            (user_email,),
        ).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def is_known_contact(user_email, raw_sender):
    """True if the sender's address is a remembered contact."""
    address = _sender_address(raw_sender)
    if not address:
        return False
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM known_contacts WHERE user_email = ? AND address = ? LIMIT 1",
            (user_email, address),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def list_known_contacts(user_email):
    """Return remembered contacts (address + why + when) for display."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT address, reason, updated_at FROM known_contacts WHERE user_email = ? ORDER BY updated_at DESC",
            (user_email,),
        ).fetchall()
    finally:
        conn.close()
    return [{"address": r[0], "reason": r[1], "updated_at": r[2]} for r in rows]


# --- Triage runs + undo -----------------------------------------------------


def start_triage_run(run_id, user_email):
    """Record the start of a triage run so its actions can be undone later."""
    created_at = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO triage_runs (run_id, user_email, created_at, undone) VALUES (?, ?, ?, 0)",
            (run_id, user_email, created_at),
        )
        conn.commit()
    finally:
        conn.close()


def record_triage_action(run_id, user_email, gmail_id, label_id, label_name):
    """Record one label applied during a run (for later Undo)."""
    created_at = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO triage_actions
                (run_id, user_email, gmail_id, label_id, label_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, user_email, gmail_id, label_id, label_name, created_at),
        )
        conn.commit()
    finally:
        conn.close()


def last_undoable_run(user_email):
    """Return the most recent not-yet-undone run for a user, or None.

    Returns a dict {run_id, created_at, action_count} or None if nothing to undo.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            """
            SELECT r.run_id, r.created_at,
                   (SELECT COUNT(*) FROM triage_actions a WHERE a.run_id = r.run_id) AS n
            FROM triage_runs r
            WHERE r.user_email = ? AND r.undone = 0
            ORDER BY r.created_at DESC
            LIMIT 1
            """,
            (user_email,),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    if not row or not row[2]:
        return None
    return {"run_id": row[0], "created_at": row[1], "action_count": row[2]}


def get_run_actions(run_id):
    """Return all recorded (gmail_id, label_id, label_name) for a run."""
    conn = _connect()
    try:
        cur = conn.execute(
            "SELECT gmail_id, label_id, label_name FROM triage_actions WHERE run_id = ?",
            (run_id,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    return [{"gmail_id": r[0], "label_id": r[1], "label_name": r[2]} for r in rows]


def mark_run_undone(run_id):
    """Mark a run as undone so it is not offered for undo again."""
    conn = _connect()
    try:
        conn.execute("UPDATE triage_runs SET undone = 1 WHERE run_id = ?", (run_id,))
        conn.commit()
    finally:
        conn.close()
