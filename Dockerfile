FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends git nodejs npm \
    && rm -rf /var/lib/apt/lists/*
# Claude Agent SDK shells out to the claude CLI
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app
COPY pyproject.toml ./
# Install exactly what pyproject declares, version floors included. A second
# hardcoded list here meant those floors were enforced nowhere in the container,
# and the two drifted the moment a dependency was added. pytest is extra because
# gate commands run `python -m pytest` inside this image.
RUN python -c "\
import tomllib, subprocess, sys; \
deps = tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']; \
subprocess.check_call([sys.executable,'-m','pip','install','--no-cache-dir','pytest',*deps])"

# The claude CLI refuses bypassPermissions as root, and the container writes into
# your repos through a bind mount, so this user must share your host uid.
ARG LOOPGRAPH_UID=1000
RUN useradd -m -u ${LOOPGRAPH_UID} worker
USER worker

CMD ["python", "worker.py"]
