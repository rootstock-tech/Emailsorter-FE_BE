# Email-Triage-Assistant

> AI-assisted inbox triage for Gmail. Automatically categorize, prioritize, and search emails with a local-first, privacy-focused approach.

**Status:** Production Ready | **Last Updated:** August 2026 | **License:** Proprietary

## Quick Start

### Prerequisites
- Python 3.10+
- Google account with Gmail
- Groq API key (free tier available)

### 5-Minute Setup

```bash
# 1. Clone and install
git clone https://github.com/rootstock-tech/Emailsorter-FE_BE.git
cd Emailsorter-FE_BE
python -m venv .venv
.venv\Scripts\Activate.ps1  # Windows
source .venv/bin/activate   # Linux/Mac
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env with your Groq API key and secrets

# 3. Setup Google OAuth (see deployment.md for details)
# Download web_credentials.json to project root

# 4. Run
python run_server.py
# Open http://localhost:8000
```

## Features

### 📧 Email Management
- **Automatic Categorization** - Rules-based + LLM classification
- **Priority Scoring** - Highlights urgent and action-required emails
- **Deadline Extraction** - Parses explicit and relative due dates
- **Bulk Processing** - Chunk-based processing handles large inboxes

### 🧠 Smart Learning
- **Adaptive Rules** - Auto-learns from repeated LLM decisions
- **User Corrections** - Immediate trust in manual corrections
- **Conversation Memory** - Remembers who you've replied to
- **Spam Protection** - Contacts you've engaged with never marked as spam

### 🔍 Search & Access
- **Semantic Search** - Find emails by meaning, not just keywords
- **Web Dashboard** - Multi-user interface with session management
- **CLI Tool** - Single-account command-line interface
- **Gmail Add-on** - In-Gmail controls for triage, deadlines, summaries

### ✉️ Reply Assistance
- **Draft Generation** - Creates polite replies based on FAQ templates
- **Never Sends** - Only creates Gmail Drafts for manual review
- **Category-specific** - Configure which category triggers drafting

### 🔔 Notifications
- **Reminder Emails** - Auto-sends summaries for high-priority unread mail
- **Deadline Alerts** - Tracks due dates across emails
- **Scheduled Triage** - One-time or recurring processing

## Architecture

```
User Interface         Web Dashboard | CLI | Gmail Add-on
                             ↓
OAuth & Session        Google OAuth + Multi-user sessions
Management                  ↓
Core Triage Engine     Fetch → Classify → Label → Embed
                             ↓
Data Persistence       SQLite Database (tokens, rules, embeddings)
                             ↓
External Services      Gmail API + Groq LLM
```

**Key Design Principles:**
- ✅ Privacy-first: All tokens stored locally
- ✅ Safety-first: Never sends emails automatically
- ✅ Deterministic-first: Rules before LLM
- ✅ Single-worker: Scheduler state is process-local
- ✅ Embedded: No external vector database

For detailed architecture, see [architecture.md](architecture.md)

## Project Structure

```
Email-Triage-Assistant/
├── app/
│   ├── main.py              # CLI entry point & triage pipeline
│   ├── server.py            # FastAPI web server
│   ├── classifier.py        # Rules + LLM classification
│   ├── rules.py             # Keyword/domain rule engine
│   ├── gmail_client.py      # OAuth + Gmail API integration
│   ├── db.py                # SQLite schema & queries
│   ├── priority.py          # Priority scoring
│   ├── deadlines.py         # Deadline extraction
│   ├── auto_reply.py        # Reply draft generation
│   ├── summarize.py         # Email summarization
│   ├── search.py            # Semantic search
│   └── web_auth.py          # OAuth flow (web)
├── gmail-addon/
│   ├── Code.gs              # Apps Script code
│   └── appsscript.json      # Apps Script manifest
├── static/
│   ├── index.html           # Dashboard UI
│   ├── app.js               # Frontend logic
│   └── app.css              # Dashboard styles
├── tests/
│   └── test_regression.py   # 59-test regression suite
├── requirements.txt         # Python dependencies
├── .env.example             # Environment template
└── README.md                # This file
```

## Getting Started

### Local Development

1. **Install & Configure** (see Quick Start above)

2. **Run Web Dashboard**
   ```bash
   python run_server.py
   ```
   Visit http://localhost:8000 → Click "Connect Gmail" → Authorize

3. **Triage Inbox**
   - Click **"Run Triage"** to process all unread emails
   - View results by category
   - Edit categories as needed

4. **Search & Manage**
   - Use **Search** tab for semantic queries
   - View extracted **Deadlines**
   - Schedule future runs with **Run on**

### Command-Line Interface

```bash
python -m app.main
```

- Authenticates via OAuth (browser opens on first run)
- Processes up to 200 unread emails per run
- Prints summary with category counts
- Marks processed emails with `AI-Processed` label
- Re-run to continue through inbox

### Gmail Add-on

1. Create Apps Script project (see [deployment.md](deployment.md))
2. Copy `gmail-addon/Code.gs` and `appsscript.json`
3. Deploy as Add-on
4. Open Gmail → Click add-on icon
5. Triage, view deadlines, generate summaries inline

## Configuration

### Environment Variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `GROQ_API_KEY` | Yes | Groq API key for LLM |
| `SESSION_SECRET` | Yes | Signs session cookies |
| `ADDON_SHARED_SECRET` | Yes | Validates add-on requests |
| `APP_ENV` | No | `development` or `production` |
| `APP_DB_PATH` | No | SQLite path (default: `./app.db`) |
| `HOST` | No | Bind address (default: `0.0.0.0`) |
| `PORT` | No | Server port (default: `8000`) |
| `REMINDER_EMAILS_ENABLED` | No | Enable auto-reminders (default: `false`) |
| `OAUTH_REDIRECT_URI` | No | OAuth callback URL |

