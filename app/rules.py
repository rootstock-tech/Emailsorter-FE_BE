"""Deterministic domain rules for fast, high-confidence classification.

Only very reliable rules live here -- everything nuanced is left to the AI
classifier, which judges intent far better than keyword matching. (Keyword
rules like "urgent" -> Red Flag or "invoice" -> Needs Action misfired on
marketing mail and hurt accuracy, so they were removed.) Rules run before any
LLM call; the first matching rule whose category the user actually uses wins.
"""


def _field(email, name):
    """Return a lowercased string field from the email dict, safe for missing keys."""
    return (email.get(name) or "").lower()


def _sender_has(email, *needles):
    sender = _field(email, "sender")
    return any(needle in sender for needle in needles)


def _is_newsletter(email):
    # Sender addresses like newsletter@, marketing@, noreply-updates@ are
    # reliably bulk/marketing mail.
    return _sender_has(email, "newsletter@", "marketing@", "noreply-updates@")


# Only high-confidence rules. Everything else falls through to the AI classifier.
RULES = [
    (_is_newsletter, "Spam/Newsletter"),
]


def classify_by_rules(email, categories):
    """Return a matching rule's category, or ``None`` if none apply.

    A rule only counts if its target category is in the user's ``categories``
    list; rules for categories the user does not use are skipped so the email
    falls through to the AI classifier (which only knows the user's categories).
    """
    for matcher, category in RULES:
        if category not in categories:
            continue
        if matcher(email):
            return category
    return None
