import json
import asyncio
import tempfile
import unittest
from datetime import date, datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import BackgroundTasks

from app import config, db
from app import server
from app.classifier import classify_emails
from app.deadlines import extract_deadline, extract_deadline_with_llm
from app.gmail_client import _unread_query, authenticate_from_token_json, unread_message_ids
from app.main import _has_user_participation
from app.priority import compute_priority
from app.reminders import send_user_reminders
from app.summarize import summarize_email


ROOT = Path(__file__).resolve().parents[1]


class DatabaseRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = Path(self.temp_dir.name) / "test.db"
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_manual_correction_becomes_active_sender_rule(self):
        user = "user@example.com"
        sender = "Alice <alice@acme.com>"

        db.record_user_correction(user, sender, "Needs Action")

        active = db.get_active_rules(user)
        self.assertEqual(db.match_learned_rule(active, sender), "Needs Action")

    def test_weighted_correction_captures_subject_and_old_category(self):
        user = "user@example.com"
        db.record_user_correction(
            user,
            "Billing <billing@acme.com>",
            "Needs Action",
            subject="Invoice approval required",
            old_category="Others",
        )

        active = db.get_active_rules(user)
        self.assertEqual(
            db.match_weighted_rule(
                active,
                {"sender": "billing@acme.com", "subject": "Invoice approval required today"},
            ),
            "Needs Action",
        )
        self.assertIn("invoice", active["weighted"][0]["keyword_signature"])
        self.assertTrue(active["context"])

    def test_manual_sender_correction_does_not_override_unrelated_subject(self):
        user = "user@example.com"
        db.record_user_correction(
            user,
            "LinkedIn <messages@linkedin.com>",
            "Spam/Newsletter",
            subject="Weekly network digest suggestions",
        )
        active = db.get_active_rules(user)

        self.assertIsNone(
            db.match_weighted_rule(
                active,
                {
                    "sender": "messages@linkedin.com",
                    "subject": "Security alert: reset your compromised account",
                },
            )
        )

    def test_latest_manual_correction_remains_active_after_category_change(self):
        user = "user@example.com"
        sender = "Billing <billing@acme.com>"
        db.record_user_correction(
            user,
            sender,
            "Others",
            subject="Weekly billing digest",
        )
        db.record_user_correction(
            user,
            sender,
            "Needs Action",
            subject="Invoice approval required",
            old_category="Others",
        )

        active = db.get_active_rules(user)
        self.assertNotIn("billing@acme.com", active["disabled_senders"])
        self.assertEqual(
            db.match_weighted_rule(
                active,
                {
                    "sender": "billing@acme.com",
                    "subject": "Invoice approval required today",
                },
            ),
            "Needs Action",
        )

    def test_legacy_public_domain_rule_is_absent_from_prompt_context(self):
        user = "user@example.com"
        conn = db._connect()
        try:
            conn.execute(
                """
                INSERT INTO learned_rules
                    (user_email, match_type, match_value, category, hits, active)
                VALUES (?, 'domain', 'gmail.com', 'Spam/Newsletter', 5, 1)
                """,
                (user,),
            )
            conn.commit()
        finally:
            conn.close()

        active = db.get_active_rules(user)
        self.assertNotIn("gmail.com", active["domain"])
        self.assertFalse(any("gmail.com" in item for item in active["context"]))

    def test_disabled_weighted_correction_no_longer_matches(self):
        user = "user@example.com"
        db.record_user_correction(
            user,
            "Billing <billing@acme.com>",
            "Needs Action",
            subject="Invoice approval",
        )
        db.set_rule_active(user, "sender", "billing@acme.com", "Needs Action", False)
        active = db.get_active_rules(user)
        self.assertIsNone(
            db.match_weighted_rule(
                active,
                {"sender": "billing@acme.com", "subject": "Invoice approval"},
            )
        )

    def test_automatic_others_rule_is_not_recorded(self):
        user = "user@example.com"
        sender = "reports@acme.com"

        db.record_llm_decision(user, sender, "Others")

        self.assertEqual(db.list_learned_rules(user), [])

    def test_public_mail_provider_never_creates_domain_rule(self):
        user = "user@example.com"
        for _ in range(3):
            db.record_llm_decision(
                user,
                "Person <person@gmail.com>",
                "Needs Action",
            )

        rules = db.list_learned_rules(user)
        self.assertTrue(any(rule["match_type"] == "sender" for rule in rules))
        self.assertFalse(any(rule["match_type"] == "domain" for rule in rules))

    def test_legacy_public_domain_rule_is_ignored(self):
        active = {
            "sender": {},
            "domain": {"gmail.com": "Needs Action"},
        }
        self.assertIsNone(
            db.match_learned_rule(active, "different-person@gmail.com")
        )

    def test_legacy_active_others_rule_is_deactivated_on_upgrade(self):
        user = "user@example.com"
        conn = db._connect()
        try:
            conn.execute(
                "INSERT INTO learned_rules "
                "(user_email, match_type, match_value, category, hits, active) "
                "VALUES (?, 'sender', 'reports@acme.com', 'Others', 3, 1)",
                (user,),
            )
            conn.commit()
        finally:
            conn.close()

        db.init_db()

        rule = db.list_learned_rules(user)[0]
        self.assertFalse(rule["active"])

    def test_known_contact_is_stable_across_display_name_changes(self):
        user = "user@example.com"
        db.remember_contact(user, "Alice <alice@acme.com>", "user replied")

        self.assertTrue(db.is_known_contact(user, "Alice Cooper <alice@acme.com>"))
        self.assertEqual(db.known_contacts(user), {"alice@acme.com"})
        self.assertEqual(db.list_known_contacts(user)[0]["reason"], "user replied")

    def test_conversation_memory_preserves_active_status_and_updates_category(self):
        user = "user@example.com"
        db.remember_conversation(user, "thread-1", "Alice <alice@acme.com>", status="active", category="Needs Action")
        db.remember_conversation(user, "thread-1", "alice@acme.com", status="observed", category="FAQ")

        conversation = db.get_conversation(user, "thread-1")
        self.assertEqual(conversation["status"], "active")
        self.assertEqual(conversation["last_category"], "FAQ")
        self.assertEqual(conversation["participant"], "alice@acme.com")

    def test_deadlines_are_upserted_and_ordered(self):
        user = "user@example.com"
        db.save_deadline(user, "m2", "t2", "Later", "b@example.com", "2099-08-20", "Later")
        db.save_deadline(user, "m1", "t1", "Sooner", "a@example.com", "2099-08-10", "Sooner")
        db.save_deadline(user, "m1", "t1", "Sooner updated", "a@example.com", "2099-08-09", "Sooner updated")

        items = db.upcoming_deadlines(user)

        self.assertEqual([item["gmail_id"] for item in items], ["m1", "m2"])
        self.assertEqual(items[0]["due_date"], "2099-08-09")

    def test_spam_deadline_is_hidden_and_can_be_deleted(self):
        user = "user@example.com"
        db.save_deadline(user, "spam-1", "t1", "Sale expires today", "seller@example.com", "2099-08-10", "Sale")
        db.save_priority(user, "spam-1", "t1", "seller@example.com", "Sale expires today", "Spam/Newsletter", 5, "promo", "2099-08-01")

        self.assertEqual(db.upcoming_deadlines(user), [])
        db.delete_deadline(user, "spam-1")
        conn = db._connect()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM deadlines WHERE user_email = ? AND gmail_id = ?",
                (user, "spam-1"),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 0)

    def test_reminder_state_enforces_gap_and_max_count(self):
        user = "user@example.com"
        now = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)
        self.assertTrue(db.reminder_due(user, "m1", "attention", now=now))
        db.mark_reminded(user, "m1", "attention", now=now)
        self.assertFalse(db.reminder_due(user, "m1", "attention", now=now))
        self.assertTrue(
            db.reminder_due(
                user,
                "m1",
                "attention",
                now=datetime(2026, 8, 9, 13, tzinfo=timezone.utc),
            )
        )
        db.mark_reminded(user, "m1", "attention", now=datetime(2026, 8, 9, 13, tzinfo=timezone.utc))
        db.mark_reminded(user, "m1", "attention", now=datetime(2026, 8, 10, 14, tzinfo=timezone.utc))
        self.assertFalse(
            db.reminder_due(
                user,
                "m1",
                "attention",
                now=datetime(2026, 8, 12, 14, tzinfo=timezone.utc),
            )
        )

    def test_one_time_schedule_persists_and_deletes(self):
        db.save_scheduled_triage(
            "user@example.com",
            "2026-08-09T12:00:00+05:30",
            "1w",
        )
        self.assertEqual(
            db.list_scheduled_triage(),
            [{
                "email": "user@example.com",
                "run_at": "2026-08-09T12:00:00+05:30",
                "range": "1w",
            }],
        )
        db.delete_scheduled_triage("user@example.com")
        self.assertEqual(db.list_scheduled_triage(), [])


