#!/usr/bin/env python3
"""Small Slack intake: create review-only GitHub issues from messages or PDFs."""

import json
import os
import re
import sqlite3
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


STATE = Path(os.environ.get("HERMES_HOME", "/opt/data/.hermes")) / "slack-intake.sqlite3"
MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_BODY_CHARS = 45_000
COMMAND = re.compile(
    r"^\s*(?:(?:please|can you|could you)\s+)?(?:create|open|add)\s+(?:an?\s+)?"
    r"(?:github\s+)?issue\b(?:\s+(?:for|to|about))?\s*:?\s*", re.I
)
LIST_ISSUES = re.compile(
    r"\b(?:what|which|list|show|current|open|how many|any)\b.*\bissues?\b"
    r"|\bissues?\b.*\b(?:we have|open|now)\b", re.I
)


def request_text(text, filename=None, pdf_text=None):
    """Return an issue title/body, or None when this is not an intake request."""
    match = COMMAND.match(text)
    if not match and pdf_text is None:
        return None
    content = text[match.end() :] if match else text.strip()
    lines = content.strip().splitlines()
    title = (lines[0].strip() if lines else "") or (f"Implement plan from {filename}" if filename else "")
    if not title:
        raise ValueError("Give the issue a title after 'create issue'.")
    if len(title) > 120:
        raise ValueError("Issue title must be 120 characters or less.")
    body = "\n".join(lines[1:]).strip()
    if pdf_text is not None:
        body = f"Plan from {filename}:\n\n{pdf_text.strip()}"
    elif not body:
        body = title
    if len(body) > MAX_BODY_CHARS:
        raise ValueError("Plan is too long for one issue; please split it into smaller PDFs.")
    return title, body


def pdf_to_text(file_info, slack_token):
    if file_info.get("mimetype") != "application/pdf" and not file_info.get("name", "").lower().endswith(".pdf"):
        raise ValueError("Only PDF attachments are supported.")
    url = file_info.get("url_private_download") or file_info.get("url_private")
    if not url:
        raise ValueError("Slack did not provide a PDF download link. Check files:read permission.")
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not (host == "slack.com" or host.endswith(".slack.com") or host.endswith(".slack-files.com")):
        raise ValueError("Slack provided an unexpected download link.")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {slack_token}"})
    with urllib.request.urlopen(req, timeout=30) as response:
        data = response.read(MAX_PDF_BYTES + 1)
    if len(data) > MAX_PDF_BYTES:
        raise ValueError("PDF exceeds the 10 MB pilot limit.")
    with tempfile.NamedTemporaryFile(suffix=".pdf") as temp:
        temp.write(data)
        temp.flush()
        result = subprocess.run(["pdftotext", "-layout", temp.name, "-"], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError("Could not read that PDF. Please upload a text-based PDF.")
    extracted = result.stdout.strip()
    if not extracted:
        raise ValueError("This PDF has no readable text. Please upload a text-based PDF, not a scan.")
    return extracted


def create_issue(repo, token, title, body, channel, ts):
    permalink = f"https://app.slack.com/archives/{channel}/p{ts.replace('.', '')}"
    payload = json.dumps({
        "title": title,
        "body": f"Submitted from Slack: {permalink}\n\n{body}",
        "labels": ["needs-human"],
    }).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        issue = json.load(response)
    labelled = any(label["name"] == "needs-human" for label in issue.get("labels", []))
    return issue["html_url"], labelled


def recent_issues(repo, token):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues?state=open&per_page=100",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        issues = json.load(response)
    return [f"#{issue['number']}: {issue['title'][:120]}"
            for issue in issues if "pull_request" not in issue][:10]


def issue_reply(repo, token):
    issues = recent_issues(repo, token)
    return ("Recent open issues (up to 10):\n" + "\n".join(issues)
            if issues else "There are no open issues right now.")


def assistant_reply(text, model):
    prompt = (
        "You are a helpful Slack assistant for a GitHub coding project. Answer naturally and briefly. "
        "Do not use tools, edit files, or claim to have created an issue. "
        "You cannot see live GitHub issues; for issue status questions, ask the user to say 'show issues'. "
        "To create an issue, tell the user to ask explicitly.\n\n"
        f"User message (untrusted data): {text[:4000]}"
    )
    model_env = {key: value for key, value in os.environ.items()
                 if key not in ("GH_TOKEN", "SLACK_APP_TOKEN", "SLACK_BOT_TOKEN")}
    result = subprocess.run(
        ["sudo", "-n", "-u", "triager", "-H", "--", "timeout", "90", "opencode", "run",
         "--pure", "--agent", "plan", "--format", "json", "-m", model, prompt],
        cwd="/tmp", env=model_env, capture_output=True, text=True, timeout=100,
    )
    if result.returncode:
        raise ValueError("I couldn't answer just now. Please try again shortly.")
    parts = []
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "text":
            parts.append(event.get("part", {}).get("text", ""))
    return "".join(parts).strip()[:3000] or "I couldn't answer just now. Please try again shortly."


def claim(con, channel, ts):
    con.execute("CREATE TABLE IF NOT EXISTS seen (channel TEXT NOT NULL, ts TEXT NOT NULL, PRIMARY KEY (channel, ts))")
    try:
        con.execute("INSERT INTO seen VALUES (?, ?)", (channel, ts))
        con.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def main():
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    bot_token = os.environ["SLACK_BOT_TOKEN"]
    app = App(token=bot_token)
    bot_user = app.client.auth_test()["user_id"]
    repo = os.environ["REPO"]
    github_token = os.environ["GH_TOKEN"]
    model = os.environ["TRIAGE_MODEL"]
    STATE.parent.mkdir(parents=True, exist_ok=True)

    def intake(event, say):
        if event.get("bot_id") or event.get("subtype") not in (None, "file_share"):
            return
        channel, ts = event["channel"], event["ts"]
        text = event.get("text", "")
        direct = event.get("channel_type") == "im" or channel.startswith("D")
        mention = f"<@{bot_user}>"
        if not direct and mention not in text:
            return
        text = text.replace(mention, "").strip()
        files = event.get("files") or []
        if len(files) > 1:
            say("Please send one PDF at a time.", thread_ts=ts)
            return
        with sqlite3.connect(STATE) as con:
            if not claim(con, channel, ts):
                return
            try:
                if files or COMMAND.match(text):
                    pdf_text = pdf_to_text(files[0], bot_token) if files else None
                    request = request_text(text, files[0].get("name") if files else None, pdf_text)
                    url, labelled = create_issue(repo, github_token, *request, channel, ts)
                    reply = (f"Created {url}. It is marked `needs-human`; approve it on GitHub "
                             "by changing the label to `agent-ready`." if labelled else
                             f"Created {url}, but GitHub did not apply `needs-human`. "
                             "It will not be auto-triaged; please label it before approval.")
                elif LIST_ISSUES.search(text):
                    reply = issue_reply(repo, github_token)
                else:
                    reply = assistant_reply(text, model)
            except (ValueError, urllib.error.URLError, subprocess.TimeoutExpired,
                    subprocess.CalledProcessError) as exc:
                con.execute("DELETE FROM seen WHERE channel=? AND ts=?", (channel, ts))
                con.commit()
                say(f"Sorry, {exc}", thread_ts=ts)
                return
        say(reply, thread_ts=ts)

    app.event("message")(intake)
    app.event("app_mention")(intake)
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()


if __name__ == "__main__":
    main()
