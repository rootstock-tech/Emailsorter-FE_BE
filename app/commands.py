"""Natural-language inbox commands.

Turns a plain-English instruction ("archive all newsletters older than 30 days")
into a structured, reversible action the user previews before it runs. The LLM
only produces a Gmail search query plus one safe action (label / archive / mark
as read); it never deletes. Execution happens on the server against the user's
Gmail, with a preview step first so nothing is a surprise.
"""

import json
import logging

from app.classifier import GROQ_MODEL, _get_client
from app.labeler import apply_label, get_or_create_label

logger = logging.getLogger("email_triage.commands")

# The only actions we allow. All are reversible in Gmail (no permanent delete).
ALLOWED_ACTIONS = {"label", "archive", "mark_read"}

_SYSTEM_PROMPT = (
    "You convert a user's plain-English inbox instruction into a single safe, "
    "reversible action over Gmail. Respond with ONLY a JSON object, no extra "
    "text, with these keys:\n"
    '  "action": one of "label", "archive", "mark_read"\n'
    '  "query": a Gmail search query string that selects the target emails\n'
    '  "label": the label name to apply (only when action is "label", else null)\n'
    '  "summary": a short human sentence describing what will happen\n\n'
    "Use standard Gmail search operators for the query, e.g. from:, to:, "
    "subject:, older_than:30d, newer_than:7d, category:promotions, "
    "category:social, is:unread, has:attachment, label:. Combine with spaces "
    "(AND) or OR. Never invent an action outside the three allowed. Never "
    "delete. If the user asks to delete, use archive instead.\n\n"
    'Example: "archive all newsletters older than 30 days" -> '
    '{"action":"archive","query":"category:promotions older_than:30d","label":null,'
    '"summary":"Archive promotional emails older than 30 days"}\n'
    'Example: "label all mail from stripe as Finance" -> '
    '{"action":"label","query":"from:stripe.com","label":"Finance",'
    '"summary":"Label all emails from stripe.com as Finance"}\n'
    'Example: "mark everything from linkedin as read" -> '
    '{"action":"mark_read","query":"from:linkedin.com is:unread","label":null,'
    '"summary":"Mark unread LinkedIn emails as read"}'
)


def parse_command(text):
    """Parse a natural-language instruction into a structured action dict.

    Returns {"action", "query", "label", "summary"} on success, or
    {"error": "..."} if it cannot be understood or the LLM is unavailable.
    """
    text = (text or "").strip()
    if not text:
        return {"error": "Type an instruction first."}

    client = _get_client()
    if client is None:
        return {"error": "AI is not configured on the server (no API key)."}

    try:
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0,
            max_tokens=200,
        )
        raw = response.choices[0].message.content.strip()
    except Exception as exc:  # noqa: BLE001 - surface as a friendly error
        logger.warning("Command parse failed: %s", exc)
        return {"error": "Could not understand that instruction. Try rephrasing."}

    # The model may wrap JSON in code fences; strip them defensively.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {"error": "Could not understand that instruction. Try rephrasing."}

    action = parsed.get("action")
    query = parsed.get("query")
    if action not in ALLOWED_ACTIONS or not isinstance(query, str) or not query.strip():
        return {"error": "That instruction is not something I can safely do."}

    label = parsed.get("label")
    if action == "label" and (not isinstance(label, str) or not label.strip()):
        return {"error": "Tell me which label to apply."}

    return {
        "action": action,
        "query": query.strip(),
        "label": label.strip() if isinstance(label, str) else None,
        "summary": (parsed.get("summary") or "").strip(),
    }


def _search_ids(service, query, max_results=100):
    """Return message ids matching a Gmail search query (up to max_results)."""
    ids = []
    resp = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=min(max_results, 100))
        .execute()
    )
    for msg in resp.get("messages", []):
        ids.append(msg["id"])
    return ids


def _brief(service, message_id):
    """Return a small {subject, sender} preview for one message."""
    try:
        msg = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["Subject", "From"],
            )
            .execute()
        )
        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        return {
            "subject": headers.get("subject", "(no subject)"),
            "sender": headers.get("from", ""),
        }
    except Exception:  # noqa: BLE001 - a preview row is best-effort
        return {"subject": "(preview unavailable)", "sender": ""}


def preview_command(service, parsed, sample=5):
    """Count matching emails and return a few sample rows for confirmation."""
    ids = _search_ids(service, parsed["query"], max_results=100)
    samples = [_brief(service, mid) for mid in ids[:sample]]
    return {"count": len(ids), "samples": samples, "ids": ids}


def execute_command(service, parsed, ids, account_key=None):
    """Apply the parsed action to the given message ids. Returns count affected."""
    action = parsed["action"]
    affected = 0

    label_id = None
    if action == "label":
        label_id = get_or_create_label(service, parsed["label"], account_key=account_key)

    for message_id in ids:
        try:
            if action == "label":
                apply_label(service, message_id, label_id)
            elif action == "archive":
                service.users().messages().modify(
                    userId="me", id=message_id, body={"removeLabelIds": ["INBOX"]}
                ).execute()
            elif action == "mark_read":
                service.users().messages().modify(
                    userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}
                ).execute()
            affected += 1
        except Exception as exc:  # noqa: BLE001 - keep going on per-message errors
            logger.warning("Command action failed for %s: %s", message_id, exc)
    return affected
