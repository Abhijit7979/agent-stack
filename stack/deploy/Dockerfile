# Official Hermes image + what the stack needs: gh, opencode, and unprivileged runner users
# for the coding agent (it can't read /opt/data, where GH_TOKEN and Hermes state live).
FROM nousresearch/hermes-agent:latest

# One unprivileged user per parallel slot (runner1, runner2) plus one for triage. /srv/agent-work is hermes-only; each
# task's work tree is opened to its slot user by ACL in work-issue.sh.
RUN apt-get -o Acquire::Retries=3 update \
 && apt-get install -y --no-install-recommends gh sudo acl poppler-utils \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g opencode-ai@latest \
 && for u in runner1 runner2 triager; do useradd -m -U -s /bin/bash "$u"; done \
 && echo 'hermes ALL=(runner1,runner2,triager) NOPASSWD: ALL' > /etc/sudoers.d/runners && chmod 440 /etc/sudoers.d/runners \
 && mkdir -p /srv/agent-work/wt /srv/agent-work/git /srv/agent-work/logs \
 && chown -R hermes:hermes /srv/agent-work \
 && chmod 711 /srv/agent-work /srv/agent-work/wt && chmod 700 /srv/agent-work/git /srv/agent-work/logs

COPY --chown=root:root . /stack
