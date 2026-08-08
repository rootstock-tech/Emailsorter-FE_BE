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

import base64
import json
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
from starlette.concurrency import run_in_threadpool

from app.config import (
    ADDON_SHARED_SECRET,
    GMAIL_PUBSUB_TOPIC,
    PUSH_AUTH_TOKEN,
    REMINDER_EMAILS_ENABLED,
    SESSION_HTTPS_ONLY,
    SESSION_SECRET,
    validate_production_config,
)
from app.db import (
    delete_learned_rule,
    delete_priority,
    clear_priority,
    get_auto_triage,
    get_priority_item,
    get_run_actions,
    get_user_settings,
    get_user_token,
    get_watch_state,
    has_embeddings,
    init_db,
    last_undoable_run,
    list_auto_triage,
    list_known_contacts,
    list_learned_rules,
    list_user_emails,
    mark_run_undone,
    record_user_correction,
    remember_contact,
    save_priority,
    save_user_token,
    save_user_settings,
    save_watch_state,
    set_auto_triage,
    set_rule_active,
    start_triage_run,
    top_priority,
    upcoming_deadlines,
)
from app.summarize import summarize_email
from app.classifier import CLASSIFICATION_SUMMARY, category_definitions
from app.commands import execute_command, parse_command, preview_command
from app.labeler import apply_label, get_or_create_label, remove_label
from app.gmail_client import (
    PROCESSED_LABEL,
    RANGE_DAYS,
    authenticate_from_token_json,
    count_unread_unprocessed,
    fetch_email_by_id,
    start_watch,
    unread_message_ids,
)
from app.main import triage_until_empty
from app.priority import compute_priority
from app.reminders import send_user_reminders
from app.search import search_emails
from app.web_auth import exchange_code, login_url

logger = logging.getLogger("email_triage.server")

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

validate_production_config()
init_db()

# Background scheduler for optional periodic auto-processing. Started and
# stopped alongside the app via the lifespan handler below.
scheduler = BackgroundScheduler()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    scheduler.start()
    logger.info("Background scheduler started.")
    # Gmail push watches expire (~7 days); renew everyone's daily when enabled.
    if GMAIL_PUBSUB_TOPIC:
        scheduler.add_job(
            _renew_all_watches,
            "interval",
            hours=24,
            id="gmail_watch_renewal",
            replace_existing=True,
        )
    if REMINDER_EMAILS_ENABLED:
        scheduler.add_job(
            _send_all_reminders,
            "interval",
            hours=3,
            id="email_reminders",
            replace_existing=True,
        )
    # Restore recurring auto-triage jobs saved by users across restarts.
    for email, minutes in list_auto_triage():
        _register_recurring(email, minutes)
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
    return authenticate_from_token_json(
        token_json,
        on_refresh=lambda refreshed: save_user_token(email, refreshed),
    )


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

    if not code or not code_verifier or not expected_state or not returned_state:
        return RedirectResponse("/?auth_error=1")

    if not secrets.compare_digest(expected_state, returned_state):
        return RedirectResponse("/?auth_error=1")

    try:
        email = exchange_code(code, code_verifier)
    except Exception:  # noqa: BLE001 - surface as a friendly retry, not a 500
        logger.exception("OAuth token exchange failed.")
        return RedirectResponse("/?auth_error=1")

    request.session["user_email"] = email
    # Enable real-time push for this user (best-effort; no-op if push disabled).
    _register_watch(email)
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
    if date:
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            return JSONResponse({"error": "date must be YYYY-MM-DD"}, status_code=400)

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

    previous_priority = top_priority(email, 10000)

    def restore_previous_priority():
        try:
            clear_priority(email)
            for item in previous_priority:
                save_priority(
                    email,
                    item.get("gmail_id"),
                    item.get("thread_id"),
                    item.get("sender"),
                    item.get("subject"),
                    item.get("category"),
                    item.get("score"),
                    item.get("reason"),
                    item.get("date"),
                )
        except Exception:  # noqa: BLE001 - keep original triage outcome
            logger.exception("Could not restore priority snapshot for %s.", email)

    try:
        settings = get_user_settings(email)
        # Preserve prior priority rows so unread 24-hour reminder candidates
        # survive frequent auto-triage runs; new results upsert by Gmail id.
        run_id = secrets.token_hex(8)
        try:
            start_triage_run(run_id, email)
        except Exception:  # noqa: BLE001 - undo/priority are best-effort
            run_id = None
        counts = triage_until_empty(
            service,
            categories=settings["categories"],
            faq_category=settings["faq_category"],
            category_prompts=settings.get("category_prompts"),
            user_email=email,
            progress_cb=report,
            should_cancel=should_cancel,
            on_chunk_done=on_chunk_done,
            sort_range=sort_range,
            date=date,
            run_id=run_id,
        )
        if _cancel_by_user.get(email, False):
            restore_previous_priority()
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
        restore_previous_priority()
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


