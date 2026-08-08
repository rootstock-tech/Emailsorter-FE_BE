"""Priority scoring: rank each email by how much it likely needs attention.

Pure, deterministic heuristics (no extra LLM cost). Combines the category the
email was sorted into, how recent it is, and simple sender signals into a 0-100
score, and builds a short human reason explaining the score so the dashboard can
show *why* a mail is near the top.
"""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

# Base score per category. Known category names map by intent; anything else
# (a user's custom category) gets a neutral middle base.
_CATEGORY_BASE = {
    "red flag": 90,
    "needs action": 70,
    "faq": 45,
    "others": 25,
    "spam/newsletter": 8,
    "spam": 8,
    "newsletter": 8,
    "promotions": 8,
}
_DEFAULT_BASE = 50

# Sender fragments that mark bulk/automated mail (lowers priority).
_BULK_SENDER_HINTS = (
    "no-reply",
    "noreply",
    "no_reply",
    "donotreply",
    "do-not-reply",
    "mailer",
    "newsletter",
    "notifications",
    "notification",
    "updates",
    "marketing",
    "info@",
    "support@",
    "team@",
)


def _category_base(category):
    key = (category or "").strip().lower()
    if key in _CATEGORY_BASE:
        return _CATEGORY_BASE[key]
    for hint, value in _CATEGORY_BASE.items():
        if hint in key:
            return value
    return _DEFAULT_BASE


def _hours_old(date_str):
    """Age of the email in hours, or None if the date cannot be parsed."""
    if not date_str:
        return None
    try:
        parsed = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        try:
            parsed = parsedate_to_datetime(date_str)
        except (TypeError, ValueError):
            return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - parsed
    return max(0.0, delta.total_seconds() / 3600.0)


def _recency_points(hours):
    """Newer mail scores higher. Returns (points, human_phrase)."""
    if hours is None:
        return 0, None
    if hours < 3:
        return 15, "just arrived"
    if hours < 12:
        return 10, "arrived today"
    if hours < 48:
        return 5, "recent"
    if hours > 24 * 14:
        return -5, "over two weeks old"
    return 0, None


def _is_bulk_sender(sender):
    s = (sender or "").lower()
    return any(hint in s for hint in _BULK_SENDER_HINTS)


def compute_priority(email, category, learned_rules=None):
    """Return (score, reason) for one classified email.

    ``score`` is an int roughly in 0-100 (clamped). ``reason`` is a short
    human-readable string explaining the main factors.
    """
    reasons = []

    base = _category_base(category)
    score = base
    if base >= 70:
        reasons.append(f"sorted as {category}")
    elif base <= 10:
        reasons.append(f"low-priority {category}")

    hours = _hours_old(email.get("date"))
    pts, phrase = _recency_points(hours)
    score += pts
    if phrase:
        reasons.append(phrase)

    if _is_bulk_sender(email.get("sender")):
        score -= 12
        reasons.append("automated/bulk sender")
    else:
        # A real person/address on an action-worthy category nudges it up.
        if base >= 60:
            score += 8
            reasons.append("from a direct sender")

    # A reply in an ongoing thread is easy to miss, so lift it up the list.
    if email.get("is_reply"):
        score += 15
        reasons.append("reply in an ongoing thread")

    score = max(0, min(100, int(round(score))))
    reason = "; ".join(reasons) if reasons else "routine mail"
    return score, reason
