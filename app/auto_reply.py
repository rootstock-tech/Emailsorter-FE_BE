"""Draft polite auto-replies for the user's configured auto-reply category.

This module is category-agnostic: the caller (main.py) decides which category
triggers a draft. It NEVER sends mail -- it only creates Gmail *drafts* for a
human to review and send, using ``drafts().create`` exclusively (never
``drafts().send`` or ``messages().send``).
"""

import base64
import logging
from email.mime.text import MIMEText

from groq import Groq

# Reuse the same model the classifier uses. The classification *logic* is not
# touched — only the shared model constant is imported.
from app.classifier import GROQ_MODEL
from app.config import GROQ_API_KEY

logger = logging.getLogger(__name__)

# Placeholder FAQ question/answer pairs. These are examples the client will
# finalize later; they exist to ground the generated replies in real answers.
FAQ_TEMPLATES = [
    "Q: What are your business hours? "
    "A: We are open Monday to Friday, 9:00 AM to 6:00 PM (local time), "
    "excluding public holidays.",
    "Q: How do I check my order or invoice status? "
    "A: Sign in to your account and open the 'Orders' section to see the "
    "latest status of any order or invoice.",
    "Q: What is your refund policy? "
    "A: Eligible items can be refunded within 30 days of purchase. Approved "
    "refunds are returned to the original payment method within 5-7 business "
    "days.",
    "Q: How do I reset my password or recover my account? "
    "A: Use the 'Forgot password' link on the sign-in page to receive a reset "
    "email. If you can no longer access that email, contact support to verify "
    "your identity.",
]

_SYSTEM_PROMPT = (
    "You are a helpful customer support assistant. Write ONLY the body text of "
    "a short, polite email reply. Begin with a simple greeting such as 'Hi,'. "
    "Do NOT include a subject line, email headers, or a formal 'Dear ...' "
    "salutation beyond that simple greeting. Keep the reply under about 120 "
    "words. Ground your answer in the provided FAQ reference material where it "
    "is relevant; if the question is not covered, politely acknowledge it and "
    "say the team will follow up."
)

# Lazily-created singleton client, mirroring the pattern in classifier.py.
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


def draft_reply_body(email):
    """Generate a short, polite reply body for an email, or ``None`` on failure.

    Uses Groq with low reasoning effort and grounds the reply in FAQ_TEMPLATES.
    """
    client = _get_client()
    if client is None:
        logger.warning("GROQ_API_KEY not set; cannot draft reply.")
        return None

    faq_reference = "\n".join(f"- {item}" for item in FAQ_TEMPLATES)
    user_prompt = (
        f"FAQ reference material:\n{faq_reference}\n\n"
        f"Incoming email subject: {_truncate(email.get('subject'), 300)}\n"
        f"Incoming email body:\n{_truncate(email.get('body'), 1500)}\n\n"
        "Write the reply body now."
    )

    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        # No reasoning_effort: GROQ_MODEL (llama-3.3-70b-versatile) is not a
        # reasoning model, so this param isn't applicable.
        temperature=0.3,
        max_tokens=512,
    )
    return response.choices[0].message.content.strip()


def _reply_subject(subject):
    """Return the subject prefixed with 'Re: ' unless it already is."""
    subject = (subject or "").strip()
    if subject.lower().startswith("re:"):
        return subject
    return f"Re: {subject}".strip()


def create_draft_reply(service, email):
    """Create a Gmail *draft* reply for an email. Never sends. Returns the draft
    resource on success, or ``None`` on any failure (logged as a warning)."""
    try:
        reply_text = draft_reply_body(email)
        if not reply_text:
            logger.warning(
                "No reply body generated for %s; skipping draft.",
                email.get("id", "<unknown>"),
            )
            return None

        message = MIMEText(reply_text)
        message["To"] = email.get("sender", "")
        message["Subject"] = _reply_subject(email.get("subject", ""))

        # Deliberately NOT threaded into the original conversation: a threaded
        # draft inherits the original email's labels, so FAQ drafts would show
        # up under the "FAQ" label. Keeping the draft standalone means it lives
        # only in Drafts, and the category label shows just the real inbox mail.
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

        draft_message = {"raw": raw}

        return (
            service.users()
            .drafts()
            .create(userId="me", body={"message": draft_message})
            .execute()
        )
    except Exception as exc:  # noqa: BLE001 - one failed draft must not stop the batch
        logger.warning(
            "Failed to create draft reply for %s: %s",
            email.get("id", "<unknown>"),
            exc,
        )
        return None
