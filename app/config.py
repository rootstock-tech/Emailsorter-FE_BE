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
