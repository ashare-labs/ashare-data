"""Explicit clock evidence; never reconstruct a missing wall-clock timestamp."""

from datetime import datetime


def read_clock(clock, errors, label):
    try:
        return clock()
    except Exception as error:
        errors.append(dict(event=label, type=type(error).__name__, message=str(error)))
        return None


def wall_clock_order(events):
    """Equality is allowed. Missing, invalid and decreasing stamps are distinct.

    Check all observed adjacent stamps, including across unknown values; a
    missing timestamp cannot conceal a separately observed backwards step.
    """
    missing = []
    invalid = []
    backwards = []
    previous = None
    for entry in events:
        value = entry["at"]
        name = entry["event"]
        if value is None:
            missing.append(name)
            continue
        try:
            if not isinstance(value, str):
                raise TypeError("timestamp must be ISO string")
            stamp = datetime.fromisoformat(value)
            if stamp.tzinfo is None:
                raise ValueError("timezone absent")
        except (ValueError, TypeError) as error:
            invalid.append(dict(event=name, reason=str(error)))
            continue
        if previous is not None and stamp < previous[1]:
            backwards.append(dict(before=previous[0], after=name))
        previous = (name, stamp)
    state = "invalid" if invalid or backwards else "unknown" if missing else "valid"
    return dict(
        state=state,
        valid=state == "valid",
        missing_events=missing,
        invalid_values=invalid,
        backwards=backwards,
        evidence="observed wall-clock order only; does not prove UTC accuracy or detect unobserved clock steps",
    )


def monotonic_duration(start, end):
    """Elapsed-operation evidence only, not a replacement for UTC timestamps."""
    if start is None or end is None:
        state = "unknown"
        duration = None
    elif type(start) is not int or type(end) is not int:
        state = "invalid"
        duration = None
    else:
        duration = end - start
        state = "valid" if duration >= 0 else "invalid"
    return dict(
        state=state,
        start_ns=start,
        end_ns=end,
        elapsed_ns=duration,
        interval="query invocation through SDK iteration return; excludes validation and persistence",
        evidence="elapsed monotonic clock; cannot supply, repair or validate wall-clock UTC",
    )
