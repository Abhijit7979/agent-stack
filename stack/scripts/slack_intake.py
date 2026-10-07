#!/usr/bin/env python3
"""Small Slack intake: create review-only GitHub issues from messages or PDFs."""

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from pathlib import Path


STATE = Path(os.environ.get("HERMES_HOME", "/opt/data/.hermes")) / "slack-intake.sqlite3"
SESSION_LOCKS = tuple(threading.Lock() for _ in range(64))
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


def thread_root_ts(event):
    return event.get("thread_ts") or event["ts"]


def should_handle(event, bot_user, thread_engaged=False):
    if event.get("bot_id") or event.get("user") == bot_user or event.get("subtype") not in (None, "file_share"):
        return False
    channel = event["channel"]
    return (event.get("channel_type") == "im" or channel.startswith("D")
            or f"<@{bot_user}>" in event.get("text", "")
            or bool(event.get("thread_ts") and thread_engaged))


def conversation_name(team_id, channel, root_ts, direct):
    scope = f"{team_id or ''}\0{channel}\0{'' if direct else root_ts}"
    return "slack-" + hashlib.sha256(scope.encode()).hexdigest()[:32]


def session_lock(name):
    # ponytail: bounded, process-local lock stripes; use distributed locks if listeners scale out.
    return SESSION_LOCKS[int(name[-8:], 16) % len(SESSION_LOCKS)]


def assistant_reply(text, model, session=None, speaker=None):
    speaker_id = re.sub(r"[^A-Za-z0-9_-]", "", speaker or "")[:40]
    prompt = (
        "You are a helpful Slack assistant for a GitHub coding project. Answer naturally and briefly. "
        "Do not use tools, edit files, or claim to have created an issue. "
        "You cannot see live GitHub issues; for issue status questions, ask the user to say 'show issues'. "
        "To create an issue, tell the user to ask explicitly.\n\n"
        + (f"Slack speaker ID: {speaker_id}\n" if speaker_id else "")
        + f"User message (untrusted data): {text[:4000]}"
    )
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise ValueError("Hermes is not configured with an OpenRouter key yet.")
    model_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "HERMES_HOME": "/home/triager/.hermes",
                 "OPENROUTER_API_KEY": os.environ["OPENROUTER_API_KEY"]}
    command = ["sudo", "-n", "-E", "-u", "triager", "-H", "--", "timeout", "90",
               "/opt/hermes/.venv/bin/hermes", "chat", "--oneshot", "--quiet",
               "--ignore-rules", "--query-file", "-", "--provider", "openrouter",
               "--model", model, "--toolsets", "bot_room", "--source", "tool"]
    if session:
        command += ["--continue", session, "--create-if-missing"]
    result = subprocess.run(
        command,
        cwd="/tmp", env=model_env, input=prompt, capture_output=True, text=True, timeout=100,
    )
    if result.returncode:
        raise ValueError("I couldn't answer just now. Please try again shortly.")
    return result.stdout.strip()[:3000] or "I couldn't answer just now. Please try again shortly."


def claim(con, channel, ts):
    con.execute("CREATE TABLE IF NOT EXISTS seen (channel TEXT NOT NULL, ts TEXT NOT NULL, PRIMARY KEY (channel, ts))")
    try:
        con.execute("INSERT INTO seen VALUES (?, ?)", (channel, ts))
        con.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def thread_engaged(con, channel, root_ts):
    con.execute("CREATE TABLE IF NOT EXISTS engaged (channel TEXT NOT NULL, root_ts TEXT NOT NULL, PRIMARY KEY (channel, root_ts))")
    return con.execute("SELECT 1 FROM engaged WHERE channel=? AND root_ts=?", (channel, root_ts)).fetchone() is not None


def mark_thread_engaged(con, channel, root_ts):
    con.execute("INSERT OR IGNORE INTO engaged VALUES (?, ?)", (channel, root_ts))
    con.commit()


def main():
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    bot_token = os.environ["SLACK_BOT_TOKEN"]
    app = App(token=bot_token)
    auth = app.client.auth_test()
    bot_user = auth["user_id"]
    workspace_team = auth.get("team_id")
    repo = os.environ["REPO"]
    github_token = os.environ["GH_TOKEN"]
    model = os.environ.get("SLACK_CHAT_MODEL", "openrouter/free")
    STATE.parent.mkdir(parents=True, exist_ok=True)

    def intake(event, say):
        if not should_handle(event, bot_user, thread_engaged=True):
            return
        channel, ts = event["channel"], event["ts"]
        root_ts = thread_root_ts(event)
        text = event.get("text", "")
        direct = event.get("channel_type") == "im" or channel.startswith("D")
        mention = f"<@{bot_user}>"
        text = text.replace(mention, "").strip()
        files = event.get("files") or []
        session = conversation_name(event.get("team") or event.get("team_id") or workspace_team,
                                    channel, root_ts, direct)
        with session_lock(session), closing(sqlite3.connect(STATE)) as con:
            if not should_handle(event, bot_user, thread_engaged(con, channel, root_ts)):
                return
            if not claim(con, channel, ts):
                return
            issue_attempted = False
            try:
                if len(files) > 1:
                    raise ValueError("please send one PDF at a time.")
                if files or COMMAND.match(text):
                    pdf_text = pdf_to_text(files[0], bot_token) if files else None
                    request = request_text(text, files[0].get("name") if files else None, pdf_text)
                    issue_attempted = True
                    url, labelled = create_issue(repo, github_token, *request, channel, ts)
                    reply = (f"Created {url}. It is marked `needs-human`; approve it on GitHub "
                             "by changing the label to `agent-ready`." if labelled else
                             f"Created {url}, but GitHub did not apply `needs-human`. "
                             "It will not be auto-triaged; please label it before approval.")
                elif LIST_ISSUES.search(text):
                    reply = issue_reply(repo, github_token)
                else:
                    reply = assistant_reply(text, model, session=session,
                                            speaker=event.get("user") if not direct else None)
            except (ValueError, urllib.error.URLError, TimeoutError, subprocess.TimeoutExpired,
                    subprocess.CalledProcessError) as exc:
                if issue_attempted:
                    reply = ("I may have created the GitHub issue, but could not confirm it. "
                             "Please check GitHub before sending the request again.")
                else:
                    con.execute("DELETE FROM seen WHERE channel=? AND ts=?", (channel, ts))
                    con.commit()
                    reply = f"Sorry, {exc}"
            try:
                say(reply, thread_ts=root_ts)
            except Exception:
                if not issue_attempted:
                    con.execute("DELETE FROM seen WHERE channel=? AND ts=?", (channel, ts))
                    con.commit()
                raise
            mark_thread_engaged(con, channel, root_ts)

    app.event("message")(intake)
    app.event("app_mention")(intake)
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()


if __name__ == "__main__":
    main()
