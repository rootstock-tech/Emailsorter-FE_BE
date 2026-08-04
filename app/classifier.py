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
    "Others",
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

# Name fragments that mark a category as a good "catch-all" for emails that do
# not clearly fit anywhere else (used to pick a safe fallback that is always one
# of the user's own categories -- so we never invent an extra label).
_CATCHALL_HINTS = (
    "low",
    "other",
    "misc",
    "general",
    "spam",
    "newsletter",
    "promo",
    "update",
)


def _pick_default_category(categories):
    """Choose a fallback category from the user's own list.

    Prefers a low-priority / catch-all category if the user has one, else falls
    back to the last configured category. Never returns a name outside
    ``categories``, so an unmatched email can never create a brand-new label.
    """
    if not categories:
        return DEFAULT_CATEGORY
    for category in categories:
        if any(hint in category.lower() for hint in _CATCHALL_HINTS):
            return category
    return categories[-1]


# Short guidance for the built-in category names so the model classifies by
# real intent instead of guessing from the label. Custom categories fall back
# to their plain name.
_CATEGORY_HINTS = {
    "Needs Action": (
        "Mail where you personally have to reply or do something, like a direct "
        "question, a request, a task to complete, or a bill you need to pay."
    ),
    "FAQ": (
        "A common, everyday question that you can answer with a standard reply, "
        "the kind of thing that gets asked again and again."
    ),
    "Red Flag": (
        "Urgent or serious mail such as complaints, legal notices, security or "
        "fraud alerts, account problems, or anything that needs escalating."
    ),
    "Others": (
        "Everything that does not clearly belong anywhere else, like receipts, "
        "order and payment confirmations, verification codes, and routine "
        "notifications you only need to be aware of."
    ),
    "Spam/Newsletter": (
        "Marketing, promotions, sales, newsletters, and bulk mail that you never "
        "personally asked for."
    ),
}

# Plain-language summary of how a label is chosen, shown in the app's label guide.
CLASSIFICATION_SUMMARY = (
    "We start with a simple check: mail from marketing and newsletter addresses "
    "goes straight to Spam/Newsletter. Everything else is read by an AI model "
    "that decides the category from what the email is really about, not just the "
    "words in it. If a mail does not clearly fit anywhere, it goes to Others. We "
    "also add a hidden AI-Processed label to mail we have already handled so the "
    "same message is never sorted twice."
)


def category_definitions():
    """Return a copy of the human-readable definition for each known category."""
    return dict(_CATEGORY_HINTS)


def _build_system_prompt(categories):
    """Build the classifier system prompt from a user's category list.

    Includes a short description of each recognized category so the model
    classifies by real intent rather than guessing from the label name.
    """
    lines = []
    for category in categories:
        hint = _CATEGORY_HINTS.get(category)
        lines.append(f"- {category}: {hint}" if hint else f"- {category}")
    guidance = "\n".join(lines)
    example = list(categories)[:3] or list(categories)

    return (
        "You are an expert email triage assistant. Classify each email into "
        "exactly ONE of these categories, picking the single best fit based on "
        "the email's real intent and whether the recipient must act:\n"
        f"{guidance}\n\n"
        "Rules of thumb:\n"
        "- Classify by the email's real PURPOSE, not just topic keywords. A "
        "promotional email about courses, jobs, products or events is marketing "
        "-- put it in the newsletter/promotions category, not a topic category "
        "it merely mentions (e.g. an ad for online courses is NOT 'educational').\n"
        "- Marketing, promotions, sales, job alerts, social/network "
        "notifications and newsletters are NEVER an action or urgent category, "
        "even if they say 'urgent' or 'act now'.\n"
        "- Automated receipts, confirmations and verification codes are "
        "informational, not action items.\n"
        "- Only choose an action or urgent category when a real human genuinely "
        "needs to respond or do something personally addressed to them.\n"
        "- Assign a category ONLY when the email genuinely belongs there. If it "
        "does not clearly fit any specific category, choose the most general or "
        "low-priority one instead of forcing a wrong label.\n\n"
        "You will get a numbered list of emails. Respond with ONLY a JSON array "
        "of category strings -- one per email, in the same order, no extra "
        f"text. Example for {len(example)} emails: {json.dumps(example)}"
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


def classify_emails(emails, categories=None, default_category=None, faq_category=None, progress_cb=None, should_cancel=None):
    """Classify a list of emails, returning a same-length list of categories.

    ``categories`` is the user's active category list (falls back to
    VALID_CATEGORIES). ``default_category`` is used when the LLM fails or returns
    an unrecognized value (falls back to DEFAULT_CATEGORY). ``faq_category`` is
    accepted for a consistent interface with callers but is not needed for
    classification itself. ``progress_cb``, when provided, is called with the
    cumulative number of emails classified so far (after rules, then after each
    LLM batch) so a caller can show progress during the classification phase.

    Emails resolved by deterministic rules skip the LLM entirely. The rest are
    sent to Groq in batches of BATCH_SIZE. Any batch failure or per-item parse
    mismatch falls back to ``default_category`` so an email is never silently
    dropped.
    """
    if categories is None:
        categories = list(VALID_CATEGORIES)
    if default_category is None or default_category not in categories:
        # Always fall back to one of the user's own categories so an unmatched
        # or failed email never spawns an extra label they did not configure.
        default_category = _pick_default_category(categories)

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

    # Rule-classified emails are already done; report that head start.
    done_count = len(emails) - len(pending_indices)
    if progress_cb is not None:
        progress_cb(done_count)

    for start in range(0, len(pending_indices), BATCH_SIZE):
        # Stop classifying as soon as a cancel is requested -- this is the slow
        # phase, so checking here makes Stop take effect within a batch instead
        # of after the whole chunk is classified.
        if should_cancel is not None and should_cancel():
            logger.info("Classification cancelled after %d email(s).", done_count)
            break

        chunk_indices = pending_indices[start : start + BATCH_SIZE]
        chunk_emails = [emails[i] for i in chunk_indices]

        # One retry: a transient failure (rate limit / bad JSON) should not
        # silently dump a whole batch into the default category.
        results = None
        for attempt in range(2):
            try:
                results = _classify_batch_with_groq(
                    chunk_emails, system_prompt, valid_categories
                )
            except Exception as exc:  # noqa: BLE001 - never let one batch break the run
                logger.warning(
                    "Groq batch classification failed (attempt %d): %s",
                    attempt + 1,
                    exc,
                )
                results = None
            if results is not None:
                break
            if attempt == 0:
                time.sleep(2)  # brief backoff before the single retry

        if results is None:
            results = [None] * len(chunk_emails)

        for idx, result in zip(chunk_indices, results):
            classified[idx] = result if result is not None else default_category

        done_count += len(chunk_indices)
        if progress_cb is not None:
            progress_cb(done_count)

        # Small pause between batches to stay comfortably under rate limits.
        if start + BATCH_SIZE < len(pending_indices):
            time.sleep(1)

    return classified


def classify_email(email, categories=None, default_category=None, faq_category=None):
    """Classify a single email. Convenience wrapper around classify_emails."""
    return classify_emails([email], categories, default_category, faq_category)[0]
