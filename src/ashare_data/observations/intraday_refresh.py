"""Bounded quote refresh, never a tolerance on a source timestamp.

HHMMSS establishes an encoded resolution, not a rounding rule or a bound on
either clock's error. Every selected response still has to pass snapshot()'s
unaltered source <= its own receipt comparison.
"""

from ashare_data.core.model import require

POLICY = "strict-receipt-order-quote-refresh.v1"
MAX_ROUNDS = 2
QUOTE_SOURCES = ("sse_quote_600000", "sse_quote_600036", "sse_quote_601318", "sina_quotes")
CLOCK_BLOCKER = "CURRENT_LIMITS_UNVERIFIED:官方快照不是当日或晚于收讫"


def clock_only(assessment):
    """Refresh only a complete capture blocked solely on same-day clock order.

    A day mismatch, malformed response, halt, missing list or network failure
    must not be hidden by replacing the quote tail. Forward labels of *any*
    size remain invalid; this predicate only authorizes another observation.
    """
    issues = assessment.get("source_diagnostics", [])
    return (
        assessment["blockers"] == [CLOCK_BLOCKER]
        and bool(issues)
        and all(
            i["category"] == "source_time_ahead_of_receipt" and i["source"] in QUOTE_SOURCES[:-1]
            for i in issues
        )
    )


def state(assessment, rounds, limit):
    retryable = clock_only(assessment)
    return dict(
        rounds=rounds,
        retryable=retryable,
        status="ready"
        if assessment["ready"]
        else "refresh_quotes"
        if retryable and rounds < limit
        else "retry_limit_reached"
        if retryable
        else "blocked_other_evidence",
    )


def validate(packet, bodies, assess, expected):
    """Recompute each retry trigger and the last-response projection on read."""
    policy = packet["refresh_policy"]
    limit = policy.get("max_rounds")
    require(
        policy.get("name") == POLICY and type(limit) is int and 0 <= limit <= MAX_ROUNDS,
        "INTRADAY_REFRESH_POLICY",
        "仅有限次重新观察，不放宽时间比较",
    )
    events = packet["responses"]
    extra = len(events) - len(expected)
    require(
        extra >= 0 and extra % len(QUOTE_SOURCES) == 0,
        "INTRADAY_REFRESH_SEQUENCE",
        "须保留完整初始采集及每轮四份报价",
    )
    rounds = extra // len(QUOTE_SOURCES)
    require(
        rounds <= limit
        and [e["name"] for e in events] == list(expected) + list(QUOTE_SOURCES) * rounds
        and [e["sequence"] for e in events] == list(range(len(events))),
        "INTRADAY_REFRESH_SEQUENCE",
        "重试顺序或次数不符",
    )
    selected = {}
    for index, event in enumerate(events):
        selected[event["name"]] = {k: v for k, v in event.items() if k not in {"name", "sequence"}}
        count = index + 1
        if count < len(events) and count >= len(expected) and (count - len(expected)) % 4 == 0:
            before = dict(packet, sources=dict(selected), completed_at=event["observed_at"])
            require(
                clock_only(assess(before, bodies)),
                "INTRADAY_REFRESH_TRIGGER",
                "禁止用重试覆盖其他阻断或已通过的输入",
            )
    require(selected == packet["sources"], "INTRADAY_REFRESH_SELECTION", "必须使用各源最后回执")
    require(
        packet["refresh"] == state(packet["assessment"], rounds, limit),
        "INTRADAY_REFRESH_STATE",
        "重试状态不符",
    )
