"""FastAPI web app wrapping the existing triage pipeline (multi-user).

Each connected Gmail account gets an isolated session, token (stored in the
database), and triage progress/results.

Routes:
- GET  /auth/login    -> redirect to the Google consent URL
- GET  /auth/callback -> exchange the auth code, store the user in the session
- POST /api/triage    -> process the whole inbox in the background
- GET  /api/triage/status -> progress of the current user's background run
- POST /api/triage/cancel -> request cancellation of a running triage
- GET  /api/summary   -> counts from the current user's last run (or null)
- GET  /api/auth/status -> whether the current user is connected
- POST /api/auth/logout -> clear the session (keeps the stored token)
- POST /api/triage/schedule -> schedule a one-time run at a future datetime
- GET  /api/triage/schedule -> the user's pending scheduled run (if any)
- POST /api/triage/schedule/cancel -> cancel the user's scheduled run
- GET  /api/settings/categories -> the user's category settings
- POST /api/settings/categories -> save the user's category settings
- GET  /api/search    -> semantic search over the user's processed emails
- GET  /              -> serve the static dashboard

Every /api route requires an authenticated session; unauthenticated requests
receive 401.

Security model:
- Authentication is Google OAuth2 (PKCE web flow) requesting the gmail.modify
  scope. The PKCE code_verifier is kept in the session, never exposed to the
  client, and consumed once at /auth/callback.
- The logged-in identity lives in a signed session cookie (SessionMiddleware).
  The signing key comes from SESSION_SECRET; if unset, a random per-process key
  is generated (no predictable hardcoded fallback). Cookies are same_site=lax
  (CSRF mitigation) and https_only in production (SESSION_HTTPS_ONLY).
- Per-user OAuth tokens are stored in SQLite (see app/db.py) and are sensitive;
  app.db is gitignored and must be access-restricted / encrypted at rest.
- Every /api route resolves the user from the session and returns 401 when
  absent, so one user can never act on another's mailbox. Request bodies
  (categories, dates) are validated before use.
"""

import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import SESSION_HTTPS_ONLY, SESSION_SECRET
from app.db import (
    get_user_settings,
    get_user_token,
    has_embeddings,
    init_db,
    save_user_settings,
)
from app.classifier import CLASSIFICATION_SUMMARY, category_definitions
from app.gmail_client import (
    RANGE_DAYS,
    authenticate_from_token_json,
    count_unread_unprocessed,
)
from app.main import triage_until_empty
from app.search import search_emails
from app.web_auth import exchange_code, login_url

logger = logging.getLogger("email_triage.server")

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

init_db()

# Background scheduler for optional periodic auto-processing. Started and
# stopped alongside the app via the lifespan handler below.
scheduler = BackgroundScheduler()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    scheduler.start()
    logger.info("Background scheduler started.")
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        logger.info("Background scheduler shut down.")


app = FastAPI(title="Email Triage Assistant", lifespan=lifespan)

_session_secret = SESSION_SECRET
if not _session_secret:
    # No hardcoded fallback: a predictable secret would let anyone forge signed
    # session cookies and impersonate a user. Generate a random per-process
    # secret instead. Sessions won't survive a restart (users simply re-login),
    # which is acceptable for development.
    logger.warning(
        "SESSION_SECRET is not set; generating a temporary random secret. "
        "Sessions will reset on restart -- set SESSION_SECRET in your .env for "
        "stable, secure sessions before deploying."
    )
    _session_secret = secrets.token_hex(32)

app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    # Not sent on cross-site requests -> basic CSRF mitigation.
    same_site="lax",
    # Only sent over HTTPS in production (off by default for local http dev).
    https_only=SESSION_HTTPS_ONLY,
)

# Per-user triage progress, keyed by email. Each value is a dict shaped like:
# {"status": "idle"|"running"|"done"|"error", "counts": None|dict, "error": None|str}
_progress_by_user: dict[str, dict] = {}

# Per-user one-time scheduled triage runs:
# email -> {"job_id": str, "run_at": str (ISO 8601)}.
_scheduled_runs_by_user: dict[str, dict] = {}

# Per-user cancel flags: email -> True when a running triage should stop.
_cancel_by_user: dict[str, bool] = {}


def _default_progress():
    return {
        "status": "idle",
        "counts": None,
        "error": None,
        "percent": 0,
        "first_chunk_done": False,
    }