class ClassifierRegressionTests(unittest.TestCase):
    def test_high_confidence_newsletter_rule_skips_llm(self):
        emails = [{"sender": "newsletter@example.com", "subject": "Weekly", "body": "News"}]
        with patch("app.classifier._classify_batch_with_groq") as groq:
            result = classify_emails(emails, categories=["Spam/Newsletter", "Others"])

        self.assertEqual(result, ["Spam/Newsletter"])
        groq.assert_not_called()

    def test_others_rule_is_temporary_and_specific_result_is_learned(self):
        emails = [{"sender": "reports@acme.com", "subject": "Status", "body": "Please approve"}]
        active = {"sender": {"reports@acme.com": "Others"}, "domain": {}}
        observations = []
        with (
            patch("app.classifier.classify_by_rules", return_value=None),
            patch("app.classifier._classify_batch_with_groq", return_value=["Needs Action"]) as groq,
            patch("app.classifier.record_llm_decision", side_effect=lambda *args: observations.append(args)),
        ):
            result = classify_emails(
                emails,
                categories=["Needs Action", "Others"],
                default_category="Others",
                user_email="user@example.com",
                learned_rules=active,
            )

        self.assertEqual(result, ["Needs Action"])
        groq.assert_called_once()
        self.assertEqual(observations[0][-1], "Needs Action")

    def test_automatic_spam_rule_does_not_override_important_current_mail(self):
        emails = [{
            "sender": "notifications-noreply@linkedin.com",
            "subject": "Your account was compromised - reset access",
            "body": "We detected unauthorized access. Secure your account now.",
        }]
        active = {
            "sender": {"notifications-noreply@linkedin.com": "Spam/Newsletter"},
            "domain": {"linkedin.com": "Spam/Newsletter"},
            "weighted": [],
            "context": ["Historically this sender was often Spam/Newsletter"],
        }
        with (
            patch("app.classifier.classify_by_rules", return_value=None),
            patch("app.classifier._classify_batch_with_groq", return_value=["Red Flag"]) as groq,
        ):
            result = classify_emails(
                emails,
                categories=["Red Flag", "Spam/Newsletter", "Others"],
                default_category="Others",
                learned_rules=active,
            )

        self.assertEqual(result, ["Red Flag"])
        groq.assert_called_once()

    def test_others_llm_result_is_not_promoted_to_a_rule(self):
        emails = [{"sender": "reports@acme.com", "subject": "Status", "body": "FYI"}]
        with (
            patch("app.classifier.classify_by_rules", return_value=None),
            patch("app.classifier._classify_batch_with_groq", return_value=["Others"]),
            patch("app.classifier.record_llm_decision") as record,
        ):
            result = classify_emails(
                emails,
                categories=["Needs Action", "Others"],
                default_category="Others",
                user_email="user@example.com",
            )

        self.assertEqual(result, ["Others"])
        record.assert_not_called()


