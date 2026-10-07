"""Small offline check for Slack intake's approval boundary."""

from contextlib import closing
import importlib.util
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


path = Path(__file__).parents[1] / "scripts" / "slack_intake.py"
spec = importlib.util.spec_from_file_location("slack_intake", path)
intake = importlib.util.module_from_spec(spec)
spec.loader.exec_module(intake)


class IntakeTest(unittest.TestCase):
    def test_message_and_pdf(self):
        self.assertEqual(intake.request_text("create issue: Fix login\nIt fails"), ("Fix login", "It fails"))
        self.assertEqual(intake.request_text("can you create an issue for fixing login"),
                         ("fixing login", "fixing login"))
        self.assertEqual(intake.request_text("Implement this", "plan.pdf", "Step one"),
                         ("Implement this", "Plan from plan.pdf:\n\nStep one"))
        self.assertIsNone(intake.request_text("hello"))

    def test_issue_list_stays_out_of_model(self):
        issues = io.BytesIO(json.dumps([
            {"number": 3, "title": "Fix login"},
            {"number": 4, "title": "PR", "pull_request": {}},
        ]).encode())
        with patch.object(intake.urllib.request, "urlopen", return_value=issues):
            self.assertEqual(intake.recent_issues("acme/app", "secret"), ["#3: Fix login"])
        self.assertIsNotNone(intake.LIST_ISSUES.search("What issues we have now?"))
        with patch.object(intake, "recent_issues", return_value=["#3: Fix login"]):
            self.assertEqual(intake.issue_reply("acme/app", "secret"),
                             "Recent open issues (up to 10):\n#3: Fix login")
        with patch.dict(intake.os.environ, {"OPENROUTER_API_KEY": "test-key", "GH_TOKEN": "private"}), \
             patch.object(intake.urllib.request, "urlopen") as send, \
             patch.object(intake.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "Hello!\n", "")) as run:
            self.assertEqual(intake.assistant_reply("Hi", "model"),
                             "Hello!")
        send.assert_not_called()
        self.assertNotIn("Fix login", run.call_args.kwargs["input"])
        self.assertNotIn("GH_TOKEN", run.call_args.kwargs["env"])
        self.assertIn("/opt/hermes/.venv/bin/hermes", run.call_args.args[0])
        self.assertIn("--query-file", run.call_args.args[0])

    def test_issue_is_review_only(self):
        response = io.BytesIO(json.dumps({"html_url": "https://github.com/acme/app/issues/1",
                                          "labels": [{"name": "needs-human"}]}).encode())
        with patch.object(intake.urllib.request, "urlopen", return_value=response) as send:
            url, labelled = intake.create_issue("acme/app", "secret", "Fix", "Details", "C123", "123.456")
        body = json.loads(send.call_args.args[0].data)
        self.assertEqual(body["labels"], ["needs-human"])
        self.assertIn("Submitted from Slack:", body["body"])
        self.assertTrue(labelled)
        self.assertTrue(url.endswith("/1"))

    def test_duplicate_message_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory, closing(sqlite3.connect(Path(directory) / "state.db")) as con:
            self.assertTrue(intake.claim(con, "C123", "123.456"))
            self.assertFalse(intake.claim(con, "C123", "123.456"))

    def test_pdf_reads_only_slack_downloads(self):
        file = {"name": "plan.pdf", "url_private_download": "https://files.slack.com/plan.pdf"}
        with patch.object(intake.urllib.request, "urlopen", return_value=io.BytesIO(b"%PDF-test")), \
             patch.object(intake.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "Plan text", "")):
            self.assertEqual(intake.pdf_to_text(file, "secret"), "Plan text")
        file["url_private_download"] = "http://localhost/private"
        with self.assertRaises(ValueError):
            intake.pdf_to_text(file, "secret")

    def test_thread_routing_and_session_names(self):
        root = {"channel": "C123", "ts": "100.1", "text": "<@UBOT> hi"}
        follow_up = {"channel": "C123", "ts": "101.1", "thread_ts": "100.1", "text": "Thanks"}
        unrelated = {"channel": "C123", "ts": "102.1", "text": "Someone else's chat"}
        self.assertEqual(intake.thread_root_ts(follow_up), "100.1")
        self.assertTrue(intake.should_handle(root, "UBOT"))
        self.assertTrue(intake.should_handle(follow_up, "UBOT", thread_engaged=True))
        self.assertFalse(intake.should_handle(follow_up, "UBOT", thread_engaged=False))
        self.assertFalse(intake.should_handle(unrelated, "UBOT"))
        self.assertFalse(intake.should_handle({**root, "bot_id": "B1"}, "UBOT"))

        channel_session = intake.conversation_name("T1", "C123", "100.1", False)
        self.assertEqual(channel_session, intake.conversation_name("T1", "C123", "100.1", False))
        self.assertNotEqual(channel_session, intake.conversation_name("T1", "C123", "200.1", False))
        self.assertNotEqual(channel_session, intake.conversation_name("T1", "C456", "100.1", False))
        self.assertEqual(intake.conversation_name("T1", "D123", "100.1", True),
                         intake.conversation_name("T1", "D123", "200.1", True))

    def test_engaged_thread_replies_at_root(self):
        class FakeApp:
            instance = None

            def __init__(self, token):
                self.client = types.SimpleNamespace(auth_test=lambda: {"user_id": "UBOT"})
                self.handlers = {}
                FakeApp.instance = self

            def event(self, name):
                return lambda handler: self.handlers.setdefault(name, handler)

        bolt = types.ModuleType("slack_bolt")
        bolt.App = FakeApp
        adapter = types.ModuleType("slack_bolt.adapter")
        socket_mode = types.ModuleType("slack_bolt.adapter.socket_mode")
        socket_mode.SocketModeHandler = lambda app, token: types.SimpleNamespace(start=lambda: None)
        say = Mock()
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(sys.modules, {"slack_bolt": bolt, "slack_bolt.adapter": adapter,
                                      "slack_bolt.adapter.socket_mode": socket_mode}), \
             patch.dict(intake.os.environ, {"SLACK_BOT_TOKEN": "bot-secret", "SLACK_APP_TOKEN": "app-secret",
                                         "GH_TOKEN": "gh-secret", "REPO": "acme/app"}), \
             patch.object(intake, "STATE", Path(directory) / "state.db"), \
             patch.object(intake, "assistant_reply", return_value="Hi there") as chat:
            intake.main()
            handler = FakeApp.instance.handlers["message"]
            handler({"channel": "C123", "ts": "100.1", "text": "<@UBOT> hi", "team": "T1", "user": "U1"}, say)
            handler({"channel": "C123", "ts": "101.1", "thread_ts": "100.1",
                     "text": "Thanks", "team": "T1", "user": "U2"}, say)
            handler({"channel": "C123", "ts": "102.1", "text": "Unrelated", "team": "T1"}, say)
            handler({"channel": "C123", "ts": "103.1", "thread_ts": "200.1",
                     "text": "Another thread", "team": "T1"}, say)
            self.assertEqual(say.call_count, 2)
            self.assertEqual(chat.call_count, 2)
            self.assertEqual([call.kwargs["thread_ts"] for call in say.call_args_list], ["100.1", "100.1"])
            self.assertEqual(chat.call_args_list[0].kwargs["session"],
                             chat.call_args_list[1].kwargs["session"])
            self.assertEqual([call.kwargs["speaker"] for call in chat.call_args_list], ["U1", "U2"])

            # A lost GitHub POST response is uncertain: do not POST again on Slack retry.
            say.reset_mock(side_effect=True)
            uncertain = {"channel": "C123", "ts": "106.1", "thread_ts": "100.1",
                         "text": "create issue: Fix signup", "team": "T1"}
            post_attempts = []

            def lost_response(request, timeout):
                if request.get_method() == "POST":
                    post_attempts.append(request)
                    raise intake.urllib.error.URLError("response lost")
                return io.BytesIO(b"[]")

            with patch.object(intake.urllib.request, "urlopen", side_effect=lost_response), \
                 patch.object(intake, "create_issue", wraps=intake.create_issue) as create:
                handler(uncertain, say)
                handler(uncertain, say)
            create.assert_called_once()
            self.assertEqual(len(post_attempts), 1)

            # A failed acknowledgement cannot create the same GitHub issue twice.
            say.reset_mock(side_effect=True)
            say.side_effect = [RuntimeError("send failed"), None]
            issue = {"channel": "C123", "ts": "105.1", "thread_ts": "100.1",
                     "text": "create issue: Fix login", "team": "T1"}
            with patch.object(intake, "create_issue", return_value=("https://github.com/acme/app/issues/1", True)) as create:
                with self.assertRaises(RuntimeError):
                    handler(issue, say)
                handler(issue, say)
            create.assert_called_once()

            # A failed Slack send must not consume the event; the retry should respond.
            say.reset_mock(side_effect=True)
            chat.reset_mock()
            say.side_effect = [RuntimeError("send failed"), None]
            retry = {"channel": "C123", "ts": "104.1", "thread_ts": "100.1",
                     "text": "Can you repeat that?", "team": "T1"}
            with self.assertRaises(RuntimeError):
                handler(retry, say)
            handler(retry, say)
            self.assertEqual(say.call_count, 2)
            self.assertEqual(chat.call_count, 2)
            self.assertEqual([call.kwargs["thread_ts"] for call in say.call_args_list], ["100.1", "100.1"])

    def test_hermes_session_does_not_receive_platform_tokens(self):
        with patch.dict(intake.os.environ, {"OPENROUTER_API_KEY": "router-secret", "GH_TOKEN": "gh-secret",
                                         "SLACK_BOT_TOKEN": "bot-secret", "SLACK_APP_TOKEN": "app-secret"}), \
             patch.object(intake.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "Hello!\n", "")) as run:
            self.assertEqual(intake.assistant_reply("Hi", "openrouter/free", session="slack-t1-c123"), "Hello!")
        command = run.call_args.args[0]
        self.assertIn("slack-t1-c123", command)
        self.assertEqual(command[command.index("--toolsets") + 1], "bot_room")
        self.assertNotIn("gh-secret", repr(run.call_args))
        self.assertNotIn("bot-secret", repr(run.call_args))
        self.assertNotIn("app-secret", repr(run.call_args))
        self.assertEqual(run.call_args.kwargs["env"]["OPENROUTER_API_KEY"], "router-secret")


if __name__ == "__main__":
    unittest.main()
