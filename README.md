# Email Triage Assistant

An AI-assisted inbox triage tool for Gmail. It connects to your Gmail account,
reads your unread email, sorts each message into a category, labels it in Gmail,
and can draft (never send) polite replies for a category you choose. It runs as a
multi-user web dashboard or as a single-account command-line tool, and it can
search your processed email by meaning rather than keywords.

Built for individuals and small teams who want to keep a busy inbox under control
without giving an AI the ability to send mail on their behalf.

## Features

- **Gmail OAuth (multi-user).** The web app uses a browser-based OAuth flow with
  PKCE; each connected account's token is stored separately in a local SQLite
  database, keyed by the email from the OpenID Connect ID token. A signed session
  cookie tracks who is logged in. The CLI uses the classic installed-app flow.
- **Hybrid rule + AI classification.** Fast deterministic keyword/domain rules
  run first; anything they don't catch is classified by Groq's LLM
  (`llama-3.3-70b-versatile`) in batches. If the model is unavailable or returns
  an unexpected value, the email falls back to a safe default category rather
  than being dropped.
- **Adaptive learning.** Repeated specific LLM decisions become transparent
  sender/domain rules after three consistent observations. A manual correction
  is trusted immediately and stores subject keywords, old/new category, weight,
  and recency. Weighted sender/domain/keyword evidence runs before the LLM, and
  past corrections are included in its prompt. `Others` is deliberately never
  auto-learned, so later mail can still be reconsidered.
- **Conversation-aware spam protection.** Senders the user has replied to are
  remembered as known contacts and are never left in a spam/newsletter bucket.
- **Priority, alerts, and deadlines.** Deterministic scoring lifts urgent mail
  and replies; unread red-flag/action mail is pinned in the Gmail add-on; explicit
  due dates are extracted and shown soonest first. A validated structured Groq
  fallback resolves relative deadlines that deterministic parsing cannot.
- **Reminder emails.** An opt-in scheduler sends deduplicated summaries for
  unread high-priority mail older than 24 hours and deadlines due within 7 days.
- **One-click summaries.** When a Gmail message is open, the add-on can send its
  subject/body to the backend and display a short Groq-generated bullet summary.
- **Customizable categories.** Each user defines their own category list and
  which category (if any) triggers auto-reply drafting. Rules only apply for
  categories the user actually uses.
- **Auto-reply drafting (Drafts only).** For the configured category, the app
  generates a short, polite reply grounded in editable FAQ templates and saves it
  as a **Gmail draft**. It never calls send — a human always reviews and sends.
- **Chained / chunked inbox processing.** Email is fetched and processed in
  chunks (200 at a time). Each processed message is tagged with an `AI-Processed`
  label so repeated runs advance through the inbox instead of reprocessing the
  same messages. One click processes the whole inbox until it's caught up.
- **One-time run scheduling.** Schedule a triage run for a specific date and time
  (including later today). Backed by APScheduler; you can view or cancel a pending
  run.
- **Semantic search.** Every processed email is embedded locally with a small
  `fastembed` model and stored in SQLite. Search ranks results by cosine
  similarity to your query's embedding — no external vector database needed.
- **Gmail add-on.** An Apps Script card provides triage controls, priority mail,
  unread alerts, deadlines, undo, scheduling, and contextual summarization in
  Gmail web/mobile.

## Architecture

Two entry points share one pipeline. The **CLI** (`app.main`) uses an
installed-app OAuth flow (`credentials.json` → `token.json`). The **web app**
(`app.server`, FastAPI) is multi-user: a PKCE web OAuth flow
(`web_credentials.json`) stores each account's token in SQLite (`app.db`), keyed
by the email from the OIDC ID token, with a signed session cookie identifying the
current user. The core pipeline (`app.main.triage` / `triage_until_empty`) fetches
not-yet-processed inbox mail (`gmail_client`), classifies it with deterministic,
learned, then Groq rules (`rules` + `classifier` + `db`), applies the category and
hidden `AI-Processed` labels (`labeler`), optionally drafts a reply, calculates
priority, extracts deadlines, remembers conversation contacts, and stores a local
embedding. APScheduler runs scheduled/recurring triage. `app.server` serves both
the dashboard and secret-protected Gmail add-on API; `gmail-addon/` contains the
Apps Script client.

```
app/
  config.py        Env/config (GROQ_API_KEY, SESSION_SECRET)
  gmail_client.py  OAuth + fetch unread email; applies the "AI-Processed" label
  web_auth.py      Multi-user web OAuth (PKCE); one token per account
  db.py            SQLite: tokens, per-user category settings, embeddings
                   learned rules, contacts, priority, deadlines, undo state
  classifier.py    Rules-first, Groq-LLM-fallback classification
  rules.py         Deterministic keyword/domain rules
  auto_reply.py    Groq-drafted replies (Gmail Drafts only)
  priority.py      Deterministic attention scoring + explanations
  deadlines.py     High-precision explicit deadline extraction
  summarize.py     Groq-powered bullet summaries for the Gmail add-on
  commands.py      Preview/execute natural-language bulk mailbox commands
  search.py        Local embeddings + cosine-similarity search
  main.py          Triage pipeline + CLI entry point
  server.py        FastAPI app: routes, session, scheduler
static/            Dashboard: index.html, app.js, app.css
gmail-addon/       Apps Script Gmail add-on (Code.gs + manifest)
tests/             Isolated unittest regression suite
```

