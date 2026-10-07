#!/usr/bin/env python3
"""Slack chat and explicit development requests for the configured GitHub repo."""

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
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
    r"^[ \t]*(?:(?:please|can you|could you)\s+)?(?:create|open|add)\s+(?:an?\s+)?"
    r"(?:github\s+)?issue\b(?:\s+in\s+(?:(?:this|the)\s+)?repo(?:sitory)?)?"
    r"(?:\s+(?:for|to|about))?[ \t]*:?[ \t]*", re.I | re.M
)
REPO_URL = re.compile(r"https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)", re.I)
ISSUE_URL = re.compile(r"https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)/issues/([0-9]+)", re.I)
REPO_HINT = re.compile(r"\bin\s+([A-Za-z0-9][A-Za-z0-9 ._-]{0,80}?)\s+(?:github\s+)?repo(?:sitory)?\b", re.I)
REPO_TASK = re.compile(r"\b(?:add|update|fix|change|implement|build|create|remove|document)\b", re.I)
GENERIC_ISSUE = re.compile(r"^\s*(?:and\s+)?(?:solve|fix|do|implement)\s+it[.!?\s]*$", re.I)
LIST_ISSUES = re.compile(
    r"\b(?:what|which|list|show|current|open|how many|any)\b.*\bissues?\b"
    r"|\bissues?\b.*\b(?:we have|open|now)\b", re.I
)
DEVELOP = re.compile(
    r"^\s*(?:(?:please|can you|could you|would you|i want you to|i need you to)\s+)?"
    r"(?:implement|build|fix|develop|code|make|create|refactor|update|change|"
    r"start (?:working|development) on)\b", re.I
)
START_DEVELOPMENT = re.compile(
    r"^\s*(?:(?:please|can you|could you)\s+)?(?:"
    r"(?:start|begin|resume)\s+(?:the\s+)?(?:development|developing|work(?:ing)?)(?:\s+on)?"
    r"|(?:fix|implement|work on)\s+(?:github\s+)?issue\s*#\d+)\b", re.I
)
GITHUB_ACTION = re.compile(
    r"^\s*(?:(?:please|can you|could you|would you|i want you to|i need you to)\s+)?"
    r"(?:create|make|open|add|delete|rename|merge|change)\s+"
    r"(?:(?:a|an|the|new|github)\s+)*(?:repositor(?:y|ies)|repos?|branches?|pull requests?|PRs?)\b"
    r"|^\s*(?:(?:please|can you|could you|would you)\s+)?"
    r"(?:fix|implement|create|work on|start (?:working|development) on)\s+(?:github\s+)?issues?\s*#\d+\b",
    re.I,
)


def request_text(text, filename=None, pdf_text=None, develop=False):
    """Return an issue title/body, or None when this is not an intake request."""
    match = COMMAND.search(text)
    if not match and pdf_text is None and not develop:
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
        body = ((f"Slack request:\n\n{content.strip()}\n\n" if develop else "")
                + f"Plan from {filename}:\n\n{pdf_text.strip()}")
    elif not body:
        body = title
    if len(body) > MAX_BODY_CHARS:
        raise ValueError("Plan is too long for one issue; please split it into smaller PDFs.")
    return title, body


def generic_issue_request(text, command):
    return bool(command and GENERIC_ISSUE.fullmatch(
        REPO_HINT.sub("", text[command.end():], count=1).strip(" ,:.-\n")))


def repo_from_text(text):
    match = REPO_URL.search(text)
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2).removesuffix('.git').rstrip('.')}"


def same_repo(left, right):
    return bool(left and right) and left.casefold() == right.casefold()


def repo_hint(text):
    match = REPO_HINT.search(text)
    if not match:
        return None
    hint = match.group(1).strip()
    return None if hint.lower() in ("a", "this", "the", "that", "my", "same", "current") else hint


