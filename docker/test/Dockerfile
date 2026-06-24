# ClawsomeFlow test image — runs the pytest suite fully isolated from the host.
# Built ONLY from read-only copies staged by build-and-test.sh; it never writes
# back to the source tree and never touches the live csflow service.
FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# System deps: git (worktree tests), tmux (clawteam spawn), build tools, procps.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git tmux curl ca-certificates build-essential procps \
    && rm -rf /var/lib/apt/lists/*

# Node 22 for the real openclaw CLI (public npm package, no auth to install).
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

# Real openclaw runtime CLI.
RUN npm install -g openclaw@2026.6.5

# Real clawteam protocol CLI, installed from the staged local source checkout
# (clawteam 0.3.0 is not published to PyPI).
COPY clawteam /opt/clawteam
RUN uv pip install --system /opt/clawteam

# Fake, auth-free stand-ins for the login-gated agent CLIs. Tests that shell out
# to an LLM agent get a canned reply instead of needing a real account/API key
# ("制造假回复来闭环"). One generic script, symlinked under every platform name.
COPY fake-bin/fake-cli /usr/local/bin/fake-cli
RUN chmod +x /usr/local/bin/fake-cli \
    && for n in claude codex hermes gemini qwen kimi opencode cursor nanobot qoder codebuddy; do \
         ln -sf /usr/local/bin/fake-cli /usr/local/bin/$n; \
       done

# Tests create throwaway git repos whose flows target the `main` branch and need
# a commit identity. Pin both system-wide so the suite behaves like a dev box
# with `init.defaultBranch=main` configured (bare git defaults to `master`).
# Placed late so the heavy node/openclaw/clawteam layers above stay cached.
RUN git config --system init.defaultBranch main \
    && git config --system user.email "tests@clawsomeflow.local" \
    && git config --system user.name "ClawsomeFlow Tests"

# ClawsomeFlow source (read-only copy) + backend install.
COPY app /app
WORKDIR /app
# Editable install so pytest imports the in-tree backend/app package. The
# force-included frontend/dist is provided as a placeholder by the staging step.
RUN uv pip install --system -e "/app/backend[dev]"

COPY run-tests.sh /usr/local/bin/run-tests.sh
RUN chmod +x /usr/local/bin/run-tests.sh

ENTRYPOINT ["/usr/local/bin/run-tests.sh"]