def _parse_future_datetime(value):
    """Parse an ISO 8601 string into a future datetime, or return None.

    Returns None if the value is missing, unparseable, or not in the future.
    """
    if not isinstance(value, str) or not value.strip():
        return None

    text = value.strip()
    # datetime.fromisoformat only accepts a trailing 'Z' on Python 3.11+; map it
    # to +00:00 so UTC timestamps parse on older versions too.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    now = datetime.now(parsed.tzinfo) if parsed.tzinfo else datetime.now()
    # Small grace period so picking "today, right now" doesn't get rejected
    # just because a couple of minutes passed between the user's click and this
    # request reaching the server.
    if parsed <= now - timedelta(minutes=2):
        return None
    return parsed


async def _read_run_opts(request):
    """Read the sort range ("1d"/"1w") and optional anchor date from the body."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - no/invalid body just means defaults
        body = {}
    if not isinstance(body, dict):
        body = {}
    sort_range = body.get("range")
    if sort_range not in RANGE_DAYS:
        sort_range = "1d"
    date = body.get("date")
    date = date.strip() if isinstance(date, str) and date.strip() else None
    return sort_range, date


def current_user_email(request):
    """Return the logged-in user's email from the session, or None."""
    return request.session.get("user_email")


def _unauthenticated():
    return JSONResponse({"error": "Not authenticated"}, status_code=401)


def _service_for_user(email):
    """Build a Gmail service for a user from their stored token, or None."""
    token_json = get_user_token(email)
    if token_json is None:
        return None
    return authenticate_from_token_json(token_json)


@app.get("/auth/login")
def auth_login(request: Request):
    """Redirect the user to Google's OAuth consent screen.

    The PKCE code_verifier generated for this login attempt is stashed in the
    session so /auth/callback can retrieve it -- it cannot be recovered from
    the auth code alone.
    """
    auth_url, state, code_verifier = login_url()
    request.session["oauth_state"] = state
    request.session["oauth_code_verifier"] = code_verifier
    return RedirectResponse(auth_url)


@app.get("/auth/callback")
def auth_callback(request: Request):
    """Handle the OAuth redirect: exchange the code, store the user, go home.

    Any failure (user denied, expired/duplicate login attempt, PKCE mismatch)
    redirects back to the dashboard with an error flag instead of returning a
    500, so the user can simply try connecting again.
    """
    if request.query_params.get("error"):
        return RedirectResponse("/?auth_error=1")

    code = request.query_params.get("code")
    returned_state = request.query_params.get("state")
    expected_state = request.session.pop("oauth_state", None)
    code_verifier = request.session.pop("oauth_code_verifier", None)

    if not code or not code_verifier:
        return RedirectResponse("/?auth_error=1")

    # A newer login attempt replaced this one's verifier -> the codes won't
    # match. Ask the user to retry cleanly rather than failing cryptically.
    if expected_state and returned_state and expected_state != returned_state:
        return RedirectResponse("/?auth_error=1")

    try:
        email = exchange_code(code, code_verifier)
    except Exception:  # noqa: BLE001 - surface as a friendly retry, not a 500
        logger.exception("OAuth token exchange failed.")
        return RedirectResponse("/?auth_error=1")

    request.session["user_email"] = email
    return RedirectResponse("/")


@app.post("/api/triage")
async def api_triage(request: Request, background_tasks: BackgroundTasks):
    """Start a triage run in the background for the current user.

    Optional JSON body {"date": "YYYY-MM-DD"} restricts the run to a single
    day's emails (used for "today" or a past date). Returns 401 if not
    authenticated, 409 if a run is already in progress. Poll /api/triage/status
    for progress and the final counts.
    """
    email = current_user_email(request)
    if not email:
        return _unauthenticated()

    service = _service_for_user(email)
    if service is None:
        return _unauthenticated()

    sort_range, date = await _read_run_opts(request)

    progress = _progress_by_user.setdefault(email, _default_progress())
    if progress["status"] == "running":
        return JSONResponse(
            {"status": "running", "error": "A triage run is already in progress."},
            status_code=409,
        )

    progress.update(status="running", counts=None, error=None, percent=0, first_chunk_done=False)
    background_tasks.add_task(_run_triage, email, service, sort_range, date)
    return JSONResponse({"status": "started"})


