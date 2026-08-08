# Email Triage Assistant - Manual E2E Test Email Pack

This pack contains ready-to-send emails that exercise the complete triage flow:
all five default categories, batched LLM classification, deterministic spam,
alerts, priority, FAQ drafts, deadlines, replies, conversation memory, learning,
summarization, reminders, re-run protection, and undo.

## Test accounts

- **Recipient / triage account:** `anjaneyatiwarii@gmail.com`
- **Sender / second account:** replace `{{TEST_SENDER}}` with the second account.
- Use a fresh sender that has not been corrected before, if possible.

## Important setup before sending

1. Open the dashboard and temporarily set **Auto-sort = Off**. This keeps all
   baseline messages together so the run covers more than one LLM batch.
2. In **Learned rules**, temporarily disable any sender rule matching the test
   sender. If a broad rule exists for the sender's domain, disable it for the
   baseline run and restore it afterward.
3. Keep the inbox closed while sending T01-T13. In particular, do not open T01,
   T03, or T12 because the alert test needs them to remain unread.
4. Send every baseline email from the same second account unless a case says
   otherwise. Prefixes such as `[E2E-T01]` make cleanup easy.
5. The dates below assume testing on **8 August 2026**. If testing later, replace:
   - `2026-08-09` / `tomorrow` with the next day.
   - `2026-08-12` and `12 August 2026` with a future date within seven days.
   - `2026-08-01` with any past date.

## Phase A - Baseline batch

Send T01-T13 before running triage. The 13-message set crosses the classifier's
10-message batch size, so it also verifies multi-batch processing.

### T01 - Direct action request

**Subject**

```text
[E2E-T01] Please approve the website copy
```

**Body**

```text
Hi,

Please review the attached website copy and reply with either approval or the
changes you need. I cannot publish it until I receive your decision.

Thanks
```

**Expected**

- Label: `Needs Action`
- High priority, normally around 90+ for a recent direct sender
- Appears in add-on **Needs your attention** while unread
- No deadline entry

---

### T02 - Reusable FAQ question

**Subject**

```text
[E2E-T02] What are your support hours?
```

**Body**

```text
Hello,

What are your normal support hours, and which email address should customers use
for general support questions?

Regards
```

**Expected**

- Label: `FAQ`
- One reply appears in Gmail **Drafts**; it must not be sent automatically
- Medium priority

---

### T03 - Serious security red flag

**Subject**

```text
[E2E-T03] Security incident - unauthorized account access
```

**Body**

```text
This is a formal security escalation. An unknown person accessed our account and
changed the recovery phone number. Please investigate immediately and confirm
the account has been secured.
```

**Expected**

- Label: `Red Flag`
- Priority score should be at or near 100
- Appears in add-on **Needs your attention** while unread

---

### T04 - Informational payment receipt

**Subject**

```text
[E2E-T04] Payment receipt for order RS-2048
```

**Body**

```text
Your payment of INR 2,499 was received successfully. Order RS-2048 is confirmed.
No action is required. Keep this message for your records.
```

**Expected**

- Label: `Others`
- No alert and no deadline
- Lower priority than action/red-flag mail

---

### T05 - Informational verification code

**Subject**

```text
[E2E-T05] Your verification code is 481920
```

**Body**

```text
Use verification code 481920 to complete your sign-in. This code expires in ten
minutes. If you did not request it, you can ignore this email.
```

**Expected**

- Label: `Others`
- No deadline row; there is no parseable calendar date

---

### T06 - Normal marketing promotion

**Subject**

```text
[E2E-T06] Weekend sale - save 50 percent
```

**Body**

```text
Our biggest sale is live. Buy any annual plan this weekend and get 50 percent
off. Browse the offers and upgrade today. This is a promotional campaign.
```

**Expected**

- Label: `Spam/Newsletter`
- No alert
- Low priority

---

### T07 - Marketing that uses fake urgency

**Subject**

```text
[E2E-T07] URGENT - act now to claim your exclusive offer
```

**Body**

```text
Urgent: act now before this sales offer disappears. Purchase our premium course
today and receive a free bonus. This message is an advertisement, not a personal
request.
```

**Expected**

- Label: `Spam/Newsletter`, not `Red Flag` or `Needs Action`
- Verifies that promotional intent wins over words such as urgent/act now

---

### T08 - Action plus explicit ISO deadline

**Subject**

```text
[E2E-T08] Submit the signed contract by 2026-08-12
```

**Body**

```text
Please sign the attached contract and submit it by 2026-08-12. We cannot start
the project until the signed copy is received. The submission deadline is
2026-08-12.
```

**Expected**

- Label: `Needs Action`
- Deadline saved as `2026-08-12`
- Appears in **Upcoming deadlines**
- High priority

---

### T09 - Action plus relative deadline (LLM fallback)

**Subject**

```text
[E2E-T09] Report is due tomorrow
```

**Body**

```text
Please finish the weekly report and submit it tomorrow. Reply after uploading the
final file.
```

**Expected when sent on 8 August 2026**

