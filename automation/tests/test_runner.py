import json
import os
import sys
import time
from pathlib import Path

import pytest

from ops import notify, routines, runner
from ops.registry import Job
from ops.runner import Context, RunResult


def make_job(tmp_path, **overrides) -> Job:
    fields = dict(name="t", type="shell", cwd=tmp_path, run="true", timeout=10, notify="signal")
    fields.update(overrides)
    return Job(**fields)


def make_ctx(**overrides) -> Context:
    fields = dict(settings={}, channels=[], log=lambda line: None)
    fields.update(overrides)
    return Context(**fields)


def process_gone(pid: int) -> bool:
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        return stat.read_text().split(")")[-1].split()[0] == "Z"   # zombie = dead, awaiting reaping
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


# --------------------------------------------------------------------------- execute

def test_success_captures_output(tmp_path):
    result = runner.execute(make_job(tmp_path, run="echo hello"), make_ctx())
    assert (result.status, result.exit_code, result.output) == ("success", 0, "hello\n")
    assert result.signal is None


def test_failure_merges_stderr(tmp_path):
    result = runner.execute(make_job(tmp_path, run="echo boom >&2; exit 3"), make_ctx())
    assert (result.status, result.exit_code) == ("failed", 3)
    assert "boom" in result.output


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_timeout_kills_the_whole_process_group(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "KILL_GRACE", 1)
    stubborn = (
        f"{sys.executable} -c \"import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"open('pid', 'w').write(str(os.getpid())); time.sleep(60)\" & sleep 60"
    )
    started = time.monotonic()
    result = runner.execute(make_job(tmp_path, run=stubborn, timeout=1), make_ctx())
    elapsed = time.monotonic() - started
    assert result.status == "timeout" and result.exit_code is None
    assert elapsed < 8, "runner must not wait for a process that ignores SIGTERM"
    pid = int((tmp_path / "pid").read_text())
    for _ in range(50):
        if process_gone(pid):
            break
        time.sleep(0.1)
    assert process_gone(pid), "grandchild ignoring SIGTERM must be SIGKILLed"


def test_signal_file(tmp_path):
    result = runner.execute(make_job(tmp_path, run='echo "IV spike on AMD" > "$OPS_NOTIFY"'), make_ctx())
    assert result.ok and result.signal == "IV spike on AMD"


def test_child_environment_is_least_privilege(tmp_path):
    job = make_job(tmp_path, run="env > env.txt", env={"MODE": "test"}, secrets=("API_KEY", "MISSING"))
    ctx = make_ctx(
        settings={"API_KEY": "s3cret", "OTHER_SECRET": "x"},
        base_env={"PATH": os.environ["PATH"], "KEEP": "1", "OPS_SECRETS_JSON": '{"OTHER_SECRET": "x"}'},
    )
    result = runner.execute(job, ctx)
    env = dict(line.split("=", 1) for line in (tmp_path / "env.txt").read_text().splitlines() if "=" in line)
    assert env["API_KEY"] == "s3cret" and env["MODE"] == "test" and env["KEEP"] == "1" and env["OPS_JOB"] == "t"
    assert "OTHER_SECRET" not in env and "OPS_SECRETS_JSON" not in env
    assert "secret MISSING is not set" in result.notes


def test_start_error(tmp_path):
    result = runner.execute(make_job(tmp_path / "missing-dir", run="true"), make_ctx())
    assert result.status == "error" and result.exit_code is None


# --------------------------------------------------------------------------- claude jobs

FAKE_CLAUDE = """#!{python}
import json, os, sys
with open(os.environ["FAKE_ARGV"], "w") as fh:
    json.dump({{"argv": sys.argv[1:], "oauth": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")}}, fh)
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "garbage":
    print("not json")
    sys.exit(0)
text = {{"ok": "AMD IV rank 97%", "quiet": "Checked all tickers.\\n`NOTHING_TO_REPORT`", "error": "boom"}}[mode]
print(json.dumps({{"type": "result", "subtype": "success", "is_error": mode == "error",
                  "result": text, "total_cost_usd": 0.0123,
                  "permission_denials": [{{"tool_name": "Bash"}}] if mode == "quiet" else []}}))
"""


@pytest.fixture
def fake_claude(tmp_path):
    path = tmp_path / "claude"
    path.write_text(FAKE_CLAUDE.format(python=sys.executable), encoding="utf-8")
    path.chmod(0o755)
    return path


