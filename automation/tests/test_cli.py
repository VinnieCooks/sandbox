from pathlib import Path

import pytest

from ops.cli import main

REPO_REGISTRY = Path(__file__).resolve().parents[1] / "jobs.yaml"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A throwaway repo layout: <root>/automation/jobs.yaml."""
    automation = tmp_path / "automation"
    automation.mkdir()
    monkeypatch.setenv("OPS_STATE_DIR", str(tmp_path / "state"))

    def write(jobs_yaml: str) -> str:
        path = automation / "jobs.yaml"
        path.write_text("defaults: {cwd: ..}\n" + jobs_yaml, encoding="utf-8")
        return str(path)
    return write


def test_repository_workflow_is_in_sync(capsys):
    assert main(["--registry", str(REPO_REGISTRY), "check"]) == 0
    assert "workflow in sync" in capsys.readouterr().out


def test_sync_then_check_detects_drift(repo, tmp_path, capsys):
    registry = repo("jobs:\n  - {name: a, run: 'true', schedule: '7 9 * * *'}\n")
    assert main(["--registry", registry, "check"]) == 1                 # no workflow yet
    assert main(["--registry", registry, "sync"]) == 0
    assert (tmp_path / ".github/workflows/ops-scheduler.yml").is_file()
    assert main(["--registry", registry, "check"]) == 0
    repo("jobs:\n  - {name: a, run: 'true', schedule: '8 9 * * *'}\n")
    capsys.readouterr()
    assert main(["--registry", registry, "check"]) == 1
    out = capsys.readouterr().out
    assert '+    - cron: "8 9 * * *"' in out and "out of date" in out


def test_list(repo, capsys):
    registry = repo("jobs:\n  - {name: a, run: 'true', schedule: '7 9 * * *'}\n  - {name: b, run: 'true'}\n")
    assert main(["--registry", registry, "list"]) == 0
    out = capsys.readouterr().out
    assert "7 9 * * * (UTC)" in out and "manual" in out


def test_run_dry_run_and_failure_exit_codes(repo, capsys):
    registry = repo("jobs:\n  - {name: ok, run: 'echo fine', notify: always}\n  - {name: bad, run: 'exit 4'}\n")
    assert main(["--registry", registry, "run", "ok", "--dry-run"]) == 0
    assert "(dry run) notification [success] ok ok" in capsys.readouterr().out
    assert main(["--registry", registry, "run", "bad", "--dry-run"]) == 1


def test_undelivered_notification_fails_the_run(repo, monkeypatch, capsys):
    registry = repo("jobs:\n  - {name: ok, run: 'echo fine', notify: always}\n  - {name: quiet, run: 'true'}\n")
    assert main(["--registry", registry, "run", "ok"]) == 1                # no channel configured
    monkeypatch.setenv("NTFY_TOPIC", "t")
    monkeypatch.setenv("NTFY_SERVER", "not-a-url")
    assert main(["--registry", registry, "run", "ok"]) == 1                # channel errored
    assert "notified: ntfy=error" in capsys.readouterr().out
    assert main(["--registry", registry, "run", "quiet"]) == 0             # nothing was due


def test_run_scheduled_matches_the_fired_cron(repo, capsys):
    registry = repo("jobs:\n"
                    "  - {name: a, run: 'echo A', schedule: '7 9 * * *'}\n"
                    "  - {name: b, run: 'echo B', schedule: ['7  9 * * *', '8 9 * * *']}\n"
                    "  - {name: c, run: 'echo C', schedule: '8 9 * * *'}\n")
    assert main(["--registry", registry, "run-scheduled", "--cron", "7 9 * * *"]) == 0
    out = capsys.readouterr().out
    assert "[ops] a: success" in out and "[ops] b: success" in out and "[ops] c:" not in out
    assert main(["--registry", registry, "run-scheduled", "--cron", "9 9 * * *"]) == 1


def test_channels_and_notify_test(repo, monkeypatch, capsys):
    registry = repo("jobs: []\n")
    assert main(["--registry", registry, "channels"]) == 1
    assert "configured channels: none" in capsys.readouterr().out
    monkeypatch.setenv("NTFY_TOPIC", "t")
    assert main(["--registry", registry, "channels"]) == 0
    assert "configured channels: ntfy" in capsys.readouterr().out
    assert main(["--registry", registry, "notify-test", "--dry-run"]) == 0
    assert "would send to: ntfy" in capsys.readouterr().out


def test_env_file_next_to_registry_is_read(repo, tmp_path, capsys):
    registry = repo("jobs: []\n")
    (tmp_path / "automation" / ".env").write_text("TELEGRAM_BOT_TOKEN=x\nTELEGRAM_CHAT_ID=1\n", encoding="utf-8")
    assert main(["--registry", registry, "channels"]) == 0
    assert "configured channels: telegram" in capsys.readouterr().out


def test_invalid_registry_exits_2(repo, capsys):
    assert main(["--registry", repo("jobs:\n  - {name: X}\n"), "list"]) == 2
    assert "error: job 'X'" in capsys.readouterr().err
