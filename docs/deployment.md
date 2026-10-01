# Email-Triage-Assistant: Deployment Guide

## Prerequisites

- Python 3.10 or higher
- Google Cloud project with Gmail API enabled
- Groq API key
- Git

## Local Development Setup

### 1. Clone Repository

```bash
git clone https://github.com/rootstock-tech/Emailsorter-FE_BE.git
cd Emailsorter-FE_BE
```

### 2. Create Virtual Environment

**Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

**Linux/Mac:**
```bash
python -m venv .venv
source .venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

> First semantic search run will download fastembed model (~70 MB)

### 4. Configure Environment Variables

Copy the example environment file:
```bash
cp .env.example .env
```

Edit `.env` with your configuration:

| Variable | Purpose | Example |
|----------|---------|---------|
| `GROQ_API_KEY` | Groq API key for LLM | `gsk_...` |
| `SESSION_SECRET` | Sign session cookies | `your-long-random-secret-key` |
| `ADDON_SHARED_SECRET` | Gmail add-on shared secret | `addon-secret-key` |
| `APP_ENV` | Environment mode | `development` or `production` |
| `APP_DB_PATH` | SQLite database path | `./app.db` |
| `HOST` | Server bind address | `0.0.0.0` |
| `PORT` | Server port | `8000` |
| `REMINDER_EMAILS_ENABLED` | Enable auto-reminders | `false` |
| `OAUTH_REDIRECT_URI` | OAuth callback URL | `http://localhost:8000/auth/callback` |
| `ADDON_GOOGLE_AUDIENCE` | Google Cloud OAuth client ID | Optional (for production) |

### 5. Google Cloud Setup

