"""Load and validate jobs.yaml, the single source of truth for scheduled work."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from croniter import croniter

ENGINES = ("github", "server")
NOTIFY_POLICIES = ("always", "signal", "failure", "never")
JOB_TYPES = ("shell", "claude")

GITHUB_MAX_TIMEOUT = 6 * 3600   # GitHub-hosted runners stop a job after 6 h
GITHUB_MIN_INTERVAL = 5 * 60    # shortest schedule GitHub honours

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ROUTINE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_DURATION = re.compile(r"^(\d+)\s*([smh]?)$")
_CRON_NAMES = {
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
    "sun", "mon", "tue", "wed", "thu", "fri", "sat",
}

_TOP_KEYS = {"timezone", "defaults", "jobs"}
_DEFAULT_KEYS = {"cwd", "timeout", "notify", "engine"}
_JOB_KEYS = {
    "name", "description", "type", "run", "prompt", "prompt_file", "schedule",
    "timezone", "engine", "timeout", "notify", "enabled", "cwd", "env", "secrets",
    "routine", "model", "allowed_tools", "max_turns", "max_budget_usd",
}
_CLAUDE_ONLY = {"prompt", "prompt_file", "model", "allowed_tools", "max_turns", "max_budget_usd"}


class RegistryError(ValueError):
    """jobs.yaml is invalid; the message names the job and the problem."""


@dataclass
class Job:
    name: str
    type: str
    cwd: Path
    run: str | None = None
    prompt: str | None = None
    prompt_file: Path | None = None
    schedules: tuple[str, ...] = ()
    timezone: str = "UTC"
    engine: str = "github"
    timeout: int = 900
    notify: str = "signal"
    enabled: bool = True
    env: dict[str, str] = field(default_factory=dict)
    secrets: tuple[str, ...] = ()
    routine: str | None = None
    description: str = ""
    model: str | None = None
    allowed_tools: tuple[str, ...] = ()
    max_turns: int | None = None
    max_budget_usd: float | None = None

    @property
    def scheduled(self) -> bool:
        return self.enabled and bool(self.schedules)

    def prompt_text(self) -> str:
        if self.prompt is not None:
            return self.prompt
        assert self.prompt_file is not None
        return self.prompt_file.read_text(encoding="utf-8")


@dataclass
class Registry:
    path: Path
    timezone: str
    jobs: list[Job]
    warnings: list[str] = field(default_factory=list)

    def get(self, name: str) -> Job:
        for job in self.jobs:
            if job.name == name:
                return job
        known = ", ".join(j.name for j in self.jobs) or "none"
        raise RegistryError(f"no job named {name!r} (known: {known})")

    def scheduled(self, engine: str) -> list[Job]:
        return [j for j in self.jobs if j.scheduled and j.engine == engine]


def parse_duration(value: Any, where: str) -> int:
    """90 / '90s' / '15m' / '2h' -> seconds."""
    if isinstance(value, bool) or value is None:
        raise RegistryError(f"{where}: timeout must look like 90s, 15m or 2h")
    if isinstance(value, int):
        seconds = value
    else:
        match = _DURATION.match(str(value).strip().lower())
        if not match:
            raise RegistryError(f"{where}: timeout {value!r} must look like 90s, 15m or 2h")
        seconds = int(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[match.group(2)]
    if seconds <= 0:
        raise RegistryError(f"{where}: timeout must be positive")
    return seconds


def normalize_cron(expr: str) -> str:
    return " ".join(str(expr).split())


def min_gap_seconds(expr: str) -> float:
    """Smallest gap between consecutive fire times (sampled)."""
    it = croniter(expr, datetime(2026, 1, 1, tzinfo=timezone.utc))
    times = [it.get_next(float) for _ in range(30)]
    return min(b - a for a, b in zip(times, times[1:]))


def _check_cron(expr: Any, where: str) -> str:
    expr = normalize_cron(expr)
    fields = expr.split(" ")
    # POSIX cron only: GitHub rejects croniter extensions such as L, W, #, ? and H.
    posix = all(
        tok == "*" or tok.isdigit() or tok.lower() in _CRON_NAMES
        for f in fields
        for tok in re.split(r"[,/-]", f)
    )
    if len(fields) != 5 or not posix or not croniter.is_valid(expr):
        raise RegistryError(
            f"{where}: {expr!r} is not a 5-field POSIX cron expression "
            "(minute hour day-of-month month day-of-week)"
        )
    # GitHub documents day-of-week as 0-6; croniter also accepts 7 for Sunday.
    if any(tok.isdigit() and int(tok) > 6 for tok in re.split(r"[,-]", fields[4].split("/")[0])):
        raise RegistryError(f"{where}: {expr!r}: day-of-week must be 0-6 (0 = Sunday) or SUN-SAT")
    return expr


def _check_tz(value: Any, where: str) -> str:
    try:
        ZoneInfo(str(value))
    except (ZoneInfoNotFoundError, ValueError):
        raise RegistryError(f"{where}: unknown time zone {value!r} (use an IANA name like America/New_York)") from None
    return str(value)


def _choice(value: Any, allowed: tuple[str, ...], where: str) -> str:
    if value not in allowed:
        raise RegistryError(f"{where}: {value!r} must be one of {', '.join(allowed)}")
    return value


def _unknown_keys(mapping: dict, allowed: set[str], where: str) -> None:
    extra = sorted(set(mapping) - allowed)
    if extra:
        raise RegistryError(f"{where}: unknown key(s) {', '.join(extra)}; allowed: {', '.join(sorted(allowed))}")


def _str_list(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise RegistryError(f"{where}: must be a string or a list of strings")
    return tuple(value)


def _positive(value: Any, kind: type, where: str):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise RegistryError(f"{where}: must be a positive number")
    if kind is int and not isinstance(value, int):
        raise RegistryError(f"{where}: must be a whole number")
    return kind(value)


def _parse_job(item: Any, index: int, base: Path, tz: str, defaults: dict) -> Job:
    if not isinstance(item, dict):
        raise RegistryError(f"jobs[{index}]: each job must be a mapping")
    name = item.get("name")
    where = f"job {name!r}" if isinstance(name, str) else f"jobs[{index}]"
    if isinstance(name, bool):   # YAML 1.1 reads bare on/off/yes/no as booleans
        raise RegistryError(f"{where}: name was read as {str(name).lower()}; quote it, e.g. name: \"off\"")
    if not isinstance(name, str) or not _NAME.match(name):
        raise RegistryError(f"{where}: name must be lowercase letters, digits and dashes (max 63 chars)")
    _unknown_keys(item, _JOB_KEYS, where)

    jtype = _choice(item.get("type", "shell"), JOB_TYPES, f"{where}.type")
    cwd = (base / str(item.get("cwd", defaults["cwd"]))).resolve()
    if not cwd.is_dir():
        raise RegistryError(f"{where}: cwd {cwd} does not exist")

    run, prompt, prompt_file = item.get("run"), item.get("prompt"), item.get("prompt_file")
    if jtype == "shell":
        if not isinstance(run, str) or not run.strip():
            raise RegistryError(f"{where}: shell jobs need a 'run' command")
        misplaced = sorted(k for k in _CLAUDE_ONLY if k in item)
        if misplaced:
            raise RegistryError(f"{where}: {', '.join(misplaced)} only apply to type: claude")
    else:
        if run is not None:
            raise RegistryError(f"{where}: claude jobs take 'prompt' or 'prompt_file', not 'run'")
        if (prompt is None) == (prompt_file is None):
            raise RegistryError(f"{where}: set exactly one of 'prompt' or 'prompt_file'")
        if prompt_file is not None:
            prompt_file = (cwd / str(prompt_file)).resolve()
            if not prompt_file.is_file():
                raise RegistryError(f"{where}: prompt_file {prompt_file} not found")
        elif not isinstance(prompt, str) or not prompt.strip():
            raise RegistryError(f"{where}: prompt must be non-empty text")

    schedule = item.get("schedule", [])
    if isinstance(schedule, str):
        schedule = [schedule]
    if not isinstance(schedule, list):
        raise RegistryError(f"{where}: schedule must be a cron string or a list of them")
    schedules = tuple(dict.fromkeys(_check_cron(s, where) for s in schedule))

    env = item.get("env") or {}
    if not isinstance(env, dict):
        raise RegistryError(f"{where}: env must be a mapping")
    for key, value in env.items():
        if not isinstance(key, str) or not _ENV_NAME.match(key) or value is None or isinstance(value, (dict, list)):
            raise RegistryError(f"{where}: env entry {key!r} must be NAME: scalar value")

    secrets = _str_list(item.get("secrets"), f"{where}.secrets")
    bad = [s for s in secrets if not _ENV_NAME.match(s)]
    if bad:
        raise RegistryError(f"{where}: invalid secret name(s) {', '.join(bad)}")

    routine = item.get("routine")
    if routine is not None and (not isinstance(routine, str) or not _ROUTINE.match(routine)):
        raise RegistryError(f"{where}: routine must be letters, digits or underscores (it maps to ROUTINE_<NAME>_URL/_TOKEN)")

    enabled = item.get("enabled", True)
    if not isinstance(enabled, bool):
        raise RegistryError(f"{where}: enabled must be true or false")

    model = item.get("model")
    if model is not None and not isinstance(model, str):
        raise RegistryError(f"{where}: model must be a string")

    return Job(
        name=name,
        type=jtype,
        cwd=cwd,
        run=run,
        prompt=prompt,
        prompt_file=prompt_file,
        schedules=schedules,
        timezone=_check_tz(item.get("timezone", tz), f"{where}.timezone"),
        engine=_choice(item.get("engine", defaults["engine"]), ENGINES, f"{where}.engine"),
        timeout=parse_duration(item.get("timeout", defaults["timeout"]), where),
        notify=_choice(item.get("notify", defaults["notify"]), NOTIFY_POLICIES, f"{where}.notify"),
        enabled=enabled,
        env={k: str(v) for k, v in env.items()},
        secrets=secrets,
        routine=routine,
        description=str(item.get("description", "")),
        model=model,
        allowed_tools=_str_list(item.get("allowed_tools"), f"{where}.allowed_tools"),
        max_turns=_positive(item.get("max_turns"), int, f"{where}.max_turns"),
        max_budget_usd=_positive(item.get("max_budget_usd"), float, f"{where}.max_budget_usd"),
    )


def _github_checks(reg: Registry) -> list[str]:
    """Hard errors GitHub would hit at runtime, plus soft warnings."""
    warnings: list[str] = []
    tz_by_cron: dict[str, str] = {}
    for job in reg.scheduled("github"):
        if job.timeout > GITHUB_MAX_TIMEOUT:
            raise RegistryError(f"job {job.name!r}: GitHub-hosted runs stop at 6h; use engine: server")
        for expr in job.schedules:
            gap = min_gap_seconds(expr)
            if gap < GITHUB_MIN_INTERVAL:
                raise RegistryError(
                    f"job {job.name!r}: {expr!r} fires every {gap:.0f}s; GitHub's minimum is 5 minutes. "
                    "Use engine: server for tighter schedules."
                )
            first = tz_by_cron.setdefault(expr, job.timezone)
            if first != job.timezone:
                raise RegistryError(
                    f"cron {expr!r} is used with time zones {first} and {job.timezone}; GitHub only reports "
                    "the cron string that fired, so the jobs can't be told apart. Shift one by a minute."
                )
            if expr.split(" ")[0] == "0":
                warnings.append(
                    f"job {job.name!r}: {expr!r} runs at minute 0, when GitHub's queue is longest "
                    "(late starts, occasional drops). Consider a minute like 7."
                )
    return warnings


def load(path: str | Path) -> Registry:
    path = Path(path).resolve()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise RegistryError(f"{path} not found") from None
    except yaml.YAMLError as exc:
        raise RegistryError(f"{path.name}: invalid YAML: {exc}") from None
    if not isinstance(raw, dict):
        raise RegistryError(f"{path.name}: the top level must be a mapping")
    _unknown_keys(raw, _TOP_KEYS, path.name)

    tz = _check_tz(raw.get("timezone", "UTC"), "timezone")
    d = raw.get("defaults") or {}
    if not isinstance(d, dict):
        raise RegistryError("defaults must be a mapping")
    _unknown_keys(d, _DEFAULT_KEYS, "defaults")
    defaults = {
        "cwd": d.get("cwd", "."),
        "timeout": parse_duration(d.get("timeout", "15m"), "defaults"),
        "notify": _choice(d.get("notify", "signal"), NOTIFY_POLICIES, "defaults.notify"),
        "engine": _choice(d.get("engine", "github"), ENGINES, "defaults.engine"),
    }

    items = raw.get("jobs") or []
    if not isinstance(items, list):
        raise RegistryError("jobs must be a list")
    jobs: list[Job] = []
    for index, item in enumerate(items):
        job = _parse_job(item, index, path.parent, tz, defaults)
        if any(j.name == job.name for j in jobs):
            raise RegistryError(f"duplicate job name {job.name!r}")
        jobs.append(job)

    reg = Registry(path=path, timezone=tz, jobs=jobs)
    reg.warnings.extend(_github_checks(reg))
    return reg
