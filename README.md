# Email Triage Assistant

Authenticate with the Gmail API and fetch unread emails. Classification is not
implemented yet — this stage covers authentication and email fetching only.

## Prerequisites

- Python 3.10+
- A Google account with Gmail

## Setup

### 1. Create a virtual environment and install dependencies

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Create a Google Cloud project and enable the Gmail API

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Click the project selector at the top and choose **New Project**. Give it a
   name (e.g. `email-triage-assistant`) and create it.
3. With the new project selected, open
   **APIs & Services > Library**.
4. Search for **Gmail API**, open it, and click **Enable**.

### 3. Configure the OAuth consent screen

1. Go to **APIs & Services > OAuth consent screen**.
2. Choose **External** user type and click **Create**.
3. Fill in the required app name, user support email, and developer contact
   email, then save.
4. On the **Test users** step, add the Google account you will use to log in.
   (While the app is in "Testing" mode, only listed test users can authorize it.)

### 4. Download `credentials.json`

1. Go to **APIs & Services > Credentials**.
2. Click **Create Credentials > OAuth client ID**.
3. Choose **Desktop app** as the application type and create it.
4. Click the download icon for the new client and save the file as
   `credentials.json` in the root of this project.

> `credentials.json`, `token.json`, and `.env` are git-ignored. Never commit them.

### 5. Configure environment variables

Copy the example file and fill in your Groq API key (used in a later stage):

```powershell
Copy-Item .env.example .env
```

Then edit `.env` and set `GROQ_API_KEY`.

## First-time OAuth login

Run a short Python session to trigger the installed-app OAuth flow. A browser
window opens asking you to sign in and grant read-only Gmail access. On success,
a `token.json` file is written and reused on subsequent runs.

```powershell
python -c "from app.gmail_client import authenticate, fetch_unread_emails; s = authenticate(); print(len(fetch_unread_emails(s)), 'unread emails')"
```

If the token expires, it is refreshed automatically. Delete `token.json` to force
a fresh login.

## Web dashboard

A FastAPI app wraps the same fetch → classify → label pipeline behind a small
web dashboard, with a browser-based OAuth flow (no local console login needed).

### 1. Create a Web application OAuth client

The dashboard uses a **Web application** OAuth client, which is separate from the
Desktop client used by the CLI:

1. In the Google Cloud console, go to **APIs & Services > Credentials**.
2. Click **Create Credentials > OAuth client ID** and choose **Web application**.
3. Under **Authorized redirect URIs**, add:
   `http://localhost:8000/auth/callback`
4. Create the client, download the JSON, and save it as `web_credentials.json`
   in the project root. (It is git-ignored, like the other secret files.)

### 2. Run the server

```powershell
uvicorn app.server:app --reload --port 8000
```

Then open <http://localhost:8000>.

- If Gmail is not connected yet (no `token.json`), the dashboard shows a
  **Connect Gmail** button that starts the OAuth flow. After you consent, the
  token is saved to `token.json` (the same file the CLI uses) and you are
  redirected back to the dashboard.
- Click **Run Triage** to fetch unread emails, classify them, apply labels, and
  see the per-category counts.