class FeatureRegressionTests(unittest.TestCase):
    def test_deadline_requires_cue_and_chooses_earliest_future_date(self):
        email = {
            "subject": "Submit documents by 15 Aug 2026",
            "body": "The final deadline is 20 Aug 2026.",
        }
        self.assertEqual(
            extract_deadline(email, today=date(2026, 8, 8)),
            ("2026-08-15", email["subject"]),
        )
        self.assertEqual(
            extract_deadline({"subject": "Meeting 15 Aug 2026", "body": ""}, today=date(2026, 8, 8)),
            (None, None),
        )

    def test_llm_deadline_fallback_validates_structured_future_date(self):
        payload = '{"has_deadline":true,"date":"2026-08-09","what":"Submit report"}'
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=payload))]
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: response))
        )
        with patch("app.deadlines._get_client", return_value=client):
            result = extract_deadline_with_llm(
                {"subject": "Report due tomorrow", "body": "Please submit tomorrow."},
                today=date(2026, 8, 8),
            )
        self.assertEqual(result, ("2026-08-09", "Submit report"))

    def test_llm_deadline_rejects_ungrounded_date(self):
        payload = '{"has_deadline":true,"date":"2026-08-20","what":"Invented"}'
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=payload))]
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: response))
        )
        with patch("app.deadlines._get_client", return_value=client):
            result = extract_deadline_with_llm(
                {"subject": "Please respond before launch", "body": "No date was provided."},
                today=date(2026, 8, 8),
            )
        self.assertEqual(result, (None, None))

    def test_incoming_reply_headers_do_not_imply_user_participation(self):
        email = {"is_reply": True, "thread_id": "thread-1"}
        with patch("app.main.thread_has_user_reply", return_value=False):
            self.assertFalse(
                _has_user_participation(object(), email, None, "Needs Action")
            )

    def test_reply_gets_priority_boost(self):
        now = format_datetime(datetime.now(timezone.utc))
        normal_score, _ = compute_priority({"sender": "alice@example.com", "date": now}, "Needs Action")
        reply_score, reason = compute_priority(
            {"sender": "alice@example.com", "date": now, "is_reply": True},
            "Needs Action",
        )

        self.assertGreater(reply_score, normal_score)
        self.assertIn("reply in an ongoing thread", reason)

    def test_priority_recency_accepts_pipeline_iso_timestamp(self):
        now = datetime.now(timezone.utc).isoformat()

        score, reason = compute_priority(
            {"sender": "alice@example.com", "date": now},
            "Others",
        )

        self.assertEqual(score, 40)
        self.assertIn("just arrived", reason)

    def test_unread_helper_drops_read_and_failed_messages(self):
        labels = {"unread": ["INBOX", "UNREAD"], "read": ["INBOX"]}

        class Request:
            def __init__(self, message_id):
                self.message_id = message_id

            def execute(self):
                if self.message_id == "missing":
                    raise RuntimeError("gone")
                return {"labelIds": labels[self.message_id]}

        class Messages:
            def get(self, userId, id, format):
                return Request(id)

        class Users:
            def messages(self):
                return Messages()

        class Service:
            def users(self):
                return Users()

        self.assertEqual(unread_message_ids(Service(), ["unread", "read", "missing"]), {"unread"})

    def test_summarize_returns_model_text_and_handles_failure(self):
        content = "- Action required\n- Due Friday"
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **kwargs: response)
            )
        )
        with patch("app.summarize._get_client", return_value=client):
            self.assertEqual(summarize_email("Subject", "Body", "Alice"), content)

        broken = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("down")))
            )
        )
        with (
            patch("app.summarize._get_client", return_value=broken),
            patch("app.summarize.logger.exception"),
        ):
            self.assertIsNone(summarize_email("Subject", "Body"))

    def test_reminder_engine_marks_only_after_successful_send(self):
        attention = [{"gmail_id": "m1", "category": "Needs Action", "subject": "Approve", "sender": "a@b.com"}]
        deadlines = [{"gmail_id": "m2", "due_date": "2026-08-10", "subject": "Submit"}]
        marked = []
        with (
            patch("app.reminders.collect_due_reminders", return_value=(attention, deadlines)),
            patch("app.reminders.send_reminder_email") as send,
            patch("app.reminders.mark_reminded", side_effect=lambda *args, **kwargs: marked.append(args)),
        ):
            result = send_user_reminders(object(), "user@example.com")
        self.assertTrue(result["sent"])
        send.assert_called_once()
        self.assertEqual(len(marked), 2)

        with (
            patch("app.reminders.collect_due_reminders", return_value=(attention, [])),
            patch("app.reminders.send_reminder_email", side_effect=RuntimeError("send failed")),
            patch("app.reminders.mark_reminded") as mark,
        ):
            with self.assertRaises(RuntimeError):
                send_user_reminders(object(), "user@example.com")
        mark.assert_not_called()

    def test_reminder_engine_marks_only_items_in_the_email(self):
        attention = [
            {"gmail_id": f"m{i}", "category": "Needs Action", "subject": f"Item {i}"}
            for i in range(25)
        ]
        marked = []
        with (
            patch("app.reminders.collect_due_reminders", return_value=(attention, [])),
            patch("app.reminders.send_reminder_email"),
            patch("app.reminders.mark_reminded", side_effect=lambda *args, **kwargs: marked.append(args)),
        ):
            result = send_user_reminders(object(), "user@example.com")
        self.assertEqual(result["attention"], 20)
        self.assertEqual(len(marked), 20)
        self.assertNotIn("m24", [args[1] for args in marked])

    def test_triage_query_excludes_its_own_reminder_email(self):
        query = _unread_query("1d", "2026-08-08")
        self.assertIn('-subject:"Email Triage reminder: items need your attention"', query)


