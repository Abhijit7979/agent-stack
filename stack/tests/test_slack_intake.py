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
        self.assertEqual(intake.request_text("Implement this with React", "plan.pdf", "Step one", develop=True),
                         ("Implement this with React",
                          "Slack request:\n\nImplement this with React\n\nPlan from plan.pdf:\n\nStep one"))
        self.assertIsNone(intake.request_text("hello"))
        message = ("Hi , https://github.com/Abhijit7979/theastraveda_website\n\n"
                   "Create a issue in this repo for improving readme file currently")
        self.assertEqual(intake.repo_from_text(message), "Abhijit7979/theastraveda_website")
        self.assertEqual(intake.request_text(message),
                         ("improving readme file currently", "improving readme file currently"))

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

    def test_create_issue_labels_and_slack_link(self):
        response = io.BytesIO(json.dumps({"html_url": "https://github.com/acme/app/issues/1",
                                          "labels": [{"name": "needs-human"}]}).encode())
        with patch.object(intake.urllib.request, "urlopen", return_value=response) as send:
            url, labelled = intake.create_issue("acme/app", "secret", "Fix", "Details", "C123", "123.456")
        body = json.loads(send.call_args.args[0].data)
        self.assertEqual(body["labels"], ["needs-human"])
        self.assertIn("Submitted from Slack:", body["body"])
        self.assertTrue(labelled)
        self.assertTrue(url.endswith("/1"))
        response = io.BytesIO(json.dumps({"html_url": "https://github.com/acme/other/issues/2"}).encode())
        with patch.object(intake.urllib.request, "urlopen", return_value=response) as send:
            _, labelled = intake.create_issue("acme/other", "secret", "Fix", "Details", "C123",
                                              "123.456", label=None)
        self.assertTrue(labelled)
        self.assertNotIn("labels", json.loads(send.call_args.args[0].data))

    def test_repo_preflight_uses_github_url_without_trailing_slash(self):
        with patch.object(intake.urllib.request, "urlopen",
                          return_value=io.BytesIO(b'{"has_issues":true}')) as send:
            self.assertTrue(intake.github_get("acme/other", "secret", "")["has_issues"])
        self.assertEqual(send.call_args.args[0].full_url,
                         "https://api.github.com/repos/acme/other")

    def test_spoken_repo_name_must_resolve_uniquely(self):
        self.assertEqual(intake.repo_hint("in theastraveda website repo, update README"),
                         "theastraveda website")
        self.assertEqual(intake.repo_hint("It's in Astra Veda website repo, not here"),
                         "Astra Veda website")
        self.assertIsNone(intake.repo_hint("in this repo, update README"))
        repos = io.BytesIO(json.dumps([
            {"full_name": "Abhijit7979/theastraveda_website"},
            {"full_name": "Abhijit7979/testing-my-agent-layer"},
        ]).encode())
        with patch.object(intake.urllib.request, "urlopen", return_value=repos) as send:
            self.assertEqual(intake.resolve_repo_hint("Astra Veda website", "secret"),
                             "Abhijit7979/theastraveda_website")
        self.assertIn("/user/repos?", send.call_args.args[0].full_url)
        with patch.object(intake.urllib.request, "urlopen", return_value=io.BytesIO(b"[]")):
            with self.assertRaisesRegex(ValueError, "GitHub URL"):
                intake.resolve_repo_hint("unknown repo", "secret")
        ambiguous = io.BytesIO(json.dumps([
            {"full_name": "Abhijit7979/astraveda_website"},
            {"full_name": "Abhijit7979/astra-veda-app"},
        ]).encode())
        with patch.object(intake.urllib.request, "urlopen", return_value=ambiguous):
            with self.assertRaisesRegex(ValueError, "GitHub URL"):
                intake.resolve_repo_hint("Astra Veda", "secret")

    def test_explicit_development_labels_issue_and_dispatches_without_slack_tokens(self):
        self.assertTrue(intake.DEVELOP.match("Can you build a home page?"))
        self.assertFalse(intake.DEVELOP.match("How do I build a home page?"))
        self.assertTrue(intake.COMMAND.match("create issue: Build a home page"))
        self.assertTrue(intake.DEVELOP.match("Create an HTML home page"))
        self.assertTrue(intake.GITHUB_ACTION.match("create a new repo"))
        self.assertTrue(intake.GITHUB_ACTION.match("merge PR #3"))
        self.assertTrue(intake.GITHUB_ACTION.match("fix issue #3"))
        self.assertTrue(intake.START_DEVELOPMENT.match("Start development"))
        self.assertFalse(intake.GITHUB_ACTION.match("Build a home page and open a PR"))
        self.assertEqual(intake.request_text("Fix login", develop=True), ("Fix login", "Fix login"))
        response = io.BytesIO(json.dumps({"html_url": "https://github.com/acme/app/issues/2",
                                          "labels": [{"name": "agent-ready"}]}).encode())
        with patch.object(intake.urllib.request, "urlopen", return_value=response) as send:
            self.assertEqual(intake.create_issue("acme/app", "secret", "Fix", "Details", "C123",
                                                 "123.456", label="agent-ready"),
                             ("https://github.com/acme/app/issues/2", True))
        self.assertEqual(json.loads(send.call_args.args[0].data)["labels"], ["agent-ready"])
        with patch.dict(intake.os.environ, {"HERMES_HOME": "/opt/data", "GH_TOKEN": "gh-secret",
                                         "SLACK_BOT_TOKEN": "bot-secret", "SLACK_APP_TOKEN": "app-secret"}), \
             patch.object(intake.subprocess, "run", return_value=subprocess.CompletedProcess([], 0,
                                                                                "Dispatched acme/app#2 to opencode\n", "")) as run:
            self.assertTrue(intake.dispatch_ready(2, "acme/app"))
            self.assertFalse(intake.dispatch_ready(3, "acme/app"))
        self.assertEqual(run.call_args.args[0], ["/opt/data/scripts/dispatch.sh", "acme/app", "3"])
        self.assertNotIn("SLACK_BOT_TOKEN", run.call_args.kwargs["env"])
        self.assertNotIn("SLACK_APP_TOKEN", run.call_args.kwargs["env"])

    def test_workspace_write_boundary(self):
        client = types.SimpleNamespace(users_info=lambda user: {"user": {"team_id": "T1"}})
        self.assertTrue(intake.workspace_member({"user": "U1", "team": "T1"}, client, "T1"))
        self.assertFalse(intake.workspace_member({"user": "U1", "user_team": "EXT"}, client, "T1"))
        self.assertFalse(intake.workspace_member({"user": "U1", "team": "T1"}, client, "T2"))
        external = types.SimpleNamespace(users_info=lambda user: {"user": {"team_id": "EXT"}})
        self.assertFalse(intake.workspace_member({"user": "U1"}, external, "T1"))
        with patch.object(intake, "github_get", return_value={"state": "closed", "labels": [{"name": "agent-pr"}]}):
            self.assertEqual(intake.queue_issue("acme/app", "secret", 2), "pr")

    def test_status_posts_once_to_original_thread(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(intake, "STATE", Path(directory) / "state.db"):
            with closing(sqlite3.connect(intake.STATE)) as con:
                intake.track_job(con, "C123", "100.1", "https://github.com/acme/app/issues/2", "acme/app")
            client = Mock()
            with patch.object(intake, "dispatch_ready", return_value=True), \
                 patch.object(intake, "issue_status", return_value=("working", "Started")):
                intake.poll_status(client, "acme/app", "secret")
                intake.poll_status(client, "acme/app", "secret")
            client.chat_postMessage.assert_called_once_with(channel="C123", thread_ts="100.1",
                                                            text="Started")
            with patch.object(intake, "issue_status", return_value=("pr", "PR ready")):
                intake.poll_status(client, "acme/app", "secret")
                intake.poll_status(client, "acme/app", "secret")
            self.assertEqual(client.chat_postMessage.call_count, 2)
            with closing(sqlite3.connect(intake.STATE)) as con:
                self.assertEqual(con.execute("SELECT count(*) FROM jobs_v2").fetchone()[0], 0)
        with patch.object(intake, "github_get", side_effect=[
                {"labels": [{"name": "agent-pr"}]},
                [{"body": "🤖 https://github.com/acme/app/pull/7 — ready"}]]):
            self.assertIn("https://github.com/acme/app/pull/7",
                          intake.issue_status("acme/app", "secret", 2)[1])

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
                self.client = types.SimpleNamespace(auth_test=lambda: {"user_id": "UBOT", "team_id": "T1"},
                                                    users_info=lambda user: {"user": {"team_id": "T1"}},
                                                    conversations_replies=lambda **kwargs: {"messages": [
                                                        {"user": "U1", "text": "See https://github.com/acme/other"}]
                                                    })
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

            screenshot_request = {"channel": "C456", "ts": "200.1", "team": "T1", "user": "U1",
                                  "text": ("Hi <@UBOT>, https://github.com/acme/other\n"
                                           "Create a issue in this repo for improving readme")}
            with patch.object(intake, "github_get", return_value={"permissions": {"push": True},
                                                                 "has_issues": True}), \
                 patch.object(intake, "create_issue", return_value=("https://github.com/acme/other/issues/3", True)) as create, \
                 patch.object(intake, "dispatch_ready", return_value=True) as dispatch:
                handler(screenshot_request, say)
            self.assertEqual(create.call_args.args[0], "acme/other")
            self.assertEqual(create.call_args.args[2], "improving readme")
            self.assertEqual(create.call_args.kwargs["label"], "agent-ready")
            dispatch.assert_called_once_with(3, "acme/other")
            with closing(sqlite3.connect(intake.STATE)) as con:
                self.assertEqual(intake.saved_repo(con, "C456", "200.1"), "acme/other")

            # Recover a repository from a thread started before repo context was persisted.
            with closing(sqlite3.connect(intake.STATE)) as con:
                con.execute("DELETE FROM thread_repos")
                con.commit()
            say.reset_mock()
            other_repo = {"channel": "C123", "ts": "108.1", "thread_ts": "100.1",
                          "text": "Create an issue to improve the README", "team": "T1", "user": "U2"}
            with patch.object(intake, "github_get", return_value={"permissions": {"push": True},
                                                                 "has_issues": True}) as get, \
                 patch.object(intake, "create_issue", return_value=("https://github.com/acme/other/issues/2", True)) as create, \
                 patch.object(intake, "dispatch_ready", return_value=True) as dispatch:
                handler(other_repo, say)
            self.assertEqual(get.call_args_list[0].args, ("acme/other", "gh-secret", ""))
            self.assertEqual(create.call_args.args[0], "acme/other")
            self.assertEqual(create.call_args.kwargs["label"], "agent-ready")
            dispatch.assert_called_once_with(2, "acme/other")
            self.assertIn("acme/other/issues/2", say.call_args.args[0])
            with closing(sqlite3.connect(intake.STATE)) as con:
                self.assertEqual(intake.saved_repo(con, "C123", "100.1"), "acme/other")

            say.reset_mock()
            external_development = {"channel": "C123", "ts": "109.1", "thread_ts": "100.1",
                                    "text": "Start development", "team": "T1", "user": "U2"}
            with patch.object(intake, "github_get", return_value={"state": "open", "author_association": "COLLABORATOR",
                                                                 "labels": [{"name": "agent-ready"}]}), \
                 patch.object(intake, "dispatch_ready", return_value=True) as dispatch:
                handler(external_development, say)
            dispatch.assert_called_once_with(2, "acme/other")
            self.assertIn("Queued development", say.call_args.args[0])

            # A thread without repo context must not silently use the default repository.
            with closing(sqlite3.connect(intake.STATE)) as con:
                con.execute("DELETE FROM thread_repos")
                con.commit()
            FakeApp.instance.client.conversations_replies = lambda **kwargs: {"messages": []}

            say.reset_mock()
            development = {"channel": "C123", "ts": "107.1", "thread_ts": "100.1",
                           "text": "Can you build a home page and open a PR?", "team": "T1", "user": "U2"}
            with patch.object(intake, "create_issue", return_value=("https://github.com/acme/app/issues/2", True)) as create, \
                 patch.object(intake, "ensure_agent_labels"), \
                 patch.object(intake, "dispatch_ready", return_value=True) as dispatch:
                handler(development, say)
                handler(development, say)
            create.assert_not_called()
            dispatch.assert_not_called()
            self.assertEqual(say.call_count, 2)
            self.assertIn("URL", say.call_args.args[0])
            self.assertEqual(say.call_args.kwargs["thread_ts"], "100.1")

            # A lost GitHub POST response is uncertain: do not POST again on Slack retry.
            say.reset_mock(side_effect=True)
            uncertain = {"channel": "C123", "ts": "106.1", "thread_ts": "100.1",
                         "text": "https://github.com/acme/app\ncreate issue: Fix signup",
                         "team": "T1", "user": "U1"}
            post_attempts = []

            def lost_response(request, timeout):
                if request.get_method() == "POST":
                    post_attempts.append(request)
                    raise intake.urllib.error.URLError("response lost")
                return io.BytesIO(b"[]")

            with patch.object(intake.urllib.request, "urlopen", side_effect=lost_response), \
                 patch.object(intake, "ensure_agent_labels"), \
                 patch.object(intake, "create_issue", wraps=intake.create_issue) as create:
                handler(uncertain, say)
                handler(uncertain, say)
            create.assert_called_once()
            self.assertEqual(len(post_attempts), 1)

            # A failed acknowledgement cannot create the same GitHub issue twice.
            say.reset_mock(side_effect=True)
            say.side_effect = [RuntimeError("send failed"), None]
            issue = {"channel": "C123", "ts": "105.1", "thread_ts": "100.1",
                     "text": "https://github.com/acme/app\ncreate issue: Fix login",
                     "team": "T1", "user": "U1"}
            with patch.object(intake, "ensure_agent_labels"), \
                 patch.object(intake, "dispatch_ready", return_value=True), \
                 patch.object(intake, "create_issue", return_value=("https://github.com/acme/app/issues/1", True)) as create:
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
        with patch.dict(intake.os.environ, {"OPENROUTER_API_KEY": "router-secret"}), \
             patch.object(intake.subprocess, "run", return_value=subprocess.CompletedProcess(
                 [], 0, "<|tool_call_start|>[read_file(path='/tmp/README.md')]<|tool_call_end|>", "")):
            self.assertNotIn("tool_call", intake.assistant_reply("show the README", "model"))

    def test_spoken_repo_task_and_generic_follow_up(self):
        root = {"channel": "C1", "ts": "100.1", "team": "T1", "user": "U1",
                "text": "<@UBOT> in theastraveda website repo, in readme file add which agent skills are used"}

        class FakeApp:
            instance = None

            def __init__(self, token):
                self.client = types.SimpleNamespace(
                    auth_test=lambda: {"user_id": "UBOT", "team_id": "T1"},
                    users_info=lambda user: {"user": {"team_id": "T1"}},
                    conversations_replies=lambda **kwargs: {"messages": [root]},
                )
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
             patch.dict(intake.os.environ, {"SLACK_BOT_TOKEN": "bot", "SLACK_APP_TOKEN": "app",
                                         "GH_TOKEN": "gh", "REPO": "Abhijit7979/testing-my-agent-layer"}), \
             patch.object(intake, "STATE", Path(directory) / "state.db"), \
             patch.object(intake, "resolve_repo_hint", return_value="Abhijit7979/theastraveda_website"), \
             patch.object(intake, "github_get", return_value={"permissions": {"push": True}, "has_issues": True}), \
             patch.object(intake, "create_issue", return_value=(
                 "https://github.com/Abhijit7979/theastraveda_website/issues/3", True)) as create, \
             patch.object(intake, "dispatch_ready", return_value=True) as dispatch:
            intake.main()
            handler = FakeApp.instance.handlers["message"]
            handler(root, say)
            self.assertEqual(create.call_args.args[0], "Abhijit7979/theastraveda_website")
            dispatch.assert_called_once_with(3, "Abhijit7979/theastraveda_website")
            with closing(sqlite3.connect(intake.STATE)) as con:
                self.assertEqual(intake.saved_repo(con, "C1", "100.1"),
                                 "Abhijit7979/theastraveda_website")
            follow_up = {"channel": "C1", "ts": "101.1", "thread_ts": "100.1", "team": "T1",
                         "user": "U1", "text": "Create a issue and solve it"}
            with patch.object(intake, "queue_issue", return_value="working"):
                handler(follow_up, say)
            create.assert_called_once()
            self.assertIn("already running", say.call_args.args[0])
            with closing(sqlite3.connect(intake.STATE)) as con:
                intake.remember_issue(con, "C1", "100.1",
                                      "https://github.com/Abhijit7979/testing-my-agent-layer/issues/8")
            handler({**follow_up, "ts": "102.1"}, say)
            self.assertEqual(create.call_count, 2)
            self.assertEqual(create.call_args.args[0], "Abhijit7979/theastraveda_website")
            self.assertIn("readme file add", create.call_args.args[2])

    def test_issue_write_requires_an_unambiguous_repo(self):
        class FakeApp:
            instance = None

            def __init__(self, token):
                self.client = types.SimpleNamespace(
                    auth_test=lambda: {"user_id": "UBOT", "team_id": "T1"},
                    users_info=lambda user: {"user": {"team_id": "T1"}},
                    conversations_replies=lambda **kwargs: {"messages": []},
                )
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
        default_repo = "Abhijit7979/testing-my-agent-layer"
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(sys.modules, {"slack_bolt": bolt, "slack_bolt.adapter": adapter,
                                      "slack_bolt.adapter.socket_mode": socket_mode}), \
             patch.dict(intake.os.environ, {"SLACK_BOT_TOKEN": "bot", "SLACK_APP_TOKEN": "app",
                                         "GH_TOKEN": "gh", "REPO": default_repo}), \
             patch.object(intake, "STATE", Path(directory) / "state.db"), \
             patch.object(intake, "create_issue", return_value=(
                 f"https://github.com/{default_repo}/issues/10", True)) as create, \
             patch.object(intake, "ensure_agent_labels"), \
             patch.object(intake, "dispatch_ready", return_value=True):
            intake.main()
            handler = FakeApp.instance.handlers["message"]
            for number, text in enumerate(("Create a issue and solve it", "Build a home page",
                                           "Create issue: Fix README"), start=1):
                handler({"channel": "C1", "ts": f"{number}.1", "team": "T1",
                         "user": "U1", "text": f"<@UBOT> {text}"}, say)
                self.assertIn("GitHub URL", say.call_args.args[0])
            create.assert_not_called()

            # The configured default is still valid when the user explicitly links it.
            handler({"channel": "C1", "ts": "4.1", "team": "T1", "user": "U1",
                     "text": (f"<@UBOT> https://github.com/{default_repo}\n"
                              "Create issue: Fix README")}, say)
            create.assert_called_once()
            self.assertEqual(create.call_args.args[0], default_repo)

            # A cached project is not sufficient when Slack cannot recover the thread.
            create.reset_mock()
            with closing(sqlite3.connect(intake.STATE)) as con:
                intake.saved_repo(con, "C2", "10.1")
                intake.remember_repo(con, "C2", "10.1", default_repo)
                intake.thread_engaged(con, "C2", "10.1")
                intake.mark_thread_engaged(con, "C2", "10.1")
            FakeApp.instance.client.conversations_replies = Mock(side_effect=RuntimeError("Slack unavailable"))
            handler({"channel": "C2", "ts": "11.1", "thread_ts": "10.1", "team": "T1",
                     "user": "U1", "text": "Create a issue and solve it"}, say)
            create.assert_not_called()
            self.assertIn("URL", say.call_args.args[0])

            # Conflicting project references are a clarification, never a write.
            FakeApp.instance.client.conversations_replies = lambda **kwargs: {"messages": []}
            with patch.object(intake, "resolve_repo_hint", return_value="Abhijit7979/theastraveda_website"), \
                 patch.object(intake, "github_get", return_value={"permissions": {"push": True},
                                                                "has_issues": True}):
                handler({"channel": "C3", "ts": "20.1", "team": "T1", "user": "U1",
                         "text": ("<@UBOT> in Astra Veda website repo, "
                                  "https://github.com/acme/other\ncreate issue: Fix README")}, say)
            create.assert_not_called()
            self.assertIn("URL", say.call_args.args[0])

    def test_correction_in_thread_overrides_stale_project_and_hides_tool_tags(self):
        target_repo = "Abhijit7979/theastraveda_website"
        default_repo = "Abhijit7979/testing-my-agent-layer"
        root = {"channel": "C1", "ts": "100.1", "team": "T1", "user": "U1",
                "text": "<@UBOT> in theastraveda website repo, add agent skills used to README"}
        correction = {"channel": "C1", "ts": "101.1", "thread_ts": "100.1", "team": "T1",
                      "user": "U1", "text": "It's in Astra Veda website repo, not here"}

        class FakeApp:
            instance = None

            def __init__(self, token):
                self.client = types.SimpleNamespace(
                    auth_test=lambda: {"user_id": "UBOT", "team_id": "T1"},
                    users_info=lambda user: {"user": {"team_id": "T1"}},
                    conversations_replies=lambda **kwargs: {"messages": [root, correction]},
                )
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
             patch.dict(intake.os.environ, {"SLACK_BOT_TOKEN": "bot", "SLACK_APP_TOKEN": "app",
                                         "GH_TOKEN": "gh", "REPO": default_repo,
                                         "OPENROUTER_API_KEY": "model-key"}), \
             patch.object(intake, "STATE", Path(directory) / "state.db"), \
             patch.object(intake, "resolve_repo_hint", return_value=target_repo), \
             patch.object(intake, "github_get", return_value={"permissions": {"push": True},
                                                            "has_issues": True}), \
             patch.object(intake, "create_issue", return_value=(
                 f"https://github.com/{target_repo}/issues/3", True)) as create, \
             patch.object(intake, "dispatch_ready", return_value=True) as dispatch:
            intake.main()
            with closing(sqlite3.connect(intake.STATE)) as con:
                intake.saved_repo(con, "C1", "100.1")
                intake.remember_repo(con, "C1", "100.1", default_repo)
                intake.thread_engaged(con, "C1", "100.1")
                intake.mark_thread_engaged(con, "C1", "100.1")
            handler = FakeApp.instance.handlers["message"]
            handler({"channel": "C1", "ts": "102.1", "thread_ts": "100.1", "team": "T1",
                     "user": "U1", "text": "Create a issue and solve it"}, say)
            create.assert_called_once()
            self.assertEqual(create.call_args.args[0], target_repo)
            self.assertIn("agent skills used", create.call_args.args[2])
            dispatch.assert_called_once_with(3, target_repo)
            with closing(sqlite3.connect(intake.STATE)) as con:
                self.assertEqual(intake.saved_repo(con, "C1", "100.1"), target_repo)

            with patch.object(intake.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 0, "<|tool_call_start|>[glob(path='/home/triager')]<|tool_call_end|>", "")):
                handler({"channel": "C1", "ts": "103.1", "thread_ts": "100.1", "team": "T1",
                         "user": "U1", "text": "What was that?"}, say)
            self.assertNotIn("tool_call", say.call_args.args[0])
            self.assertNotIn("/home/triager", say.call_args.args[0])

            # Switching projects invalidates the earlier project's task description.
            switch = {"channel": "C2", "ts": "201.1", "thread_ts": "200.1", "team": "T1",
                      "user": "U1", "text": f"Switch to https://github.com/{default_repo}"}
            FakeApp.instance.client.conversations_replies = lambda **kwargs: {"messages": [
                {**root, "channel": "C2", "ts": "200.1"}, switch]}
            with closing(sqlite3.connect(intake.STATE)) as con:
                intake.saved_repo(con, "C2", "200.1")
                intake.remember_repo(con, "C2", "200.1", target_repo)
                intake.thread_engaged(con, "C2", "200.1")
                intake.mark_thread_engaged(con, "C2", "200.1")
            handler({"channel": "C2", "ts": "202.1", "thread_ts": "200.1", "team": "T1",
                     "user": "U1", "text": "Create a issue and solve it"}, say)
            create.assert_called_once()
            self.assertIn("describe what to change", say.call_args.args[0].lower())


if __name__ == "__main__":
    unittest.main()
