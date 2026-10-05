import json
import sys
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from email_sender import agent
from email_sender.models import DraftEmail, EmailState, UserDetails
from email_sender.storage import Store


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        from test_delivery import DeliveryTests
        fixture = DeliveryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.settings = fixture.settings
        self.patch = patch("email_sender.agent.Settings.from_env", return_value=self.settings)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def state(self, **kwargs):
        state = EmailState(subject="Example", body="Example", receipt_email="recipient@example.com", **kwargs)
        Store(self.settings.database).register(state.request_id, state.owner)
        return state

    def test_explicit_approval_required(self):
        for answer, expected in [("yes", "send"), ("YES", "send"), ("", "review"),
                                 ("   ", "review"), ("no", "cancel"), ("cancel", "cancel"),
                                 ("Make it shorter", "draft")]:
            with self.subTest(answer=answer):
                state = self.state()
                with patch("email_sender.agent.interrupt", return_value=answer):
                    agent.review_node(state)
                self.assertEqual(agent.router(state), expected)

    @patch("email_sender.agent.send_email_result")
    def test_changed_draft_cannot_send(self, send):
        state = self.state()
        with patch("email_sender.agent.interrupt", return_value="yes"):
            agent.review_node(state)
        state.body = "Changed after approval"
        self.assertEqual(agent.send_node(state).status, "blocked")
        send.assert_not_called()

    def test_content_validation(self):
        for subject, body in [("", "Valid"), ("Subject\nInjected", "Valid"),
                              ("Valid", "word " * 151), ("word " * 31, "Valid")]:
            with self.assertRaises(ValueError):
                DraftEmail(subject=subject, body=body)

    def test_recipient_name_normalization(self):
        from email_sender.models import RecipientDetails
        valid = RecipientDetails(receipt_email="hr@example.com", mail_reason="followup", receipt_name="hr")
        self.assertEqual(valid.receipt_name, "Hr")
        junk = RecipientDetails(receipt_email="hr@example.com", mail_reason="followup", receipt_name="o")
        self.assertEqual(junk.receipt_name, "")

    @patch("email_sender.agent.get_llm")
    def test_draft_prompt_includes_sender_name(self, get_llm):
        llm = MagicMock(invoke=MagicMock(return_value=DraftEmail(subject="S", body="B")))
        get_llm.return_value.with_structured_output.return_value = llm
        state = self.state(receipt_name="Hr", mail_reason="followup")
        with patch("email_sender.agent.Settings.from_env", return_value=replace(self.settings, sender_name="Sunil Masani")):
            agent.draft_node(state)
        payload = json.loads(llm.invoke.call_args[0][0][1][1])
        self.assertEqual(payload["sender_name"], "Sunil Masani")
        self.assertEqual(payload["recipient_name"], "Hr")

    def test_missing_details_clarification(self):
        state = self.state(mail_reason="")
        state.receipt_email = "not an email"
        with patch("email_sender.agent.interrupt", return_value={"receipt_email": "valid@example.com", "mail_reason": "Arrange a meeting"}):
            agent.validate_node(state)
        self.assertEqual(state.receipt_email, "valid@example.com")

    @patch("email_sender.agent.get_llm")
    def test_off_topic_request_is_declined_without_asking_for_recipient(self, get_llm):
        get_llm.return_value.with_structured_output.return_value.invoke.return_value = UserDetails(is_email_request=False)
        state = self.state(question="explain python")
        with patch("email_sender.agent.interrupt", side_effect=AssertionError("Off-topic input must not reach clarification")):
            agent.retriver_node(state)
        self.assertEqual(state.status, "declined")
        self.assertIn("compose and send emails", state.response)
        self.assertEqual(agent.after_retrieval(state), "end")

    @patch("email_sender.agent.get_llm")
    def test_repeated_invalid_recipient_keeps_asking(self, get_llm):
        get_llm.return_value.with_structured_output.side_effect = lambda schema: MagicMock(invoke=MagicMock(
            return_value=UserDetails(receipt_email="", mail_reason="") if schema is UserDetails else DraftEmail(subject="S", body="B")))
        state = EmailState(question="hii")
        config = {"configurable": {"thread_id": state.request_id}}
        with agent.open_graph(self.settings) as app:
            app.invoke(state.model_dump(), config)
            app.invoke(agent.Command(resume={"receipt_email": "still invalid", "receipt_name": "", "mail_reason": "y"}), config)
            result = app.invoke(agent.Command(resume={"receipt_email": "valid@example.com", "receipt_name": "", "mail_reason": "y"}), config)
            self.assertIn("__interrupt__", result)
            self.assertEqual(result["__interrupt__"][0].value["kind"], "review")

    def test_run_pending_detects_interrupt_despite_empty_next(self):
        output = StringIO()
        with agent.open_graph(self.settings) as app:
            with patch("email_sender.agent.get_llm") as get_llm:
                get_llm.return_value.with_structured_output.return_value.invoke.return_value = UserDetails(receipt_email="", mail_reason="")
                state = EmailState(question="hii")
                config = {"configurable": {"thread_id": state.request_id}}
                app.invoke(state.model_dump(), config)
                app.invoke(agent.Command(resume={"receipt_email": "still invalid", "receipt_name": "", "mail_reason": "y"}), config)
            with redirect_stdout(output), patch("builtins.input", return_value="cancel"):
                agent.run_pending(app, config)
        self.assertIn("Provide a valid", output.getvalue())
        self.assertIn("AI: Email sending cancelled.", output.getvalue())

    def test_revision_limit(self):
        state = self.state(feedback_count=2)
        with patch("email_sender.agent.interrupt", return_value="Another revision"):
            agent.review_node(state)
        self.assertEqual(agent.router(state), "cancel")

    @patch("email_sender.agent.get_llm")
    def test_checkpoint_restart_and_fresh_state(self, get_llm):
        get_llm.return_value.with_structured_output.side_effect = lambda schema: MagicMock(invoke=MagicMock(return_value=
            UserDetails(receipt_email="recipient@example.com", mail_reason="Arrange a meeting")
            if schema is UserDetails else DraftEmail(subject="Meeting", body="Please attend.")))
        first = EmailState(question="Email recipient@example.com about a meeting")
        config = {"configurable": {"thread_id": first.request_id}}
        with agent.open_graph(self.settings) as app:
            result = app.invoke(first.model_dump(), config)
            self.assertTrue(result["__interrupt__"])
        with agent.open_graph(self.settings) as app:
            snapshot = app.get_state(config)
            self.assertEqual(snapshot.interrupts[0].value["to"], "recipient@example.com")
            result = app.invoke(agent.Command(resume="cancel"), config)
            self.assertEqual(result["status"], "cancelled")
            second = EmailState(question="Email recipient@example.com about a meeting")
            second_config = {"configurable": {"thread_id": second.request_id}}
            app.invoke(second.model_dump(), second_config)
            self.assertEqual(app.get_state(second_config).values["feedback_count"], 0)
            self.assertNotEqual(first.request_id, second.request_id)

    @patch("email_sender.agent.get_llm")
    @patch("email_sender.agent.send_email_result")
    def test_llm_failure_ends_without_sending(self, send, get_llm):
        get_llm.side_effect = RuntimeError("private key")
        state = self.state(question="Email recipient@example.com")
        with agent.open_graph(self.settings) as app:
            result = app.invoke(state.model_dump(), {"configurable": {"thread_id": state.request_id}})
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("private key", result["response"])
        send.assert_not_called()

    def test_legacy_agent_import_is_noninteractive(self):
        with patch("builtins.input", side_effect=AssertionError("Import must not prompt")):
            import agent as legacy
        self.assertIs(legacy.final_graph, agent.final_graph)
        self.assertIs(legacy.EmailState, EmailState)
        self.assertTrue(legacy.final_graph.get_graph().nodes)

    @patch("email_sender.agent.get_llm")
    @patch("email_sender.delivery.smtplib.SMTP_SSL")
    def test_full_approve_and_send(self, smtp, get_llm):
        smtp.return_value.send_message.return_value = {}
        get_llm.return_value.with_structured_output.side_effect = lambda schema: MagicMock(invoke=MagicMock(return_value=
            UserDetails(receipt_email="recipient@example.com", mail_reason="Arrange a meeting")
            if schema is UserDetails else DraftEmail(subject="Meeting", body="Please attend.")))
        state = EmailState(question="Email recipient@example.com about a meeting")
        config = {"configurable": {"thread_id": "legacy-notebook-thread"}}
        result = agent.final_graph.invoke(state, config)
        self.assertTrue(result["__interrupt__"])
        result = agent.final_graph.invoke(agent.Command(resume="yes"), config)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(agent.final_graph.get_state(config).values["status"], "accepted")
        with self.assertRaises(ValueError):
            agent.final_graph.invoke({"question": "A different email"}, config)
        smtp.assert_called_once()

    @patch("email_sender.agent.get_llm")
    def test_cli_eof_resume_and_purge(self, get_llm):
        get_llm.return_value.with_structured_output.side_effect = lambda schema: MagicMock(invoke=MagicMock(return_value=
            UserDetails(receipt_email="recipient@example.com", mail_reason="Arrange a meeting")
            if schema is UserDetails else DraftEmail(subject="Meeting", body="Please attend.")))
        output = StringIO()
        with redirect_stdout(output), patch("builtins.input", side_effect=["Email recipient@example.com about a meeting", EOFError]):
            agent.main([])
        store = Store(self.settings.database)
        with store.connection() as connection:
            request_id = connection.execute("SELECT request_id FROM email_requests").fetchone()[0]
        with redirect_stdout(output), patch("builtins.input", return_value="cancel"):
            agent.main(["--resume", request_id])
        self.assertEqual(store.get(request_id, agent.getpass.getuser())["status"], "cancelled")
        with redirect_stdout(output):
            agent.main(["--purge-request", request_id])
        with agent.open_graph(self.settings) as app:
            self.assertFalse(app.get_state({"configurable": {"thread_id": request_id}}).values)
        self.assertIn("send ledger retained", output.getvalue())
        self.assertEqual(store.get(request_id, agent.getpass.getuser())["status"], "cancelled")

    @patch("email_sender.agent.get_llm")
    def test_invented_recipient_requires_clarification(self, get_llm):
        get_llm.return_value.with_structured_output.return_value.invoke.return_value = UserDetails(
            receipt_email="invented@example.com", mail_reason="Arrange a meeting")
        state = self.state(question="Write a meeting invitation")
        agent.retriver_node(state)
        self.assertEqual(state.receipt_email, "")

    def test_stale_approval_is_rejected(self):
        state = self.state()
        with patch("email_sender.agent.interrupt", return_value={"action": "approve", "digest": "stale"}):
            agent.review_node(state)
        self.assertFalse(state.approved)
        self.assertEqual(agent.router(state), "review")


if __name__ == "__main__":
    unittest.main()