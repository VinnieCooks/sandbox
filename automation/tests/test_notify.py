import urllib.error

import pytest

from ops import notify
from ops.notify import Message


@pytest.fixture
def posts(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "post_json", lambda url, payload, headers=None: sent.append((url, payload, headers)))
    return sent


def test_configured_channels_and_problems():
    settings = {
        "NTFY_TOPIC": "t", "TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "42",
        "SLACK_WEBHOOK_URL": "https://hooks.slack.com/x", "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/x",
        "SMTP_HOST": "smtp.example.com", "EMAIL_TO": "me@example.com",
    }
    assert [c.name for c in notify.configured_channels(settings)] == ["ntfy", "telegram", "slack", "discord", "email"]
    assert notify.configured_channels({}) == []
    problems = notify.channel_problems({"TELEGRAM_BOT_TOKEN": "tok", "SMTP_HOST": "h", "EMAIL_TO": "e", "SMTP_PORT": "x"})
    assert "telegram: TELEGRAM_BOT_TOKEN set but TELEGRAM_CHAT_ID missing" in problems
    assert "email: SMTP_PORT must be a number" in problems


def test_ntfy_payload(posts):
    notify.Ntfy("secret-topic", "https://ntfy.example.com/", token="tk").send(
        Message("job failed", "é" * 3000, level="error", url="https://github.com/run/1"))
    url, payload, headers = posts[0]
    assert url == "https://ntfy.example.com/"
    assert payload["topic"] == "secret-topic"
    assert payload["priority"] == 4 and payload["tags"] == ["rotating_light"]
    assert payload["click"] == "https://github.com/run/1"
    assert len(payload["message"].encode("utf-8")) <= 3900       # ntfy.sh limit is 4,096 bytes
    assert payload["message"].endswith("[truncated]")
    assert headers == {"Authorization": "Bearer tk"}


def test_telegram_payload(posts):
    notify.Telegram("123:ABC", "42").send(Message("title", "x" * 5000, level="success", url="https://u"))
    url, payload, _ = posts[0]
    assert url == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert payload["chat_id"] == "42" and payload["disable_web_page_preview"] is True
    assert payload["text"].startswith("✅ title\n\n") and len(payload["text"]) <= 4000


def test_slack_escapes_markup(posts):
    notify.Slack("https://hooks.slack.com/x").send(Message("a<b>", "x & <y>", url="https://u"))
    text = posts[0][1]["text"]
    assert "*a&lt;b&gt;*" in text and "x &amp; &lt;y&gt;" in text and text.endswith("<https://u|Open run>")


def test_discord_never_pings(posts):
    notify.Discord("https://discord.com/api/webhooks/x").send(Message("t", "@everyone " + "x" * 3000))
    payload = posts[0][1]
    assert payload["allowed_mentions"] == {"parse": []}
    assert len(payload["content"]) <= 1990


class FakeSMTP:
    instances = []

    def __init__(self, host, port):
        self.host, self.port, self.calls = host, port, []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, message):
        self.calls.append(("send", message["Subject"], message["To"], message.get_content()))


@pytest.mark.parametrize("port, tls", [(587, True), (465, False)])
def test_email(port, tls):
    FakeSMTP.instances.clear()
    notify.Email("smtp.example.com", port, "me", "pw", "a@x.com, b@y.com", "me@x.com", smtp_factory=FakeSMTP).send(
        Message("job failed", "details", level="error", url="https://u"))
    calls = FakeSMTP.instances[0].calls
    assert ("starttls" in calls) is tls
    assert ("login", "me", "pw") in calls
    send = calls[-1]
    assert send[1] == "❌ job failed" and send[2] == "a@x.com, b@y.com" and "details\n\nhttps://u" in send[3]


class Flaky(notify.Channel):
    def __init__(self, name, errors):
        self.name, self.errors, self.calls = name, list(errors), 0

    def send(self, msg):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)


def http_error(code):
    return urllib.error.HTTPError("https://secret.example/hook", code, "err", {}, None)


def test_dispatch_retries_transient_errors_only():
    sleeps = []
    retried = Flaky("retried", [http_error(503)])
    rejected = Flaky("rejected", [http_error(400)])
    leaky = Flaky("leaky", [ValueError("unknown url type: 'hooks.slack.com/services/SECRET'")])
    fine = Flaky("fine", [])
    results = notify.dispatch(Message("t", "b"), [retried, rejected, leaky, fine], sleep=sleeps.append)
    assert results == {"retried": "ok", "rejected": "error: HTTP 400",
                       "leaky": "error: invalid URL or value - check this channel's settings", "fine": "ok"}
    assert (retried.calls, rejected.calls, fine.calls) == (2, 1, 1)
    assert sleeps == [2.0]
    assert "SECRET" not in str(results)


def test_dispatch_gives_up_after_attempts():
    down = Flaky("down", [urllib.error.URLError("timed out")] * 3)
    assert notify.dispatch(Message("t", "b"), [down], sleep=lambda s: None) == {"down": "error: network error: timed out"}
    assert down.calls == 2


def test_clip_helpers():
    assert notify.clip("short", 10) == "short"
    clipped = notify.clip("x" * 100, 40)
    assert len(clipped) == 40 and clipped.endswith("[truncated]")
    clipped = notify.clip_bytes("€" * 100, 50)                   # 3-byte characters
    assert len(clipped.encode("utf-8")) <= 50 and clipped.endswith("[truncated]")
