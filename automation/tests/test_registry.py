from pathlib import Path

import pytest

from ops.registry import RegistryError, load, parse_duration

REPO_REGISTRY = Path(__file__).resolve().parents[1] / "jobs.yaml"


def test_repository_jobs_yaml_is_valid():
    reg = load(REPO_REGISTRY)
    names = {j.name for j in reg.jobs}
    assert {"heartbeat", "wheel-screener", "market-brief", "self-update"} <= names
    assert reg.get("heartbeat").scheduled
    assert not reg.get("wheel-screener").scheduled      # disabled until it has live data
    assert (REPO_REGISTRY.parent.parent / "wheel_screener.py").is_file()
    assert reg.get("heartbeat").cwd == REPO_REGISTRY.parent.parent


def test_defaults_and_overrides(write_registry):
    reg = write_registry("""
timezone: Europe/London
defaults: {timeout: 2m, notify: always, engine: server}
jobs:
  - name: a
    run: echo a
    schedule: "7 * * * *"
  - name: b
    run: echo b
    timeout: 30s
    notify: never
    engine: github
    timezone: Asia/Kolkata
    schedule: ["7  9 * * MON-FRI", "7 9 * * mon-fri", "7 9 * * MON-FRI"]
    env: {MODE: test, RETRIES: 3}
    secrets: [API_KEY]
""")
    a, b = reg.jobs
    assert (a.timeout, a.notify, a.engine, a.timezone) == (120, "always", "server", "Europe/London")
    assert (b.timeout, b.notify, b.engine, b.timezone) == (30, "never", "github", "Asia/Kolkata")
    assert b.schedules == ("7 9 * * MON-FRI", "7 9 * * mon-fri")     # whitespace normalized, duplicates dropped
    assert b.env == {"MODE": "test", "RETRIES": "3"}
    assert b.secrets == ("API_KEY",)
    assert reg.scheduled("server") == [a] and reg.scheduled("github") == [b]


@pytest.mark.parametrize("value, seconds", [(45, 45), ("45", 45), ("90s", 90), ("15m", 900), ("2h", 7200), (" 3 m ", 180)])
def test_parse_duration(value, seconds):
    assert parse_duration(value, "x") == seconds


@pytest.mark.parametrize("value", ["1d", "-5", "abc", True, None, 0, "0m"])
def test_parse_duration_rejects(value):
    with pytest.raises(RegistryError):
        parse_duration(value, "x")


@pytest.mark.parametrize("body, message", [
    ("jobz: []", "unknown key(s) jobz"),
    ("jobs:\n  - {name: a, run: x, schedul: '7 * * * *'}", "unknown key(s) schedul"),
    ("jobs:\n  - {name: Bad_Name, run: x}", "name must be lowercase"),
    ("jobs:\n  - {name: off, run: x}", "name was read as false; quote it"),
    ("jobs:\n  - {name: a, run: x}\n  - {name: a, run: y}", "duplicate job name"),
    ("jobs:\n  - {name: a}", "need a 'run' command"),
    ("jobs:\n  - {name: a, type: claude, run: x}", "not 'run'"),
    ("jobs:\n  - {name: a, type: claude, prompt: hi, prompt_file: p.md}", "exactly one of"),
    ("jobs:\n  - {name: a, run: x, model: opus}", "only apply to type: claude"),
    ("jobs:\n  - {name: a, type: claude, prompt_file: missing.md}", "not found"),
    ("jobs:\n  - {name: a, run: x, schedule: '0 */5 * * * *'}", "5-field POSIX cron"),
    ("jobs:\n  - {name: a, run: x, schedule: '0 9 L * *'}", "5-field POSIX cron"),
    ("jobs:\n  - {name: a, run: x, schedule: 'H * * * *'}", "5-field POSIX cron"),
    ("jobs:\n  - {name: a, run: x, schedule: '0 9 * * 7'}", "day-of-week must be 0-6"),
    ("jobs:\n  - {name: a, run: x, schedule: '61 * * * *'}", "5-field POSIX cron"),
    ("timezone: Mars/Olympus\njobs: []", "unknown time zone"),
    ("jobs:\n  - {name: a, run: x, engine: lambda}", "must be one of github, server"),
    ("jobs:\n  - {name: a, run: x, notify: sometimes}", "must be one of always"),
    ("jobs:\n  - {name: a, run: x, cwd: nowhere}", "does not exist"),
    ("jobs:\n  - {name: a, run: x, enabled: 'yes please'}", "enabled must be true or false"),
    ("jobs:\n  - {name: a, run: x, secrets: ['bad-name']}", "invalid secret name"),
    ("jobs:\n  - {name: a, run: x, routine: 'deep dive'}", "routine must be"),
    ("jobs:\n  - {name: a, run: x, env: {K: [1]}}", "must be NAME: scalar"),
    ("jobs:\n  - {name: a, type: claude, prompt: hi, max_turns: 2.5}", "whole number"),
    ("jobs:\n  - {name: a, run: x, schedule: '*/2 * * * *'}", "GitHub's minimum is 5 minutes"),
    ("jobs:\n  - {name: a, run: x, schedule: '7 * * * *', timeout: 7h}", "stop at 6h"),
    ("jobs:\n  - {name: a, run: x, schedule: '7 * * * *'}\n"
     "  - {name: b, run: y, schedule: '7 * * * *', timezone: Asia/Tokyo}", "can't be told apart"),
    ("jobs: {a: 1}", "jobs must be a list"),
    ("[1, 2]", "top level must be a mapping"),
    ("jobs: [", "invalid YAML"),
])
def test_invalid_registries(write_registry, body, message):
    with pytest.raises(RegistryError, match=None) as info:
        write_registry(body)
    assert message in str(info.value)


def test_server_engine_allows_minute_schedules_and_long_timeouts(write_registry):
    reg = write_registry("jobs:\n  - {name: a, run: x, engine: server, schedule: '* * * * *', timeout: 12h}")
    assert reg.jobs[0].schedules == ("* * * * *",)


def test_disabled_github_jobs_skip_github_checks(write_registry):
    reg = write_registry("jobs:\n  - {name: a, run: x, schedule: '*/1 * * * *', enabled: false}")
    assert reg.scheduled("github") == []


def test_top_of_hour_warning(write_registry):
    reg = write_registry("jobs:\n  - {name: a, run: x, schedule: '0 9 * * *'}")
    assert any("minute 0" in w for w in reg.warnings)


def test_claude_job_with_prompt_file(write_registry, tmp_path):
    (tmp_path / "p.md").write_text("Say hi", encoding="utf-8")
    reg = write_registry(
        "jobs:\n  - {name: a, type: claude, prompt_file: p.md, allowed_tools: WebSearch, max_turns: 3, max_budget_usd: 1}"
    )
    job = reg.jobs[0]
    assert job.prompt_text() == "Say hi"
    assert job.allowed_tools == ("WebSearch",)
    assert (job.max_turns, job.max_budget_usd) == (3, 1.0)


def test_get_unknown_job_lists_known(write_registry):
    reg = write_registry("jobs:\n  - {name: a, run: x}")
    with pytest.raises(RegistryError, match="known: a"):
        reg.get("b")
