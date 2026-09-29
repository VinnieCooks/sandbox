"""Generate the GitHub Actions workflow (the `github` engine) from jobs.yaml.

GitHub reports which cron fired in `github.event.schedule`; the workflow passes
it to `ops run-scheduled`, which runs the jobs using that cron. The file is
fully generated, so jobs.yaml stays the only thing you edit; `ops check`
(run in CI) fails when the two drift apart.
"""

from __future__ import annotations

import math
from pathlib import Path

from .registry import Registry

WORKFLOW_FILE = Path(".github/workflows/ops-scheduler.yml")

_HEAD = """\
# GENERATED from automation/jobs.yaml by `python -m ops sync`. Do not edit by hand.
# GitHub runs schedules only from the default branch, and only while Actions is enabled.
name: ops-scheduler

on:
"""

_BODY = """\
  workflow_dispatch:
    inputs:
      job:
        description: Job name from automation/jobs.yaml
        required: true
        type: string

permissions:
  contents: read

concurrency:
  group: ops-scheduler-${{{{ github.event.schedule || inputs.job }}}}
  cancel-in-progress: false

jobs:
  run:
    runs-on: ubuntu-latest
    timeout-minutes: {timeout}
    steps:
      - uses: actions/checkout@v7
        with:
          persist-credentials: false
      - uses: actions/setup-python@v7
        with:
          python-version: "3.12"
          cache: pip
          cache-dependency-path: |
            automation/requirements.txt
            automation/jobs/requirements.txt
      - name: Install dependencies
        run: pip install -r automation/requirements.txt -r automation/jobs/requirements.txt
{claude_step}      - name: Run jobs
        working-directory: automation
        env:
          PYTHONUNBUFFERED: "1"
          # Every repository secret is visible to the runner process; each job only
          # receives the secrets it lists under `secrets:` in jobs.yaml.
          OPS_SECRETS_JSON: ${{{{ toJSON(secrets) }}}}
          # Job output stays out of the (public) log unless the repo variable OPS_REDACT_LOGS is "0".
          OPS_REDACT_LOGS: ${{{{ vars.OPS_REDACT_LOGS || '1' }}}}
          OPS_CRON: ${{{{ github.event.schedule }}}}
          OPS_JOB: ${{{{ inputs.job }}}}
        run: |
          if [ "$GITHUB_EVENT_NAME" = "schedule" ]; then
            python -m ops run-scheduled --cron "$OPS_CRON"
          else
            python -m ops run "$OPS_JOB"
          fi
"""

_CLAUDE_STEP = """\
      - name: Install Claude Code CLI
        run: npm install -g @anthropic-ai/claude-code
"""


def workflow_path(reg: Registry) -> Path:
    """automation/jobs.yaml -> <repo root>/.github/workflows/ops-scheduler.yml"""
    return reg.path.parent.parent / WORKFLOW_FILE


def timeout_minutes(reg: Registry) -> int:
    """Longest possible run: jobs sharing a cron run one after another."""
    github_jobs = [j for j in reg.jobs if j.engine == "github"]
    per_cron: dict[str, int] = {}
    for job in reg.scheduled("github"):
        for expr in job.schedules:
            per_cron[expr] = per_cron.get(expr, 0) + job.timeout
    longest = max([*per_cron.values(), *(j.timeout for j in github_jobs), 300])
    return min(360, math.ceil(longest / 60) + 10)


def render(reg: Registry) -> str:
    jobs = reg.scheduled("github")
    entries = sorted({(expr, job.timezone) for job in jobs for expr in job.schedules})
    lines = []
    if entries:
        lines.append("  schedule:")
        for expr, tz in entries:
            names = ", ".join(sorted(j.name for j in jobs if expr in j.schedules))
            lines.append(f'    - cron: "{expr}"  # {names}')
            if tz != "UTC":
                lines.append(f'      timezone: "{tz}"')
    # Only when an enabled claude job needs it: the install adds time to every run.
    needs_claude = any(j.type == "claude" and j.engine == "github" and j.enabled for j in reg.jobs)
    body = _BODY.format(timeout=timeout_minutes(reg), claude_step=_CLAUDE_STEP if needs_claude else "")
    return _HEAD + "".join(line + "\n" for line in lines) + body
