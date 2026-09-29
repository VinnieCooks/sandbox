"""Command line: `python -m ops <command>` from the automation/ directory."""

from __future__ import annotations

import argparse
import difflib
import os
import signal
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from croniter import croniter

from . import notify, routines, runner, workflow
from .registry import Registry, RegistryError, load, normalize_cron
from .scheduler import Server
from .settings import load_settings

DEFAULT_REGISTRY = Path(__file__).resolve().parent.parent / "jobs.yaml"


def _run_url(settings: dict[str, str]) -> str | None:
    keys = ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")
    if all(settings.get(k) for k in keys):
        return f"{settings['GITHUB_SERVER_URL']}/{settings['GITHUB_REPOSITORY']}/actions/runs/{settings['GITHUB_RUN_ID']}"
    return None


def make_context(reg: Registry, *, dry_run: bool = False) -> runner.Context:
    env_file = Path(os.environ.get("OPS_ENV_FILE") or reg.path.parent / ".env")
    settings = load_settings(env_file=env_file)
    return runner.Context(
        settings=settings,
        channels=notify.configured_channels(settings),
        redact_logs=settings.get("OPS_REDACT_LOGS") == "1",
        state_dir=Path(settings.get("OPS_STATE_DIR") or reg.path.parent / "state"),
        run_url=_run_url(settings),
        dry_run=dry_run,
    )


def _next_run(expr: str, tz: str) -> datetime:
    return croniter(expr, datetime.now(ZoneInfo(tz))).get_next(datetime)


def cmd_list(reg: Registry, _args) -> int:
    rows = [("NAME", "ENGINE", "SCHEDULE", "NEXT RUN", "NOTIFY", "STATE")]
    for job in reg.jobs:
        schedule = "; ".join(job.schedules) or "manual"
        if job.schedules:
            schedule += f" ({job.timezone})"
            upcoming = min(_next_run(e, job.timezone) for e in job.schedules)
            next_run = upcoming.strftime("%a %Y-%m-%d %H:%M %Z") if job.enabled else "-"
        else:
            next_run = "-"
        rows.append((job.name, job.engine, schedule, next_run, job.notify, "enabled" if job.enabled else "disabled"))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip())
    for warning in reg.warnings:
        print(f"warning: {warning}")
    return 0


def cmd_run(reg: Registry, args) -> int:
    job = reg.get(args.job)
    if not job.enabled:
        print(f"[ops] note: {job.name} is disabled in jobs.yaml; running it because you asked")
    result = runner.run_and_report(job, make_context(reg, dry_run=args.dry_run))
    return 0 if result.ok and result.delivered else 1


def cmd_run_scheduled(reg: Registry, args) -> int:
    cron = normalize_cron(args.cron)
    jobs = [j for j in reg.scheduled("github") if cron in j.schedules]
    if not jobs:
        print(f"[ops] error: no enabled github job uses cron {cron!r}; run `python -m ops check`")
        return 1
    ctx = make_context(reg)
    results = [runner.run_and_report(job, ctx) for job in jobs]
    # A failed job or an undelivered notification fails the run, so GitHub's own
    # failed-run email still reaches you when a channel is broken.
    return 0 if all(r.ok and r.delivered for r in results) else 1


def cmd_serve(reg: Registry, _args) -> int:
    server = Server(reg.path, make_context)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    if not server.ctx.channels:
        print("[ops] warning: no notification channel configured; see `python -m ops channels`")
    server.run_forever(stop)
    return 0


def cmd_sync(reg: Registry, _args) -> int:
    path = workflow.workflow_path(reg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(workflow.render(reg), encoding="utf-8")
    print(f"wrote {path}")
    return 0


def cmd_check(reg: Registry, _args) -> int:
    for warning in reg.warnings:
        print(f"warning: {warning}")
    path = workflow.workflow_path(reg)
    expected = workflow.render(reg)
    actual = path.read_text(encoding="utf-8") if path.exists() else ""
    if actual != expected:
        diff = difflib.unified_diff(actual.splitlines(True), expected.splitlines(True),
                                    fromfile=f"{path} (on disk)", tofile="generated from jobs.yaml")
        sys.stdout.writelines(diff)
        print(f"\nerror: {path.name} is out of date; run `python -m ops sync` and commit it")
        return 1
    counts = {e: len(reg.scheduled(e)) for e in ("github", "server")}
    print(f"ok: {len(reg.jobs)} jobs ({counts['github']} scheduled on github, {counts['server']} on server); workflow in sync")
    return 0


def cmd_channels(reg: Registry, _args) -> int:
    ctx = make_context(reg)
    names = [c.name for c in ctx.channels]
    print("configured channels: " + (", ".join(names) if names else "none"))
    for problem in notify.channel_problems(ctx.settings):
        print(f"problem: {problem}")
    return 0 if names else 1


def cmd_notify_test(reg: Registry, args) -> int:
    ctx = make_context(reg, dry_run=args.dry_run)
    sent_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    message = notify.Message(
        title="ops test notification",
        body=f"If you can read this, notifications work.\nSent {sent_at} from {runner.where_label()}.",
        level="info", url=ctx.run_url,
    )
    if args.dry_run:
        print(f"(dry run) would send to: {', '.join(c.name for c in ctx.channels) or 'no channels'}")
        return 0
    if not ctx.channels:
        print("no channels configured; see `python -m ops channels`")
        return 1
    results = notify.dispatch(message, ctx.channels)
    for name, status in results.items():
        print(f"{name}: {status}")
    return 0 if all(v == "ok" for v in results.values()) else 1


def cmd_fire_routine(reg: Registry, args) -> int:
    ctx = make_context(reg)
    try:
        url, token = routines.credentials(args.name, ctx.settings)
        response = routines.fire(url, token, args.text)
    except routines.RoutineError as exc:
        print(f"error: {exc}")
        return 1
    print(f"started: {response.get('claude_code_session_url', response)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ops", description="Run and schedule jobs from jobs.yaml, with notifications.")
    parser.add_argument("--registry", default=os.environ.get("OPS_REGISTRY", str(DEFAULT_REGISTRY)),
                        help="path to jobs.yaml (default: %(default)s)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show jobs, schedules and next run times")
    p = sub.add_parser("run", help="run one job now; notifies per its policy")
    p.add_argument("job")
    p.add_argument("--dry-run", action="store_true", help="print notifications instead of sending them")
    p = sub.add_parser("run-scheduled", help="(github engine) run the jobs whose cron fired")
    p.add_argument("--cron", required=True)
    sub.add_parser("serve", help="(server engine) run the scheduler loop until stopped")
    sub.add_parser("sync", help="regenerate .github/workflows/ops-scheduler.yml from jobs.yaml")
    sub.add_parser("check", help="validate jobs.yaml and verify the workflow is in sync")
    sub.add_parser("channels", help="show which notification channels are configured")
    p = sub.add_parser("notify-test", help="send a test notification to every configured channel")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("fire-routine", help="start a Claude Cloud Routine through its API trigger")
    p.add_argument("name", help="routine key: uses ROUTINE_<NAME>_URL and ROUTINE_<NAME>_TOKEN")
    p.add_argument("--text", default="", help="context passed to the routine")
    args = parser.parse_args(argv)

    commands = {
        "list": cmd_list, "run": cmd_run, "run-scheduled": cmd_run_scheduled, "serve": cmd_serve,
        "sync": cmd_sync, "check": cmd_check, "channels": cmd_channels,
        "notify-test": cmd_notify_test, "fire-routine": cmd_fire_routine,
    }
    try:
        return commands[args.command](load(args.registry), args)
    except RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
