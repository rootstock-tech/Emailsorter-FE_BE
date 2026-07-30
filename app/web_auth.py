"""Web-application OAuth2 flow (multi-user).

This is distinct from ``gmail_client.py``'s installed-app flow: it does not open
a local browser or run a local server. Instead it builds a consent URL that the
FastAPI app redirects the user to, and later exchanges the returned auth code
for tokens.

Each user's credentials are stored in the database keyed by their email address
(extracted from the OpenID Connect ID token), so multiple accounts can connect
with isolated tokens.
"""

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from google_auth_oauthlib.flow import Flow

from app.config import OAUTH_REDIRECT_URI
from app.db import save_user_token
from app.gmail_client import SCOPES

# The web flow additionally requests OpenID Connect scopes so we can identify
# which Google account connected. These are added HERE ONLY -- gmail_client.py's
# SCOPES (used by the installed-app/CLI flow) are left unchanged.
WEB_SCOPES = SCOPES + [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]

# The OAuth client secret for a *Web application* client (downloaded from the
# Google Cloud console). This is different from ``credentials.json``, which is a
# *Desktop app* client used by the installed-app flow.
WEB_CREDENTIALS_FILE = "web_credentials.json"

# Must exactly match an authorized redirect URI configured on the web OAuth
# client in the Google Cloud console. Configurable (OAUTH_REDIRECT_URI) so the
# same code runs in production; defaults to the local dev URL.
REDIRECT_URI = OAUTH_REDIRECT_URI


def _build_flow():
    """Create an OAuth Flow bound to the web client secret and redirect URI."""
    return Flow.from_client_secrets_file(
        WEB_CREDENTIALS_FILE,
        scopes=WEB_SCOPES,
        redirect_uri=REDIRECT_URI,
    )


def login_url():
    """Return (auth_url, code_verifier) for redirecting the user to Google.

    The Flow generates a fresh PKCE code_verifier/code_challenge pair here.
    The verifier MUST be persisted (e.g. in the session) by the caller and
    passed back into exchange_code() -- a *different* Flow instance is used
    for the callback, and it has no way to recover the original verifier on
    its own.
    """
    flow = _build_flow()
    auth_url, _state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        # "select_account" makes Google show the account chooser (any Google
        # accounts already signed in on this browser/device) instead of
        # silently reusing whichever session is currently active -- needed
        # since this app supports multiple users.
        prompt="select_account consent",
    )
    return auth_url, flow.code_verifier


def exchange_code(code, code_verifier):
    """Exchange an auth code for tokens, store them per-user, return the email.

    ``code_verifier`` must be the one returned by the login_url() call that
    started this flow (PKCE requires the same verifier at both ends).
    The user's email is read from the verified ID token. Credentials are saved
    to the database via ``save_user_token`` (not to token.json), so each account
    stays isolated.
    """
    flow = _build_flow()
    flow.code_verifier = code_verifier
    flow.fetch_token(code=code)

    creds = flow.credentials

    id_info = id_token.verify_oauth2_token(
        creds.id_token,
        google_requests.Request(),
        flow.client_config["client_id"],
    )
    email = id_info["email"]

    save_user_token(email, creds.to_json())
    return email