## Requirements

- **Python 3.10+**
- A Google account with Gmail
- A **Groq API key** (for classification and reply drafting)

Python dependencies are pinned in `requirements.txt` (FastAPI + uvicorn, the
google-api-python-client and auth libraries, groq, apscheduler, fastembed, numpy,
itsdangerous, python-dotenv, python-multipart).

## Setup

### 1. Create a virtual environment and install dependencies

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> The first time semantic search runs, `fastembed` downloads a small embedding
> model (~70 MB) into its local cache.

### 2. Configure environment variables

```powershell
Copy-Item .env.example .env
```

Then edit `.env`:

| Variable         | Purpose                                                                                                    |
| ---------------- | ---------------------------------------------------------------------------------------------------------- |
| `GROQ_API_KEY`   | Groq API key used for classification and reply drafting.                                                   |
| `APP_ENV`        | Set to `production` on the hosted service to enable fail-fast security validation.                         |
| `SESSION_SECRET` | Secret used to sign the web app's session cookies. Set a long random value for anything beyond local use.  |
| `ADDON_SHARED_SECRET` | Shared secret required by every `/api/addon/*` request. Match the add-on's `ADDON_SECRET`.           |
| `OAUTH_REDIRECT_URI` | OAuth callback URL; set this to the public HTTPS callback when deployed.                                  |
| `APP_DB_PATH` | SQLite path; set it inside a persistent mounted volume in production.                                         |
| `HOST` / `PORT` | Bind address and port. Hosted deployments normally use `0.0.0.0` and the platform-provided port.              |
| `REMINDER_EMAILS_ENABLED` | Set `true` to enable automatic self-reminder emails every three hours.                            |

### 3. Google Cloud Console setup

1. Open the [Google Cloud Console](https://console.cloud.google.com/) and create
   (or select) a project.
2. **Enable the Gmail API:** _APIs & Services → Library →_ search **Gmail API** →
   **Enable**.
3. **Configure the OAuth consent screen:** _APIs & Services → OAuth consent
   screen_ → **External** → fill in app name, support email, and developer
   contact. Under **Test users**, add every Google account that will sign in
   (while the app is unverified, only listed test users can authorize it).
4. **Create OAuth client credentials** (_APIs & Services → Credentials → Create
   Credentials → OAuth client ID_). This project uses **two** clients:
   - **Desktop app** — used by the CLI's installed-app flow. Download it and save
     as `credentials.json` in the project root.
   - **Web application** — used by the web dashboard. Add
     `http://localhost:8000/auth/callback` as an **Authorized redirect URI**,
     then download it and save as `web_credentials.json` in the project root.

The requested scopes are `gmail.modify` (read email and apply labels/drafts),
plus `openid` and `email` for the web flow (to identify the connected account).

> `credentials.json`, `web_credentials.json`, `token.json`, `.env`, and `app.db`
> are git-ignored. Never commit them.

## Running

### Web dashboard (multi-user)

```powershell
uvicorn app.server:app --reload --port 8000
```

Open <http://localhost:8000>, click **Connect Gmail**, and authorize. Then:

- **Run Triage** processes the whole inbox in chunks and shows per-category
  counts (and how many auto-reply drafts were created).
- **Categories** lets you edit your category list and pick the auto-reply
  category.
- **Search** finds processed emails by meaning.
- **Run on** schedules a one-time run (including later today).

### CLI (single account)

```powershell
python -m app.main
```

This runs the installed-app OAuth flow (opening a browser on first use and
caching `token.json`), then classifies and labels up to 200 not-yet-processed
unread emails and prints a summary. Because processed emails are tagged
`AI-Processed`, running it again continues through the rest of the inbox.

## Testing

Run the isolated regression suite from the project root:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The suite uses a temporary SQLite database and mocked Gmail/Groq clients; it
does not change the connected inbox or production `app.db`. Before deployment,
also run the live smoke checks in `RUNBOOK.md` against the running backend.

## Pilot deployment constraints

- Run exactly one backend worker. Scheduler state and per-user run locks are
  process-local; multiple workers can duplicate recurring triage.
- Mount `APP_DB_PATH` on durable storage. SQLite contains OAuth tokens, learned
  memory, deadlines, priority, and settings.
- One-time scheduled runs are currently in memory and must be recreated after a
  restart. Recurring auto-triage intervals are persisted and restored.
- The Gmail add-on uses one shared secret for the pilot. Public multi-tenant use
  should replace it with Google identity-token verification bound to the user.

## The Google "unverified app" screen

While your OAuth app is still in **Testing** (unverified), Google shows a warning
during sign-in — _"Google hasn't verified this app."_ This is expected for a
self-hosted project: choose **Advanced → Go to &lt;app&gt; (unsafe)** to continue.
An unverified app is also limited to **100 users**, and only accounts you added as
**test users** on the OAuth consent screen can authorize it. Lifting the warning
and the cap requires submitting the app for Google verification; for personal or
small-team use, running it unverified with test users is perfectly fine.

---

Built by **RootStock Technology**.