def _run_triage(email, service, sort_range="1d", date=None):
    """Run the full chained triage for one user and record the outcome."""
    progress = _progress_by_user.setdefault(email, _default_progress())
    _cancel_by_user[email] = False
    total = count_unread_unprocessed(service, sort_range, date)
    progress.update(
        status="running",
        counts=None,
        error=None,
        percent=0,
        first_chunk_done=False,
    )

    def report(units):
        current = _progress_by_user.get(email)
        if current is None:
            return
        # Cap at 99% while running -- 100% is reserved for a fully caught-up
        # inbox (set on completion), so it never reads 100% while work remains.
        current["percent"] = min(99, int(units / total * 100)) if total else 0

    def should_cancel():
        return _cancel_by_user.get(email, False)

    def on_chunk_done(chunk_index, processed):
        current = _progress_by_user.get(email)
        if current is not None and processed > 0:
            # The first chunk's emails are now embedded -> search can be used.
            current["first_chunk_done"] = True

    try:
        settings = get_user_settings(email)
        counts = triage_until_empty(
            service,
            categories=settings["categories"],
            faq_category=settings["faq_category"],
            user_email=email,
            progress_cb=report,
            should_cancel=should_cancel,
            on_chunk_done=on_chunk_done,
            sort_range=sort_range,
            date=date,
        )
        if _cancel_by_user.get(email, False):
            progress.update(status="cancelled", counts=counts, error=None)
        else:
            progress.update(
                status="done",
                counts=counts,
                error=None,
                percent=100,
                first_chunk_done=True,
            )
    except Exception:  # noqa: BLE001 - surface failure via status endpoint
        logger.exception("Background triage run failed for %s.", email)
        progress.update(
            status="error",
            counts=None,
            error="Triage failed due to an internal error. Please try again.",
        )
    finally:
        _cancel_by_user.pop(email, None)


def _run_scheduled_triage(email, sort_range="1d"):
    """APScheduler entry point for a one-time scheduled run.

    Re-fetches the user's Gmail service (the token may need refreshing) and runs
    the same triage as a manual run, for the given ``sort_range``.
    """
    # The job is firing now; clear the pending-schedule record.
    _scheduled_runs_by_user.pop(email, None)

    progress = _progress_by_user.setdefault(email, _default_progress())
    if progress["status"] == "running":
        logger.info(
            "Scheduled run for %s skipped; a run is already in progress.", email
        )
        return

    service = _service_for_user(email)
    if service is None:
        logger.warning(
            "Scheduled run for %s skipped: no valid stored token.", email
        )
        return

    _run_triage(email, service, sort_range)


def _cancel_scheduled_run(email):
    """Remove a user's pending scheduled job. Returns True if one existed."""
    entry = _scheduled_runs_by_user.pop(email, None)
    if not entry:
        return False
    try:
        scheduler.remove_job(entry["job_id"])
    except JobLookupError:
        pass
    return True


@app.get("/api/triage/status")
def api_triage_status(request: Request):
    """Return the current user's background triage progress."""
    email = current_user_email(request)
    if not email:
        return _unauthenticated()
    progress = _progress_by_user.get(email, _default_progress())
    return JSONResponse(progress)


@app.post("/api/triage/cancel")
def api_triage_cancel(request: Request):
    """Request cancellation of the current user's running triage.

    Cancellation is cooperative: the background run stops at the next email/chunk
    boundary and keeps whatever it has already labeled.
    """
    email = current_user_email(request)
    if not email:
        return _unauthenticated()
    progress = _progress_by_user.get(email)
    if not progress or progress.get("status") != "running":
        return JSONResponse({"status": "not_running"})
    _cancel_by_user[email] = True
    return JSONResponse({"status": "cancelling"})


@app.get("/api/summary")
def api_summary(request: Request):
    """Return counts from the current user's last triage run, or null."""
    email = current_user_email(request)
    if not email:
        return _unauthenticated()
    progress = _progress_by_user.get(email)
    counts = progress["counts"] if progress else None
    return JSONResponse({"counts": counts})


@app.get("/api/auth/status")
def api_auth_status(request: Request):
    """Report whether the current user is authenticated and connected."""
    email = current_user_email(request)
    if not email:
        return _unauthenticated()
    if get_user_token(email) is None:
        return _unauthenticated()
    return JSONResponse(
        {
            "authenticated": True,
            "email": email,
            "has_search_data": has_embeddings(email),
        }
    )


@app.post("/api/auth/logout")
def api_logout(request: Request):
    """Clear the session. Keeps the stored token so the user can reconnect."""
    request.session.clear()
    return JSONResponse({"status": "logged_out"})


@app.get("/api/labels/guide")
def api_labels_guide(request: Request):
    """Return the labels this tool applies, each with a definition, plus a short
    explanation of how emails are sorted into them."""
    email = current_user_email(request)
    if not email:
        return _unauthenticated()
    settings = get_user_settings(email)
    definitions = category_definitions()
    labels = [
        {
            "name": category,
            "description": definitions.get(category)
            or "This is your own category, so mail is sorted into it based on its name.",
        }
        for category in settings["categories"]
    ]
    return JSONResponse(
        {
            "labels": labels,
            "how": CLASSIFICATION_SUMMARY,
            "faq_category": settings["faq_category"],
        }
    )


