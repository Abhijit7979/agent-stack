# Official Hermes image + what the stack needs: gh, opencode, and an unprivileged `runner` user
# for the coding agent (it can't read /opt/data, where GH_TOKEN and Hermes state live).
FROM nousresearch/hermes-agent:latest

RUN apt-get -o Acquire::Retries=3 update \
 && apt-get install -y --no-install-recommends gh sudo \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g opencode-ai@latest \
 && useradd -m -g hermes -s /bin/bash runner \
 && echo 'hermes ALL=(runner) NOPASSWD: ALL' > /etc/sudoers.d/runner && chmod 440 /etc/sudoers.d/runner \
 && mkdir -p /srv/agent-work && chown hermes:hermes /srv/agent-work && chmod 2770 /srv/agent-work

COPY --chown=root:root . /stack
