# Official Hermes image + what the stack needs: gh, opencode, and an unprivileged `runner` user
# for the coding agent (it can't read /opt/data, where GH_TOKEN and Hermes state live).
FROM nousresearch/hermes-agent:latest

# /srv/agent-work is hermes-owned and not writable by runner. wt/ is setgid agentwork, so each
# per-issue work tree is shared with runner. git/ and logs/ are hermes-only.
RUN apt-get -o Acquire::Retries=3 update \
 && apt-get install -y --no-install-recommends gh sudo \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g opencode-ai@latest \
 && groupadd agentwork && useradd -m -U -G agentwork -s /bin/bash runner \
 && echo 'hermes ALL=(runner) NOPASSWD: ALL' > /etc/sudoers.d/runner && chmod 440 /etc/sudoers.d/runner \
 && mkdir -p /srv/agent-work/wt /srv/agent-work/git /srv/agent-work/logs \
 && chown -R hermes:hermes /srv/agent-work && chmod 755 /srv/agent-work \
 && chgrp agentwork /srv/agent-work/wt && chmod 2755 /srv/agent-work/wt \
 && chmod 700 /srv/agent-work/git /srv/agent-work/logs

COPY --chown=root:root . /stack