class AddonContractTests(unittest.TestCase):
    def test_manifest_has_message_read_scope_and_contextual_trigger(self):
        manifest = json.loads((ROOT / "gmail-addon" / "appsscript.json").read_text(encoding="utf-8"))
        scopes = manifest["oauthScopes"]
        triggers = manifest["addOns"]["gmail"]["contextualTriggers"]

        self.assertIn("https://www.googleapis.com/auth/gmail.addons.current.message.readonly", scopes)
        self.assertIn("openid", scopes)
        self.assertEqual(triggers[0]["onTriggerFunction"], "onGmailMessageOpen")

    def test_addon_code_contains_all_manifest_and_button_handlers(self):
        code = (ROOT / "gmail-addon" / "Code.gs").read_text(encoding="utf-8")
        for function_name in (
            "onHomepage",
            "onGmailMessageOpen",
            "summarizeCurrentMessage",
            "saveCategoryCorrection",
            "openAttentionMails",
            "openUpcomingDeadlines",
            "runTriage",
            "setAuto",
            "undoRun",
        ):
            self.assertIn(f"function {function_name}(", code)
        self.assertNotIn("Priority inbox", code)
        self.assertNotIn("digest.top", code)
        self.assertIn("Needs your attention (' + alerts.length + ')'", code)
        self.assertIn("Upcoming deadlines (' + deadlines.length + ')'", code)
        self.assertIn("escapeHtml_(item.subject", code)
        self.assertIn("Connect Gmail", code)
        self.assertIn("/auth/login?return_to=addon", code)
        self.assertIn("/api/addon/message-context", code)
        self.assertIn("/api/addon/feedback", code)
        self.assertIn("Save & Teach Assistant", code)


