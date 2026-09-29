"""Execute one job, decide whether to notify, and report.

Job contract (shell and claude jobs alike):
  * exit code 0 = success; anything else = failure; exceeding `timeout` = timeout
  * text written to the file named by $OPS_NOTIFY is a "signal": the message to
    send even on success (notify: signal sends only when there is one)
  * declared `secrets` arrive as environment variables; OPS_SECRETS_JSON never does
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Callable, Mapping

from . import notify, routines
from .registry import Job

CAPTURE_BYTES = 256 * 1024     # keep the last 256 KiB of output per stream
BODY_CHARS = 3000              # output tail placed in a failure notification
KILL_GRACE = 5                 # seconds between SIGTERM and SIGKILL on timeout
QUIET_TOKEN = "NOTHING_TO_REPORT"
CLAUDE_AUTH = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")


@dataclass
class RunResult:
    job: str
    status: str                    # success | failed | timeout | error
    exit_code: int | None
    started: datetime
    duration: float
    output: str
    signal: str | None = None
    cost_usd: float | None = None
    notes: list[str] = field(default_factory=list)
    notified: dict[str, str] = field(default_factory=dict)
    routine_url: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "success"

    @property
    def delivered(self) -> bool:
        """False when a notification was due but did not reach every channel."""
        return all(v == "ok" for v in self.notified.values())


@dataclass
class Context:
    settings: Mapping[str, str]
    channels: list[notify.Channel]
    redact_logs: bool = False
    state_dir: Path | None = None
    run_url: str | None = None
    dry_run: bool = False
    base_env: Mapping[str, str] | None = None     # defaults to os.environ
    log: Callable[[str], None] = print


# --------------------------------------------------------------------------- execution

def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _kill_tree(proc: subprocess.Popen) -> None:
    """Stop the job and everything it started (its own process group)."""
    if os.name != "posix":
        proc.kill()
        proc.wait()
        return
    _signal_group(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=KILL_GRACE)
    except subprocess.TimeoutExpired:
        pass
    _signal_group(proc.pid, signal.SIGKILL)
    proc.wait()


def _tail(fh: IO[bytes]) -> str:
    fh.seek(0, os.SEEK_END)
    size = fh.tell()
    fh.seek(max(0, size - CAPTURE_BYTES))
    text = fh.read().decode("utf-8", "replace")
    return ("…[earlier output truncated]\n" + text) if size > CAPTURE_BYTES else text


def _spawn(cmd: str | list[str], *, shell: bool, cwd: Path, env: dict[str, str], timeout: int,
           split_stderr: bool) -> tuple[int | None, str, str, bool]:
    """Run to completion or timeout. Output goes to temp files, not pipes, so a
    stray background process holding a pipe open can never hang the runner."""
    posix = os.name == "posix"
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(
            cmd, shell=shell, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=out, stderr=err if split_stderr else subprocess.STDOUT,
            start_new_session=posix,
        )
        try:
            code: int | None = proc.wait(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            code, timed_out = None, True
        if posix and not timed_out:
            _signal_group(proc.pid, signal.SIGTERM)   # reap leftovers the job forgot about
        return code, _tail(out), (_tail(err) if split_stderr else ""), timed_out


def _child_env(job: Job, ctx: Context, notes: list[str]) -> dict[str, str]:
    env = dict(os.environ if ctx.base_env is None else ctx.base_env)
    env.pop("OPS_SECRETS_JSON", None)
    env.update(job.env)
    for name in job.secrets:
        if ctx.settings.get(name):
            env[name] = ctx.settings[name]
        else:
            notes.append(f"secret {name} is not set")
    if job.type == "claude":
        auth = [n for n in CLAUDE_AUTH if ctx.settings.get(n)]
        env.update({n: ctx.settings[n] for n in auth})
        if not auth:
            notes.append("no Claude credentials: set CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`) or ANTHROPIC_API_KEY")
    env["OPS_JOB"] = job.name
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def claude_command(job: Job, settings: Mapping[str, str]) -> list[str]:
    binary = settings.get("OPS_CLAUDE_BIN") or shutil.which("claude")
    if not binary:
        raise RuntimeError("claude CLI not found: npm install -g @anthropic-ai/claude-code (or set OPS_CLAUDE_BIN)")
    prompt = job.prompt_text()
    if job.notify == "signal":
        prompt += f"\n\nIf there is nothing worth notifying me about, reply with exactly {QUIET_TOKEN} and nothing else."
    cmd = [binary]
    if settings.get("ANTHROPIC_API_KEY"):
        cmd.append("--bare")   # faster, reproducible start; bare mode authenticates only with an API key
    cmd += ["-p", prompt, "--output-format", "json", "--permission-mode", "dontAsk", "--no-session-persistence"]
    if job.model:
        cmd += ["--model", job.model]
    if job.max_turns:
        cmd += ["--max-turns", str(job.max_turns)]
    if job.max_budget_usd:
        cmd += ["--max-budget-usd", str(job.max_budget_usd)]
    if job.allowed_tools:
        cmd += ["--allowedTools", *job.allowed_tools]   # variadic: keep last
    return cmd


def _is_quiet(text: str) -> bool:
    lines = [line.strip().strip("`*_. ") for line in text.splitlines() if line.strip()]
    return bool(lines) and lines[-1] == QUIET_TOKEN


def _apply_claude_output(result: RunResult, stdout: str, stderr: str) -> None:
    data = None
    for candidate in (stdout.strip(), stdout.strip().splitlines()[-1] if stdout.strip() else ""):
        try:
            data = json.loads(candidate)
            break
        except ValueError:
            continue
    if not isinstance(data, dict):
        result.output = "\n".join(p for p in (stdout.strip(), stderr.strip()) if p)
        if result.ok:
            result.status = "failed"
            result.notes.append("claude did not return JSON")
        return
    text = str(data.get("result") or "").strip()
    result.output = text or stderr.strip()
    cost = data.get("total_cost_usd")
    result.cost_usd = cost if isinstance(cost, (int, float)) else None
    denials = data.get("permission_denials") or []
    if denials:   # usually means allowed_tools is too narrow for the prompt
        result.notes.append(f"Claude was denied {len(denials)} tool call(s); widen allowed_tools if the result is incomplete")
    if data.get("is_error") or data.get("subtype", "success") != "success":
        if result.ok:
            result.status = "failed"
    elif result.ok and text and not _is_quiet(text) and not result.signal:
        result.signal = text


def execute(job: Job, ctx: Context) -> RunResult:
    started = datetime.now(timezone.utc)
    t0 = time.monotonic()
    notes: list[str] = []
    env = _child_env(job, ctx, notes)
    with tempfile.TemporaryDirectory(prefix="ops-") as tmp:
        signal_file = Path(tmp) / "notify.txt"
        env["OPS_NOTIFY"] = str(signal_file)
        try:
            if job.type == "claude":
                cmd: str | list[str] = claude_command(job, ctx.settings)
            else:
                cmd = job.run or ""
            code, out, err, timed_out = _spawn(
                cmd, shell=job.type == "shell", cwd=job.cwd, env=env,
                timeout=job.timeout, split_stderr=job.type == "claude",
            )
        except (OSError, RuntimeError) as exc:
            return RunResult(job.name, "error", None, started, time.monotonic() - t0, str(exc), notes=notes)
        signal_text = signal_file.read_text(encoding="utf-8", errors="replace").strip() if signal_file.exists() else ""

    status = "timeout" if timed_out else ("success" if code == 0 else "failed")
    result = RunResult(job.name, status, code, started, time.monotonic() - t0, out, signal_text or None, notes=notes)
    if job.type == "claude" and not timed_out:
        _apply_claude_output(result, out, err)
    elif job.type == "claude":
        result.output = "\n".join(p for p in (out.strip(), err.strip()) if p)
    return result


# --------------------------------------------------------------------------- reporting

def should_notify(job: Job, result: RunResult) -> bool:
    if job.notify == "never":
        return False
    if not result.ok:
        return True
    if job.notify == "always":
        return True
    return job.notify == "signal" and bool(result.signal)


def fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def where_label() -> str:
    return "GitHub Actions" if os.environ.get("GITHUB_ACTIONS") == "true" else socket.gethostname()


def _tail_text(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def build_message(job: Job, result: RunResult, run_url: str | None = None) -> notify.Message:
    if result.status == "timeout":
        level, title = "error", f"{job.name} timed out after {fmt_duration(job.timeout)}"
    elif result.status == "error":
        level, title = "error", f"{job.name} could not run"
    elif result.status == "failed":
        exit_note = f" (exit {result.exit_code})" if result.exit_code not in (None, 0) else ""
        level, title = "error", f"{job.name} failed{exit_note}"
    elif result.signal:
        level, title = "info", job.name
    else:
        level, title = "success", f"{job.name} ok"

    body = result.signal if (result.ok and result.signal) else (_tail_text(result.output, BODY_CHARS) or "(no output)")
    extras = list(result.notes)
    if result.signal and not result.ok:
        extras.append(f"signal before failing: {result.signal}")
    if result.routine_url:
        extras.append(f"Claude routine started: {result.routine_url}")
    meta = f"{fmt_duration(result.duration)} on {where_label()}"
    if result.cost_usd:
        meta += f" · ${result.cost_usd:.2f}"
    return notify.Message(title=title, body="\n\n".join(p for p in (body, "\n".join(extras), meta) if p),
                          level=level, url=run_url)


def _escalate(job: Job, result: RunResult, ctx: Context) -> None:
    if not (job.routine and result.ok and result.signal):
        return
    try:
        url, token = routines.credentials(job.routine, ctx.settings)
        if ctx.dry_run:
            result.notes.append(f"(dry run) would start Claude routine {job.routine}")
            return
        response = routines.fire(url, token, f"[{job.name}] {result.signal}")
        result.routine_url = response.get("claude_code_session_url")
    except routines.RoutineError as exc:
        result.notes.append(f"Claude routine {job.routine} not started: {exc}")


def _log(job: Job, result: RunResult, ctx: Context) -> None:
    sent = ", ".join(f"{k}={v}" for k, v in result.notified.items()) or "none"
    exit_note = "" if result.exit_code is None else f" (exit {result.exit_code})"
    ctx.log(f"[ops] {job.name}: {result.status} in {fmt_duration(result.duration)}{exit_note}"
            f" · signal: {'yes' if result.signal else 'no'} · notified: {sent}")
    for note in result.notes:
        ctx.log(f"[ops] {job.name}: {note}")
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    if ctx.redact_logs:
        ctx.log(f"[ops] {job.name}: output hidden (OPS_REDACT_LOGS=1); it goes to notifications only")
    elif result.output.strip():
        ctx.log(f"::group::{job.name} output" if in_actions else f"----- {job.name} output -----")
        ctx.log(result.output.rstrip())
        ctx.log("::endgroup::" if in_actions else f"----- end {job.name} -----")
    if in_actions and not result.ok:
        ctx.log(f"::error title=ops job {result.status}::{job.name} {result.status}{exit_note}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            if fh.tell() == 0:
                fh.write("| job | status | duration | signal | notified |\n|---|---|---|---|---|\n")
            fh.write(f"| {job.name} | {result.status}{exit_note} | {fmt_duration(result.duration)} | "
                     f"{'yes' if result.signal else 'no'} | {sent} |\n")


def _record(job: Job, result: RunResult, ctx: Context) -> None:
    if ctx.state_dir is None:
        return
    try:
        (ctx.state_dir / "last").mkdir(parents=True, exist_ok=True)
        (ctx.state_dir / "last" / f"{job.name}.log").write_text(result.output, encoding="utf-8")
        entry = {
            "job": job.name, "status": result.status, "exit_code": result.exit_code,
            "started": result.started.isoformat(timespec="seconds"), "duration_s": round(result.duration, 2),
            "signal": bool(result.signal), "notified": result.notified, "cost_usd": result.cost_usd,
        }
        with open(ctx.state_dir / "history.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError as exc:
        ctx.log(f"[ops] warning: could not write run history: {exc}")


def report(job: Job, result: RunResult, ctx: Context) -> RunResult:
    _escalate(job, result, ctx)
    if should_notify(job, result):
        message = build_message(job, result, ctx.run_url)
        if ctx.dry_run:
            ctx.log(f"[ops] (dry run) notification [{message.level}] {message.title}\n{message.body}")
            result.notified = {"dry-run": "ok"}
        elif not ctx.channels:
            ctx.log("[ops] warning: no notification channel configured; see `python -m ops channels`")
            result.notified = {"none": "error: no notification channel configured"}
        else:
            result.notified = notify.dispatch(message, ctx.channels)
    _log(job, result, ctx)
    _record(job, result, ctx)
    return result


def run_and_report(job: Job, ctx: Context) -> RunResult:
    return report(job, execute(job, ctx), ctx)
