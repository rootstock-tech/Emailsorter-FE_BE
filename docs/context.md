# Email-Triage-Assistant: Project Context

## Project Overview

**Email Triage Assistant** is an AI-assisted inbox management tool for Gmail designed to help individuals and small teams maintain control over busy inboxes without giving AI the ability to send emails automatically.

## Purpose & Problem Statement

Managing large volumes of emails is time-consuming and often results in important messages being missed. The Email Triage Assistant solves this by:
- Automatically categorizing incoming emails
- Prioritizing urgent and action-requiring messages
- Extracting and tracking deadlines
- Enabling semantic search across processed emails
- Drafting (never sending) polite replies for specific categories

## Target Users

- Individuals with high-volume Gmail inboxes
- Small teams requiring collaborative email management
- Users who want AI assistance with strict safety boundaries (no auto-send)

## Key Features

### Core Functionality
1. **Hybrid Classification** - Fast keyword/domain rules + Groq LLM fallback
2. **Adaptive Learning** - Rules automatically learned from repeated LLM decisions and user corrections
3. **Multi-category Support** - Users define custom category lists
4. **Priority Scoring** - Deterministic scoring lifts urgent mail
5. **Deadline Extraction** - Explicit and relative deadline parsing with LLM fallback
6. **Semantic Search** - Local embeddings (no external vector DB) for meaning-based search
7. **Auto-reply Drafting** - Draft replies (Gmail Drafts only, never sends)
8. **Reminder System** - Auto-sends deduplicated summaries for high-priority unread mail

### Access Methods
1. **Web Dashboard** - Multi-user web interface (FastAPI + OAuth)
2. **Command-line Interface** - Single-account installed-app flow
3. **Gmail Add-on** - Apps Script integration for in-Gmail controls

## Deployment Models

- **Local Development** - Web dashboard at `http://localhost:8000`
- **Self-hosted** - Single-worker deployment with persistent SQLite
- **Cloud-ready** - Configurable for Heroku, DigitalOcean, AWS, etc.

## Key Constraints & Design Decisions

1. **Single Worker Only** - Scheduler state is process-local; multiple workers cause duplicate processing
2. **Durable Database** - SQLite must be on persistent storage (contains tokens, rules, deadlines)
3. **Safety First** - Never sends emails; only creates Gmail Drafts for manual review
4. **Privacy** - Tokens are stored locally, never transmitted to external services
5. **No External Vector DB** - Uses lightweight local embeddings for search

## Technology Stack

- **Backend:** Python 3.10+, FastAPI, Uvicorn
- **LLM:** Groq API (`llama-3.3-70b-versatile`)
- **Database:** SQLite (local)
- **Embeddings:** fastembed (local, ~70 MB model)
- **Gmail Integration:** Google API Python Client, OAuth 2.0
- **Frontend:** HTML/CSS/JavaScript (static files)
- **Gmail Add-on:** Google Apps Script
- **Scheduling:** APScheduler

## Repository

**GitHub:** https://github.com/rootstock-tech/Emailsorter-FE_BE.git

## Project Status

- **Status:** Production Ready
- **Last Updated:** August 2026
- **Testing:** Comprehensive regression suite (59 tests) + real-Gmail validation
- **Deployment:** Ready for self-hosted and cloud deployments
