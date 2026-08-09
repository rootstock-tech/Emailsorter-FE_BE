# Gmail Testing and Validation Guide

This document records the testing performed against real Gmail accounts and the
steps needed to reproduce it. Account addresses, OAuth credentials, tokens,
shared secrets, and temporary tunnel URLs are intentionally omitted.

## Validation status

Last updated: 9 August 2026.

| Area | Result | Evidence |
| --- | --- | --- |
| Automated regression | Pass | 59 tests passed on 9 August 2026 |
| Baseline live classification | Pass | 13/13 test messages received the expected category |
| Reply/thread handling | Pass | Same-thread reply inherited `Needs Action`, scored 100, and activated conversation memory |
| Manual correction learning | Pass | Similar follow-up was classified from learned guidance without Groq; unrelated mail still used Groq |
| Explicit and relative deadlines | Pass | Future dates saved; past/no-cue dates rejected; spam deadlines excluded |
| Unread add-on alerts | Pass | Five unread attention items returned during the test run |
| Add-on deadline list | Pass | Both expected upcoming deadlines returned |
| Contextual summary | Pass | Open-message summary returned grounded bullets |
| Reminder email | Pass | Self-reminder sent once; immediate duplicate suppressed; spam excluded |
| Second-account OAuth pilot | Pass | Separate Google test account connected and add-on status returned `connected=true` |
| Add-on date selector | Automated pass | Contract, payload, validation, and full regression pass; republish `Code.gs` for a live UI check |

## Safety boundaries

Use dedicated test accounts or test-only messages. Running triage changes Gmail
labels and adds the hidden `AI-Processed` label. FAQ handling may create a draft,
but the application never sends that draft automatically. Reminder testing can
send an email from the connected account to itself only when explicitly enabled.

Never commit or share these files:

- `.env`
- `credentials.json`
- `web_credentials.json`
- `token.json`
- `app.db`

## 1. Automated checks

From the repository root in Windows PowerShell:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe -m compileall -q app run_server.py tests
.venv\Scripts\python.exe -m pip check
Get-Content gmail-addon\Code.gs -Raw | node --check
Get-Content gmail-addon\appsscript.json -Raw | ConvertFrom-Json | Out-Null
```

Expected:

- All unit/regression tests end with `OK`.
- Compile, dependency, Apps Script syntax, and manifest checks exit successfully.
- Tests use temporary databases and mocked Gmail/Groq calls; they do not modify a real inbox.

## 2. Local Gmail setup

1. Create `.env` from `.env.example` and provide a Groq API key, session secret,
   add-on shared secret, and OAuth callback URL.
2. Put the web OAuth client JSON at `web_credentials.json`.
3. In Google Cloud, enable Gmail API, configure the OAuth consent screen, and add
   each Gmail account under **Test users** while the app is in Testing mode.
4. Ensure the OAuth web client has the exact callback URI used by the backend.
5. Start the backend:

   ```powershell
   .venv\Scripts\python.exe run_server.py
   ```

6. Open `http://127.0.0.1:8000`, click **Connect Gmail**, and complete consent.
7. For a public add-on callback during local testing, use an HTTPS tunnel and set
   both `OAUTH_REDIRECT_URI` and the OAuth client's authorized redirect URI to
   `<public-backend>/auth/callback`. Temporary tunnel URLs change after restart.

## 3. Gmail add-on setup

1. Create or open the Apps Script project.
2. Paste `gmail-addon/Code.gs` and `gmail-addon/appsscript.json`.
3. Set Script Properties:
   - `BACKEND_URL`: stable HTTPS backend origin, without a trailing slash.
   - `ADDON_SECRET`: same value as backend `ADDON_SHARED_SECRET`.
4. Create/install a Gmail test deployment and authorize the requested scopes.
5. Open Gmail, refresh it, and open **Email Triage Assistant** from the side panel.
6. Click **Connect Gmail** if the add-on reports that the account is not connected.

Manifest/scope changes require reauthorization. A `Code.gs`-only change normally
requires updating the deployment but not adding a new OAuth scope.

## 4. Baseline live classification test

Use one recipient/triage account and a separate sender account. Turn auto-sort
off and keep the test messages unread until alerts are checked. Use future dates
relative to the day of testing.

Send these 13 messages before running triage. Prefixing subjects with `[E2E-Txx]`
makes cleanup straightforward.

| ID | Subject/body intent | Expected |
| --- | --- | --- |
| T01 | `Please approve the website copy`; asks for review and approval | `Needs Action`, high priority, unread alert |
| T02 | `What are your support hours?`; reusable support question | `FAQ`, Gmail draft created but not sent |
| T03 | `Security incident - unauthorized account access`; asks for immediate investigation | `Red Flag`, near-100 priority, unread alert |
| T04 | Payment receipt; explicitly says no action required | `Others`, no alert/deadline |
| T05 | Verification code expiring in minutes, no calendar date | `Others`, no deadline |
| T06 | Weekend sale/discount campaign | `Spam/Newsletter` |
| T07 | Promotional offer containing words such as urgent and act now | `Spam/Newsletter`, not an action/red-flag false positive |
| T08 | Sign and submit contract by an explicit future `YYYY-MM-DD` date | `Needs Action`, exact deadline saved |
| T09 | Submit report `tomorrow` | `Needs Action`, relative date resolved and saved |
| T10 | Informational event date with no due/deadline cue | `Others`, no deadline |
| T11 | Historical completed work with a past deadline | `Others`, no upcoming deadline |
| T12 | Final account suspension/security escalation | `Red Flag`, unread alert |
| T13 | Successful backup status; no action required | `Others`, no alert/deadline |

