"""Deterministic keyword/domain rules for fast email classification.

Rules run before any LLM call. Each rule is a ``(matcher_fn, category)`` pair
where ``matcher_fn(email)`` returns ``True`` when the rule applies. The first
matching rule wins, so order matters: put higher-priority signals (e.g. red
flags) earlier.
"""


def _field(email, name):
    """Return a lowercased string field from the email dict, safe for missing keys."""
    return (email.get(name) or "").lower()


def _sender_has(email, *needles):
    sender = _field(email, "sender")
    return any(needle in sender for needle in needles)


def _subject_has(email, *needles):
    subject = _field(email, "subject")
    return any(needle in subject for needle in needles)


def _text_has(email, *needles):
    """Match against subject and body combined."""
    text = _field(email, "subject") + " " + _field(email, "body")
    return any(needle in text for needle in needles)


def _is_automated_invoice(email):
    return _sender_has(email, "noreply@", "no-reply@") and _subject_has(
        email, "invoice", "receipt"
    )


def _is_otp(email):
    return _subject_has(email, "otp", "verification code")


def _is_red_flag(email):
    return _text_has(
        email, "urgent", "asap", "immediately", "complaint", "legal action"
    )


def _is_newsletter(email):
    return _sender_has(
        email, "newsletter@", "marketing@", "noreply-updates@"
    )


# Order matters: first match wins. Red flags are checked early so that an
# "urgent" message is never demoted to a newsletter/invoice category.
RULES = [
    (_is_red_flag, "Red Flag"),
    (_is_automated_invoice, "Needs Action"),
    (_is_otp, "FAQ"),
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
