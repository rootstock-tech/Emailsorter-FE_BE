# Email Triage Assistant — Runbook

A quick reference of every command needed to set up, run, and manage the app.
Run all commands from the project root (the folder that contains `run_server.py`).

> Platform: Windows PowerShell. On macOS/Linux, replace `.venv\Scripts\...`
> with `.venv/bin/...` and use `cp` instead of `Copy-Item`.

---

## 1. One-time setup

| Step | Command | What it does |
| ---- | ------- | ------------ |
| Create virtual environment | `python -m venv .venv` | Makes an isolated Python environment in `.venv/` so project packages don't touch your system Python. |
| Activate the environment | `.venv\Scripts\Activate.ps1` | Turns the venv on for the current terminal. You'll see `(.venv)` in the prompt. |
| Install dependencies | `pip install -r requirements.txt` | Installs FastAPI, uvicorn, Groq, fastembed, APScheduler, Google auth libraries, etc. |
| Create the env file | `Copy-Item .env.example .env` | Copies the template so you can fill in your own keys. |
| Generate a session secret | `python -c "import secrets; print(secrets.token_hex(32))"` | Prints a random value to paste into `SESSION_SECRET` in `.env`. |

After copying `.env`, open it and fill in:
- `GROQ_API_KEY` — your Groq API key (needed for classification + reply drafting).
- `SESSION_SECRET` — paste the value generated above.

You also need two Google OAuth files in the project root (see README → Google Cloud setup):
- `credentials.json` — Desktop client (used by the CLI).
- `web_credentials.json` — Web client (used by the dashboard). Redirect URI must be `http://localhost:8000/auth/callback`.

---

## 2. Running the app

| Command | What it does |
| ------- | ------------ |
| `.venv\Scripts\python.exe run_server.py` | **Recommended.** Starts the web dashboard on http://127.0.0.1:8000. Auto-reload is OFF on purpose (a triage runs as a long background task and reload would interrupt it). |
| `uvicorn app.server:app --port 8000` | Alternative way to start the same server. |
| `uvicorn app.server:app --reload --port 8000` | Dev mode with auto-reload (use only when editing code and no triage is running). |
| `python -m app.main` | Runs the single-account **CLI** version: classifies/labels up to 200 unread emails and prints a summary. |

Then open **http://localhost:8000** in a browser and click **Connect Gmail**.

---

## 3. Using the dashboard (what each button does)

| Action | What happens |
| ------ | ------------ |
| **Connect Gmail** | Starts the Google OAuth login. The account must be added as a Test User in Google Cloud (while the app is unverified). |
| **Run Triage** (with date + range) | Fetches all inbox mail in the chosen window (Up to 1 day / Up to 1 week), classifies each email, and applies Gmail labels. No count limit. |
| **Stop** | Cancels an in-progress triage run. |
| **Categories** | Edit your category list and pick which category triggers auto-reply drafting. "Others" is always on as a catch-all. |
| **Search** | Finds already-processed emails by meaning (semantic search). Only works after at least one triage run. |
| **Schedule / Run on** | Schedules a one-time triage for a chosen date/time. Backed by APScheduler; you can view or cancel it. |
| **Label guide** | Lists every label the tool applies and how it decides where each email goes. |

---

## 4. Managing the server

| Command | What it does |
| ------- | ------------ |
| `Ctrl + C` (in the server terminal) | Stops the running server. |
| `Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue \| Select-Object -ExpandProperty OwningProcess -Unique \| ForEach-Object { Stop-Process -Id $_ -Force }` | Force-kills whatever process is holding port 8000 (use if the server is stuck or a previous instance is still running). |
| `try { Invoke-WebRequest 'http://localhost:8000/' -TimeoutSec 2 -UseBasicParsing \| Out-Null; 'up' } catch { 'down' }` | Quick check whether the server is running. |

> **Note:** because auto-reload is off, after changing any Python code you must
> stop the server (`Ctrl + C` or the kill command above) and start it again for
> the change to take effect. Frontend (`static/`) changes only need a browser
> hard-refresh (`Ctrl + F5`).

---

## 5. Files you must NOT commit or share publicly

These hold secrets and are git-ignored. Keep them out of any public repo:
`.env`, `credentials.json`, `web_credentials.json`, `token.json`, `app.db`.
