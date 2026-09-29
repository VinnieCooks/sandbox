import yaml

from ops import workflow


def parse(text):
    data = yaml.safe_load(text)
    return data, data.get("on", data.get(True))      # YAML 1.1 reads the key `on` as True


def test_render_groups_jobs_by_cron_and_time_zone(write_registry):
    reg = write_registry("""
timezone: America/New_York
jobs:
  - {name: a, run: x, schedule: "7 9 * * 1-5", timeout: 20m}
  - {name: b, run: y, schedule: ["7 9 * * 1-5", "17 */6 * * *"], timeout: 30m}
  - {name: c, run: z, schedule: "11 3 * * *", timezone: UTC}
  - {name: paused, run: z, schedule: "13 3 * * *", enabled: false}
  - {name: srv, run: z, schedule: "* * * * *", engine: server}
""")
    text = workflow.render(reg)
    data, on = parse(text)
    assert on["schedule"] == [
        {"cron": "11 3 * * *"},
        {"cron": "17 */6 * * *", "timezone": "America/New_York"},
        {"cron": "7 9 * * 1-5", "timezone": "America/New_York"},
    ]
    assert '- cron: "7 9 * * 1-5"  # a, b' in text
    assert "13 3" not in text and "* * * * *" not in text      # disabled and server jobs stay off GitHub
    assert on["workflow_dispatch"]["inputs"]["job"]["required"] is True
    run = data["jobs"]["run"]
    assert run["timeout-minutes"] == 60                        # a + b share a cron: 20m + 30m + 10m setup
    assert "Install Claude Code CLI" not in text
    assert data["permissions"] == {"contents": "read"}


def test_render_without_schedules_keeps_manual_trigger(write_registry):
    data, on = parse(workflow.render(write_registry("jobs:\n  - {name: a, run: x}\n")))
    assert "schedule" not in on and "workflow_dispatch" in on


def test_claude_step_only_for_enabled_github_claude_jobs(write_registry):
    disabled = write_registry("jobs:\n  - {name: a, type: claude, prompt: hi, enabled: false}\n")
    assert "Install Claude Code CLI" not in workflow.render(disabled)
    enabled = write_registry("jobs:\n  - {name: a, type: claude, prompt: hi}\n")
    assert "npm install -g @anthropic-ai/claude-code" in workflow.render(enabled)


def test_timeout_is_capped_at_github_maximum(write_registry):
    reg = write_registry("jobs:\n"
                         "  - {name: a, run: x, schedule: '7 * * * *', timeout: 5h}\n"
                         "  - {name: b, run: x, schedule: '7 * * * *', timeout: 5h}\n")
    assert workflow.timeout_minutes(reg) == 360
