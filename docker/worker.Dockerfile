# syntax=docker/dockerfile:1.7
#
# Worker image for the Docker backend of `orchestrator.workers.docker_claude_code`.
# Built as `orchestrator/worker:latest` by `scripts/build-worker-image.sh`
# (added in a later unit — for now, build manually with
# `docker build -f docker/worker.Dockerfile -t orchestrator/worker:latest .`).
#
# Contents:
#   - Python 3.12 (matches the orchestrator's lower CI matrix bound)
#   - Node 22 LTS (Claude Code is published to npm + needs node at runtime)
#   - git + GitHub CLI (`gh`) for repo operations from inside the container
#   - `claude` CLI (Claude Code) installed globally via npm
#
# Container runtime contract (enforced by `DockerClaudeCodeWorker`):
#   - Runs as non-root user `agent` (uid/gid 1000)
#   - Rootfs is read-only; only `/workspace`, `/tmp`, `/home/agent/.cache`
#     and `/home/agent/.claude/sessions` are writable
#   - OAuth mode: `~/.claude` from the host is bind-mounted read-only at
#     `/home/agent/.claude`. The directory baked into the image is just a
#     placeholder so the mount target exists.
#   - API-key mode: `ANTHROPIC_API_KEY` is forwarded via `--env`; no mount.
#
# The image deliberately does NOT bake any secrets or host-side dotfiles
# (no `~/.gitconfig`, no `~/.ssh`, no `~/.aws`). The worker writes a
# minimal `gitconfig` for the `agent` user inside the image so `git`
# operations have an author identity without leaking the host's.

FROM python:3.12-slim-bookworm

ARG NODE_MAJOR=22
ARG AGENT_UID=1000
ARG AGENT_GID=1000

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NODE_ENV=production

# Base tooling: git, curl, ca-certs, gnupg for repo signing, plus the
# GitHub CLI repo. Pinning specific versions of system packages is more
# noise than it's worth here — the worker is short-lived and the image
# is rebuilt on every release.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        gnupg \
        tini \
    && install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | gpg --dearmor -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
    && apt-get install -y --no-install-recommends gh nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code globally so `claude` is on PATH for the non-root
# agent user. The package name is `@anthropic-ai/claude-code`.
RUN npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force

# Non-root agent user. uid/gid 1000 matches the `--user 1000:1000` flag
# the worker passes to `docker run`. Creating it here keeps `/home/agent`
# in the image so the rootfs read-only flag at runtime doesn't reject
# the mount targets we set up below.
RUN groupadd --system --gid ${AGENT_GID} agent \
    && useradd --system --uid ${AGENT_UID} --gid ${AGENT_GID} \
        --home-dir /home/agent --create-home --shell /bin/bash agent \
    && mkdir -p /home/agent/.claude/sessions \
                /home/agent/.cache \
                /workspace \
    && chown -R agent:agent /home/agent /workspace

# Minimal gitconfig for the agent user — the worker explicitly does NOT
# mount the host's ~/.gitconfig. Commits made from inside the container
# use a generic identity; real authorship is set by the coder via
# `git -c user.email=... commit`.
RUN printf '[user]\n\tname = orchestrator-worker\n\temail = worker@orchestrator.local\n[init]\n\tdefaultBranch = main\n[safe]\n\tdirectory = /workspace\n' \
        > /home/agent/.gitconfig \
    && chown agent:agent /home/agent/.gitconfig

USER agent
WORKDIR /workspace

# tini reaps zombies (claude shells out to git/gh and orphaned children
# would otherwise pile up across the container's life).
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["claude", "--version"]
