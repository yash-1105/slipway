# The sandbox agent tool calls run in. Nothing an agent asks for executes in the
# orchestrator process -- see CLAUDE.md, "Agent tool calls run only inside the
# sandbox".
#
# Built by `make sandbox-image`. Started per run by app/sandbox/impl/docker.py,
# which adds --read-only, --cap-drop ALL, --network none (or an allowlisted
# bridge), a pids limit and CPU/memory caps. This image assumes all of that and
# does not try to re-impose it.

FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_AUDIT=false

# The toolchain an agent is allowed to use. Adding to this list is a decision:
# it widens what a generated application can be built from, and every entry has
# to be something we are willing to ship to a client.
RUN apt-get update && apt-get install --yes --no-install-recommends \
        ca-certificates \
        curl \
        git \
        python3 \
        python3-venv \
        python3-pip \
        nodejs \
        npm \
        make \
        ripgrep \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged, and the workspace is the only thing it owns. The container runs
# --read-only, so anything that needs to write must write under /workspace or
# the tmpfs at /tmp.
RUN useradd --create-home --uid 10001 --shell /bin/bash agent \
    && mkdir -p /workspace \
    && chown agent:agent /workspace

USER agent
WORKDIR /workspace

# No CMD that does work: the orchestrator starts this with `sleep infinity` and
# drives it with `docker exec`, so the container's lifetime is the run's, not a
# single command's.
CMD ["sleep", "infinity"]
