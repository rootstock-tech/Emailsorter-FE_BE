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

# Secret key used to sign session cookies (Starlette SessionMiddleware).
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
