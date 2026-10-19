"""Small credential-free HTTP diagnostics and persistent serial request pacing.

Intervals and fallback cooldowns are local policies, never provider quota claims.
Only ordinary collection successes drive recovery; this adds no probe requests.
"""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import re


def _date(value):
    try:
        result = parsedate_to_datetime(value)
        return result.replace(tzinfo=result.tzinfo or timezone.utc).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def retry_after(headers, now):
    normalized = {str(k).lower(): str(v).strip() for k, v in headers.items()}
    raw = normalized.get("retry-after")
    if raw is None:
        return {"present": False, "valid": False, "seconds": None}
    if re.fullmatch(r"\d{1,12}", raw):
        return {"present": True, "valid": True, "seconds": int(raw), "format": "delta_seconds"}
    target = _date(raw)
    if target is None:
        return {"present": True, "valid": False, "seconds": None}
    server = _date(normalized.get("date"))
    # Waiting at least the server's delta avoids retrying early under clock skew.
    seconds = max(0, target - now, target - server if server is not None else 0)
    return {"present": True, "valid": True, "seconds": seconds, "format": "http_date"}


def _redact(value):
    text = str(value)
    text = re.sub(r"https?://[^\s<>\"']+", "[URL redacted]", text)
    text = re.sub(r"(?i)\b(bearer|authorization|cookie|password|secret|token|api[-_ ]?key|sdk[-_ ]?key)\b[^,;\n}]*",
                  r"\1=[redacted]", text)
    text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[email redacted]", text)
    text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b[0-9a-fA-F:]{3,}:[0-9a-fA-F:]+\b", "[address redacted]", text)
    text = re.sub(r"\b[A-Za-z0-9_+=/-]{24,}\b", "[opaque value redacted]", text)
    return text[:512]


def diagnostics(status, raw, headers, now):
    normalized = {str(k).lower(): str(v).strip() for k, v in headers.items()}
    safe = {}
    for key in ("retry-after", "date", "content-length", "ratelimit-limit", "ratelimit-remaining",
                "ratelimit-reset", "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset"):
        value = normalized.get(key)
        if value is not None:
            parsed = _date(value)
            safe[key] = (value if re.fullmatch(r"\d{1,12}", value)
                         else datetime.fromtimestamp(parsed, timezone.utc).isoformat()
                         if parsed is not None else "[invalid value omitted]")
    if "content-type" in normalized:
        mime = normalized["content-type"].split(";", 1)[0].strip().lower()
        safe["content-type"] = mime if re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", mime) else "[invalid value omitted]"
    result = {"response_headers": safe, "server_retry_after": retry_after(headers, now)}
    if status != 200:
        sample = raw[:4096].decode("utf-8", errors="replace")
        try:
            body = json.loads(sample)
            if isinstance(body, dict):
                sample = json.dumps({k: _redact(v) for k, v in body.items()
                                     if k.lower() in {"code", "status", "message", "msg", "error", "detail", "reason"}
                                     and isinstance(v, (str, int, float, bool))}, ensure_ascii=False)
            else:
                sample = "[non-object JSON error body omitted]"
        except ValueError:
            sample = _redact(sample)
        result.update(error_body_redacted=sample[:1024], error_body_sample_truncated=len(raw) > 4096,
                      error_body_policy="allowlisted_JSON_scalars_or_redacted_text; no_raw_error_body_saved")
    return result


def success(state, headers, now, base, *, adaptive=False):
    state["consecutive_errors"] = 0
    hint = retry_after(headers, now)
    if hint["valid"] and hint["seconds"]:
        state["cooldown_until"] = max(state.get("cooldown_until", 0), now + hint["seconds"])
        state["success_streak"] = 0
        return
    if not adaptive:
        return
    state.setdefault("recovery_window_started", now)
    state["success_streak"] = state.get("success_streak", 0) + 1
    current = max(base, state.get("interval", base))
    floor = max(5.0, base)
    if state["success_streak"] >= 120 and now - state["recovery_window_started"] >= 1800 and current > floor:
        state["interval"] = max(floor, round(current * 0.9, 3))
        state["last_pacing_change"] = {"at": now, "from": current, "to": state["interval"],
                                       "reason": "120_natural_successes_and_30_minutes; local_policy"}
        state["success_streak"], state["recovery_window_started"] = 0, now


def failure(state, status, headers, now, base):
    state["consecutive_errors"] = state.get("consecutive_errors", 0) + 1
    state["success_streak"], state["recovery_window_started"] = 0, now
    hint = retry_after(headers, now)
    wait = hint["seconds"] if hint["valid"] else 0
    if status in (401, 402, 403):
        state["blocked"] = f"HTTP/provider {status}: access/payment review required"
    elif status == 429:
        count = state.get("rate_limit_events", 0) + 1
        state["rate_limit_events"] = count
        state["interval"] = max(10, max(base, state.get("interval", base)) * 2)
        wait = max(wait, min(86400, 3600 * 2 ** min(count - 1, 5)))
        state["last_rate_limit_at"] = now
    elif state["consecutive_errors"] >= 3:
        wait = max(wait, 1800)
    if wait:
        state["cooldown_until"] = max(state.get("cooldown_until", 0), now + wait)
    return {"server_retry_after": hint, "local_or_server_wait_seconds": wait,
            "cooldown_until": state.get("cooldown_until"), "interval_seconds": state.get("interval", base),
            "fallback_policy": "serial_local_backoff; not_published_provider_quota"}