Run from either interface:

- Dashboard: choose the target date and `Up to 1 day`, then click **Run Triage**.
- Add-on: choose **Sort up to**, select `Up to 1 day`, then click **Run triage now**.

Verify:

1. All 13 messages have the expected visible category label.
2. T01, T03, and T12 appear under **Needs your attention** while unread.
3. T08 and T09 appear under **Upcoming deadlines**.
4. T02 creates one draft and sends no email.
5. A second run processes zero of the same messages because `AI-Processed` prevents duplication.
6. Opening an alert message removes it from the unread alert list.

Observed live result on 8 August 2026: **13/13 expected categories**.

## 5. Reply and conversation test

1. From the sender account, send a message asking the triage account to confirm a
   launch checklist. Run triage and verify `Needs Action`.
2. From the triage account, use Gmail's **Reply** button and send a response.
3. From the sender account, reply again in the same Gmail thread and request final approval.
4. Run triage again.

Expected:

- The incoming reply remains in the same Gmail thread.
- It inherits the prior non-spam category.
- Priority includes the ongoing-thread reason.
- The sender/thread becomes an active conversation and receives spam protection.

Observed: category `Needs Action`, priority score 100, same thread ID, active
conversation stored, and sender remembered as a known contact.

## 6. Adaptive correction test

Run this after baseline classification because correction memory intentionally
influences future messages.

1. Send an informational message with subject `Vendor compliance packet update`.
2. Run triage and change its category to `Needs Action` using either the dashboard
   category control or the open-message add-on dropdown and **Save & Teach Assistant**.
3. Send `Vendor compliance packet requires review` from the same sender.
4. Send a third, unrelated informational subject from that sender.
5. Run triage after each message.

Expected and observed:

- The correction immediately updates the current Gmail label and stored priority.
- The similar compliance subject becomes `Needs Action` from learned guidance
  without an LLM call.
- The unrelated subject is not trapped by the correction and remains eligible
  for Groq classification.
- `Others` never becomes a hard automatic rule.
- Public mailbox domains such as `gmail.com` are not learned as broad domain rules.

## 7. Add-on feature checks

### Homepage

- Status changes from Ready to running and back after triage.
- Date picker and range are sent together to the backend.
- **Needs your attention (N)** opens only unread action/red-flag items.
- **Upcoming deadlines (N)** opens future deadlines in date order.
- Auto-sort can be enabled and disabled.
- **Undo last sort** removes the last run's category/processed labels and allows reprocessing.

### Open-message card

1. Open a processed Gmail message.
2. Open the add-on side panel.
3. Confirm **Current Category** matches Gmail.
4. Change the category and click **Save & Teach Assistant**.
5. Click **Summarize this email**.

Expected:

- Category correction updates Gmail immediately and records subject-aware learning.
- Summary contains 2-4 factual bullets and preserves requests, dates, and amounts.
- No executable HTML or invented facts are displayed.

Observed during live testing: five attention items and two upcoming deadlines were
returned; contextual summaries were grounded in the selected message.

## 8. Reminder email test

Reminder sending is opt-in. Set and restart the backend:

```env
REMINDER_EMAILS_ENABLED=true
```

Keep an unread high-priority message older than 24 hours and/or a deadline within
seven days, then invoke the reminder job through the normal scheduler or the
`send_user_reminders` function for the connected test account.

Expected and observed:

- One reminder is sent from the connected Gmail account to itself.
- The tested email contained two valid upcoming deadlines.
- Promotional/spam mail was excluded.
- Running again immediately sent no duplicate.
- Reminder-generated mail was excluded from triage, preventing a loop.

Set `REMINDER_EMAILS_ENABLED=false` and restart after testing unless reminders
should remain active.

## 9. Second-account onboarding test

1. Add the second account as an OAuth consent-screen Test User.
2. Install/open the add-on under that account.
3. Click **Connect Gmail** and complete the backend OAuth flow.
4. Return to Gmail and refresh the add-on.
5. Verify add-on status reports connected and run a small one-message triage.

Observed: the second account's token was stored independently and the public
add-on status endpoint returned `connected=true`. No account credentials were
shared with the application operator.

## 10. Cleanup

Search Gmail for test messages:

```text
subject:"[E2E-"
```

Delete test messages and FAQ drafts as appropriate. Disable test-created learned
rules, restore auto-sort/reminder settings, and remove temporary test users or
expired callback URLs when no longer needed. Do not commit the test database.

## Current pilot constraints

- Use exactly one backend worker because run progress and locks are process-local.
- Store `APP_DB_PATH` on persistent storage; it contains OAuth tokens, settings,
  memory, deadlines, schedules, and priority metadata.
- Use a stable HTTPS backend for client use. Quick tunnels are suitable only for
  temporary testing because their hostnames expire.
- OAuth Testing mode requires explicitly listed Test Users and may show Google's
  unverified-app warning.
- Public rollout requires Google verification and the relevant restricted-scope
  security review.
