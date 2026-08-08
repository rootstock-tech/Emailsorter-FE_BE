"""Entry point: fetch unread emails, classify them, and apply Gmail labels."""

import logging
import time
from collections import Counter

from app.auto_reply import create_draft_reply
from app.classifier import classify_emails
from app.db import (
    DEFAULT_CATEGORIES,
    DEFAULT_FAQ_CATEGORY,
    get_active_rules,
    is_known_contact,
    record_triage_action,
    remember_contact,
    save_deadline,
    save_email_embedding,
    save_priority,
)
from app.deadlines import extract_deadline
from app.gmail_client import PROCESSED_LABEL, authenticate, fetch_unread_emails, thread_has_user_reply
from app.labeler import apply_label, get_or_create_label
from app.priority import compute_priority
from app.search import embed_text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("email_triage")


def _account_email(service):
    """Best-effort Gmail address for the connected account (used as a cache key
    so label ids are never shared across accounts). Returns None on failure."""
    try:
        return service.users().getProfile(userId="me").execute().get("emailAddress")
    except Exception:  # noqa: BLE001 - falling back to None is fine
        return None

# Result key holding the count of drafted auto-replies (a sub-count, not a
# separate email). Kept as a fixed string so the dashboard can render it as a
# distinct stat regardless of which category triggers drafting.
DRAFTED_KEY = "FAQ (drafted)"


# Name fragments that mark a category as spam/newsletter (bulk mail). Used so an
# active conversation is never left in one of these buckets.
_SPAMMY_HINTS = ("spam", "newsletter", "promo", "junk")


def _is_spammy(category):
    """True if ``category`` looks like a spam/newsletter bucket."""
    name = (category or "").lower()
    return any(hint in name for hint in _SPAMMY_HINTS)


def _conversation_target(categories):
    """Pick the category an active conversation should move to (never spam).

    Prefers "Needs Action" if the user has it (a live thread usually wants a
    reply), else the first non-spammy category, else the last one.
    """
    for name in categories:
        if name.lower() == "needs action":
            return name
    for name in categories:
        if not _is_spammy(name):
            return name
    return categories[-1] if categories else "Needs Action"