def resolve_repo_hint(hint, token):
    """Match a spoken repository name only when GitHub yields one unambiguous accessible repo."""
    key = re.sub(r"[^a-z0-9]", "", hint.lower())
    if not key:
        return None
    repos = []
    for page in range(1, 11):
        req = urllib.request.Request(
            "https://api.github.com/user/repos?affiliation=owner,collaborator,organization_member"
            f"&per_page=100&page={page}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            batch = json.load(response)
        repos.extend(repo["full_name"] for repo in batch)
        if len(batch) < 100:
            break
    else:
        raise ValueError("I couldn't check every accessible repo. Please send its GitHub URL.")
    names = [(repo, re.sub(r"[^a-z0-9]", "", repo.rsplit("/", 1)[1].lower())) for repo in repos]
    exact = [repo for repo, name in names if name == key or name.removeprefix("the") == key.removeprefix("the")]
    if len(exact) != 1:
        raise ValueError(f"I couldn't uniquely identify the `{hint}` repo. Please send its GitHub URL.")
    return exact[0]


def saved_repo(con, channel, root_ts):
    con.execute("CREATE TABLE IF NOT EXISTS thread_repos (channel TEXT NOT NULL, root_ts TEXT NOT NULL, "
                "repo TEXT NOT NULL, PRIMARY KEY (channel, root_ts))")
    row = con.execute("SELECT repo FROM thread_repos WHERE channel=? AND root_ts=?",
                      (channel, root_ts)).fetchone()
    return row[0] if row else None


def remember_repo(con, channel, root_ts, repo):
    con.execute("INSERT OR REPLACE INTO thread_repos VALUES (?, ?, ?)", (channel, root_ts, repo))
    con.commit()


def remember_issue(con, channel, root_ts, issue_url):
    con.execute("CREATE TABLE IF NOT EXISTS thread_issues (channel TEXT NOT NULL, root_ts TEXT NOT NULL, "
                "issue_url TEXT NOT NULL, PRIMARY KEY (channel, root_ts))")
    con.execute("INSERT OR REPLACE INTO thread_issues VALUES (?, ?, ?)", (channel, root_ts, issue_url))
    con.commit()


def thread_issue(con, client, event, bot_user):
    con.execute("CREATE TABLE IF NOT EXISTS thread_issues (channel TEXT NOT NULL, root_ts TEXT NOT NULL, "
                "issue_url TEXT NOT NULL, PRIMARY KEY (channel, root_ts))")
    row = con.execute("SELECT issue_url FROM thread_issues WHERE channel=? AND root_ts=?",
                      (event["channel"], thread_root_ts(event))).fetchone()
    if row:
        return row[0]
    try:
        messages = client.conversations_replies(channel=event["channel"],
                                                ts=thread_root_ts(event), limit=100)["messages"]
    except Exception:
        return None
    for message in reversed(messages):
        if message.get("user") != bot_user and not message.get("bot_id"):
            continue
        match = ISSUE_URL.search(message.get("text", ""))
        if match:
            remember_issue(con, event["channel"], thread_root_ts(event), match.group())
            return match.group()
    return None


def repo_from_thread(client, event, bot_user, workspace_team, github_token):
    """Recover a repo link from a thread begun before context persistence existed."""
    try:
        messages = client.conversations_replies(channel=event["channel"],
                                                ts=thread_root_ts(event), limit=100)["messages"]
    except Exception as exc:
        raise ValueError("I couldn't check this thread's project. Please send the GitHub repository URL.") from exc
    for message in reversed(messages):
        if message.get("bot_id") or message.get("user") == bot_user:
            continue
        if not workspace_member(message, client, workspace_team):
            continue
        text = message.get("text", "")
        linked = repo_from_text(text)
        hint = repo_hint(text)
        spoken = resolve_repo_hint(hint, github_token) if hint else None
        if linked and spoken and not same_repo(linked, spoken):
            raise ValueError("this thread names two different repositories. Please send one repository URL.")
        repo = linked or spoken
        if repo:
            return repo
    return None


def task_from_thread(client, event, bot_user, workspace_team, target_repo, github_token):
    try:
        messages = client.conversations_replies(channel=event["channel"], ts=thread_root_ts(event), limit=100)["messages"]
    except Exception as exc:
        raise ValueError("I couldn't read the task in this thread. Please describe it again.") from exc
    active_repo = task = None
    for message in messages:
        if message.get("bot_id") or message.get("user") == bot_user:
            continue
        if not workspace_member(message, client, workspace_team):
            continue
        text = message.get("text", "").replace(f"<@{bot_user}>", "").strip()
        linked = repo_from_text(text)
        hint = repo_hint(text)
        spoken = resolve_repo_hint(hint, github_token) if hint else None
        if linked and spoken and not same_repo(linked, spoken):
            raise ValueError("this thread names two different repositories. Please describe the task again with one URL.")
        mentioned = linked or spoken
        if mentioned and not same_repo(mentioned, active_repo):
            active_repo, task = mentioned, None
        command = COMMAND.search(text)
        if (same_repo(active_repo, target_repo) and REPO_TASK.search(text)
                and not generic_issue_request(text, command)):
            task = REPO_HINT.sub("", text, count=1).strip(" ,:.-\n")
    return task


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


def create_issue(repo, token, title, body, channel, ts, label="needs-human"):
    if label not in (None, "needs-human", "agent-ready"):
        raise ValueError("Unsupported issue label.")
    permalink = f"https://app.slack.com/archives/{channel}/p{ts.replace('.', '')}"
    issue_data = {
        "title": title,
        "body": f"Submitted from Slack: {permalink}\n\n{body}",
    }
    if label:
        issue_data["labels"] = [label]
    payload = json.dumps(issue_data).encode()
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
    labelled = label is None or any(item["name"] == label for item in issue.get("labels", []))
    return issue["html_url"], labelled


def dispatch_ready(number, repo):
    """Start ready work now; cron remains the fallback when all slots are busy."""
    home = os.environ.get("HERMES_HOME", "/opt/data")
    env = {key: os.environ[key] for key in ("PATH", "HOME", "GH_TOKEN", "XDG_CONFIG_HOME")
           if key in os.environ}
    env["HERMES_HOME"] = home
    result = subprocess.run([str(Path(home) / "scripts" / "dispatch.sh"), repo, str(number)],
                            env=env, cwd="/tmp", capture_output=True, text=True, timeout=30)
    return result.returncode == 0 and f"Dispatched {repo}#{number} " in result.stdout


def ensure_jobs(con, default_repo):
    con.execute("CREATE TABLE IF NOT EXISTS jobs_v2 (repo TEXT NOT NULL, issue INTEGER NOT NULL, "
                "channel TEXT NOT NULL, root_ts TEXT NOT NULL, state TEXT NOT NULL, "
                "PRIMARY KEY (repo, issue))")
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone():
        con.execute("INSERT OR IGNORE INTO jobs_v2 SELECT ?, issue, channel, root_ts, state FROM jobs",
                    (default_repo,))
        con.execute("DROP TABLE jobs")
    con.commit()


def track_job(con, channel, root_ts, issue_url, default_repo):
    match = ISSUE_URL.search(issue_url)
    if not match:
        raise ValueError("GitHub returned an unexpected issue URL.")
    repo, number = f"{match.group(1)}/{match.group(2)}", int(match.group(3))
    ensure_jobs(con, default_repo)
    con.execute("INSERT OR REPLACE INTO jobs_v2 VALUES (?, ?, ?, ?, ?)",
                (repo, number, channel, root_ts, "ready"))
    con.commit()


def github_get(repo, token, path):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}" + (f"/{path}" if path else ""),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def github_write(repo, token, path, payload=None, method="POST"):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"}, method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response) if response.status != 204 else None


