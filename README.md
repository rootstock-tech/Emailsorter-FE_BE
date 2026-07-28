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

## Architecture

Two entry points share one pipeline. The **CLI** (`app.main`) uses an
installed-app OAuth flow (`credentials.json` → `token.json`). The **web app**
(`app.server`, FastAPI) is multi-user: a PKCE web OAuth flow
(`web_credentials.json`) stores each account's token in SQLite (`app.db`), keyed
by the email from the OIDC ID token, with a signed session cookie identifying the
current user. The core pipeline (`app.main.triage` / `triage_until_empty`) fetches
unread, not-yet-processed emails (`gmail_client`), classifies them with a
rules-first, Groq-LLM-fallback classifier (`classifier` + `rules`), applies the
Gmail category label plus an `AI-Processed` marker (`labeler`), optionally drafts
a reply for the configured category (`auto_reply`, drafts only), and stores a
local embedding per email (`search` + `fastembed`) for semantic search. Scheduled
runs are driven by APScheduler. The dashboard (`static/`) is a no-build,
vanilla-JS single page.

```
app/
  config.py        Env/config (GROQ_API_KEY, SESSION_SECRET)
  gmail_client.py  OAuth + fetch unread email; applies the "AI-Processed" label
  web_auth.py      Multi-user web OAuth (PKCE); one token per account
  db.py            SQLite: tokens, per-user category settings, embeddings
  classifier.py    Rules-first, Groq-LLM-fallback classification
  rules.py         Deterministic keyword/domain rules
  auto_reply.py    Groq-drafted replies (Gmail Drafts only)
  search.py        Local embeddings + cosine-similarity search
  main.py          Triage pipeline + CLI entry point
  server.py        FastAPI app: routes, session, scheduler
static/            Dashboard: index.html, app.js, app.css
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
| `SESSION_SECRET` | Secret used to sign the web app's session cookies. Set a long random value for anything beyond local use.  |

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

