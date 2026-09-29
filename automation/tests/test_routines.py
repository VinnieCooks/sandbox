import io
import json
import urllib.error

import pytest

from ops import routines

URL = "https://api.anthropic.com/v1/claude_code/routines/trig_01ABC/fire"


def test_credentials():
    settings = {"ROUTINE_DEEP_DIVE_URL": URL, "ROUTINE_DEEP_DIVE_TOKEN": "tok"}
    assert routines.credentials("deep_dive", settings) == (URL, "tok")
    with pytest.raises(routines.RoutineError, match="ROUTINE_OTHER_URL and ROUTINE_OTHER_TOKEN"):
        routines.credentials("other", settings)
    with pytest.raises(routines.RoutineError, match="https://"):
        routines.credentials("x", {"ROUTINE_X_URL": "http://insecure", "ROUTINE_X_TOKEN": "t"})


def test_fire_sends_documented_request():
    captured = {}

    def opener(request, timeout):
        captured.update(request=request, timeout=timeout)
        return io.BytesIO(json.dumps({"type": "routine_fire",
                                      "claude_code_session_url": "https://claude.ai/code/session_1"}).encode())

    response = routines.fire(URL, "tok", "x" * 70_000, opener=opener)
    request = captured["request"]
    assert response["claude_code_session_url"] == "https://claude.ai/code/session_1"
    assert request.get_method() == "POST" and request.full_url == URL
    assert request.get_header("Authorization") == "Bearer tok"
    assert request.get_header("Anthropic-version") == "2023-06-01"
    assert len(json.loads(request.data)["text"]) == routines.MAX_TEXT


def test_fire_reports_api_errors_without_retrying():
    calls = []

    def opener(request, timeout):
        calls.append(request)
        body = io.BytesIO(b'{"type":"error","error":{"type":"rate_limit_error","message":"hourly fire limit"}}')
        raise urllib.error.HTTPError(URL, 429, "Too Many Requests", {}, body)

    with pytest.raises(routines.RoutineError, match="HTTP 429: hourly fire limit"):
        routines.fire(URL, "tok", opener=opener)
    assert len(calls) == 1


def test_fire_network_error():
    def opener(request, timeout):
        raise urllib.error.URLError("connection refused")

    with pytest.raises(routines.RoutineError, match="network error: connection refused"):
        routines.fire(URL, "tok", opener=opener)