def ensure_agent_labels(repo, token):
    for name, color in (("agent-ready", "0E8A16"), ("agent-working", "FBCA04"),
                        ("agent-pr", "1D76DB"), ("needs-human", "D93F0B")):
        try:
            github_get(repo, token, f"labels/{name}")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            github_write(repo, token, "labels", {"name": name, "color": color})


def queue_issue(repo, token, number):
    issue = github_get(repo, token, f"issues/{number}")
    if "pull_request" in issue:
        raise ValueError("that is a pull request, not an issue.")
    labels = {item["name"] for item in issue.get("labels", [])}
    if "agent-pr" in labels:
        return "pr"
    if issue.get("state") != "open":
        raise ValueError("that issue is not open.")
    if issue.get("author_association") not in ("OWNER", "MEMBER", "COLLABORATOR"):
        raise ValueError("the issue author is not a repository collaborator.")
    if "agent-working" in labels:
        return "working"
    ensure_agent_labels(repo, token)
    if "needs-human" in labels:
        github_write(repo, token, f"issues/{number}/labels/needs-human", method="DELETE")
    if "agent-ready" not in labels:
        github_write(repo, token, f"issues/{number}/labels", {"labels": ["agent-ready"]})
    return "ready"


def issue_status(repo, token, number):
    issue = github_get(repo, token, f"issues/{number}")
    labels = {item["name"] for item in issue.get("labels", [])}
    if "agent-pr" in labels:
        # ponytail: first 100 comments cover a pilot issue; paginate if threads grow past that.
        comments = github_get(repo, token, f"issues/{number}/comments?per_page=100")
        pattern = re.compile(rf"https://github\.com/{re.escape(repo)}/pull/\d+", re.I)
        for comment in reversed(comments):
            match = pattern.search(comment.get("body", ""))
            if match:
                return "pr", f"Development finished for issue #{number}: {match.group()} (ready for review)."
    elif "needs-human" in labels:
        return "failed", f"Development stopped on issue #{number}; please review it on GitHub."
    elif "agent-working" in labels:
        return "working", f"Development started on issue #{number}. I'll share the PR here when it is ready."
    elif "agent-ready" in labels:
        return "ready", None
    return None, None


