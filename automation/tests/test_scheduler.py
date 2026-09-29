import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

from ops import notify, scheduler
from ops.registry import load
from ops.runner import Context
from ops.scheduler import Server, due_local_minute

UTC = timezone.utc


class InlineExecutor:
    """Runs submitted work immediately, making scheduler tests deterministic."""

    def submit(self, fn, *args):
        fn(*args)

    def shutdown(self, wait=True, cancel_futures=False):
        pass


class Capture(notify.Channel):
    name = "capture"

    def __init__(self):
        self.messages = []

    def send(self, msg):
        self.messages.append(msg)


@pytest.fixture
def server_factory(tmp_path):
    def build(jobs_yaml: str, run_job=None, channels=None, settings=None, clock=None, inline=True):
        path = tmp_path / "jobs.yaml"
        path.write_text(jobs_yaml, encoding="utf-8")
        ran, lines = [], []
        server = Server(
            path,
            lambda reg: Context(settings=settings or {}, channels=channels or [], log=lines.append),
            run_job=run_job or (lambda job, ctx: ran.append(job.name)),
            clock=clock, log=lines.append,
        )
        if inline:
            server.pool = InlineExecutor()
        return server, ran, lines
    return build


def test_due_local_minute_uses_the_jobs_time_zone(tmp_path):
    (tmp_path / "jobs.yaml").write_text(
        "timezone: America/New_York\njobs:\n  - {name: a, run: x, engine: server, schedule: '37 9 * * 1-5'}\n")
    job = load(tmp_path / "jobs.yaml").jobs[0]
    assert due_local_minute(job, datetime(2026, 9, 29, 13, 37, tzinfo=UTC)) == datetime(2026, 9, 29, 9, 37)
    assert due_local_minute(job, datetime(2026, 9, 29, 13, 38, tzinfo=UTC)) is None
    assert due_local_minute(job, datetime(2026, 10, 3, 13, 37, tzinfo=UTC)) is None      # Saturday


def test_only_server_jobs_run_and_dst_repeat_is_skipped(server_factory):
    server, ran, _ = server_factory(
        "timezone: America/New_York\njobs:\n"
        "  - {name: nightly, run: x, engine: server, schedule: '30 1 * * *'}\n"
        "  - {name: on-github, run: x, schedule: '30 1 * * *'}\n")
    # 2026-11-01: US clocks fall back, so 01:30 local happens at 05:30 UTC (EDT) and 06:30 UTC (EST).
    assert server.tick(datetime(2026, 11, 1, 5, 30, tzinfo=UTC)) == ["nightly"]
    assert server.tick(datetime(2026, 11, 1, 6, 30, tzinfo=UTC)) == []
    assert server.tick(datetime(2026, 11, 2, 6, 30, tzinfo=UTC)) == ["nightly"]
    assert ran == ["nightly", "nightly"]


def test_a_job_never_overlaps_itself(server_factory):
    release, started = threading.Event(), threading.Event()

    def slow(job, ctx):
        started.set()
        release.wait(5)

    server, _, lines = server_factory("jobs:\n  - {name: slow, run: x, engine: server, schedule: '* * * * *'}\n",
                                      run_job=slow, inline=False)
    t0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    assert server.tick(t0) == ["slow"]
    assert started.wait(5)
    assert server.tick(t0 + timedelta(minutes=1)) == []
    assert any("previous run still going" in line for line in lines)
    release.set()
    server.pool.shutdown(wait=True)
    assert server.running == set()


def test_runner_crash_is_contained(server_factory):
    def boom(job, ctx):
        raise RuntimeError("bug")

    server, _, lines = server_factory("jobs:\n  - {name: a, run: x, engine: server, schedule: '* * * * *'}\n",
                                      run_job=boom)
    server.tick(datetime(2026, 9, 29, 12, 0, tzinfo=UTC))
    assert any("runner crashed" in line for line in lines) and server.running == set()


def test_reload_on_change_and_reject_invalid(server_factory, tmp_path):
    channel = Capture()
    server, ran, lines = server_factory("jobs:\n  - {name: a, run: x, engine: server, schedule: '* * * * *'}\n",
                                        channels=[channel])
    path = tmp_path / "jobs.yaml"

    path.write_text("jobs:\n  - {name: b, run: x, engine: server, schedule: '* * * * *'}\n")
    os.utime(path, (server.mtime + 10, server.mtime + 10))
    server.reload_if_changed()
    assert [j.name for j in server.registry.jobs] == ["b"]

    path.write_text("jobs:\n  - {name: BAD, run: x}\n")
    os.utime(path, (server.mtime + 10, server.mtime + 10))
    server.reload_if_changed()
    assert [j.name for j in server.registry.jobs] == ["b"]            # previous version kept
    assert channel.messages and channel.messages[0].title == "jobs.yaml rejected on server"


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def test_run_forever_ticks_each_minute_and_caps_catch_up(server_factory):
    clock = FakeClock(datetime(2026, 9, 29, 12, 0, 30, tzinfo=UTC))
    server, ran, lines = server_factory("jobs:\n  - {name: every, run: x, engine: server, schedule: '* * * * *'}\n",
                                        clock=clock)
    stop = threading.Event()
    waits = []

    def fake_wait(seconds):
        waits.append(seconds)
        clock.now += timedelta(seconds=seconds)
        if len(waits) == 3:                       # simulate a suspended machine: jump an hour
            clock.now += timedelta(hours=1)
        if clock.now >= datetime(2026, 9, 29, 13, 6, tzinfo=UTC):
            stop.set()

    server.run_forever(stop, wait=fake_wait)
    # 12:01, 12:02, then the jump to 13:03 runs once (not 60 catch-up runs), then 13:04, 13:05
    assert ran == ["every"] * 5
    assert any("clock jumped" in line for line in lines)


def test_heartbeat_pings(server_factory, monkeypatch):
    pinged = threading.Event()

    class Response:
        def close(self):
            pinged.set()

    monkeypatch.setattr(scheduler.urllib.request, "urlopen", lambda request, timeout: Response())
    server, _, _ = server_factory("jobs: []\n", settings={"OPS_HEARTBEAT_URL": "https://hc-ping.com/abc"})
    server._heartbeat()
    assert pinged.wait(5)