def triage(service, max_results=200, categories=None, faq_category=None, category_prompts=None, user_email=None, progress_cb=None, work_offset=0.0, should_cancel=None, sort_range=None, date=None, run_id=None):
    """Fetch unread emails, classify and label each, return counts.

    The caller supplies an authenticated Gmail ``service`` object, so the same
    pipeline can run for whichever user is appropriate (CLI or per-web-user).
    ``max_results`` caps how many unread emails are fetched per run.
    ``categories`` is the user's active category list and ``faq_category`` the
    category that triggers auto-reply drafting. When both are omitted, the
    built-in defaults are used (and drafting targets the default FAQ category).
    ``user_email``, when provided, enables storing a semantic embedding per
    email so it can be searched later.

    Progress: each email is worth 1 "work unit" -- 0.5 when it is classified and
    0.5 when it is labeled -- so the bar advances during both the (slow)
    classification phase and the labeling phase. ``progress_cb`` is called with
    the cumulative work units done (``work_offset`` plus this chunk's progress).
    ``should_cancel``, when provided, is polled before each email; if it returns
    True the chunk stops early.
    """
    if categories is None:
        categories = DEFAULT_CATEGORIES
        if faq_category is None:
            faq_category = DEFAULT_FAQ_CATEGORY

    emails = fetch_unread_emails(service, max_results=max_results, sort_range=sort_range, date=date)
    logger.info("Fetched %d unread email(s).", len(emails))
    fetched = len(emails)

    # Bail out before the (slow) classification if a stop was already requested.
    if should_cancel is not None and should_cancel():
        logger.info("Triage cancelled before classification.")
        return {}

    # Classification is the first half of each email's work unit.
    def _on_classified(n_classified):
        if progress_cb is not None:
            progress_cb(work_offset + n_classified * 0.5)

    # Load the user's learned rules once so senders/domains the LLM has already
    # settled on are sorted deterministically (and cheaply) this run.
    learned_rules = get_active_rules(user_email) if user_email else None

    classified = classify_emails(
        emails,
        categories=categories,
        faq_category=faq_category,
        category_prompts=category_prompts,
        user_email=user_email,
        learned_rules=learned_rules,
        progress_cb=_on_classified,
        should_cancel=should_cancel,
    )
    logger.info("Classified %d email(s).", len(classified))

    # Conversation-aware spam guard: if a mail was filed as spam/newsletter but
    # the user has already replied in that thread, it is a live conversation, so
    # move it out of spam. Only spam-classified mail triggers the (per-thread)
    # Gmail lookup, keeping the extra API calls to a minimum.
    conversation_target = _conversation_target(list(categories))
    for i, (email, category) in enumerate(zip(emails, classified)):
        if not _is_spammy(category):
            continue
        sender = email.get("sender")
        # Remembered contact -> never spam, and no Gmail call needed.
        if user_email and is_known_contact(user_email, sender):
            classified[i] = conversation_target
            logger.info("Known contact kept out of spam: %s", email.get("id", "<unknown>"))
            continue
        # Otherwise check the thread once; if the user replied, remember the
        # sender so future runs skip this lookup entirely.
        if thread_has_user_reply(service, email.get("thread_id")):
            classified[i] = conversation_target
            if user_email:
                try:
                    remember_contact(user_email, sender, reason="you replied in this thread")
                except Exception:  # noqa: BLE001 - memory write is best-effort
                    pass
            logger.info(
                "Kept conversation out of spam: %s -> %s",
                email.get("id", "<unknown>"),
                conversation_target,
            )

    counts = Counter()
    faq_drafted = 0
    processed_in_chunk = 0

    # Resolve the account once (used as the label-cache key) and pre-create a
    # label for every configured category, so all of the user's categories --
    # including newly added ones -- show up in Gmail even before an email is
    # sorted into them.
    account_key = user_email or _account_email(service)
    category_label_ids = {}
    for category_name in categories:
        try:
            category_label_ids[category_name] = get_or_create_label(
                service, category_name, account_key=account_key
            )
        except Exception as exc:  # noqa: BLE001 - one label failure must not stop the run
            logger.error("Could not pre-create label %r: %s", category_name, exc)
    try:
        processed_label_id = get_or_create_label(
            service, PROCESSED_LABEL, hidden=True, account_key=account_key
        )
    except Exception as exc:  # noqa: BLE001 - keep going without the internal label
        logger.error("Could not create the %s label: %s", PROCESSED_LABEL, exc)
        processed_label_id = None

    for email, category in zip(emails, classified):
        if should_cancel is not None and should_cancel():
            logger.info(
                "Triage cancelled mid-chunk after %d labeled email(s).",
                processed_in_chunk,
            )
            break

        message_id = email.get("id", "<unknown>")

        try:
            label_id = category_label_ids.get(category)
            if label_id is None:
                # A category produced by classification that was not in the
                # configured list (e.g. the default fallback) -- create it now.
                label_id = get_or_create_label(
                    service, category, account_key=account_key
                )
                category_label_ids[category] = label_id
            apply_label(service, message_id, label_id)

            # Record the applied category label so this run can be undone later.
            if run_id and user_email:
                try:
                    record_triage_action(
                        run_id, user_email, message_id, label_id, category
                    )
                except Exception as exc:  # noqa: BLE001 - undo log is best-effort
                    logger.warning("Could not record undo action for %s: %s", message_id, exc)

            # Mark as processed (with an internal, hidden label) so the *next*
            # fetch skips this email instead of re-processing it.
            if processed_label_id is not None:
                apply_label(service, message_id, processed_label_id)
        except Exception as exc:  # noqa: BLE001 - keep the batch going
            logger.error(
                "Labeling failed for %s (category %s): %s",
                message_id,
                category,
                exc,
            )
            continue

        counts[category] += 1
        processed_in_chunk += 1

        # Labeling is the second half of each email's work unit. Report as soon
        # as the email is labeled, before the slower draft/embedding steps.
        if progress_cb is not None:
            progress_cb(work_offset + fetched * 0.5 + processed_in_chunk * 0.5)

        # Draft a reply for the configured auto-reply category. Draft creation
        # never raises (returns None on failure), so it cannot stop the batch.
        if faq_category is not None and category == faq_category:
            if create_draft_reply(service, email) is not None:
                faq_drafted += 1

        # Score this email's priority (deterministic, no LLM) so the dashboard
        # can surface the most important mail first, with a short reason.
        if user_email:
            try:
                score, reason = compute_priority(email, category, learned_rules)
                save_priority(
                    user_email,
                    email.get("id", ""),
                    email.get("thread_id", ""),
                    email.get("sender", ""),
                    email.get("subject", "") or "",
                    category,
                    score,
                    reason,
                    email.get("date", ""),
                )
            except Exception as exc:  # noqa: BLE001 - priority is best-effort
                logger.warning("Failed to score priority for %s: %s", message_id, exc)

        # Extract an explicit deadline (if any) so the user can be reminded
        # before it. Best-effort and deterministic -- never blocks the batch.
        if user_email:
            try:
                due_date, description = extract_deadline(email)
                if due_date:
                    save_deadline(
                        user_email,
                        email.get("id", ""),
                        email.get("thread_id", ""),
                        email.get("subject", "") or "",
                        email.get("sender", ""),
                        due_date,
                        description,
                    )
            except Exception as exc:  # noqa: BLE001 - deadline capture is best-effort
                logger.warning("Failed to extract deadline for %s: %s", message_id, exc)

        # Store a semantic embedding so this email is searchable later. This is
        # best-effort: any failure is logged and skipped so it never breaks
        # labeling or the rest of the batch.
        if user_email:
            try:
                subject = email.get("subject", "") or ""
                body = email.get("body", "") or ""
                vector = embed_text(f"{subject}\n{body[:500]}")
                save_email_embedding(
                    user_email,
                    email.get("id", ""),
                    email.get("thread_id", ""),
                    email.get("sender", ""),
                    subject,
                    body[:200],
                    email.get("date", ""),
                    vector,
                )
            except Exception as exc:  # noqa: BLE001 - embedding is best-effort
                logger.warning(
                    "Failed to store embedding for %s: %s", message_id, exc
                )

    result = dict(counts)
    if faq_category is not None and counts.get(faq_category):
        # Separate key so the auto-reply category's own count keeps its meaning.
        result[DRAFTED_KEY] = faq_drafted
    return result