def poll_status(client, default_repo, token):
    with closing(sqlite3.connect(STATE)) as con:
        ensure_jobs(con, default_repo)
        for repo, number, channel, root_ts, previous in con.execute(
                "SELECT repo, issue, channel, root_ts, state FROM jobs_v2").fetchall():
            try:
                if previous == "ready":
                    dispatch_ready(number, repo)
                state, message = issue_status(repo, token, number)
                if not state or state == previous:
                    continue
                if message:
                    client.chat_postMessage(channel=channel, thread_ts=root_ts, text=message)
                if state in ("pr", "failed"):
                    con.execute("DELETE FROM jobs_v2 WHERE repo=? AND issue=?", (repo, number))
                else:
                    con.execute("UPDATE jobs_v2 SET state=? WHERE repo=? AND issue=?",
                                (state, repo, number))
                con.commit()
            except Exception as exc:
                # Slack delivery can fail transiently; leave the row to retry next tick.
                print(f"Slack status update for issue #{number} failed ({type(exc).__name__})",
                      file=sys.stderr, flush=True)
                continue


def status_loop(client, repo, token):
    while True:
        threading.Event().wait(45)
        try:
            poll_status(client, repo, token)
        except Exception as exc:
            print(f"Slack status polling failed ({type(exc).__name__})", file=sys.stderr, flush=True)


