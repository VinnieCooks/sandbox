"""Bridge from jobs to Claude Cloud Routines via a routine's API trigger.

A job with `routine: deep_dive` starts that routine when it signals, using
ROUTINE_DEEP_DIVE_URL and ROUTINE_DEEP_DIVE_TOKEN (copied from the routine's
API trigger at claude.ai/code/routines).
Reference: https://platform.claude.com/docs/en/api/claude-code/routines-fire
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable, Mapping

API_VERSION = "2023-06-01"
MAX_TEXT = 65_536   # documented limit for the `text` field


class RoutineError(RuntimeError):
    pass


def credentials(name: str, settings: Mapping[str, str]) -> tuple[str, str]:
    key = name.upper()
    url, token = settings.get(f"ROUTINE_{key}_URL"), settings.get(f"ROUTINE_{key}_TOKEN")
    if not url or not token:
        raise RoutineError(f"set ROUTINE_{key}_URL and ROUTINE_{key}_TOKEN")
    if not url.startswith("https://"):
        raise RoutineError(f"ROUTINE_{key}_URL must start with https://")
    return url, token


def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        return json.loads(exc.read() or b"{}").get("error", {}).get("message") or exc.reason
    except (ValueError, AttributeError):
        return str(exc.reason)


def fire(url: str, token: str, text: str = "", *, timeout: float = 30.0,
         opener: Callable = urllib.request.urlopen) -> dict:
    """Start one routine run and return the API's JSON (includes claude_code_session_url).

    Deliberately no retry: the endpoint has no idempotency key, so retrying an
    ambiguous failure could start a duplicate session.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps({"text": text[:MAX_TEXT]}).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-version": API_VERSION,
            "Content-Type": "application/json",
        },
    )
    try:
        with opener(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raise RoutineError(f"HTTP {exc.code}: {_error_detail(exc)}") from None
    except urllib.error.URLError as exc:
        raise RoutineError(f"network error: {exc.reason}") from None