- Label: `Needs Action`
- Deadline saved as `2026-08-09`
- Verifies the structured Groq deadline fallback and ISO validation

---

### T10 - Date without a deadline cue

**Subject**

```text
[E2E-T10] Team meetup information for 15 August 2026
```

**Body**

```text
For your information, the team meetup is on 15 August 2026. The venue is already
booked and no response or action is required.
```

**Expected**

- Label: normally `Others`
- **No deadline** entry because the email does not say due/deadline/respond by

---

### T11 - Past deadline must not create a reminder

**Subject**

```text
[E2E-T11] Historical record - deadline was 1 August 2026
```

**Body**

```text
For record keeping only: the old submission deadline was 1 August 2026 and the
work was completed. No action is required now.
```

**Expected**

- Label: normally `Others`
- No upcoming deadline because the date is in the past

---

### T12 - Legal/account escalation

**Subject**

```text
[E2E-T12] Final notice regarding account suspension
```

**Body**

```text
This is a final account escalation. Service will be suspended because the
identity review failed. A responsible account owner must contact us immediately
to prevent disruption.
```

**Expected**

- Label: `Red Flag`
- Near-100 priority
- Add-on unread alert

---

### T13 - Routine status notification

**Subject**

```text
[E2E-T13] Backup completed successfully
```

**Body**

```text
The scheduled backup completed successfully. All 1,250 files were copied and no
errors were found. This is an informational notification; no action is required.
```

**Expected**

- Label: `Others`
- No alert, deadline, or draft

## Run and verify Phase A

1. Without opening the test emails, open the Gmail add-on homepage.
2. Select **Up to 1 day** and click **Run triage now**.
3. Wait until status returns to Ready/Done.
4. In Gmail, confirm every message has its expected category plus the hidden
   processing behavior (a second run should not process it again).
5. Open the add-on homepage and verify:
   - T01, T03, and T12 are pinned while unread.
   - T08 and T09 appear under Upcoming deadlines.
   - Priority inbox ranks Red Flag and Needs Action above Others/Spam.
6. Open T01 in Gmail, then close/reopen the add-on. T01 should disappear from
   alerts while T03/T12 remain if still unread.
7. Check Gmail Drafts for the T02 FAQ draft. Do not send it during this check.
8. Run triage again without sending new messages. Expected last-run count: zero.

### Reset learning before Phase B

Because T01, T08, and T09 are three `Needs Action` observations from the same
sender, the baseline can intentionally activate the three-hit sender/domain
learning rule. This confirms automatic learning, but it would make later tests
pass without exercising reply or manual-correction logic.

Before Phase B:

1. Record/screenshot the learned rule as evidence of three-hit learning.
2. Delete the rules created for `{{TEST_SENDER}}`.
3. If all test mail came from a public domain such as `gmail.com`, remove or
   disable the test-created domain rule too, then restore any pre-existing rule
   after testing. A fresh test domain/alias is preferable.

## Optional deterministic newsletter rule

The hard rule checks the actual From header for `newsletter@`, `marketing@`, or
`noreply-updates@`. A normal Gmail address cannot test this exact rule merely by
changing the subject.

If an alias such as `marketing@your-test-domain.com` is available, send:

**Subject**

```text
[E2E-D01] Personal question hidden inside marketing mail
```

**Body**

```text
Can you personally approve this request? This message comes from a marketing
sender and is used to prove the deterministic sender rule runs before the LLM.
```

**Expected:** `Spam/Newsletter` without relying on LLM classification.

## Phase B - Real reply and conversation memory

This test must use Gmail's **Reply** button so the same thread and reply headers
are preserved.

### T14A - Start a direct project thread

From the second account, send:

**Subject**

```text
[E2E-T14] Confirm the launch checklist
```

**Body**

```text
Please review the launch checklist and confirm whether all deployment tasks are
complete.
```

Run triage. Expected: `Needs Action`.

### T14B - Prove user participation

1. From the triage account, open T14A and click **Reply**.
2. Send: `I am reviewing it now. Please share the final hosting confirmation.`
3. From the second account, reply in the same thread:

```text
The hosting is confirmed. Please approve the final launch today. Also, our new
premium offer is available, but the main purpose of this reply is launch approval.
```

4. Run triage again.

**Expected**

- Incoming reply is detected from `In-Reply-To` / `References`.
- It inherits the previous non-spam thread category (`Needs Action`).
- Priority reason includes `reply in an ongoing thread`.
- Thread/participant becomes an active conversation and is protected from spam.

### T14C - False reply-header safety

An automated incoming follow-up that references an old message must not become a
trusted contact unless Gmail shows a message sent by the triage account in that
thread. This is covered automatically; manual verification requires a separate
thread where the triage account never replies.

## Phase C - Learning from a manual correction (run last)

Run this phase last because a manual correction intentionally affects future mail
from the same sender.

### T15A - Create a correction

Send:

**Subject**

```text
[E2E-T15] Vendor compliance packet update
```

**Body**

```text
The vendor compliance packet has been uploaded for reference. The system has
recorded the upload successfully.
```

1. Run triage.
2. In the dashboard Priority inbox, change this sender's category to
   `Needs Action` using the dropdown.