class DeploymentSafetyTests(unittest.TestCase):
    class Request:
        def __init__(self, query=None, session=None, body=None, headers=None):
            self.query_params = query or {}
            self.session = session or {}
            self._body = body or {}
            self.headers = headers or {}

        async def json(self):
            return self._body

    def test_oauth_callback_rejects_missing_or_mismatched_state(self):
        cases = [
            ({"code": "code"}, {"oauth_state": "expected", "oauth_code_verifier": "verifier"}),
            ({"code": "code", "state": "wrong"}, {"oauth_state": "expected", "oauth_code_verifier": "verifier"}),
        ]
        for query, session in cases:
            with self.subTest(query=query), patch("app.server.exchange_code") as exchange:
                response = server.auth_callback(self.Request(query=query, session=session))
                self.assertEqual(response.status_code, 307)
                self.assertEqual(response.headers["location"], "/?auth_error=1")
                exchange.assert_not_called()

    def test_addon_oauth_callback_returns_to_connected_page(self):
        request = self.Request(
            query={"code": "code", "state": "expected"},
            session={
                "oauth_state": "expected",
                "oauth_code_verifier": "verifier",
                "oauth_return_to": "addon",
            },
        )
        with (
            patch("app.server.exchange_code", return_value="user@example.com"),
            patch("app.server._register_watch"),
        ):
            response = server.auth_callback(request)

        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/?addon_connected=1")

    def test_addon_identity_token_binds_claimed_email(self):
        request = self.Request(
            headers={
                "x-addon-secret": "secret",
                "x-addon-identity": "signed-token",
            }
        )
        with (
            patch("app.server.ADDON_SHARED_SECRET", "secret"),
            patch("app.server.ADDON_GOOGLE_AUDIENCE", "client-id"),
            patch(
                "app.server.google_id_token.verify_oauth2_token",
                return_value={
                    "email": "user@example.com",
                    "email_verified": True,
                },
            ) as verify,
        ):
            email = server._addon_verified_email(request, "user@example.com")

        self.assertEqual(email, "user@example.com")
        self.assertEqual(verify.call_args.args[2], "client-id")

    def test_addon_identity_rejects_different_claimed_email(self):
        request = self.Request(
            headers={
                "x-addon-secret": "secret",
                "x-addon-identity": "signed-token",
            }
        )
        with (
            patch("app.server.ADDON_SHARED_SECRET", "secret"),
            patch("app.server.ADDON_GOOGLE_AUDIENCE", "client-id"),
            patch(
                "app.server.google_id_token.verify_oauth2_token",
                return_value={
                    "email": "real@example.com",
                    "email_verified": True,
                },
            ),
        ):
            email = server._addon_verified_email(request, "other@example.com")

        self.assertIsNone(email)

    def test_unsafe_production_configuration_fails_fast(self):
        with (
            patch.object(config, "IS_PRODUCTION", True),
            patch.object(config, "SESSION_SECRET", "short"),
            patch.object(config, "SESSION_HTTPS_ONLY", False),
            patch.object(config, "OAUTH_REDIRECT_URI", "http://localhost/callback"),
            patch.object(config, "ADDON_SHARED_SECRET", ""),
        ):
            with self.assertRaisesRegex(RuntimeError, "Unsafe production configuration"):
                config.validate_production_config()

    def test_push_is_disabled_without_topic_or_token(self):
        request = self.Request(query={"token": "anything"})
        with patch("app.server.GMAIL_PUBSUB_TOPIC", ""), patch("app.server.PUSH_AUTH_TOKEN", ""):
            response = asyncio.run(server.api_gmail_push(request, BackgroundTasks()))
        self.assertEqual(response.status_code, 404)

    def test_push_rejects_wrong_token(self):
        request = self.Request(query={"token": "wrong"})
        with patch("app.server.GMAIL_PUBSUB_TOPIC", "projects/p/topics/t"), patch("app.server.PUSH_AUTH_TOKEN", "right"):
            response = asyncio.run(server.api_gmail_push(request, BackgroundTasks()))
        self.assertEqual(response.status_code, 401)

    def test_triage_rejects_malformed_date_before_starting_task(self):
        request = self.Request(
            session={"user_email": "user@example.com"},
            body={"range": "1d", "date": "not-a-date"},
        )
        tasks = BackgroundTasks()
        with patch("app.server._service_for_user", return_value=object()):
            response = asyncio.run(server.api_triage(request, tasks))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(tasks.tasks), 0)

    def test_category_settings_reject_case_insensitive_duplicates(self):
        request = self.Request(
            session={"user_email": "user@example.com"},
            body={
                "categories": ["Needs Action", "needs action", "Others"],
                "faq_category": None,
            },
        )
        with patch("app.server.get_user_token", return_value="token"):
            response = asyncio.run(server.api_save_categories(request))
        self.assertEqual(response.status_code, 400)
        self.assertIn("unique", json.loads(response.body)["error"])

    def test_busy_scheduled_run_is_retried_instead_of_lost(self):
        email = "scheduled@example.com"
        server._progress_by_user[email] = server._default_progress()
        server._progress_by_user[email]["status"] = "running"
        server._scheduled_runs_by_user[email] = {
            "job_id": f"triage-scheduled-{email}",
            "run_at": "old",
            "range": "1w",
        }
        try:
            with (
                patch.object(server.scheduler, "add_job") as add_job,
                patch("app.server.save_scheduled_triage") as persist,
            ):
                server._run_scheduled_triage(email, "1w")

            add_job.assert_called_once()
            persist.assert_called_once()
            self.assertEqual(server._scheduled_runs_by_user[email]["range"], "1w")
            self.assertNotEqual(server._scheduled_runs_by_user[email]["run_at"], "old")
        finally:
            server._progress_by_user.pop(email, None)
            server._scheduled_runs_by_user.pop(email, None)

    def test_persisted_schedule_is_restored_after_restart(self):
        entry = {
            "email": "restore@example.com",
            "run_at": "2099-08-09T12:00:00+05:30",
            "range": "1w",
        }
        try:
            with (
                patch("app.server.list_scheduled_triage", return_value=[entry]),
                patch("app.server.save_scheduled_triage") as persist,
                patch.object(server.scheduler, "add_job") as add_job,
            ):
                server._restore_scheduled_runs()

            add_job.assert_called_once()
            persist.assert_called_once()
            self.assertEqual(
                server._scheduled_runs_by_user[entry["email"]]["range"],
                "1w",
            )
        finally:
            server._scheduled_runs_by_user.pop(entry["email"], None)

    def test_refreshed_web_token_is_persisted(self):
        credentials = SimpleNamespace(
            valid=False,
            expired=True,
            refresh_token="refresh",
            refresh=lambda request: None,
            to_json=lambda: '{"token":"new"}',
        )
        persisted = []
        with (
            patch("app.gmail_client.Credentials.from_authorized_user_info", return_value=credentials),
            patch("app.gmail_client.build", return_value="service"),
        ):
            service = authenticate_from_token_json(
                '{"token":"old"}',
                on_refresh=persisted.append,
            )

        self.assertEqual(service, "service")
        self.assertEqual(persisted, ['{"token":"new"}'])

    def test_command_execute_uses_exact_preview_plan(self):
        plan = {
            "parsed": {"action": "archive", "query": "from:a", "label": None, "summary": "Archive A"},
            "ids": ["m1", "m2"],
            "created_at": datetime.now().timestamp(),
        }
        request = self.Request(session={"user_email": "user@example.com", "command_preview": plan})
        with (
            patch("app.server._service_for_user", return_value=object()),
            patch("app.server.parse_command") as parse,
            patch("app.server.preview_command") as preview,
            patch("app.server.execute_command", return_value=2) as execute,
        ):
            response = asyncio.run(server.api_command_execute(request))

        self.assertEqual(response.status_code, 200)
        parse.assert_not_called()
        preview.assert_not_called()
        self.assertEqual(execute.call_args.args[1], plan["parsed"])
        self.assertEqual(execute.call_args.args[2], ["m1", "m2"])
        self.assertNotIn("command_preview", request.session)

    def test_addon_summarize_can_fetch_message_by_gmail_id(self):
        request = self.Request(
            body={"email": "user@example.com", "gmail_id": "gmail-1"},
        )
        with (
            patch("app.server._addon_authorized", return_value=True),
            patch("app.server.get_user_token", return_value="token"),
            patch("app.server._service_for_user", return_value=object()),
            patch(
                "app.server.fetch_email_by_id",
                return_value={"subject": "Subject", "body": "Body", "sender": "Alice"},
            ) as fetch,
            patch("app.server.summarize_email", return_value="- Summary") as summarize,
        ):
            response = asyncio.run(server.api_addon_summarize(request))

        self.assertEqual(response.status_code, 200)
        fetch.assert_called_once()
        summarize.assert_called_once_with("Subject", "Body", "Alice")
        self.assertEqual(json.loads(response.body)["summary"], "- Summary")

    def test_dedicated_addon_alerts_endpoint(self):
        request = self.Request(query={"email": "user@example.com"})
        expected = [{"gmail_id": "m1", "category": "Needs Action"}]
        with (
            patch("app.server._addon_authorized", return_value=True),
            patch("app.server.get_user_token", return_value="token"),
            patch("app.server._addon_alert_items", return_value=expected),
        ):
            response = server.api_addon_alerts(request, limit=3)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["alerts"], expected)

    def test_addon_digest_loads_all_attention_and_deadline_items(self):
        request = self.Request(query={"email": "user@example.com"})
        with (
            patch("app.server._addon_authorized", return_value=True),
            patch("app.server._addon_verified_email", return_value="user@example.com"),
            patch("app.server.get_user_token", return_value="token"),
            patch("app.server.list_learned_rules", return_value=[]),
            patch("app.server._addon_alert_items", return_value=[]) as alerts,
            patch("app.server.upcoming_deadlines", return_value=[]) as deadlines,
            patch("app.server.last_undoable_run", return_value=None),
        ):
            response = server.api_addon_digest(request)

        self.assertEqual(response.status_code, 200)
        alerts.assert_called_once_with("user@example.com", limit=200)
        deadlines.assert_called_once_with("user@example.com", limit=1000)

    def test_addon_message_context_returns_current_category_controls(self):
        request = self.Request(
            query={"email": "user@example.com", "gmail_id": "m1"}
        )
        message = {"subject": "Invoice", "sender": "Alice <alice@example.com>"}
        settings = {"categories": ["Needs Action", "Others"]}
        with (
            patch("app.server._addon_authorized", return_value=True),
            patch("app.server.get_user_token", return_value="token"),
            patch(
                "app.server._addon_message_context",
                return_value=(object(), message, settings, {}, "Others", None),
            ),
        ):
            response = server.api_addon_message_context(request)

        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["current_category"], "Others")
        self.assertEqual(payload["categories"], settings["categories"])
        self.assertEqual(payload["subject"], "Invoice")

    def test_addon_feedback_uses_authoritative_correction_helper(self):
        request = self.Request(
            body={
                "email": "user@example.com",
                "gmail_id": "m1",
                "category": "Needs Action",
            }
        )
        result = {
            "status": "updated",
            "old_category": "Others",
            "new_category": "Needs Action",
            "categories": ["Needs Action", "Others"],
        }
        with (
            patch("app.server._addon_authorized", return_value=True),
            patch("app.server.get_user_token", return_value="token"),
            patch("app.server._apply_addon_correction", return_value=result) as apply,
        ):
            response = asyncio.run(server.api_addon_feedback(request))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body), result)
        apply.assert_called_once_with(
            "user@example.com",
            "m1",
            "Needs Action",
        )

    def test_feedback_relabels_current_email_and_updates_priority(self):
        request = self.Request(
            session={"user_email": "user@example.com"},
            body={
                "gmail_id": "m1",
                "sender": "Alice <alice@example.com>",
                "subject": "Vendor compliance packet update",
                "old_category": "Others",
                "category": "Needs Action",
            },
        )
        item = {
            "gmail_id": "m1",
            "thread_id": "t1",
            "sender": "Alice <alice@example.com>",
            "subject": "Vendor compliance packet update",
            "category": "Others",
            "date": "2026-08-08T08:00:00+00:00",
        }
        with (
            patch("app.server.get_user_token", return_value="token"),
            patch("app.server.get_user_settings", return_value={"categories": ["Others", "Needs Action"]}),
            patch("app.server.get_priority_item", return_value=item),
            patch("app.server._service_for_user", return_value=object()),
            patch("app.server.get_or_create_label", side_effect=["new-label", "old-label"]),
            patch("app.server.apply_label") as apply,
            patch("app.server.remove_label") as remove,
            patch("app.server.compute_priority", return_value=(78, "corrected")),
            patch("app.server.save_priority") as save,
            patch("app.server.record_user_correction") as record,
            patch("app.server.remember_contact"),
            patch("app.server.list_learned_rules", return_value=[]),
        ):
            response = asyncio.run(server.api_feedback(request))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(apply.call_args.args[1:], ("m1", "new-label"))
        self.assertEqual(remove.call_args.args[1:], ("m1", "old-label"))
        self.assertEqual(save.call_args.args[5], "Needs Action")
        record.assert_called_once()

    def test_feedback_learns_authoritative_stored_sender_and_subject(self):
        request = self.Request(
            session={"user_email": "user@example.com"},
            body={
                "gmail_id": "m1",
                "sender": "forged@attacker.example",
                "subject": "Forged subject",
                "category": "Needs Action",
            },
        )
        item = {
            "gmail_id": "m1",
            "thread_id": "t1",
            "sender": "Real Sender <real@example.com>",
            "subject": "Real invoice approval",
            "category": "Others",
            "date": "2026-08-08T08:00:00+00:00",
        }
        with (
            patch("app.server.get_user_token", return_value="token"),
            patch("app.server.get_user_settings", return_value={"categories": ["Others", "Needs Action"]}),
            patch("app.server.get_priority_item", return_value=item),
            patch("app.server._service_for_user", return_value=object()),
            patch("app.server.get_or_create_label", side_effect=["new-label", "old-label"]),
            patch("app.server.apply_label"),
            patch("app.server.remove_label"),
            patch("app.server.compute_priority", return_value=(78, "corrected")),
            patch("app.server.save_priority"),
            patch("app.server.record_user_correction") as record,
            patch("app.server.remember_contact"),
            patch("app.server.list_learned_rules", return_value=[]),
        ):
            response = asyncio.run(server.api_feedback(request))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(record.call_args.args[1], item["sender"])
        self.assertEqual(record.call_args.kwargs["subject"], item["subject"])

    def test_feedback_rolls_back_new_label_when_old_label_removal_fails(self):
        request = self.Request(
            session={"user_email": "user@example.com"},
            body={
                "gmail_id": "m1",
                "sender": "real@example.com",
                "category": "Needs Action",
            },
        )
        item = {
            "gmail_id": "m1",
            "thread_id": "t1",
            "sender": "real@example.com",
            "subject": "Real subject",
            "category": "Others",
            "date": "2026-08-08T08:00:00+00:00",
        }
        with (
            patch("app.server.get_user_token", return_value="token"),
            patch("app.server.get_user_settings", return_value={"categories": ["Others", "Needs Action"]}),
            patch("app.server.get_priority_item", return_value=item),
            patch("app.server._service_for_user", return_value=object()),
            patch("app.server.get_or_create_label", side_effect=["new-label", "old-label"]),
            patch("app.server.apply_label") as apply,
            patch("app.server.remove_label", side_effect=[RuntimeError("remove failed"), None]) as remove,
            patch("app.server.record_user_correction") as record,
            patch("app.server.logger.exception"),
        ):
            response = asyncio.run(server.api_feedback(request))

        self.assertEqual(response.status_code, 500)
        self.assertEqual(apply.call_args_list[0].args[1:], ("m1", "new-label"))
        self.assertEqual(remove.call_args_list[-1].args[1:], ("m1", "new-label"))
        record.assert_not_called()

    def test_partial_undo_remains_retryable(self):
        actions = [
            {"gmail_id": "ok", "label_id": "label-1"},
            {"gmail_id": "fail", "label_id": "label-2"},
        ]

        def remove(service, gmail_id, label_id):
            if gmail_id == "fail":
                raise RuntimeError("Gmail failure")

        with (
            patch("app.server.last_undoable_run", return_value={"run_id": "run-1"}),
            patch("app.server.get_run_actions", return_value=actions),
            patch("app.server.get_or_create_label", return_value="processed"),
            patch("app.server.remove_label", side_effect=remove),
            patch("app.server.delete_priority"),
            patch("app.server.mark_run_undone") as mark,
        ):
            result = server._perform_undo("user@example.com", object())

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["failed"], 1)
        mark.assert_not_called()

    def test_failed_triage_restores_previous_priority_snapshot(self):
        old = {
            "gmail_id": "old",
            "thread_id": "thread",
            "sender": "alice@example.com",
            "subject": "Important",
            "category": "Needs Action",
            "score": 90,
            "reason": "old snapshot",
            "date": "2026-08-08T00:00:00+00:00",
        }
        restored = []
        with (
            patch("app.server.count_unread_unprocessed", return_value=1),
            patch("app.server.top_priority", return_value=[old]),
            patch("app.server.get_user_settings", return_value={"categories": ["Others"], "faq_category": None}),
            patch("app.server.start_triage_run"),
            patch("app.server.clear_priority"),
            patch("app.server.triage_until_empty", side_effect=RuntimeError("failed")),
            patch("app.server.save_priority", side_effect=lambda *args: restored.append(args)),
            patch("app.server.logger.exception"),
        ):
            server._run_triage("user@example.com", object())

        self.assertEqual(restored[0][1], "old")
        self.assertEqual(server._progress_by_user["user@example.com"]["status"], "error")

    def test_successful_triage_preserves_existing_priority_candidates(self):
        with (
            patch("app.server.count_unread_unprocessed", return_value=0),
            patch("app.server.top_priority", return_value=[]),
            patch("app.server.get_user_settings", return_value={"categories": ["Others"], "faq_category": None}),
            patch("app.server.start_triage_run"),
            patch("app.server.clear_priority") as clear,
            patch("app.server.triage_until_empty", return_value={}),
        ):
            server._run_triage("priority-preserve@example.com", object())

        clear.assert_not_called()
        self.assertEqual(
            server._progress_by_user["priority-preserve@example.com"]["status"],
            "done",
        )


if __name__ == "__main__":
    unittest.main()