def claude_job(tmp_path, mode, **overrides):
    fields = dict(type="claude", run=None, prompt="Check IV", allowed_tools=("WebSearch", "Bash(git log *)"),
                  max_turns=5, model="sonnet", env={"FAKE_MODE": mode, "FAKE_ARGV": str(tmp_path / "argv.json")})
    fields.update(overrides)
    return make_job(tmp_path, **fields)


def test_claude_job_signals_result(tmp_path, fake_claude):
    ctx = make_ctx(settings={"OPS_CLAUDE_BIN": str(fake_claude), "CLAUDE_CODE_OAUTH_TOKEN": "oat"})
    result = runner.execute(claude_job(tmp_path, "ok"), ctx)
    assert result.ok and result.signal == "AMD IV rank 97%" and result.cost_usd == pytest.approx(0.0123)
    seen = json.loads((tmp_path / "argv.json").read_text())
    argv = seen["argv"]
    assert seen["oauth"] == "oat"
    assert "--bare" not in argv                                   # bare mode cannot use the OAuth token
    assert argv[argv.index("-p") + 1].endswith("reply with exactly NOTHING_TO_REPORT and nothing else.")
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--max-turns") + 1] == "5"
    assert argv[-3:] == ["--allowedTools", "WebSearch", "Bash(git log *)"]


def test_claude_quiet_reply_is_not_a_signal(tmp_path, fake_claude):
    ctx = make_ctx(settings={"OPS_CLAUDE_BIN": str(fake_claude), "CLAUDE_CODE_OAUTH_TOKEN": "oat"})
    result = runner.execute(claude_job(tmp_path, "quiet"), ctx)
    assert result.ok and result.signal is None
    assert any("denied 1 tool call(s)" in n for n in result.notes)


def test_claude_error_and_garbage(tmp_path, fake_claude):
    ctx = make_ctx(settings={"OPS_CLAUDE_BIN": str(fake_claude), "CLAUDE_CODE_OAUTH_TOKEN": "oat"})
    assert runner.execute(claude_job(tmp_path, "error"), ctx).status == "failed"
    garbage = runner.execute(claude_job(tmp_path, "garbage"), ctx)
    assert garbage.status == "failed" and "claude did not return JSON" in garbage.notes


def test_claude_api_key_uses_bare_mode_and_always_policy_skips_quiet_hint(tmp_path, fake_claude):
    ctx = make_ctx(settings={"OPS_CLAUDE_BIN": str(fake_claude), "ANTHROPIC_API_KEY": "sk"})
    runner.execute(claude_job(tmp_path, "ok", notify="always"), ctx)
    argv = json.loads((tmp_path / "argv.json").read_text())["argv"]
    assert argv[0] == "--bare"
    assert "NOTHING_TO_REPORT" not in argv[argv.index("-p") + 1]


def test_claude_missing_cli_and_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    result = runner.execute(claude_job(tmp_path, "ok"), make_ctx())
    assert result.status == "error" and "claude CLI not found" in result.output
    assert any("no Claude credentials" in n for n in result.notes)


# --------------------------------------------------------------------------- notify policy + message

@pytest.mark.parametrize("policy, status, signal, expected", [
    ("always", "success", None, True),
    ("signal", "success", None, False),
    ("signal", "success", "hi", True),
    ("failure", "success", "hi", False),
    ("failure", "failed", None, True),
    ("signal", "timeout", None, True),
    ("never", "failed", "hi", False),
])
def test_should_notify(tmp_path, policy, status, signal, expected):
    result = RunResult("t", status, 0, None, 1.0, "", signal=signal)
    assert runner.should_notify(make_job(tmp_path, notify=policy), result) is expected


def test_build_message_variants(tmp_path):
    job = make_job(tmp_path, timeout=120)
    failed = RunResult("t", "failed", 3, None, 2.0, "line\n" * 2000, notes=["secret X is not set"])
    msg = runner.build_message(job, failed, "https://run")
    assert (msg.level, msg.title, msg.url) == ("error", "t failed (exit 3)", "https://run")
    assert msg.body.startswith("…") and "secret X is not set" in msg.body

    timed_out = runner.build_message(job, RunResult("t", "timeout", None, None, 120.0, ""))
    assert timed_out.title == "t timed out after 2m00s" and "(no output)" in timed_out.body

    signalled = RunResult("t", "success", 0, None, 1.0, "noise", signal="AMD IVR 97%",
                          routine_url="https://claude.ai/code/session_1", cost_usd=0.05)
    msg = runner.build_message(job, signalled)
    assert (msg.level, msg.title) == ("info", "t")
    assert msg.body.startswith("AMD IVR 97%") and "noise" not in msg.body
    assert "Claude routine started: https://claude.ai/code/session_1" in msg.body and "$0.05" in msg.body

    assert runner.build_message(job, RunResult("t", "success", 0, None, 1.0, "fine")).title == "t ok"


