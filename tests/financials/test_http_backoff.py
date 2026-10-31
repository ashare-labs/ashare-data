"""Offline server hints, redaction and natural-success recovery regressions."""

from datetime import datetime

from ashare_data.financials.expansion import diagnostics, failure, retry_after, success


def test_headers_case_http_date_and_clock_skew_are_respected():
    now = datetime.fromisoformat("2026-10-19T14:00:00+00:00").timestamp()
    result = retry_after({"rEtRy-AfTeR": "Mon, 19 Oct 2026 17:00:00 GMT",
                          "Date": "Mon, 19 Oct 2026 13:00:00 GMT"}, now)
    assert result["seconds"] == 4 * 3600
    state = {}
    failure(state, 429, {"retry-after": "10800"}, now, 2.2)
    assert state["cooldown_until"] == now + 10800
    restored = dict(state)
    failure(restored, 503, {"retry-after": "14400"}, now, 2.2)
    assert restored["cooldown_until"] == now + 14400


def test_absent_malformed_and_zero_retry_hint_are_distinct():
    assert not retry_after({}, 0)["present"]
    assert retry_after({"Retry-After": "invalid"}, 0) == {"present": True, "valid": False, "seconds": None}
    assert retry_after({"Retry-After": "0"}, 0)["seconds"] == 0
    state = {}
    failure(state, 429, {}, 100, 2.2)
    assert state["cooldown_until"] == 3700 and state["interval"] == 10
    failure(state, 429, {}, 4000, 2.2)
    assert state["cooldown_until"] == 11200 and state["interval"] == 20


def test_diagnostics_keep_safe_message_but_no_credentials_or_sensitive_headers():
    body = b'{"code":429,"message":"Too many requests token=secret123","sdk-key":"secret456","data":{"account":"private"}}'
    result = diagnostics(429, body, {"Set-Cookie": "session=private", "Authorization": "secret",
                                   "Retry-After": "60", "X-Request-IP": "10.0.0.1"}, 0)
    rendered = str(result)
    assert "Too many requests" in rendered and "429" in rendered
    assert all(secret not in rendered for secret in ("secret123", "secret456", "private", "10.0.0.1", "Set-Cookie"))
    assert result["response_headers"] == {"retry-after": "60"}
    text = diagnostics(503, b"IP 10.0.0.1 user@example.test Bearer opaquevalue", {}, 0)
    assert "10.0.0.1" not in str(text) and "user@example.test" not in str(text)


def test_recovery_needs_both_natural_success_count_and_elapsed_time():
    state = {"interval": 10}
    for i in range(200):
        success(state, {}, i, 2.2, adaptive=True)
    assert state["interval"] == 10
    success(state, {}, 1800, 2.2, adaptive=True)
    assert state["interval"] == 9
    failure(state, None, {}, 1801, 2.2)
    success(state, {}, 3601, 2.2, adaptive=True)
    assert state["interval"] == 9 and state["success_streak"] == 1
    for window in range(1, 12):
        for i in range(120):
            success(state, {}, 3601 + 1800 * window, 2.2, adaptive=True)
    assert state["interval"] == 5


def test_success_with_server_wait_prevents_adaptive_recovery():
    state = {"interval": 10, "success_streak": 200, "recovery_window_started": 0}
    success(state, {"Retry-After": "900"}, 2000, 2.2, adaptive=True)
    assert state["interval"] == 10 and state["cooldown_until"] == 2900
    assert state["success_streak"] == 0
