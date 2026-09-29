"""Notification channels. A channel is enabled when its settings are present.

Setting names (never printed; `ops channels` only reports which are active):
  ntfy      NTFY_TOPIC, optional NTFY_SERVER (default https://ntfy.sh), NTFY_TOKEN
  telegram  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  slack     SLACK_WEBHOOK_URL
  discord   DISCORD_WEBHOOK_URL
  email     SMTP_HOST, EMAIL_TO, optional SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD, EMAIL_FROM
"""

from __future__ import annotations

import json
import smtplib
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Callable, Mapping

ICON = {"success": "✅", "info": "🔔", "warning": "⚠️", "error": "❌"}
_NTFY_PRIORITY = {"success": 3, "info": 3, "warning": 4, "error": 4}
_NTFY_TAG = {"success": "white_check_mark", "info": "bell", "warning": "warning", "error": "rotating_light"}
_TRUNCATED = "\n…[truncated]"


@dataclass
class Message:
    title: str
    body: str
    level: str = "info"          # success | info | warning | error
    url: str | None = None


def clip(text: str, limit: int) -> str:
    """Trim to `limit` characters, marking the cut."""
    if len(text) <= limit:
        return text
    return text[: limit - len(_TRUNCATED)].rstrip() + _TRUNCATED


def clip_bytes(text: str, limit: int) -> str:
    """Trim to `limit` UTF-8 bytes without splitting a character."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    marker = _TRUNCATED.encode("utf-8")
    return data[: limit - len(marker)].decode("utf-8", "ignore").rstrip() + _TRUNCATED


def post_json(url: str, payload: dict, headers: Mapping[str, str] | None = None, timeout: float = 15.0) -> bytes:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        # A custom User-Agent matters: Discord's CDN rejects the default Python-urllib one.
        headers={"Content-Type": "application/json", "User-Agent": "ops-notify/1", **(headers or {})},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


class Channel:
    name = "channel"

    def send(self, msg: Message) -> None:
        raise NotImplementedError


class Ntfy(Channel):
    name = "ntfy"

    def __init__(self, topic: str, server: str = "https://ntfy.sh", token: str | None = None):
        self.topic, self.server, self.token = topic, server.rstrip("/"), token

    def send(self, msg: Message) -> None:
        # JSON publishing keeps UTF-8 titles intact; HTTP headers would not.
        payload = {
            "topic": self.topic,
            "title": clip(msg.title, 250),
            "message": clip_bytes(msg.body, 3900),   # ntfy.sh caps messages at 4,096 bytes
            "priority": _NTFY_PRIORITY.get(msg.level, 3),
            "tags": [_NTFY_TAG.get(msg.level, "bell")],
        }
        if msg.url:
            payload["click"] = msg.url
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        post_json(self.server + "/", payload, headers)


class Telegram(Channel):
    name = "telegram"

    def __init__(self, token: str, chat_id: str):
        self.token, self.chat_id = token, chat_id

    def send(self, msg: Message) -> None:
        text = f"{ICON.get(msg.level, '')} {msg.title}\n\n{msg.body}"
        if msg.url:
            text += f"\n\n{msg.url}"
        post_json(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            {"chat_id": self.chat_id, "text": clip(text, 4000), "disable_web_page_preview": True},
        )


class Slack(Channel):
    name = "slack"

    def __init__(self, webhook: str):
        self.webhook = webhook

    def send(self, msg: Message) -> None:
        def esc(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        text = f"{ICON.get(msg.level, '')} *{esc(msg.title)}*\n{esc(clip(msg.body, 3500))}"
        if msg.url:
            text += f"\n<{msg.url}|Open run>"
        post_json(self.webhook, {"text": text})


class Discord(Channel):
    name = "discord"

    def __init__(self, webhook: str):
        self.webhook = webhook

    def send(self, msg: Message) -> None:
        text = f"{ICON.get(msg.level, '')} **{msg.title}**\n{msg.body}"
        if msg.url:
            text += f"\n{msg.url}"
        # allowed_mentions: job output must never ping @everyone.
        post_json(self.webhook, {"content": clip(text, 1990), "allowed_mentions": {"parse": []}})


class Email(Channel):
    name = "email"

    def __init__(self, host: str, port: int, user: str | None, password: str | None, to: str, sender: str,
                 smtp_factory: Callable[..., smtplib.SMTP] | None = None):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.to, self.sender = to, sender
        self._smtp = smtp_factory

    def send(self, msg: Message) -> None:
        email = EmailMessage()
        email["Subject"] = f"{ICON.get(msg.level, '')} {msg.title}"
        email["From"] = self.sender
        email["To"] = self.to
        email.set_content(msg.body + (f"\n\n{msg.url}" if msg.url else ""))
        context = ssl.create_default_context()
        if self._smtp is not None:
            server = self._smtp(self.host, self.port)
        elif self.port == 465:
            server = smtplib.SMTP_SSL(self.host, self.port, context=context, timeout=20)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=20)
        with server:
            if self.port != 465:
                server.starttls(context=context)
            if self.user and self.password:
                server.login(self.user, self.password)
            server.send_message(email)


def configured_channels(s: Mapping[str, str]) -> list[Channel]:
    channels: list[Channel] = []
    if s.get("NTFY_TOPIC"):
        channels.append(Ntfy(s["NTFY_TOPIC"], s.get("NTFY_SERVER", "https://ntfy.sh"), s.get("NTFY_TOKEN")))
    if s.get("TELEGRAM_BOT_TOKEN") and s.get("TELEGRAM_CHAT_ID"):
        channels.append(Telegram(s["TELEGRAM_BOT_TOKEN"], s["TELEGRAM_CHAT_ID"]))
    if s.get("SLACK_WEBHOOK_URL"):
        channels.append(Slack(s["SLACK_WEBHOOK_URL"]))
    if s.get("DISCORD_WEBHOOK_URL"):
        channels.append(Discord(s["DISCORD_WEBHOOK_URL"]))
    if s.get("SMTP_HOST") and s.get("EMAIL_TO"):
        channels.append(Email(
            s["SMTP_HOST"], int(s.get("SMTP_PORT", "587")), s.get("SMTP_USER"), s.get("SMTP_PASSWORD"),
            s["EMAIL_TO"], s.get("EMAIL_FROM") or s.get("SMTP_USER") or s["EMAIL_TO"],
        ))
    return channels


def channel_problems(s: Mapping[str, str]) -> list[str]:
    """Half-configured channels, which would otherwise be silently skipped."""
    pairs = [
        ("telegram", ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")),
        ("email", ("SMTP_HOST", "EMAIL_TO")),
    ]
    problems = []
    for name, keys in pairs:
        present = [k for k in keys if s.get(k)]
        if present and len(present) != len(keys):
            missing = [k for k in keys if not s.get(k)]
            problems.append(f"{name}: {', '.join(present)} set but {', '.join(missing)} missing")
    if s.get("SMTP_PORT") and not s.get("SMTP_PORT", "").isdigit():
        problems.append("email: SMTP_PORT must be a number")
    return problems


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or exc.code >= 500
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return False
    return isinstance(exc, (urllib.error.URLError, OSError, smtplib.SMTPException))


def _describe(exc: Exception) -> str:
    # Never include URLs: webhook URLs and bot tokens are secrets.
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return f"network error: {exc.reason}"
    if isinstance(exc, ValueError):   # urllib echoes the (secret) URL in these messages
        return "invalid URL or value - check this channel's settings"
    return f"{type(exc).__name__}: {exc}"


def dispatch(msg: Message, channels: list[Channel], *, attempts: int = 2, backoff: float = 2.0,
             sleep: Callable[[float], None] = time.sleep) -> dict[str, str]:
    """Send to every channel; one failing channel never blocks the others."""
    results: dict[str, str] = {}
    for channel in channels:
        for attempt in range(1, attempts + 1):
            try:
                channel.send(msg)
                results[channel.name] = "ok"
                break
            except Exception as exc:  # noqa: BLE001 - a notifier must not crash the run
                if attempt < attempts and _retryable(exc):
                    sleep(backoff * attempt)
                    continue
                results[channel.name] = f"error: {_describe(exc)}"
                break
    return results