3. Verify Learned Rules shows an active sender correction.

The stored correction includes sender, domain, subject keywords, old category,
new category, weight, and timestamp.

### T15B - Verify learned override

Send after T15A is corrected:

**Subject**

```text
[E2E-T15] Vendor compliance packet requires review
```

**Body**

```text
The updated compliance packet is available. Please check the vendor documents.
```

**Expected:** `Needs Action` from learned memory before the LLM.

### T15C - Verify Others remains temporary

After cleaning up/disabling the T15 sender correction, send three informational
messages that belong to Others. Verify that no active `Others` sender rule is
created; future messages remain eligible for LLM reconsideration.

## Phase D - One-click summary

1. Open T08 or T03 in Gmail.
2. Open the contextual Email Triage Assistant add-on.
3. Click **Summarize this email**.

**Expected**

- A 2-4 bullet summary appears.
- T08 summary mentions signing/submitting and `2026-08-12`.
- T03 summary mentions unauthorized access and required investigation.
- No invented facts or executable HTML appears.

## Phase E - Automated reminder emails

Automatic sending is opt-in. On the backend set and restart:

```env
REMINDER_EMAILS_ENABLED=true
```

### E1 - Upcoming deadline reminder (can test immediately)

- Keep T08/T09 deadline within the next seven days.
- Trigger the single-user reminder function deliberately:

```powershell
.venv\Scripts\python.exe -c "from app.server import _service_for_user; from app.reminders import send_user_reminders; u='anjaneyatiwarii@gmail.com'; print(send_user_reminders(_service_for_user(u), u))"
```

**Expected**

- One summary email is sent from the connected Gmail account to itself.
- It lists T08/T09 under upcoming deadlines.
- Re-running immediately does not resend the same deadline reminder.
- The reminder email itself is excluded from triage, preventing a reminder loop.

### E2 - 24-hour unread attention reminder

This test cannot be faked by changing an email's Date header: the app uses Gmail's
trusted `internalDate`.

1. Send a Needs Action/Red Flag message at least 24 hours before the test, or use
   an existing unread high-priority message older than 24 hours.
2. Keep it unread.
3. Run the same single-user command above.

**Expected**

- It appears under unread mail needing attention.
- Re-running within 24 hours does not send another reminder.
- Maximum reminder count is three per attention message.

After testing, disable automated sending unless it should remain active:

```env
REMINDER_EMAILS_ENABLED=false
```

Restart the backend after changing the environment setting.

## Phase F - Auto-sort, undo, and cleanup

### Auto-sort

1. Set Auto-sort to 5 minutes.
2. Send one new message from the second account.
3. Do not click Run Triage.
4. Within the next scheduler cycle, verify a category label appears.
5. Set Auto-sort back to the desired production interval or Off.

### Undo

1. Run a small test containing one or two fresh messages.
2. Click **Undo last sort**.
3. Verify category labels and the AI-Processed marker behavior are reverted.
4. Run triage again and verify those messages can be processed again.

### Cleanup

Search Gmail for:

```text
subject:"[E2E-"
```

Remove test messages/drafts as needed. Restore any learned sender/domain rules
temporarily disabled during setup. Disable the T15 correction if the second
account will be used for unrelated future testing.

## Result sheet

| ID | Expected result | Pass/Fail | Actual result / notes |
| --- | --- | --- | --- |
| T01 | Needs Action + unread alert + high priority |  |  |
| T02 | FAQ + draft created, never auto-sent |  |  |
| T03 | Red Flag + unread alert + near-100 priority |  |  |
| T04 | Others, no alert/deadline |  |  |
| T05 | Others, no calendar deadline |  |  |
| T06 | Spam/Newsletter |  |  |
| T07 | Spam despite fake urgency |  |  |
| T08 | Needs Action + explicit deadline |  |  |
| T09 | Needs Action + relative deadline fallback |  |  |
| T10 | Date present but no deadline |  |  |
| T11 | Past date rejected |  |  |
| T12 | Red Flag + alert |  |  |
| T13 | Others routine notification |  |  |
| D01 | Deterministic newsletter sender (optional) |  |  |
| T14 | Reply inheritance + conversation protection |  |  |
| T15 | Manual correction + learned override |  |  |
| D | Gmail-ID one-click summary |  |  |
| E1 | Deadline reminder + deduplication |  |  |
| E2 | 24-hour unread reminder + count/gap |  |  |
| F | Auto-sort + undo + reprocessing |  |  |

## Acceptance criteria

The manual run passes when:

- All baseline emails receive the expected category or a documented LLM variance
  is reviewed and corrected.
- Alerts include only still-unread Needs Action/Red Flag mail.
- FAQ creates a draft but sends nothing automatically.
- Valid future deadlines appear; no-cue and past dates do not.
- Replies inherit thread context only after real user participation.
- Manual correction changes future classification and can be disabled.
- Summarize returns grounded bullets for the currently open Gmail message.
- Reminder emails deduplicate correctly and do not triage themselves.
- A second run skips already processed mail; Undo makes selected mail processable.