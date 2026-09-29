"""Server engine: a minute-tick scheduler for a VPS or any always-on machine.

Each minute it evaluates every enabled `engine: server` job's cron in the job's
own time zone, starts due jobs on a thread pool, never overlaps a job with
itself, hot-reloads jobs.yaml when it changes, and optionally pings a
dead-man's switch (OPS_HEARTBEAT_URL) so you hear about it if this box dies.
"""

from __future__ import annotations

import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from croniter import croniter

from . import notify, runner
from .registry import Job, Registry, RegistryError, load

MINUTE = timedelta(minutes=1)
MAX_CATCH_UP = timedelta(minutes=5)     # after a suspend/clock jump, don't replay more than this
HEARTBEAT_EVERY = timedelta(minutes=5)


def floor_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def due_local_minute(job: Job, minute_utc: datetime) -> datetime | None:
    """The job's naive local wall-clock minute if it fires at `minute_utc`, else None."""
    local = minute_utc.astimezone(ZoneInfo(job.timezone))
    if any(croniter.match(expr, local) for expr in job.schedules):
        return local.replace(tzinfo=None)
    return None


class Server:
    def __init__(self, registry_path: Path, make_context: Callable[[Registry], runner.Context], *,
                 max_workers: int = 4, clock: Callable[[], datetime] | None = None,
                 log: Callable[[str], None] = print,
                 run_job: Callable[[Job, runner.Context], object] = runner.run_and_report):
        self.registry_path = Path(registry_path)
        self.make_context = make_context
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.log = log
        self.run_job = run_job
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ops-job")
        self.running: set[str] = set()
        self.lock = threading.Lock()
        self.last_fired: dict[str, datetime] = {}
        self.registry = load(self.registry_path)
        self.mtime = self.registry_path.stat().st_mtime
        self.ctx = make_context(self.registry)

    # -- registry --------------------------------------------------------------------

    def reload_if_changed(self) -> None:
        try:
            mtime = self.registry_path.stat().st_mtime
        except FileNotFoundError:
            return
        if mtime == self.mtime:
            return
        self.mtime = mtime
        try:
            registry = load(self.registry_path)
        except RegistryError as exc:
            self.log(f"[ops] jobs.yaml is invalid, keeping the previous version: {exc}")
            self._alert("jobs.yaml rejected on server", str(exc))
            return
        self.registry, self.ctx = registry, self.make_context(registry)
        self.log(f"[ops] reloaded jobs.yaml: {len(registry.scheduled('server'))} server job(s) scheduled")

    def _alert(self, title: str, body: str) -> None:
        if self.ctx.channels:
            notify.dispatch(notify.Message(title=title, body=body, level="error"), self.ctx.channels)

    # -- scheduling ------------------------------------------------------------------

    def tick(self, minute_utc: datetime) -> list[str]:
        """Start every job due at this minute; return the names started."""
        started = []
        for job in self.registry.scheduled("server"):
            local = due_local_minute(job, minute_utc)
            if local is None or self.last_fired.get(job.name) == local:
                continue   # the second check skips the repeated hour when DST ends
            self.last_fired[job.name] = local
            with self.lock:
                if job.name in self.running:
                    self.log(f"[ops] {job.name}: previous run still going; skipping {local:%Y-%m-%d %H:%M}")
                    continue
                self.running.add(job.name)
            self.pool.submit(self._run, job, self.ctx)
            started.append(job.name)
        return started

    def _run(self, job: Job, ctx: runner.Context) -> None:
        try:
            self.run_job(job, ctx)
        except Exception as exc:  # noqa: BLE001 - one job's crash must not stop the scheduler
            self.log(f"[ops] {job.name}: runner crashed: {exc!r}")
        finally:
            with self.lock:
                self.running.discard(job.name)

    def _heartbeat(self) -> None:
        url = self.ctx.settings.get("OPS_HEARTBEAT_URL")
        if not url:
            return

        def ping() -> None:
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "ops-server/1"})
                urllib.request.urlopen(request, timeout=10).close()
            except Exception as exc:  # noqa: BLE001
                self.log(f"[ops] heartbeat ping failed: {type(exc).__name__}")

        threading.Thread(target=ping, daemon=True).start()

    def run_forever(self, stop: threading.Event, wait: Callable[[float], object] | None = None) -> None:
        wait = wait or stop.wait
        last = floor_minute(self.clock())
        next_beat = self.clock()
        self.log(f"[ops] server engine started: {len(self.registry.scheduled('server'))} server job(s) scheduled")
        while not stop.is_set():
            now = self.clock()
            if now >= next_beat:
                self._heartbeat()
                next_beat = now + HEARTBEAT_EVERY
            remaining = (last + MINUTE - now).total_seconds()
            if remaining > 0:
                wait(remaining)
                continue
            current = floor_minute(now)
            first = last + MINUTE
            if current - first > MAX_CATCH_UP:
                self.log(f"[ops] clock jumped {current - first}; skipping missed minutes")
                first = current
            self.reload_if_changed()
            minute = first
            while minute <= current:
                self.tick(minute)
                minute += MINUTE
            last = current
        self.log("[ops] stopping: waiting for running jobs to finish")
        self.pool.shutdown(wait=True, cancel_futures=True)
