"""Application configuration loaded from environment variables."""

import os

from dotenv import load_dotenv

load_dotenv()

# Google's OAuth token endpoint can legitimately return a scope set that
# differs from what was requested -- most commonly a superset, when
# include_granted_scopes=true pulls in a scope the user granted to this
# project on a previous, separate authorization (e.g. the installed-app/CLI
# flow's earlier gmail.readonly grant). oauthlib treats any such mismatch as
# a hard error by default; this is Google's own documented workaround.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
IS_PRODUCTION = APP_ENV == "production"

# Secret key used to sign session cookies (Starlette SessionMiddleware). Keep
# this out of source control (it lives in .env) -- anyone who has it can forge
# signed session cookies and impersonate a logged-in user.
SESSION_SECRET = os.getenv("SESSION_SECRET", "")

# Send session cookies only over HTTPS. Turn this on in production by setting
# SESSION_HTTPS_ONLY=true; left off by default so local http://localhost
# development still receives the cookie.
SESSION_HTTPS_ONLY = os.getenv("SESSION_HTTPS_ONLY", "false").strip().lower() in {
    "1",
    "true",
    "yes",
}

# OAuth2 redirect URI for the web login flow. Must exactly match an authorized
# redirect URI on the web client in the Google Cloud console. Override this with
# your public callback URL in production; defaults to the local dev URL.
OAUTH_REDIRECT_URI = os.getenv(
    "OAUTH_REDIRECT_URI", "http://localhost:8000/auth/callback"
)

# Real-time Gmail push (Pub/Sub). Set to the full topic name
# (projects/<project>/topics/<topic>) to enable users.watch registration and the
# push webhook. Leave empty to keep push disabled (manual/scheduled triage only).
GMAIL_PUBSUB_TOPIC = os.getenv("GMAIL_PUBSUB_TOPIC", "").strip()

# Optional shared secret guarding the /api/gmail/push webhook. When set, the
# push subscription must call the endpoint with ?token=<value>; requests without
# a matching token are ignored so random callers cannot trigger triage.
PUSH_AUTH_TOKEN = os.getenv("PUSH_AUTH_TOKEN", "").strip()

# Shared secret for the Gmail Add-on. The add-on (running in Apps Script) sends
# this in the X-Addon-Secret header so the backend trusts its per-user calls.
# Must be set (and match the add-on's script property) for /api/addon/* to work.
ADDON_SHARED_SECRET = os.getenv("ADDON_SHARED_SECRET", "").strip()
ADDON_GOOGLE_AUDIENCE = os.getenv("ADDON_GOOGLE_AUDIENCE", "").strip()

REMINDER_EMAILS_ENABLED = os.getenv("REMINDER_EMAILS_ENABLED", "false").strip().lower() in {
    "1",
    "true",
    "yes",
}


def validate_production_config():
    """Raise with all unsafe production settings before the server starts."""
    if not IS_PRODUCTION:
        return
    problems = []
    if len(SESSION_SECRET) < 32:
        problems.append("SESSION_SECRET must be at least 32 characters")
    if not SESSION_HTTPS_ONLY:
        problems.append("SESSION_HTTPS_ONLY must be true")
    if not OAUTH_REDIRECT_URI.lower().startswith("https://"):
        problems.append("OAUTH_REDIRECT_URI must use HTTPS")
    if not ADDON_SHARED_SECRET:
        problems.append("ADDON_SHARED_SECRET is required")
    if GMAIL_PUBSUB_TOPIC and not PUSH_AUTH_TOKEN:
        problems.append("PUSH_AUTH_TOKEN is required when Gmail push is enabled")
    if problems:
        raise RuntimeError("Unsafe production configuration: " + "; ".join(problems))
