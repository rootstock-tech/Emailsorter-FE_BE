"""Gmail API OAuth2 setup and email fetching."""

import base64
import json
import os
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# gmail.modify is required to apply labels to messages during triage.
# NOTE: if you previously authenticated with the gmail.readonly scope, delete
# the existing token.json and re-authenticate so the new scope takes effect.
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"


def authenticate():
    """Run the installed-app OAuth2 flow and return a Gmail API service object.

    On first run this opens a browser for consent and stores the resulting
    token in token.json. Subsequent runs reuse (and refresh) that token.
    """
    creds = None

    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_FILE, SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w", encoding="utf-8") as token:
            token.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def authenticate_from_token_json(token_json, on_refresh=None):
    """Build a Gmail API service from stored credentials JSON.

    Used by the web (multi-user) flow, where each user's token is loaded from
    the database rather than a local token.json file. If the token has expired,
    it is refreshed via Request() (same logic as authenticate()'s refresh
    branch). The installed-app authenticate() above is intentionally unchanged.
    """
    creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)

    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        if on_refresh is not None:
            on_refresh(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def start_watch(service, topic_name, label_ids=None):
    """Register a Gmail push notification watch for the user's mailbox.

    Google publishes a Pub/Sub message to ``topic_name`` whenever the mailbox
    changes. Returns the API response, e.g. {'historyId': ..., 'expiration': ...}
    (expiration is epoch millis). The watch must be renewed before it expires
    (Google caps it at ~7 days), so callers should re-invoke this periodically.
    """
    body = {
        "topicName": topic_name,
        "labelIds": label_ids or ["INBOX"],
        "labelFilterBehavior": "INCLUDE",
    }
    return service.users().watch(userId="me", body=body).execute()


def _get_header(headers, name):
    """Return the value of a header (case-insensitive) or an empty string."""
    for header in headers:
        if header.get("name", "").lower() == name.lower():
            return header.get("value", "")
    return ""


def _decode_body(data):
    """Decode a base64url-encoded body part to text."""
    if not data:
        return ""
    decoded = base64.urlsafe_b64decode(data.encode("utf-8"))
    return decoded.decode("utf-8", errors="replace")


def _extract_body(payload):
    """Extract a plain-text body from a message payload, walking parts."""
    mime_type = payload.get("mimeType", "")
    body = payload.get("body", {})

    if mime_type == "text/plain" and body.get("data"):
        return _decode_body(body["data"])

    for part in payload.get("parts", []):
        text = _extract_body(part)
        if text:
            return text

    # Fall back to any top-level body data if no text/plain part was found.
    if body.get("data"):
        return _decode_body(body["data"])

    return ""


def _parse_date(raw_date):
    """Parse an RFC 2822 date header into an ISO string, or return the raw value."""
    if not raw_date:
        return ""
    try:
        return parsedate_to_datetime(raw_date).isoformat()
    except (TypeError, ValueError):
        return raw_date


def _internal_date(message, fallback=""):
    """Return Gmail's trusted received timestamp, falling back to Date header."""
    try:
        return datetime.fromtimestamp(
            int(message.get("internalDate")) / 1000,
            tz=timezone.utc,
        ).isoformat()
    except (TypeError, ValueError):
        return fallback


# Applied to every email once triage has processed it (in addition to its
# category label). Lets each run pick up where the previous one left off --
# unread emails already carrying this label are excluded from the next
# fetch, so processing the inbox in chunks (e.g. 200 at a time, run
# repeatedly) never reprocesses the same batch twice.
PROCESSED_LABEL = "AI-Processed"

# When a date is chosen (or defaulted to today), fetch that day plus this many
# previous days -- not just the single selected day.
# How many days back from the anchor date each range covers. "1d" = the anchor
# day; "1w" = the anchor day and the six days before it.
RANGE_DAYS = {"1d": 1, "1w": 7}
DEFAULT_RANGE = "1d"


def _unread_query(sort_range=None, date=None):
    """Build the Gmail search query for inbox mail not yet processed.

    Includes BOTH read and unread inbox mail (only messages already carrying
    PROCESSED_LABEL are excluded). The window ends on ``date`` (a "YYYY-MM-DD"
    string, or today if omitted) and reaches back by ``sort_range`` -- 1 day
    ("1d") or 1 week ("1w"). All matching mail in that window is fetched.
    """
    query = (
        f'in:inbox -label:"{PROCESSED_LABEL}" '
        '-subject:"Email Triage reminder: items need your attention"'
    )
    days = RANGE_DAYS.get(sort_range, RANGE_DAYS[DEFAULT_RANGE])
    anchor = None
    if date:
        try:
            anchor = datetime.strptime(date, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            anchor = None
    if anchor is None:
        anchor = datetime.now().date()
    end = anchor + timedelta(days=1)  # exclusive upper bound -> includes anchor day
    start = end - timedelta(days=days)
    return (
        f"{query} after:{start.strftime('%Y/%m/%d')} "
        f"before:{end.strftime('%Y/%m/%d')}"
    )


def fetch_unread_emails(service, max_results=400, sort_range=None, date=None):
    """Return a list of *not-yet-processed* inbox emails as dicts.

    "Not yet processed" means not already carrying PROCESSED_LABEL, so repeated
    calls (e.g. one run per chunk of the inbox) naturally advance through the
    inbox instead of refetching the same emails. ``sort_range`` ("1d"/"1w") and
    ``date`` set the window (see _unread_query).

    Each dict contains:
    {id, sender, subject, body, date, message_id_header, thread_id,
     in_reply_to, references, is_reply}.
    """
    query = _unread_query(sort_range, date)
    emails = []
    next_page_token = None

    while len(emails) < max_results:
        remaining = max_results - len(emails)
        response = (
            service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=min(remaining, 100),
                pageToken=next_page_token,
            )
            .execute()
        )

        messages = response.get("messages", [])
        for meta in messages:
            message = (
                service.users()
                .messages()
                .get(userId="me", id=meta["id"], format="full")
                .execute()
            )
            payload = message.get("payload", {})
            headers = payload.get("headers", [])

            in_reply_to = _get_header(headers, "In-Reply-To")
            references = _get_header(headers, "References")

            emails.append(
                {
                    "id": message.get("id", ""),
                    "sender": _get_header(headers, "From"),
                    "subject": _get_header(headers, "Subject"),
                    "body": _extract_body(payload),
                    "date": _internal_date(
                        message,
                        _parse_date(_get_header(headers, "Date")),
                    ),
                    "message_id_header": _get_header(headers, "Message-ID"),
                    "thread_id": message.get("threadId", ""),
                    "in_reply_to": in_reply_to,
                    "references": references,
                    # A mail that references an earlier message is a reply in an
                    # ongoing thread -- surfaced higher so it does not get buried.
                    "is_reply": bool(in_reply_to or references),
                }
            )

            if len(emails) >= max_results:
                break

        next_page_token = response.get("nextPageToken")
        if not next_page_token:
            break

    return emails


def fetch_email_by_id(service, gmail_id):
    """Fetch one Gmail message and return the fields used by summarization."""
    message = (
        service.users()
        .messages()
        .get(userId="me", id=gmail_id, format="full")
        .execute()
    )
    payload = message.get("payload", {})
    headers = payload.get("headers", [])
    return {
        "id": message.get("id", gmail_id),
        "thread_id": message.get("threadId", ""),
        "label_ids": message.get("labelIds", []),
        "sender": _get_header(headers, "From"),
        "subject": _get_header(headers, "Subject"),
        "body": _extract_body(payload),
    }


def thread_has_user_reply(service, thread_id):
    """Return True if the user has sent at least one message in this thread.

    A message the user sent carries the SENT label, so an active back-and-forth
    is detectable without reading bodies (metadata format only). Used to keep a
    genuine conversation from ever being filed as spam. Best-effort: returns
    False on any error or missing thread id.
    """
    if not thread_id:
        return False
    try:
        thread = (
            service.users()
            .threads()
            .get(userId="me", id=thread_id, format="metadata")
            .execute()
        )
    except Exception:  # noqa: BLE001 - conversation check is best-effort
        return False
    for message in thread.get("messages", []):
        if "SENT" in (message.get("labelIds") or []):
            return True
    return False


def unread_message_ids(service, gmail_ids):
    """Return the subset of ``gmail_ids`` whose message is still marked UNREAD.

    Used to hide alerts the user has already opened. Best-effort: a message that
    errors or no longer exists is treated as read (dropped from the result).
    """
    still_unread = set()
    for gmail_id in gmail_ids or []:
        if not gmail_id:
            continue
        try:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=gmail_id, format="minimal")
                .execute()
            )
        except Exception:  # noqa: BLE001 - read check is best-effort
            continue
        if "UNREAD" in (msg.get("labelIds") or []):
            still_unread.add(gmail_id)
    return still_unread


def send_reminder_email(service, recipient, subject, body):
    """Send a plain-text reminder from the connected Gmail account to itself."""
    message = EmailMessage()
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    return (
        service.users()
        .messages()
        .send(userId="me", body={"raw": raw})
        .execute()
    )


def count_unread_unprocessed(service, sort_range=None, date=None):
    """Return Gmail's estimate of how many not-yet-processed emails remain in
    the selected window (see _unread_query). Used to show a completion
    percentage during a run. Best-effort -- returns 0 on any error.
    """
    try:
        response = (
            service.users()
            .messages()
            .list(
                userId="me",
                q=_unread_query(sort_range, date),
                maxResults=1,
            )
            .execute()
        )
        return int(response.get("resultSizeEstimate", 0) or 0)
    except Exception:  # noqa: BLE001 - the progress estimate is best-effort
        return 0