# --------------------------------------------------------------------------- report

class Capture(notify.Channel):
    name = "capture"

    def __init__(self):
        self.messages = []

    def send(self, msg):
        self.messages.append(msg)


def test_report_redacts_logs_and_records_history(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    lines, channel = [], Capture()
    ctx = make_ctx(channels=[channel], redact_logs=True, state_dir=tmp_path / "state", log=lines.append)
    job = make_job(tmp_path, notify="always")
    for _ in range(2):
        runner.report(job, RunResult("t", "success", 0, runner.datetime.now(runner.timezone.utc), 1.0, "PRIVATE"), ctx)
    assert len(channel.messages) == 2 and "PRIVATE" in channel.messages[0].body
    assert not any("PRIVATE" in line for line in lines)
    assert summary.read_text().count("| job | status |") == 1
    history = (tmp_path / "state" / "history.jsonl").read_text().splitlines()
    assert len(history) == 2 and json.loads(history[0])["status"] == "success"
    assert (tmp_path / "state" / "last" / "t.log").read_text() == "PRIVATE"


def test_report_logs_output_when_not_redacted_and_warns_without_channels(tmp_path):
    lines = []
    result = runner.report(make_job(tmp_path, notify="always"),
                           RunResult("t", "success", 0, None, 1.0, "visible"), make_ctx(log=lines.append))
    assert "visible" in lines and any("no notification channel configured" in line for line in lines)
    assert not result.delivered                  # a due notification that went nowhere is a failure


def test_report_dry_run(tmp_path):
    lines = []
    result = runner.report(make_job(tmp_path), RunResult("t", "failed", 1, None, 1.0, "x"),
                           make_ctx(dry_run=True, channels=[Capture()], log=lines.append))
    assert result.notified == {"dry-run": "ok"} and any("(dry run) notification" in line for line in lines)


def test_escalation_to_routine(tmp_path, monkeypatch):
    fired = []

    def fake_fire(url, token, text):
        fired.append((url, token, text))
        return {"claude_code_session_url": "https://claude.ai/code/session_9"}

    monkeypatch.setattr(routines, "fire", fake_fire)
    settings = {"ROUTINE_DEEP_DIVE_URL": "https://api.anthropic.com/v1/claude_code/routines/trig_1/fire",
                "ROUTINE_DEEP_DIVE_TOKEN": "tok"}
    job, channel = make_job(tmp_path, routine="deep_dive"), Capture()

    result = runner.report(job, RunResult("t", "success", 0, None, 1.0, "", signal="IV spike AMD"),
                           make_ctx(settings=settings, channels=[channel]))
    assert fired == [(settings["ROUTINE_DEEP_DIVE_URL"], "tok", "[t] IV spike AMD")]
    assert result.routine_url == "https://claude.ai/code/session_9"
    assert "Claude routine started" in channel.messages[0].body

    runner.report(job, RunResult("t", "success", 0, None, 1.0, ""), make_ctx(settings=settings))
    runner.report(job, RunResult("t", "failed", 1, None, 1.0, "", signal="x"), make_ctx(settings=settings))
    assert len(fired) == 1                     # only successful, signalling runs escalate

    missing = runner.report(job, RunResult("t", "success", 0, None, 1.0, "", signal="x"), make_ctx())
    assert any("set ROUTINE_DEEP_DIVE_URL" in n for n in missing.notes)


def test_run_and_report_end_to_end(tmp_path):
    channel = Capture()
    result = runner.run_and_report(make_job(tmp_path, run='echo "cheap put on SOFI" > "$OPS_NOTIFY"'),
                                   make_ctx(channels=[channel]))
    assert result.notified == {"capture": "ok"}
    assert channel.messages[0].body.startswith("cheap put on SOFI")