def triage_until_empty(service, chunk_size=200, categories=None, faq_category=None, category_prompts=None, user_email=None, progress_cb=None, should_cancel=None, on_chunk_done=None, sort_range=None, date=None, max_total=None, run_id=None):
    """Run triage() in chunks until the inbox is caught up; return merged counts.

    Repeatedly processes up to ``chunk_size`` emails per iteration, merging the
    per-category counts. Stops as soon as an iteration processes 0 real emails
    (inbox caught up in the range), ``should_cancel`` returns True, or the
    cumulative processed count reaches ``max_total`` (the range's cap).
    ``progress_cb`` receives cumulative work units (see triage()), carried
    across chunks. ``on_chunk_done(chunk_index, processed)`` runs after each
    chunk (used to unlock search once the first chunk's emails are embedded).
    """
    merged = Counter()
    state = {"units": 0.0}
    total_processed = 0

    def _report(value):
        state["units"] = value
        if progress_cb is not None:
            progress_cb(value)

    chunk_index = 0
    while True:
        if should_cancel is not None and should_cancel():
            break

        this_chunk = chunk_size
        if max_total is not None:
            remaining = max_total - total_processed
            if remaining <= 0:
                break
            this_chunk = min(chunk_size, remaining)

        counts = triage(
            service,
            max_results=this_chunk,
            categories=categories,
            faq_category=faq_category,
            category_prompts=category_prompts,
            user_email=user_email,
            progress_cb=_report,
            work_offset=state["units"],
            should_cancel=should_cancel,
            sort_range=sort_range,
            date=date,
            run_id=run_id,
        )

        # Real emails processed this iteration (the drafted sub-count is not a
        # separate email, so it does not count toward "caught up").
        processed = sum(v for k, v in counts.items() if k != DRAFTED_KEY)

        for key, value in counts.items():
            merged[key] += value

        total_processed += processed
        chunk_index += 1
        if on_chunk_done is not None:
            on_chunk_done(chunk_index, processed)

        if processed == 0:
            break
        if should_cancel is not None and should_cancel():
            break
        if max_total is not None and total_processed >= max_total:
            break

        time.sleep(2)

    return dict(merged)


def print_summary(counts):
    """Print a per-category count summary."""
    if not counts:
        print("No emails were labeled.")
        return

    summary = ", ".join(
        f"{category}: {count}" for category, count in sorted(counts.items())
    )
    print(summary)


if __name__ == "__main__":
    print_summary(triage(authenticate()))