@app.post("/api/triage/schedule")
async def api_triage_schedule(request: Request):
    """Schedule a one-time full triage run at a future datetime.

    Body: {"run_at": "<ISO 8601 datetime>"}. Requires authentication. Returns
    400 if run_at is missing, unparseable, or not in the future.
    """
    email = current_user_email(request)
    if not email:
        return _unauthenticated()
    if get_user_token(email) is None:
        return _unauthenticated()

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - handled as a validation error below
        body = {}
    if not isinstance(body, dict):
        body = {}

    run_at = _parse_future_datetime(body.get("run_at"))
    if run_at is None:
        return JSONResponse(
            {"error": "run_at must be a valid, future ISO 8601 datetime."},
            status_code=400,
        )

    sort_range = body.get("range")
    if sort_range not in RANGE_DAYS:
        sort_range = "1d"

    # Replace any existing scheduled run for this user.
    _cancel_scheduled_run(email)

    job_id = f"triage-scheduled-{email}"
    scheduler.add_job(
        _run_scheduled_triage,
        trigger="date",
        run_date=run_at,
        args=[email, sort_range],
        id=job_id,
        replace_existing=True,
    )
    run_at_iso = run_at.isoformat()
    _scheduled_runs_by_user[email] = {"job_id": job_id, "run_at": run_at_iso}

    return JSONResponse({"status": "scheduled", "run_at": run_at_iso})


@app.get("/api/triage/schedule")
def api_triage_schedule_status(request: Request):
    """Return the current user's pending scheduled run_at (ISO), or null."""
    email = current_user_email(request)
    if not email:
        return _unauthenticated()
    entry = _scheduled_runs_by_user.get(email)
    return JSONResponse({"run_at": entry["run_at"] if entry else None})


@app.post("/api/triage/schedule/cancel")
def api_triage_schedule_cancel(request: Request):
    """Cancel the current user's pending scheduled run, if any."""
    email = current_user_email(request)
    if not email:
        return _unauthenticated()
    cancelled = _cancel_scheduled_run(email)
    return JSONResponse({"status": "cancelled" if cancelled else "none"})


@app.get("/api/settings/categories")
def api_get_categories(request: Request):
    """Return the current user's category settings (or defaults)."""
    email = current_user_email(request)
    if not email:
        return _unauthenticated()
    return JSONResponse(get_user_settings(email))


@app.post("/api/settings/categories")
async def api_save_categories(request: Request):
    """Save the current user's category settings.

    Body: {"categories": [str, ...], "faq_category": str | null}. Validates that
    categories is a non-empty list of non-empty strings and that faq_category,
    if provided, is one of them. Requires authentication.
    """
    email = current_user_email(request)
    if not email:
        return _unauthenticated()
    if get_user_token(email) is None:
        return _unauthenticated()

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - handled as a validation error below
        body = {}
    if not isinstance(body, dict):
        body = {}

    categories = body.get("categories")
    if not isinstance(categories, list) or not categories:
        return JSONResponse(
            {"error": "categories must be a non-empty list."}, status_code=400
        )

    cleaned = []
    for item in categories:
        if not isinstance(item, str) or not item.strip():
            return JSONResponse(
                {"error": "each category must be a non-empty string."},
                status_code=400,
            )
        cleaned.append(item.strip())

    faq_category = body.get("faq_category")
    if faq_category is not None:
        if not isinstance(faq_category, str) or faq_category.strip() not in cleaned:
            return JSONResponse(
                {"error": "faq_category must be one of the categories or null."},
                status_code=400,
            )
        faq_category = faq_category.strip()

    save_user_settings(email, cleaned, faq_category)
    return JSONResponse({"categories": cleaned, "faq_category": faq_category})


@app.get("/api/search")
def api_search(request: Request, q: str = "", limit: int = 10):
    """Semantic search over the current user's processed emails.

    Returns the top matches by meaning, each with a gmail_url that deep-links to
    the thread in the connected account. Requires authentication.
    """
    email = current_user_email(request)
    if not email:
        return _unauthenticated()

    results = search_emails(email, q, top_k=limit)
    for result in results:
        thread_id = result.get("thread_id") or ""
        result["gmail_url"] = (
            f"https://mail.google.com/mail/?authuser={email}#all/{thread_id}"
        )
    return JSONResponse({"results": results})


@app.get("/")
def index():
    """Serve the dashboard."""
    return FileResponse(STATIC_DIR / "index.html")


# Serve static assets (app.js, etc.) under /static.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
