# Email-Triage-Assistant: Architecture

## System Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                     User Interfaces                         │
│  ┌──────────────┐  ┌──────────────┐  ┌─────────────────┐   │
│  │ Web Dashboard│  │     CLI      │  │  Gmail Add-on   │   │
│  │  (FastAPI)   │  │ (installed app)  │ (Apps Script)   │   │
│  └──────┬───────┘  └──────┬───────┘  └────────┬────────┘   │
└─────────┼──────────────────┼──────────────────┼──────────────┘
          │                  │                  │
          └──────────────────┼──────────────────┘
                    OAuth 2.0 Flow
                             │
          ┌──────────────────▼──────────────────┐
          │    Gmail Client (gmail_client.py)   │
          │  • OAuth token management           │
          │  • Fetch unread emails              │
          │  • Apply labels & drafts            │
          └──────────────────┬──────────────────┘
                             │
                    Gmail API (Google)
                             │
          ┌──────────────────▼──────────────────┐
          │      Core Triage Pipeline           │
          │  (main.py / triage_until_empty)     │
          │                                     │
          │  1. Fetch emails                    │
          │  2. Classify (rules → LLM)          │
          │  3. Apply labels & priority         │
          │  4. Extract deadlines               │
          │  5. Draft replies (optional)        │
          │  6. Store embeddings                │
          └──────────────────┬──────────────────┘
                             │
          ┌──────────────────▼──────────────────┐
          │        SQLite Database              │
          │  • OAuth tokens (per account)       │
          │  • Category settings                │
          │  • Learned rules                    │
          │  • Email embeddings                 │
          │  • Deadlines & priority             │
          │  • Conversation history             │
          │  • Scheduled tasks                  │
          └──────────────────────────────────────┘
```

## Core Components

### 1. **app/gmail_client.py** - Gmail Integration
- OAuth token management (multi-user)
- Fetch unread emails in chunks (200 at a time)
- Apply Gmail labels (including hidden `AI-Processed` label)
- Create Gmail Drafts for auto-replies
- Handles conversation threading

### 2. **app/classifier.py** - Email Classification
- Rules-first approach (fast deterministic matching)
- LLM fallback (Groq) for unmatched emails
- Batch processing for efficiency
- Fallback to safe default if LLM unavailable
- Includes user corrections in prompt

### 3. **app/rules.py** - Deterministic Rules Engine
- Keyword and domain-based rules
- Learned rules from repeated LLM decisions (after 3 observations)
- User-corrected rules stored with metadata (subject keywords, weights, recency)
- Weighted evidence scoring

### 4. **app/labeler.py** - Label Management
- Gets or creates Gmail labels on first use
- Applies category label to processed emails
- Applies hidden `AI-Processed` label for tracking
- Handles label creation idempotently

### 5. **app/priority.py** - Priority Scoring
- Deterministic scoring (urgency, reply status, deadline proximity)
- Lifts high-priority emails to top of inbox
- Generates explanations for scoring decisions

### 6. **app/deadlines.py** - Deadline Extraction
- High-precision explicit deadline parsing
- Relative deadline resolution (e.g., "next Friday")
- LLM fallback for complex deadline language
- Rejects spam deadlines and past dates

### 7. **app/auto_reply.py** - Reply Drafting
- Groq-powered polite reply generation
- Grounded in user-defined FAQ templates
- Saves as Gmail Draft (never sends)
- Only for configured category

### 8. **app/summarize.py** - Email Summarization
- Groq-powered bullet-point summaries
- Used by Gmail add-on for contextual summaries
- Grounded in email content

### 9. **app/search.py** - Semantic Search
- Local embeddings with fastembed
- Cosine similarity ranking
- No external vector database
- Searches across all processed emails

### 10. **app/db.py** - Data Persistence
- SQLite database schema
- OAuth token storage (per account, keyed by email)
- Learned rules storage
- Email embeddings cache
- Deadline and priority persistence
- Undo/rollback state tracking

### 11. **app/server.py** - Web API (FastAPI)
- Multi-user OAuth flow (PKCE)
- Session management with signed cookies
- RESTful endpoints for dashboard
- Scheduled triage runner (APScheduler)
- Gmail add-on API endpoints (with shared secret)

### 12. **app/web_auth.py** - OAuth Implementation
- PKCE web flow for browser-based auth
- Single sign-on with Google
- Token storage per account
- Session identity binding

### 13. **Gmail Add-on** (Apps Script)
- In-Gmail card interface
- Date range triage controls
- Unread alerts with counts
- Deadline list display
- Category correction UI
- One-click summarization
- Undo recent actions

## Data Flow

### Triage Pipeline (Single Email)
```
Email fetched from Gmail
    ↓
Check if already processed (AI-Processed label)
    ↓
Extract headers, body, subject
    ↓
Apply deterministic rules
    ├─ Match found → use category
    └─ No match → call Groq LLM
    ↓
Classify email into category
    ↓
Remember if user replied (known contact)
    ↓
Calculate priority score
    ↓
Extract deadline (if any)
    ↓
Generate embedding (semantic search)
    ↓
Apply Gmail label
    ↓
Generate auto-reply draft (if configured)
    ↓
Store in SQLite:
  - Embedding
  - Priority
  - Deadline
  - Learned rule (if applicable)
  - Undo state
```

### Web Dashboard Flow
```
User authenticates (OAuth)
    ↓
Session created with signed cookie
    ↓
User clicks "Run Triage"
    ↓
Backend fetches unread emails
    ↓
Triage pipeline processes each
    ↓
Results aggregated:
  - Per-category counts
  - Draft count
    ↓
Dashboard displays summary
    ↓
User can:
  - Edit categories
  - Search processed emails
  - Schedule future runs
  - View deadlines
```

## Database Schema (SQLite)

### Key Tables
- **tokens** - OAuth tokens (per account, keyed by email)
- **users** - User preferences (categories, FAQ category for replies)
- **emails** - Processed email metadata and embeddings
- **learned_rules** - Auto-learned keyword/domain rules
- **deadlines** - Extracted due dates
- **priority_scores** - Calculated priority with explanations
- **contacts** - Known contacts (user has replied)
- **scheduled_runs** - Pending and recurring triage schedules
- **undo_state** - Revertible triage actions

## External Dependencies

### Google APIs
- Gmail API (gmail.modify scope)
- OAuth 2.0 (openid, email scopes)

### Groq API
- LLM classification and reply generation
- Model: `llama-3.3-70b-versatile`

### FastEmbed
- Local embeddings (~70 MB model)
- No external vector database

## Security Model

1. **OAuth Tokens**
   - Stored locally in SQLite
   - Keyed by email from OIDC ID token
   - Never transmitted to external services

2. **Session Management**
   - Signed cookies (itsdangerous)
   - User identity tied to OAuth email
   - Expiration configured

3. **Add-on Authentication**
   - Shared secret validation
   - Optional: Google Cloud audience validation
   - Binds Apps Script identity to user email

4. **Email Safety**
   - Never sends emails automatically
   - Only creates Gmail Drafts
   - Always requires manual review

## Limitations & Single-Worker Design

- **Process-local scheduler state** - Multiple workers cause duplicate processing
- **Durable storage required** - SQLite must persist across restarts
- **One-time runs** - Scheduled via APScheduler, not distributed job queue
- **Conversation memory** - Per-process in-memory cache (could be SQLite-backed)

## Scalability Notes

- Current design optimized for 1-10 users per deployment
- For larger deployments, consider:
  - Distributed job queue (Celery + Redis)
  - Persistent scheduler backend
  - Connection pooling for Groq API
  - Caching layer (Redis) for embeddings