#### 5.1 Create/Select Project
- Go to [Google Cloud Console](https://console.cloud.google.com/)
- Create a new project or select existing

#### 5.2 Enable Gmail API
1. Navigate to **APIs & Services → Library**
2. Search for **Gmail API**
3. Click **Enable**

#### 5.3 Configure OAuth Consent Screen
1. Go to **APIs & Services → OAuth consent screen**
2. Select **External** (for testing)
3. Fill in:
   - App name
   - User support email
   - Developer contact email
4. Add **Test users** (every Gmail account that will sign in during testing)

#### 5.4 Create OAuth Credentials
Create **two** OAuth client credentials:

**For CLI (installed app):**
1. **APIs & Services → Credentials → Create Credentials → OAuth client ID**
2. Select **Desktop application**
3. Download JSON and save as `credentials.json` in project root

**For Web Dashboard:**
1. **APIs & Services → Credentials → Create Credentials → OAuth client ID**
2. Select **Web application**
3. Add **Authorized redirect URIs:**
   - `http://localhost:8000/auth/callback` (local testing)
   - `https://your-domain.com/auth/callback` (production)
4. Download JSON and save as `web_credentials.json` in project root

### 6. Run Local Development Server

```bash
python run_server.py
```

Or with auto-reload:
```bash
uvicorn app.server:app --reload --port 8000
```

Then open http://localhost:8000 in your browser.

## Running the Application

### Web Dashboard (Multi-user)

```bash
uvicorn app.server:app --port 8000
```

**Access:** http://localhost:8000

**First run:**
1. Click **Connect Gmail**
2. Select your Google account
3. Grant requested permissions
4. Dashboard loads with your account info

**Actions:**
- **Run Triage** - Process entire inbox (shows per-category counts)
- **Categories** - Edit category list and auto-reply category
- **Search** - Find emails by meaning (semantic search)
- **Run on** - Schedule one-time triage run

### Command-Line Interface (Single Account)

```bash
python -m app.main
```

**First run:**
- Browser opens for OAuth authorization
- Saves token as `token.json`
- Processes up to 200 unread emails
- Prints summary (category counts, drafts created)

**Subsequent runs:**
- Uses cached token
- Continues from last processed email (marked with `AI-Processed` label)
- Processes next 200 until inbox is caught up

## Gmail Add-on Setup (Apps Script)

### 1. Create Apps Script Project
- Open [Google Apps Script](https://script.google.com/)
- Create new project

### 2. Add Code Files
1. In the editor, create/replace `Code.gs` with content from `gmail-addon/Code.gs`
2. Create new file → `appsscript.json` with content from `gmail-addon/appsscript.json`

### 3. Configure Manifest
Edit `appsscript.json`:
```json
{
  "oauthScopes": ["https://www.googleapis.com/auth/gmail.modify"],
  "gmail": {
    "name": "Email Triage",
    "logoUrl": "https://your-logo-url.png"
  }
}
```

### 4. Deploy
- Click **Deploy → New deployment**
- Type: Select **Add-on**
- Execute as: Your Google account
- Deploy as: Your Google account
- Confirm

### 5. Configure Environment
In Apps Script `Code.gs`, set:
```javascript
const BACKEND_URL = "http://localhost:8000"; // or your production URL
const ADDON_SECRET = "your-addon-secret-key"; // Match ADDON_SHARED_SECRET in .env
```

### 6. Test
- Open Gmail
- Click the add-on icon (puzzle piece) on right sidebar
- "Email Triage" card should appear

## Testing

### Automated Tests
```bash
python -m unittest discover -s tests -v
```

Expected: All 59 tests pass

### Manual Testing Checklist

1. **OAuth Flow**
   - [ ] Web dashboard login works
   - [ ] CLI OAuth opens browser and caches token
   - [ ] Multi-user session isolation works

2. **Email Classification**
   - [ ] Rules-based categorization works
   - [ ] LLM fallback works for unmatched emails
   - [ ] User corrections are learned

3. **Priority & Deadlines**
   - [ ] Priority scoring lifts urgent mail
   - [ ] Explicit deadlines extracted
   - [ ] Relative deadlines (e.g., "next Friday") work

4. **Add-on**
   - [ ] Card appears in Gmail
   - [ ] Triage status shows "connected"
   - [ ] Deadline list displays
   - [ ] Summaries generate on open message

5. **Search**
   - [ ] Semantic search finds emails by meaning
   - [ ] Results ranked by relevance

6. **Auto-reply**
   - [ ] Draft created for configured category
   - [ ] Draft appears in Gmail as Draft (not sent)
   - [ ] FAQ template grounding visible

## Production Deployment

### Recommended Platforms

**Heroku:**
```bash
heroku create your-email-app
heroku config:set GROQ_API_KEY=gsk_... SESSION_SECRET=...
git push heroku main
heroku open
```

**DigitalOcean:**
1. Create app from GitHub repo
2. Set environment variables in dashboard
3. Deploy

**AWS/Google Cloud:**
1. Use Cloud Run for serverless deployment
2. Mount persistent volume for `app.db`
3. Set environment via secrets manager

### Key Production Requirements

1. **Single Worker**
   - Do NOT run multiple workers (scheduler state is per-process)
   - Use: `gunicorn app.server:app --workers 1 --worker-class uvicorn.workers.UvicornWorker`

2. **Persistent Database**
   - Mount `APP_DB_PATH` on persistent storage
   - Backup daily (contains tokens, rules, embeddings)
   - Use managed PostgreSQL for multi-instance deployments (requires code changes)

3. **HTTPS Only**
   - Force HTTPS in production (`APP_ENV=production`)
   - Set `OAUTH_REDIRECT_URI` to HTTPS URL
   - Configure OAuth client with HTTPS redirect URI

4. **Secrets Management**
   - Use platform secrets (not `.env` files)
   - Rotate `SESSION_SECRET` quarterly
   - Use unique `ADDON_SHARED_SECRET` per deployment

5. **Monitoring**
   - Log all triage actions
   - Monitor Groq API quota and errors
   - Set up alerts for database growth
   - Track OAuth token refresh failures

6. **Backup & Recovery**
   - Daily SQLite backups
   - Test restore process monthly
   - Keep 30-day backup history

## Troubleshooting

### Issue: "Gmail API not enabled"
**Solution:** Enable Gmail API in Google Cloud Console → APIs & Services → Library

### Issue: OAuth redirect URI mismatch
**Solution:** Ensure `OAUTH_REDIRECT_URI` matches exactly in `.env` and Google Cloud OAuth client settings

### Issue: Groq API timeout
**Solution:** Add retry logic, check Groq API status, verify rate limits not exceeded

### Issue: SQLite "database is locked"
**Solution:** 
- Ensure only one worker running
- Restart server
- For persistent issues, migrate to PostgreSQL

### Issue: Add-on doesn't appear in Gmail
**Solution:**
- Verify Apps Script deployment completed
- Check browser console for errors
- Verify `ADDON_SHARED_SECRET` matches between Apps Script and backend

## Maintenance

### Weekly
- Monitor error logs
- Check Groq API usage

### Monthly
- Review learned rules for accuracy
- Update dependencies: `pip list --outdated`
- Test OAuth flow with new account

### Quarterly
- Rotate `SESSION_SECRET`
- Review security audit logs
- Test disaster recovery

## Support & Resources

- **GitHub Issues:** https://github.com/rootstock-tech/Emailsorter-FE_BE/issues
- **Groq API Docs:** https://console.groq.com/docs/
- **Gmail API Reference:** https://developers.google.com/gmail/api
- **Apps Script Guide:** https://developers.google.com/apps-script