# Recurring auto-triage: intervals (minutes) users may pick.
_AUTO_INTERVALS = {5, 10, 15, 30, 60}


def _run_recurring_triage(email):
    """APScheduler entry point for recurring auto-triage (fires every N min)."""
    progress = _progress_by_user.setdefault(email, _default_progress())
    if progress["status"] == "running":
        return  # a manual/scheduled run is active; skip this tick
    service = _service_for_user(email)
    if service is None:
        logger.warning("Recurring run for %s skipped: no valid stored token.", email)
        return
    _run_triage(email, service, "1d")


def _register_recurring(email, minutes):
    """(Re)register a user's recurring auto-triage interval job."""
    scheduler.add_job(
        _run_recurring_triage,
        trigger="interval",
        minutes=int(minutes),
        args=[email],
        id=f"triage-recurring-{email}",
        replace_existing=True,
    )


def _unregister_recurring(email):
    """Remove a user's recurring auto-triage job, if any."""
    try:
        scheduler.remove_job(f"triage-recurring-{email}")
    except JobLookupError:
        pass


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
    prompts = settings.get("category_prompts") or {}
    labels = [
        {
            "name": category,
            "description": (prompts.get(category) or "").strip()
            or definitions.get(category)
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


@app.get("/api/triage/auto")
def api_get_auto_triage(request: Request):
    """Return the user's recurring auto-triage interval in minutes, or null."""
    email = current_user_email(request)
    if not email:
        return _unauthenticated()
    return JSONResponse({"interval_minutes": get_auto_triage(email)})


@app.post("/api/triage/auto")
async def api_set_auto_triage(request: Request):
    """Turn recurring auto-triage on/off for the current user.

    Body: {"interval_minutes": int|null}. A positive allowed value enables it and
    the server re-triages the inbox on that interval; null or 0 disables it.
    """
    email = current_user_email(request)
    if not email:
        return _unauthenticated()
    if get_user_token(email) is None:
        return _unauthenticated()

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - handled as validation below
        body = {}
    if not isinstance(body, dict):
        body = {}

    minutes = body.get("interval_minutes")
    if minutes in (None, 0, "0", ""):
        set_auto_triage(email, None)
        _unregister_recurring(email)
        return JSONResponse({"interval_minutes": None})

    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return JSONResponse({"error": "interval_minutes must be an integer."}, status_code=400)
    if minutes not in _AUTO_INTERVALS:
        return JSONResponse(
            {"error": f"interval_minutes must be one of {sorted(_AUTO_INTERVALS)}."},
            status_code=400,
        )

    set_auto_triage(email, minutes)
    _register_recurring(email, minutes)
    return JSONResponse({"interval_minutes": minutes})


# --- Gmail Add-on (Apps Script) endpoints ---------------------------------
#
# The add-on runs on Google's servers and calls these with a shared secret in
# the X-Addon-Secret header. Each call names the user by email; that user must
# have connected once via the web OAuth login (so a token is stored).


def _addon_authorized(request):
    """True only if the request carries the configured add-on shared secret."""
    secret = request.headers.get("x-addon-secret") or ""
    return bool(ADDON_SHARED_SECRET) and secret == ADDON_SHARED_SECRET


@app.post("/api/addon/triage")
async def api_addon_triage(request: Request, background_tasks: BackgroundTasks):
    """Start a triage run for a user, called by the Gmail Add-on."""
    if not _addon_authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    email = (body or {}).get("email")
    if not email or get_user_token(email) is None:
        return JSONResponse({"error": "user not connected"}, status_code=404)

    progress = _progress_by_user.setdefault(email, _default_progress())
    if progress["status"] == "running":
        return JSONResponse({"status": "running"})
    service = _service_for_user(email)
    if service is None:
        return JSONResponse({"error": "user not connected"}, status_code=404)
    sort_range = (body or {}).get("range")
    if sort_range not in RANGE_DAYS:
        sort_range = "1d"
    progress.update(status="running", counts=None, error=None, percent=0, first_chunk_done=False)
    background_tasks.add_task(_run_triage, email, service, sort_range, None)
    return JSONResponse({"status": "started"})


@app.get("/api/addon/status")
def api_addon_status(request: Request):
    """Return triage status + auto-sort interval for a user (add-on)."""
    if not _addon_authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    email = request.query_params.get("email")
    if not email:
        return JSONResponse({"error": "email required"}, status_code=400)
    progress = _progress_by_user.get(email) or _default_progress()
    return JSONResponse(
        {
            "connected": get_user_token(email) is not None,
            "status": progress.get("status"),
            "percent": progress.get("percent"),
            "counts": progress.get("counts"),
            "interval_minutes": get_auto_triage(email),
        }
    )


@app.post("/api/addon/auto")
async def api_addon_auto(request: Request):
    """Set recurring auto-triage interval for a user (add-on)."""
    if not _addon_authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    email = (body or {}).get("email")
    if not email or get_user_token(email) is None:
        return JSONResponse({"error": "user not connected"}, status_code=404)
    minutes = (body or {}).get("interval_minutes")
    if minutes in (None, 0, "0", ""):
        set_auto_triage(email, None)
        _unregister_recurring(email)
        return JSONResponse({"interval_minutes": None})
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return JSONResponse({"error": "interval_minutes must be an integer."}, status_code=400)
    if minutes not in _AUTO_INTERVALS:
        return JSONResponse({"error": "invalid interval."}, status_code=400)
    set_auto_triage(email, minutes)
    _register_recurring(email, minutes)
    return JSONResponse({"interval_minutes": minutes})


@app.get("/api/addon/digest")
def api_addon_digest(request: Request):
    """Return add-on counts, unread alerts, deadlines, learning, and undo state."""
    if not _addon_authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    email = request.query_params.get("email")
    if not email or get_user_token(email) is None:
        return JSONResponse({"error": "user not connected"}, status_code=404)
    progress = _progress_by_user.get(email)
    counts = progress["counts"] if progress else None
    rules = list_learned_rules(email)
    active_rules = sum(1 for r in rules if r.get("active"))
    alerts = _addon_alert_items(email, limit=3)
    deadlines = _with_gmail_url(upcoming_deadlines(email, limit=3), email)
    run = last_undoable_run(email)
    return JSONResponse(
        {
            "counts": counts,
            "alerts": alerts,
            "deadlines": deadlines,
            "learned_active": active_rules,
            "learned_total": len(rules),
            "undo_count": run["action_count"] if run else 0,
        }
    )


def _addon_alert_items(email, limit=3):
    alert_cats = {"red flag", "needs action"}
    candidates = [
        item
        for item in top_priority(email, 50)
        if (item.get("category") or "").lower() in alert_cats
    ]
    service = _service_for_user(email)
    if service is None or not candidates:
        return []
    unread = unread_message_ids(service, [item.get("gmail_id") for item in candidates])
    return _with_gmail_url(
        [item for item in candidates if item.get("gmail_id") in unread][:limit],
        email,
    )


@app.get("/api/addon/alerts")
def api_addon_alerts(request: Request, limit: int = 3):
    """Return still-unread Needs Action and Red Flag mail for the add-on."""
    if not _addon_authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    email = request.query_params.get("email")
    if not email or get_user_token(email) is None:
        return JSONResponse({"error": "user not connected"}, status_code=404)
    return JSONResponse({"alerts": _addon_alert_items(email, max(1, min(limit, 20)))})


@app.post("/api/addon/summarize")
async def api_addon_summarize(request: Request):
    """Summarise one mail into a few bullets for the Gmail Add-on."""
    if not _addon_authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    subject = (body or {}).get("subject") or ""
    mail_body = (body or {}).get("body") or ""
    sender = (body or {}).get("sender") or ""
    gmail_id = (body or {}).get("gmail_id")
    email = (body or {}).get("email")
    if gmail_id:
        if not email or get_user_token(email) is None:
            return JSONResponse({"error": "user not connected"}, status_code=404)
        service = _service_for_user(email)
        if service is None:
            return JSONResponse({"error": "user not connected"}, status_code=404)
        try:
            message = await run_in_threadpool(fetch_email_by_id, service, gmail_id)
        except Exception:  # noqa: BLE001 - return a safe add-on error
            logger.exception("Could not fetch message %s for summarization.", gmail_id)
            return JSONResponse({"error": "message unavailable"}, status_code=404)
        subject = message.get("subject") or ""
        mail_body = message.get("body") or ""
        sender = message.get("sender") or ""
    if not subject and not mail_body:
        return JSONResponse({"error": "nothing to summarize"}, status_code=400)
    summary = await run_in_threadpool(summarize_email, subject, mail_body, sender)
    if summary is None:
        return JSONResponse({"error": "summarize unavailable"}, status_code=503)
    return JSONResponse({"summary": summary})


@app.post("/api/addon/undo")
async def api_addon_undo(request: Request):
    """Undo the most recent triage run for a user (add-on)."""
    if not _addon_authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    email = (body or {}).get("email")
    if not email or get_user_token(email) is None:
        return JSONResponse({"error": "user not connected"}, status_code=404)
    service = _service_for_user(email)
    if service is None:
        return JSONResponse({"error": "user not connected"}, status_code=404)
    return JSONResponse(_perform_undo(email, service))


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

    # Optional per-category prompts: {name: description}. Keep only non-empty
    # descriptions for categories that were actually saved.
    raw_prompts = body.get("category_prompts")
    category_prompts = {}
    if raw_prompts is not None:
        if not isinstance(raw_prompts, dict):
            return JSONResponse(
                {"error": "category_prompts must be an object."}, status_code=400
            )
        for name, prompt in raw_prompts.items():
            if not isinstance(prompt, str):
                return JSONResponse(
                    {"error": "each category prompt must be a string."},
                    status_code=400,
                )
            name = name.strip()
            prompt = prompt.strip()
            if name in cleaned and prompt:
                category_prompts[name] = prompt

    save_user_settings(email, cleaned, faq_category, category_prompts)
    return JSONResponse(
        {
            "categories": cleaned,
            "faq_category": faq_category,
            "category_prompts": category_prompts,
        }
    )


@app.get("/api/rules/learned")
def api_get_learned_rules(request: Request):
    """Return the user's learned rules (sender/domain -> category mappings)."""
    email = current_user_email(request)
    if not email:
        return _unauthenticated()
    return JSONResponse({"rules": list_learned_rules(email)})

@app.post("/api/rules/learned")
async def api_update_learned_rule(request: Request):
    """Enable, disable, or delete a single learned rule.

    Body: {match_type, match_value, category, action: "enable"|"disable"|"delete"}.
    """
    email = current_user_email(request)
    if not email:
        return _unauthenticated()
    if get_user_token(email) is None:
        return _unauthenticated()

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - handled as validation below
        body = {}
    if not isinstance(body, dict):
        body = {}

    match_type = body.get("match_type")
    match_value = body.get("match_value")
    category = body.get("category")
    action = body.get("action")
    if match_type not in ("sender", "domain") or not isinstance(match_value, str) or not isinstance(category, str):
        return JSONResponse({"error": "invalid rule identifier."}, status_code=400)
    if action not in ("enable", "disable", "delete"):
        return JSONResponse({"error": "action must be enable, disable, or delete."}, status_code=400)

    if action == "delete":
        delete_learned_rule(email, match_type, match_value, category)
    else:
        set_rule_active(email, match_type, match_value, category, action == "enable")
    return JSONResponse({"rules": list_learned_rules(email)})


@app.post("/api/feedback")
async def api_feedback(request: Request):
    """Learn from a manual relabel: force the sender into the chosen category.

    Body: {sender, subject, old_category, category}. The correction becomes an
    active weighted rule at once.
    """
    email = current_user_email(request)
    if not email:
        return _unauthenticated()
    if get_user_token(email) is None:
        return _unauthenticated()

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - handled as validation below
        body = {}
    if not isinstance(body, dict):
        body = {}

    sender = body.get("sender")
    category = body.get("category")
    gmail_id = body.get("gmail_id")
    subject = body.get("subject") if isinstance(body.get("subject"), str) else ""
    old_category = body.get("old_category")
    if not isinstance(sender, str) or not sender.strip():
        return JSONResponse({"error": "sender is required."}, status_code=400)
    if not isinstance(category, str) or not category.strip():
        return JSONResponse({"error": "category is required."}, status_code=400)

    settings = get_user_settings(email)
    category = category.strip()
    if category not in settings["categories"]:
        return JSONResponse({"error": "category is not configured."}, status_code=400)

    if isinstance(gmail_id, str) and gmail_id:
        item = get_priority_item(email, gmail_id)
        if item is None:
            return JSONResponse({"error": "email is no longer in the priority list."}, status_code=404)
        service = _service_for_user(email)
        if service is None:
            return _unauthenticated()
        old_category = item.get("category")
        try:
            new_label_id = get_or_create_label(service, category, account_key=email)
            apply_label(service, gmail_id, new_label_id)
            if old_category and old_category != category:
                old_label_id = get_or_create_label(service, old_category, account_key=email)
                remove_label(service, gmail_id, old_label_id)
            score, reason = compute_priority(
                {
                    "sender": item.get("sender"),
                    "subject": item.get("subject"),
                    "date": item.get("date"),
                },
                category,
            )
            save_priority(
                email,
                gmail_id,
                item.get("thread_id"),
                item.get("sender"),
                item.get("subject"),
                category,
                score,
                reason,
                item.get("date"),
            )
        except Exception:  # noqa: BLE001 - surface relabel failure to the UI
            logger.exception("Could not apply feedback label for %s.", gmail_id)
            return JSONResponse({"error": "Could not relabel this email."}, status_code=500)

    record_user_correction(
        email,
        sender,
        category,
        subject=subject,
        old_category=old_category if isinstance(old_category, str) else None,
    )
    # A correction out of spam also means this is someone the user deals with,
    # so remember them as a contact (never spam going forward).
    if "spam" not in category.lower() and "newsletter" not in category.lower():
        remember_contact(email, sender, reason="you moved their mail out of spam")
    return JSONResponse({"rules": list_learned_rules(email)})


# --- Priority inbox --------------------------------------------------------


def _with_gmail_url(items, email):
    """Attach a deep-link to each item's Gmail thread for the connected account."""
    for item in items:
        thread_id = item.get("thread_id") or ""
        item["gmail_url"] = (
            f"https://mail.google.com/mail/?authuser={email}#all/{thread_id}"
        )
    return items


@app.get("/api/priority")
def api_priority(request: Request, limit: int = 15):
    """Return the current user's highest-priority emails (most important first)."""
    email = current_user_email(request)
    if not email:
        return _unauthenticated()
    items = _with_gmail_url(top_priority(email, limit), email)
    return JSONResponse({"items": items})


# --- Undo last run ---------------------------------------------------------


@app.get("/api/undo")
def api_undo_status(request: Request):
    """Report whether the current user has a triage run that can be undone."""
    email = current_user_email(request)
    if not email:
        return _unauthenticated()
    return JSONResponse({"run": last_undoable_run(email)})


@app.post("/api/undo")
def api_undo(request: Request):
    """Undo the most recent triage run: remove the labels it applied.

    Also removes the internal AI-Processed marker on affected mail so it can be
    sorted again, and clears those emails from the priority list.
    """
    email = current_user_email(request)
    if not email:
        return _unauthenticated()
    service = _service_for_user(email)
    if service is None:
        return _unauthenticated()
    return JSONResponse(_perform_undo(email, service))


def _perform_undo(email, service):
    """Core undo: revert the last run's labels for a user. Returns a result dict."""
    run = last_undoable_run(email)
    if not run:
        return {"status": "none"}

    actions = get_run_actions(run["run_id"])
    processed_label_id = None
    try:
        processed_label_id = get_or_create_label(
            service, PROCESSED_LABEL, hidden=True, account_key=email
        )
    except Exception:  # noqa: BLE001 - keep undoing category labels regardless
        processed_label_id = None

    affected = set()
    failures = []
    for action in actions:
        gid = action.get("gmail_id")
        label_id = action.get("label_id")
        if not gid or not label_id:
            failures.append(gid or "unknown")
            continue
        try:
            remove_label(service, gid, label_id)
            affected.add(gid)
        except Exception:  # noqa: BLE001 - one failure must not stop the rest
            logger.warning("Undo: could not remove label from %s", gid)
            failures.append(gid)

    # Remove the hidden processed marker so undone mail can be re-sorted.
    if processed_label_id is None and affected:
        failures.extend(affected)
    elif processed_label_id is not None:
        for gid in affected:
            try:
                remove_label(service, gid, processed_label_id)
            except Exception:  # noqa: BLE001 - best-effort
                failures.append(gid)

    delete_priority(email, list(affected))
    if failures:
        return {
            "status": "partial",
            "count": len(affected),
            "failed": len(set(failures)),
            "error": "Some labels could not be reverted. Retry undo.",
        }
    mark_run_undone(run["run_id"])
    return {"status": "undone", "count": len(affected)}


# --- Natural-language commands ---------------------------------------------


@app.post("/api/command/preview")
async def api_command_preview(request: Request):
    """Parse a plain-English instruction and preview what it would affect."""
    email = current_user_email(request)
    if not email:
        return _unauthenticated()
    service = _service_for_user(email)
    if service is None:
        return _unauthenticated()

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    text = (body or {}).get("text") if isinstance(body, dict) else None

    parsed = parse_command(text)
    if parsed.get("error"):
        return JSONResponse({"error": parsed["error"]}, status_code=400)

    try:
        preview = preview_command(service, parsed)
    except Exception:  # noqa: BLE001 - surface a friendly error
        logger.exception("Command preview failed for %s.", email)
        return JSONResponse({"error": "Could not search your mail. Try again."}, status_code=500)

    request.session["command_preview"] = {
        "parsed": parsed,
        "ids": preview["ids"],
        "created_at": datetime.now().timestamp(),
    }
    return JSONResponse(
        {
            "action": parsed["action"],
            "label": parsed.get("label"),
            "summary": parsed.get("summary"),
            "count": preview["count"],
            "samples": preview["samples"],
        }
    )


@app.post("/api/command/execute")
async def api_command_execute(request: Request):
    """Execute a previously previewed plain-English instruction."""
    email = current_user_email(request)
    if not email:
        return _unauthenticated()
    service = _service_for_user(email)
    if service is None:
        return _unauthenticated()

    plan = request.session.pop("command_preview", None)
    if not isinstance(plan, dict):
        return JSONResponse({"error": "Preview this command again before running it."}, status_code=409)
    parsed = plan.get("parsed")
    ids = plan.get("ids")
    created_at = plan.get("created_at")
    if (
        not isinstance(parsed, dict)
        or parsed.get("action") not in {"label", "archive", "mark_read"}
        or not isinstance(ids, list)
        or not isinstance(created_at, (int, float))
        or datetime.now().timestamp() - created_at > 600
    ):
        return JSONResponse({"error": "That preview expired. Preview the command again."}, status_code=409)

    try:
        affected = execute_command(service, parsed, ids, account_key=email)
    except Exception:  # noqa: BLE001
        logger.exception("Command execute failed for %s.", email)
        return JSONResponse({"error": "Could not complete that action. Try again."}, status_code=500)

    return JSONResponse({"status": "done", "affected": affected, "summary": parsed.get("summary")})


# --- Daily digest ----------------------------------------------------------


@app.get("/api/digest")
def api_digest(request: Request):
    """Return a compact summary: last-run counts + top priority + rule activity."""
    email = current_user_email(request)
    if not email:
        return _unauthenticated()
    progress = _progress_by_user.get(email)
    counts = progress["counts"] if progress else None
    rules = list_learned_rules(email)
    active_rules = sum(1 for r in rules if r.get("active"))
    top = _with_gmail_url(top_priority(email, 5), email)
    deadlines = _with_gmail_url(upcoming_deadlines(email, limit=5), email)
    return JSONResponse(
        {
            "counts": counts,
            "top": top,
            "deadlines": deadlines,
            "learned_active": active_rules,
            "learned_total": len(rules),
        }
    )


@app.get("/api/deadlines")
def api_deadlines(request: Request, limit: int = 20):
    """Return the user's upcoming deadlines (soonest first) with Gmail links."""
    email = current_user_email(request)
    if not email:
        return _unauthenticated()
    items = _with_gmail_url(upcoming_deadlines(email, limit=limit), email)
    return JSONResponse({"deadlines": items})


@app.get("/api/memory")
def api_memory(request: Request):
    """Everything the assistant remembers: learned rules + known contacts."""
    email = current_user_email(request)
    if not email:
        return _unauthenticated()
    rules = list_learned_rules(email)
    return JSONResponse(
        {
            "learned_rules": rules,
            "learned_active": sum(1 for r in rules if r.get("active")),
            "contacts": list_known_contacts(email),
        }
    )


# --- Real-time Gmail push (Pub/Sub) ---------------------------------------


def _register_watch(email, service=None):
    """Register/refresh a Gmail push watch for one user (best-effort)."""
    if not GMAIL_PUBSUB_TOPIC:
        return
    try:
        service = service or _service_for_user(email)
        if service is None:
            return
        result = start_watch(service, GMAIL_PUBSUB_TOPIC)
        save_watch_state(email, result.get("historyId"), result.get("expiration"))
        logger.info("Gmail watch registered for %s (expires %s).", email, result.get("expiration"))
    except Exception:  # noqa: BLE001 - push is optional; never break the app
        logger.exception("Failed to register Gmail watch for %s.", email)


def _renew_all_watches():
    """Re-register watches for every connected user (scheduled daily)."""
    for email in list_user_emails():
        _register_watch(email)


def _send_all_reminders():
    """Send deduplicated reminder summaries for every connected account."""
    for email in list_user_emails():
        try:
            service = _service_for_user(email)
            if service is not None:
                send_user_reminders(service, email)
        except Exception:  # noqa: BLE001 - one mailbox must not stop the job
            logger.exception("Reminder run failed for %s.", email)


@app.post("/api/gmail/push")
async def api_gmail_push(request: Request, background_tasks: BackgroundTasks):
    """Receive a Gmail push notification (Pub/Sub) and triage the user's new mail.

    Pub/Sub delivers {"message": {"data": base64(JSON {emailAddress, historyId})}}.
    We acknowledge quickly with 200 and run triage in the background. Already
    handled mail carries the AI-Processed label, so a push never re-sorts old
    mail. Every path returns 200 so Pub/Sub does not retry-storm on bad input.
    """
    if not GMAIL_PUBSUB_TOPIC or not PUSH_AUTH_TOKEN:
        return JSONResponse({"error": "push disabled"}, status_code=404)
    supplied_token = request.query_params.get("token") or ""
    if not secrets.compare_digest(supplied_token, PUSH_AUTH_TOKEN):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        envelope = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": True})
    message = (envelope or {}).get("message") or {}
    data = message.get("data")
    if not data:
        return JSONResponse({"ok": True})
    try:
        payload = json.loads(base64.b64decode(data).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": True})

    email = payload.get("emailAddress")
    if not email or get_user_token(email) is None:
        return JSONResponse({"ok": True})

    # Record the latest historyId as an ack marker (keep any known expiration).
    if payload.get("historyId") is not None:
        try:
            state = get_watch_state(email) or {}
            save_watch_state(email, payload.get("historyId"), state.get("expiration"))
        except Exception:  # noqa: BLE001
            pass

    # Do not start a second run if one is already active for this user.
    progress = _progress_by_user.setdefault(email, _default_progress())
    if progress["status"] == "running":
        return JSONResponse({"ok": True})

    service = _service_for_user(email)
    if service is None:
        return JSONResponse({"ok": True})

    progress.update(status="running", counts=None, error=None, percent=0, first_chunk_done=False)
    background_tasks.add_task(_run_triage, email, service, "1d", None)
    return JSONResponse({"ok": True})


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
