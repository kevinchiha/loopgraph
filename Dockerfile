FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends git nodejs npm \
    && rm -rf /var/lib/apt/lists/*
# Claude Agent SDK shells out to the claude CLI
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app
COPY pyproject.toml ./
# Deps installed from pyproject (no package build; source is volume-mounted).
# pytest: gate commands run `python -m pytest` inside this container.
RUN pip install --no-cache-dir temporalio pyyaml httpx pytest langgraph claude-agent-sdk

# The claude CLI refuses bypassPermissions as root, and the container writes into
# your repos through a bind mount, so this user must share your host uid.
ARG LOOPGRAPH_UID=1000
RUN useradd -m -u ${LOOPGRAPH_UID} worker
USER worker

CMD ["python", "worker.py"]
