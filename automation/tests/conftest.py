import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops import registry  # noqa: E402


@pytest.fixture
def write_registry(tmp_path):
    """Write a jobs.yaml into tmp_path and load it."""
    def _write(text: str) -> registry.Registry:
        path = tmp_path / "jobs.yaml"
        path.write_text(text, encoding="utf-8")
        return registry.load(path)
    return _write


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """Keep the developer's or CI's real settings out of every test."""
    for key in ("NTFY_TOPIC", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SLACK_WEBHOOK_URL",
                "DISCORD_WEBHOOK_URL", "SMTP_HOST", "EMAIL_TO", "OPS_SECRETS_JSON", "OPS_REDACT_LOGS",
                "OPS_ENV_FILE", "OPS_STATE_DIR", "OPS_REGISTRY", "GITHUB_ACTIONS", "GITHUB_STEP_SUMMARY",
                "GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID", "ANTHROPIC_API_KEY",
                "CLAUDE_CODE_OAUTH_TOKEN", "OPS_HEARTBEAT_URL"):
        monkeypatch.delenv(key, raising=False)
