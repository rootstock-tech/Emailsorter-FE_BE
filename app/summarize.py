"""One-click email summariser: turns a long mail into a few crisp bullets."""

import logging

from groq import Groq

from app.classifier import GROQ_MODEL
from app.config import GROQ_API_KEY

logger = logging.getLogger("email_triage")

_SYSTEM_PROMPT = (
    "You summarise emails for a busy professional. Return 2 to 4 short bullet "
    "points capturing what the mail is about, any request or decision needed, "
    "and any deadline or amount mentioned. Start every bullet with '- '. "
    "Be factual, no preamble, no closing line, no invented details."
)

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
    text = text or ""
    return text[:limit]


def summarize_email(subject, body, sender=""):
    """Return a short bullet summary of one email, or ``None`` on failure."""
    client = _get_client()
    if client is None:
        logger.warning("GROQ_API_KEY not set; cannot summarize.")
        return None

    user_prompt = (
        f"From: {_truncate(sender, 200)}\n"
        f"Subject: {_truncate(subject, 300)}\n"
        f"Body:\n{_truncate(body, 4000)}\n\n"
        "Summarise now."
    )

    try:
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=300,
        )
    except Exception:
        logger.exception("Summarize call failed.")
        return None

    return response.choices[0].message.content.strip()
