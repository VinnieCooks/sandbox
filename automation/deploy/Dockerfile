# Server engine image: Python plus job dependencies. The repository is mounted at
# /repo at runtime (see docker-compose.yml), so `git pull` updates jobs without a
# rebuild; rebuild only when requirements change.
FROM python:3.12-slim

ARG INSTALL_CLAUDE=0

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 # the mounted checkout belongs to the host user; let root's git operate on it
 && git config --system --add safe.directory /repo

COPY automation/requirements.txt /tmp/requirements.txt
COPY automation/jobs/requirements.txt /tmp/requirements-jobs.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt -r /tmp/requirements-jobs.txt

# Claude Code CLI, only needed for `type: claude` jobs: build with INSTALL_CLAUDE=1.
RUN if [ "$INSTALL_CLAUDE" = "1" ]; then curl -fsSL https://claude.ai/install.sh | bash; fi
ENV PATH="/root/.local/bin:${PATH}" \
    PYTHONUNBUFFERED=1

WORKDIR /repo/automation
CMD ["python", "-m", "ops", "serve"]