def workspace_member(event, client, team_id):
    """Resolve the speaker, not only the channel, before a GitHub operation."""
    if not team_id or not event.get("user"):
        return False
    if any(event.get(key) not in (None, team_id)
           for key in ("team", "team_id", "user_team", "source_team")):
        return False
    try:
        user = client.users_info(user=event["user"])["user"]
    except Exception:
        return False
    return user.get("team_id") == team_id and not user.get("is_stranger", False)


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
    answer = result.stdout.strip()[:3000]
    if re.search(r"<\|?tool_call_(?:start|end)\|?>|\[(?:read_file|glob)\(", answer, re.I):
        return "I can't inspect repository files in chat. Ask me to make the change, and I'll start development in the linked repo."
    return answer or "I couldn't answer just now. Please try again shortly."


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
    with closing(sqlite3.connect(STATE)) as con:
        ensure_jobs(con, repo)
    threading.Thread(target=status_loop, args=(app.client, repo, github_token), daemon=True).start()

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
                start_request = bool(START_DEVELOPMENT.match(text))
                unsupported_operation = bool(GITHUB_ACTION.match(text)) and not start_request
                issue_command = COMMAND.search(text)
                hint = repo_hint(text)
                develop = (not issue_command and not start_request and not unsupported_operation
                           and bool(DEVELOP.match(text) or (hint and REPO_TASK.search(text))))
                github_request = bool(files or issue_command or develop or start_request or LIST_ISSUES.search(text) or hint)
                linked_repo = repo_from_text(text)
                member = workspace_member(event, app.client, workspace_team) if github_request or linked_repo else False
                if github_request and not member:
                    raise ValueError("only members of this Slack workspace can access GitHub through me.")
                spoken_repo = resolve_repo_hint(hint, github_token) if hint else None
                if linked_repo and spoken_repo and not same_repo(linked_repo, spoken_repo):
                    raise ValueError(f"you mentioned both `{linked_repo}` and `{spoken_repo}`. "
                                     "Please send one repository URL for this task.")
                explicit_repo = linked_repo or spoken_repo
                stored_repo = saved_repo(con, channel, root_ts)
                # A human's project mention in the thread outranks a stale saved value.
                thread_repo = (repo_from_thread(app.client, event, bot_user, workspace_team, github_token)
                               if not explicit_repo and event.get("thread_ts") and github_request else None)
                target_repo = explicit_repo or thread_repo or stored_repo
                if target_repo and not same_repo(target_repo, stored_repo) and member:
                    remember_repo(con, channel, root_ts, target_repo)
                if github_request and not target_repo and not ISSUE_URL.search(text):
                    raise ValueError("which repository should I use? Send its GitHub URL so I don't change the wrong project.")
                generic_issue = generic_issue_request(text, issue_command)
                if generic_issue and event.get("thread_ts"):
                    existing = thread_issue(con, app.client, event, bot_user)
                    if existing and same_repo(repo_from_text(existing), target_repo):
                        start_request = True
                if start_request:
                    match = ISSUE_URL.search(text)
                    issue_url = match.group() if match else thread_issue(con, app.client, event, bot_user)
                    if not issue_url:
                        reply = "Please create an issue in this thread or send its GitHub issue URL first."
                    else:
                        match = ISSUE_URL.search(issue_url)
                        issue_repo = f"{match.group(1)}/{match.group(2)}"
                        if target_repo and not same_repo(issue_repo, target_repo):
                            raise ValueError(f"the issue I found is in `{issue_repo}`, but this thread is about "
                                             f"`{target_repo}`. Ask me to create the task in the correct repo.")
                        target_repo = issue_repo
                        number = int(match.group(3))
                        state = queue_issue(target_repo, github_token, number)
                        if state == "pr":
                            _, reply = issue_status(target_repo, github_token, number)
                            reply = reply or f"A PR is already open for {issue_url}."
                        elif state == "working":
                            reply = f"Development is already running for {issue_url}."
                        else:
                            track_job(con, channel, root_ts, issue_url, repo)
                            dispatch_ready(number, target_repo)
                            reply = f"Queued development for {issue_url}. I'll post the PR in this thread."
                elif (files or issue_command or develop) and not unsupported_operation:
                    if not target_repo:
                        raise ValueError("which repository should I use? Send its GitHub URL so I don't change the wrong project.")
                    if not same_repo(target_repo, repo):
                        try:
                            details = github_get(target_repo, github_token, "")
                        except urllib.error.HTTPError as exc:
                            if exc.code in (403, 404):
                                raise ValueError(f"the bot cannot access `{target_repo}`. Check its GitHub access.") from exc
                            raise
                        if not details.get("permissions", {}).get("push") or not details.get("has_issues"):
                            raise ValueError(f"the bot cannot create issues in `{target_repo}`.")
                    ensure_agent_labels(target_repo, github_token)
                    pdf_text = pdf_to_text(files[0], bot_token) if files else None
                    task_text = (task_from_thread(app.client, event, bot_user, workspace_team,
                                                  target_repo, github_token) if generic_issue else None)
                    if generic_issue and not task_text:
                        raise ValueError(f"please describe what to change in `{target_repo}` before I create an issue.")
                    request = request_text(task_text or text, files[0].get("name") if files else None,
                                           pdf_text, develop=develop or bool(task_text))
                    issue_attempted = True
                    url, labelled = create_issue(target_repo, github_token, *request, channel, ts,
                                                 label="agent-ready")
                    if labelled:
                        remember_issue(con, channel, root_ts, url)
                        track_job(con, channel, root_ts, url, repo)
                        try:
                            dispatch_ready(int(url.rstrip("/").rsplit("/", 1)[-1]), target_repo)
                        except (OSError, subprocess.TimeoutExpired):
                            pass  # the persisted queue retries on its next tick
                        reply = f"Created {url} and queued development. I'll post the PR here."
                    else:
                        reply = (f"Created {url}, but GitHub did not apply `agent-ready`. "
                                 "Development will not start until that label is added.")
                elif LIST_ISSUES.search(text):
                    reply = issue_reply(target_repo, github_token)
                elif hint and explicit_repo:
                    reply = f"Got it. I'll use `{target_repo}` for this thread."
                elif unsupported_operation:
                    reply = (f"I can start development in `{target_repo or 'a linked repo'}` from a task description, "
                             "but cannot safely perform that GitHub operation from Slack yet.")
                else:
                    reply = assistant_reply(text, model, session=session,
                                            speaker=event.get("user") if not direct else None)
            except (ValueError, urllib.error.URLError, TimeoutError, subprocess.TimeoutExpired,
                    subprocess.CalledProcessError, sqlite3.Error) as exc:
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
