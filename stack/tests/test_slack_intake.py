"""Small offline check for Slack intake's approval boundary."""

import importlib.util
import io
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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
        with tempfile.TemporaryDirectory() as directory, sqlite3.connect(Path(directory) / "state.db") as con:
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


if __name__ == "__main__":
    unittest.main()
