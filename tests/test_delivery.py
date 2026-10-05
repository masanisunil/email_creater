import smtplib
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from email_sender.config import Settings
from email_sender.delivery import send_email_result
from email_sender.storage import Store


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = Settings(
            database=Path(self.directory.name) / "state.sqlite3", smtp_host="smtp.example.com",
            smtp_port=465, smtp_timeout=10, smtp_attempts=2, sender_email="sender@example.com",
            sender_name="Test Sender", smtp_username="sender@example.com", smtp_password="test-only",
            allowed_recipients=frozenset(), allowed_domains=frozenset(), hourly_limit=20,
            model="test", llm_timeout=10, llm_retries=0,
        )

    def send(self):
        return send_email_result("recipient@example.com", "Hello", "Test body",
                                 request_id="test-request", owner="test-user", settings=self.settings)

    @patch("email_sender.delivery.smtplib.SMTP_SSL")
    def test_success_and_duplicate(self, smtp):
        smtp.return_value.send_message.return_value = {}
        result = self.send()
        self.assertEqual(result.status, "accepted")
        self.assertIn("Email sent", result.message)
        self.assertEqual(self.send().status, "accepted")
        smtp.assert_called_once()
        self.assertEqual(smtp.call_args.kwargs["timeout"], 10)

    @patch("email_sender.delivery.smtplib.SMTP_SSL")
    def test_ambiguous_send_not_retried(self, smtp):
        smtp.return_value.send_message.side_effect = TimeoutError("private details")
        result = self.send()
        self.assertEqual(result.status, "unknown")
        self.assertNotIn("private details", result.message)
        self.assertEqual(self.send().status, "blocked")
        smtp.assert_called_once()

    @patch("email_sender.delivery.time.sleep")
    @patch("email_sender.delivery.smtplib.SMTP_SSL")
    def test_pre_send_transient_retry(self, smtp, sleep):
        server = MagicMock()
        server.send_message.return_value = {}
        smtp.side_effect = [TimeoutError(), server]
        self.assertEqual(self.send().status, "accepted")
        self.assertEqual(smtp.call_count, 2)
        sleep.assert_called_once()

    @patch("email_sender.delivery.smtplib.SMTP_SSL")
    def test_authentication_failure_not_retried(self, smtp):
        smtp.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b"private")
        self.assertEqual(self.send().status, "failed")
        smtp.assert_called_once()

    @patch("email_sender.delivery.smtplib.SMTP_SSL")
    def test_validation_and_allowlist(self, smtp):
        self.settings = replace(self.settings, allowed_domains=frozenset({"allowed.example"}))
        self.assertEqual(self.send().status, "blocked")
        self.settings = replace(self.settings, allowed_domains=frozenset(), smtp_password="")
        self.assertEqual(self.send().status, "blocked")
        smtp.assert_not_called()

    def test_owner_and_atomic_claim(self):
        store = Store(self.settings.database)
        store.register("request", "alice")
        with self.assertRaises(PermissionError):
            store.get("request", "bob")
        self.assertEqual(store.claim("request", "alice", "digest"), "claimed")
        self.assertEqual(Store(self.settings.database).claim("request", "alice", "digest"), "sending")
        self.assertEqual(store.claim("request", "alice", "different"), "blocked")

    def test_quota(self):
        store = Store(self.settings.database)
        store.register("one", "alice", limit=1)
        with self.assertRaises(ValueError):
            store.register("two", "alice", limit=1)

    @patch("email_sender.config.load_dotenv")
    def test_environment_aliases_and_secret_redaction(self, load):
        with patch.dict("os.environ", {"SENDER_EMAIL": "sender@example.com", "SMTP_USERNAME": "",
                                       "SMTP_PASSWORD": "", "SENDER_PASSWORD": "private-test-password"}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.smtp_username, "sender@example.com")
        self.assertEqual(settings.smtp_password, "private-test-password")
        self.assertNotIn("private-test-password", repr(settings))

    @patch("email_sender.delivery.smtplib.SMTP_SSL")
    def test_concurrent_attempts_only_submit_once(self, smtp):
        smtp.return_value.send_message.return_value = {}
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.send(), range(2)))
        self.assertIn("accepted", [result.status for result in results])
        smtp.assert_called_once()

    @patch("email_sender.delivery.smtplib.SMTP_SSL")
    def test_crash_claim_blocks_resubmission(self, smtp):
        store = Store(self.settings.database)
        store.register("test-request", "test-user")
        from email_sender.models import email_digest
        store.claim("test-request", "test-user", email_digest("recipient@example.com", "Hello", "Test body"))
        self.assertEqual(self.send().status, "blocked")
        smtp.assert_not_called()

    @patch("email_sender.delivery.smtplib.SMTP_SSL")
    def test_delivery_record_failure_is_unknown(self, smtp):
        import sqlite3
        smtp.return_value.send_message.return_value = {}
        with patch("email_sender.delivery.Store.finish", side_effect=sqlite3.OperationalError("private")):
            self.assertEqual(self.send().status, "unknown")
        self.assertEqual(self.send().status, "blocked")
        smtp.assert_called_once()

    def test_terminal_status_cannot_be_overwritten(self):
        store = Store(self.settings.database)
        store.register("one", "alice")
        store.claim("one", "alice", "digest")
        store.finish("one", "accepted")
        with self.assertRaises(ValueError):
            store.finish("one", "cancelled")

    @patch("email_sender.delivery.smtplib.SMTP_SSL")
    def test_invalid_content_does_not_connect(self, smtp):
        for recipient, subject in [("invalid", "Hello"), ("recipient@example.com", "Header\nInjection")]:
            result = send_email_result(recipient, subject, "Body", settings=self.settings)
            self.assertEqual(result.status, "blocked")
        smtp.assert_not_called()

    def test_legacy_transport_import(self):
        import email_send
        from email_sender import delivery
        self.assertIs(email_send.send_email, delivery.send_email)
        with patch("email_sender.delivery.send_email_result") as send:
            send.return_value.message = "Compatibility result"
            self.assertEqual(email_send.send_email("recipient@example.com", "Hello", "Body"), "Compatibility result")


if __name__ == "__main__":
    unittest.main()