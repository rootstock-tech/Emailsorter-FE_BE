"""Email classification: deterministic rules first, then Groq LLM fallback.

Emails that fall through to the LLM are classified in batches (one Groq call
covers many emails) rather than one call per email. The free-tier rate limit
is requests-per-minute, not tokens-per-request, so batching cuts the number
of calls needed by roughly BATCH_SIZE times and avoids constant 429 retries.
"""

import json
import logging
import time

from groq import Groq

from app.config import GROQ_API_KEY
from app.rules import classify_by_rules

logger = logging.getLogger(__name__)

# Default category set, used as a fallback when a caller does not pass an
# explicit per-user category list. The LLM must return one of the active set.
VALID_CATEGORIES = {
    "Needs Action",
    "FAQ",
    "Red Flag",
    "Low Priority",
    "Spam/Newsletter",
}

# When rules do not match and the LLM is unavailable or returns garbage, we
# default here. Over-flagging (Needs Action) is safer than silently dropping a
# potentially important email.
DEFAULT_CATEGORY = "Needs Action"

# Groq model used for classification. llama-3.3-70b-versatile is not a
# reasoning model (no hidden reasoning_tokens), gets double the free-tier
# rate limit vs. gpt-oss-120b (12K TPM vs 6K TPM), and uses far fewer tokens
# per call -- all of which speeds up processing under the free tier.
GROQ_MODEL = "llama-3.3-70b-versatile"

# How many emails to classify per Groq call.
BATCH_SIZE = 10


def _build_system_prompt(categories):
    """Build the classifier system prompt from a user's category list."""
    category_list = ", ".join(categories)
    example = list(categories)[:3] or list(categories)
    return (
        "You are an email triage assistant. You will be given a numbered list "
        "of emails. Classify each one into exactly one of these categories: "
        f"{category_list}. "
        "Respond with only a JSON array of strings, one category per email, in "
        "the same order, and nothing else. Example for "
        f"{len(example)} emails: {json.dumps(example)}"
    )

# Lazily-created singleton client so we don't build one per email.
_client = None


def _get_client():
    """Return a cached Groq client, or ``None`` if no API key is configured."""
    global _client
    if _client is None:
        if not GROQ_API_KEY:
            return None
        _client = Groq(api_key=GROQ_API_KEY)
    return _client


def _truncate(text, limit):
    """Trim text to keep the prompt small and cheap."""
    text = text or ""
    return text[:limit]


def _format_email(index, email):
    return (
        f"{index}. Sender: {_truncate(email.get('sender'), 200)}\n"
        f"   Subject: {_truncate(email.get('subject'), 200)}\n"
        f"   Body: {_truncate(email.get('body'), 800)}"
    )


def _classify_batch_with_groq(emails, system_prompt, valid_categories):
    """Ask Groq to classify a batch of emails. Returns a list of categories
    (or None per-item on parse mismatch), or None entirely on failure."""
    client = _get_client()
    if client is None:
        logger.warning("GROQ_API_KEY not set; skipping LLM classification.")
        return None

    user_prompt = "\n\n".join(
        _format_email(i + 1, email) for i, email in enumerate(emails)
    )

    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
        # No reasoning_effort here: llama-3.3-70b-versatile is not a reasoning
        # model, so there are no hidden reasoning tokens to budget for -- the
        # category names alone need very few output tokens.
        max_tokens=32 * len(emails),
    )

    raw = response.choices[0].message.content.strip()
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.warning("Could not parse Groq batch response as JSON: %r", raw)
        return None

    if not isinstance(parsed, list):
        return None

    # Pad/truncate defensively in case the model returns the wrong length.
    results = []
    for i in range(len(emails)):
        value = parsed[i] if i < len(parsed) else None
        results.append(value if value in valid_categories else None)
    return results


def classify_emails(emails, categories=None, default_category=None, faq_category=None):
    """Classify a list of emails, returning a same-length list of categories.

    ``categories`` is the user's active category list (falls back to
    VALID_CATEGORIES). ``default_category`` is used when the LLM fails or returns
    an unrecognized value (falls back to DEFAULT_CATEGORY). ``faq_category`` is
    accepted for a consistent interface with callers but is not needed for
    classification itself.

    Emails resolved by deterministic rules skip the LLM entirely. The rest are
    sent to Groq in batches of BATCH_SIZE. Any batch failure or per-item parse
    mismatch falls back to ``default_category`` so an email is never silently
    dropped.
    """
    if categories is None:
        categories = list(VALID_CATEGORIES)
    if default_category is None:
        default_category = DEFAULT_CATEGORY

    valid_categories = set(categories)
    system_prompt = _build_system_prompt(categories)

    classified = [None] * len(emails)
    pending_indices = []

    for i, email in enumerate(emails):
        rule_category = classify_by_rules(email, categories)
        if rule_category is not None:
            classified[i] = rule_category
        else:
            pending_indices.append(i)

    for start in range(0, len(pending_indices), BATCH_SIZE):
        chunk_indices = pending_indices[start : start + BATCH_SIZE]
        chunk_emails = [emails[i] for i in chunk_indices]

        try:
            results = _classify_batch_with_groq(
                chunk_emails, system_prompt, valid_categories
            )
        except Exception as exc:  # noqa: BLE001 - never let one batch break the run
            logger.warning("Groq batch classification failed: %s", exc)
            results = None

        if results is None:
            results = [None] * len(chunk_emails)

        for idx, result in zip(chunk_indices, results):
            classified[idx] = result if result is not None else default_category

        # Small pause between batches to stay comfortably under rate limits.
        if start + BATCH_SIZE < len(pending_indices):
            time.sleep(1)

    return classified


def classify_email(email, categories=None, default_category=None, faq_category=None):
    """Classify a single email. Convenience wrapper around classify_emails."""
    return classify_emails([email], categories, default_category, faq_category)[0]
