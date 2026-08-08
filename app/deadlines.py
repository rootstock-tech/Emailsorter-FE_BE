"""Deadline extraction: find an explicit due date in an email, deterministically.

No LLM cost -- we scan the subject and body for common date formats that appear
next to deadline-ish wording ("due", "deadline", "last date", "by", "before",
"expires", "rsvp"). The goal is high precision: only surface a reminder when the
mail clearly states a date, so we never nag the user about a date we guessed.
"""

import json
import logging
import re
from datetime import date, datetime

from app.classifier import GROQ_MODEL, _get_client

logger = logging.getLogger("email_triage.deadlines")

# Words that signal the nearby date is a deadline the user must act on.
_DEADLINE_CUES = (
    "deadline",
    "due date",
    "due by",
    "due on",
    "due",
    "last date",
    "last day",
    "by end of",
    "expires",
    "expiry",
    "rsvp",
    "respond by",
    "reply by",
    "submit by",
    "before",
    "no later than",
)

_RELATIVE_DATE_CUES = (
    "today",
    "tomorrow",
    "day after tomorrow",
    "next monday",
    "next tuesday",
    "next wednesday",
    "next thursday",
    "next friday",
    "next saturday",
    "next sunday",
    "next week",
)

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

# 2026-08-15  or  2026/8/15
_ISO_RE = re.compile(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b")
# 15/08/2026  or  15-8-26   (day first, common outside the US)
_DMY_RE = re.compile(r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})\b")
# 15 Aug 2026  /  15 August, 2026  /  Aug 15 2026
_TEXT_DMY_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s*(\d{4})\b"
)
_TEXT_MDY_RE = re.compile(
    r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})\b"
)


def _valid(y, m, d):
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _four_digit_year(y):
    y = int(y)
    if y < 100:
        y += 2000
    return y


def _find_dates(text):
    """Yield date objects for every parseable date in ``text``."""
    for y, m, d in _ISO_RE.findall(text):
        got = _valid(int(y), int(m), int(d))
        if got:
            yield got
    for d, m, y in _DMY_RE.findall(text):
        got = _valid(_four_digit_year(y), int(m), int(d))
        if got:
            yield got
    for d, mon, y in _TEXT_DMY_RE.findall(text):
        month = _MONTHS.get(mon.lower())
        if month:
            got = _valid(int(y), month, int(d))
            if got:
                yield got
    for mon, d, y in _TEXT_MDY_RE.findall(text):
        month = _MONTHS.get(mon.lower())
        if month:
            got = _valid(int(y), month, int(d))
            if got:
                yield got


def _has_cue(text):
    low = text.lower()
    return any(cue in low for cue in _DEADLINE_CUES)


def extract_deadline(email, today=None):
    """Return (due_date_iso, description) if the mail states a future deadline.

    Returns (None, None) when no deadline cue + parseable future date is found.
    ``today`` can be injected for testing; defaults to the real current date.
    Only future (or today's) dates are returned, so old dates never nag.
    """
    today = today or date.today()
    subject = (email.get("subject") or "").strip()
    body = (email.get("body") or "")
    haystack = f"{subject}\n{body}"

    if not _has_cue(haystack):
        return None, None

    # Earliest upcoming date wins (the most urgent deadline in the mail).
    upcoming = sorted(d for d in _find_dates(haystack) if d >= today)
    if not upcoming:
        return None, None

    due = upcoming[0]
    label = subject or "Deadline"
    return due.isoformat(), label[:200]


def extract_deadline_with_llm(email, today=None):
    """Resolve cue-bearing relative/ambiguous deadlines with validated JSON."""
    today = today or date.today()
    subject = (email.get("subject") or "").strip()
    body = email.get("body") or ""
    if not _has_cue(f"{subject}\n{body}"):
        return None, None
    client = _get_client()
    if client is None:
        return None, None
    prompt = (
        f"Today is {today.isoformat()}. Extract only a real action deadline from "
        "this email. Resolve relative dates such as tomorrow. Return only JSON: "
        '{"has_deadline":true|false,"date":"YYYY-MM-DD or null","what":"short description"}.\n'
        f"Subject: {subject[:300]}\nBody: {body[:2000]}"
    )
    try:
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "Extract deadlines conservatively. Never invent a date.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            max_tokens=120,
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:]
        parsed = json.loads(raw)
        if not parsed.get("has_deadline") or not parsed.get("date"):
            return None, None
        due = date.fromisoformat(parsed["date"])
        if due < today:
            return None, None
        source = f"{subject}\n{body}".lower()
        date_is_explicit = any(
            token in source
            for token in (
                due.isoformat(),
                due.strftime("%d/%m/%Y"),
                due.strftime("%d-%m-%Y"),
                f"{due.day} {due.strftime('%B %Y')}",
            )
            if token
        )
        date_is_relative = any(cue in source for cue in _RELATIVE_DATE_CUES)
        if not date_is_explicit and not date_is_relative:
            return None, None
        description = (parsed.get("what") or subject or "Deadline").strip()[:200]
        return due.isoformat(), description
    except Exception:  # noqa: BLE001 - deadline fallback is best-effort
        logger.warning("LLM deadline extraction failed.", exc_info=True)
        return None, None
