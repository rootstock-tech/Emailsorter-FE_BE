"""Scheduled reminder emails for unread attention mail and upcoming deadlines."""

from datetime import date, datetime, timedelta, timezone

from app.db import mark_reminded, reminder_due, top_priority, upcoming_deadlines
from app.gmail_client import send_reminder_email, unread_message_ids

_ATTENTION_CATEGORIES = {"needs action", "red flag"}


def _parsed_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def collect_due_reminders(service, user_email, now=None):
    """Return due attention/deadline rows without mutating reminder state."""
    now = now or datetime.now(timezone.utc)
    candidates = [
        item
        for item in top_priority(user_email, 100)
        if (item.get("category") or "").lower() in _ATTENTION_CATEGORIES
    ]
    unread = unread_message_ids(service, [item.get("gmail_id") for item in candidates])
    attention = []
    for item in candidates:
        gmail_id = item.get("gmail_id")
        received = _parsed_datetime(item.get("date"))
        if (
            gmail_id in unread
            and received is not None
            and now - received >= timedelta(hours=24)
            and reminder_due(user_email, gmail_id, "attention", now=now)
        ):
            attention.append(item)

    deadline_limit = (now.date() + timedelta(days=7)).isoformat()
    deadlines = []
    for item in upcoming_deadlines(user_email, on_or_before=deadline_limit, limit=100):
        kind = f"deadline:{item.get('due_date')}"
        if reminder_due(user_email, item.get("gmail_id"), kind, max_count=1, now=now):
            deadlines.append(item)
    return attention, deadlines


def send_user_reminders(service, user_email, now=None):
    """Send one summary email when reminders are due and persist successful sends."""
    now = now or datetime.now(timezone.utc)
    attention, deadlines = collect_due_reminders(service, user_email, now=now)
    if not attention and not deadlines:
        return {"sent": False, "attention": 0, "deadlines": 0}

    sent_attention = attention[:20]
    sent_deadlines = deadlines[:20]
    lines = ["Email Triage Assistant reminder", ""]
    if sent_attention:
        lines.append("Unread mail needing attention for more than 24 hours:")
        for item in sent_attention:
            lines.append(
                f"- [{item.get('category')}] {item.get('subject') or '(no subject)'} "
                f"from {item.get('sender') or 'unknown sender'}"
            )
        lines.append("")
    if sent_deadlines:
        lines.append("Upcoming deadlines in the next 7 days:")
        for item in sent_deadlines:
            lines.append(
                f"- Due {item.get('due_date')}: "
                f"{item.get('subject') or item.get('description') or 'Deadline'}"
            )
        lines.append("")
    lines.append("Open Gmail or the Email Triage Assistant add-on to review them.")

    send_reminder_email(
        service,
        user_email,
        "Email Triage reminder: items need your attention",
        "\n".join(lines),
    )
    for item in sent_attention:
        mark_reminded(user_email, item.get("gmail_id"), "attention", now=now)
    for item in sent_deadlines:
        mark_reminded(
            user_email,
            item.get("gmail_id"),
            f"deadline:{item.get('due_date')}",
            now=now,
        )
    return {
        "sent": True,
        "attention": len(sent_attention),
        "deadlines": len(sent_deadlines),
    }