### Categories & Rules

Each user defines:
- **Categories** - Custom labels (e.g., "Urgent", "Newsletter", "Needs Action")
- **FAQ Category** - Which category triggers auto-reply drafting
- **Rules** - Auto-learned or manual keyword/domain rules

Rules are deterministic and run before LLM for speed.

## Usage Patterns

### Pattern 1: Busy Inbox Management
```
Morning: Run triage → see high-priority unread
Afternoon: Correct miscategorized emails (teaches learning)
Evening: Check scheduled summary email
```

### Pattern 2: Deadline Tracking
```
Process inbox → deadlines extracted
View deadline list → sorted by due date
Set reminders for upcoming deadlines
```

### Pattern 3: Semantic Search
```
Search "refund status" → finds related emails by meaning
Results ranked by relevance
Covers all past processed emails
```

### Pattern 4: Team Email Management
```
Multiple users sign in (multi-user OAuth)
Each user gets separate token & rules
Categories can be team-wide or personal
```

## Testing

### Automated Tests
```bash
python -m unittest discover -s tests -v
```
Expected: **59 tests pass**

Tests cover:
- Deterministic classification
- LLM fallback behavior
- Learning & rule adaptation
- OAuth flow (mocked)
- Database operations
- Gmail integration (mocked)
- Deadline extraction
- Priority scoring
- Semantic search

### Manual Validation
See [deployment.md](deployment.md) → Testing section for comprehensive checklist covering:
- OAuth flows
- Real Gmail classification
- Priority & deadline accuracy
- Add-on functionality
- Search relevance
- Reply drafting

**Last Manual Validation:** August 9, 2026 ✅ (59 tests + 10 real-Gmail checks)

## Production Deployment

### Deployment Checklist
- [ ] Configure secure `.env` (use platform secrets, not files)
- [ ] Set `APP_ENV=production` for security validation
- [ ] Use HTTPS only (update OAuth redirect URIs)
- [ ] Mount persistent storage for `app.db`
- [ ] Deploy with single worker (no multi-worker scaling)
- [ ] Set up daily backups
- [ ] Configure monitoring & alerting
- [ ] Test OAuth flow in production

### Recommended Platforms
- **Heroku** - Easiest for small teams
- **DigitalOcean App Platform** - Good middle ground
- **AWS/GCP Cloud Run** - Best for scale

See [deployment.md](deployment.md) for platform-specific guides.

## Known Limitations

### Current Design
1. **Single Worker Only** - Scheduler state is per-process (no distributed queue)
2. **SQLite Only** - Works for 1-10 users; PostgreSQL requires code changes
3. **Process-local Cache** - Conversation memory restarts with server
4. **Groq Dependency** - No offline fallback classification

### Scaling Considerations
- For 10+ concurrent users, migrate to PostgreSQL
- For distributed processing, replace APScheduler with Celery + Redis
- For high-volume classification, batch requests to Groq API

## Security Model

### Data Security
- ✅ OAuth tokens stored locally (never sent externally)
- ✅ Database encryption at rest (recommended)
- ✅ HTTPS enforced in production
- ✅ Session cookies signed (tamper-proof)

### Email Safety
- ✅ Never sends emails automatically
- ✅ Only creates Gmail Drafts (manual review required)
- ✅ No email content logged (only metadata)
- ✅ No email forwarding to external services

### Access Control
- ✅ Multi-user OAuth isolation
- ✅ Add-on shared secret validation
- ✅ Optional: Google Cloud audience validation
- ✅ Session-based access (no direct token exposure)

## Troubleshooting

### Common Issues

**OAuth fails with "redirect URI mismatch"**
- Ensure `OAUTH_REDIRECT_URI` matches Google Cloud Console setting exactly

**Groq API timeout**
- Check Groq API status: https://status.groq.com
- Verify rate limits not exceeded
- Add retry logic for production

**SQLite "database is locked"**
- Ensure only one server instance running
- Restart server
- Check file permissions on `app.db`

**Gmail add-on doesn't appear**
- Verify Apps Script deployment completed
- Check browser console for errors
- Re-deploy add-on after code changes

**Semantic search returns no results**
- First search downloads embedding model (~70 MB)
- Process some emails first (creates embeddings)
- Check SQLite embeddings table has entries

See [deployment.md](deployment.md) for full troubleshooting guide.

## Contributing

### Development Workflow
1. Clone repository
2. Create feature branch
3. Make changes
4. Run tests: `python -m unittest discover -s tests -v`
5. Submit pull request

### Code Style
- Python: PEP 8
- JavaScript: ES6+
- SQLite: Standard schema conventions

### Testing Requirements
- All new features must include unit tests
- Regressions must pass existing test suite
- Manual validation with real Gmail required before merge

## Support & Resources

- **GitHub:** https://github.com/rootstock-tech/Emailsorter-FE_BE
- **Groq API:** https://console.groq.com/docs/
- **Gmail API:** https://developers.google.com/gmail/api
- **Apps Script:** https://developers.google.com/apps-script

## License

Proprietary - Developed by RootStock Technology

## Author

**Created by:** RootStock Technology  
**Maintained by:** [Your Team]  
**Last Updated:** October 1, 2026

---

## Additional Documentation

- [**context.md**](context.md) - Project purpose, features, and design decisions
- [**architecture.md**](architecture.md) - System design, components, and data flow
- [**deployment.md**](deployment.md) - Setup, configuration, and deployment guides

---

**Ready to get started?** See [deployment.md](deployment.md) → Quick Start section
