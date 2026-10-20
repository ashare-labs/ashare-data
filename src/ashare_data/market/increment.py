"""Exact, bounded normalization for recent release increments.

Sources retain their native labels and values. These functions do not infer
exchange completeness, live execution permission, or historical availability.
"""

from datetime import datetime
from decimal import Decimal, InvalidOperation, Context
from zoneinfo import ZoneInfo
import json

from ashare_data.storage.market_release import FIELDS

NATIVE_FIELDS = ("open", "high", "low", "close", "volume", "amount")
MAPPING_VERSION = "recent-source-observations.v1"
MINUTE_SERIES = 6


def exact_number(value, *, volume=False, nullable=False):
    if value is None:
        if nullable:
            return None
        raise ValueError("missing_numeric_value")
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        # This source declares decimal strings. Do not round a float into them.
        raise ValueError("unexpected_numeric_representation")
    try:
        number = Decimal(value)
        if not number.is_finite():
            raise ValueError("nonfinite_numeric_value")
        if volume:
            if number != number.to_integral_value() or not 0 <= number <= 2**63 - 1:
                raise ValueError("nonintegral_or_out_of_range_volume")
            return int(number)
        # DECIMAL(38,12) is accepted only with an exact round trip.
        sign, digits, exponent = number.as_tuple()
        fractional = max(-exponent, 0)
        if fractional > 12 and number != number.quantize(
            Decimal("0.000000000001"), context=Context(prec=50)
        ):
            raise ValueError("decimal_precision_not_lossless")
        if number.copy_abs() >= Decimal(10) ** 26:
            raise ValueError("decimal_out_of_range")
        return number
    except InvalidOperation as error:
        raise ValueError("invalid_decimal") from error


def normalize_values(row):
    values = {
        target: exact_number(
            row.get(native), volume=native == "volume", nullable=native in ("volume", "amount")
        )
        for native, target in zip(NATIVE_FIELDS, FIELDS)
    }
    opening, high, low, close = [values[k] for k in FIELDS[:4]]
    if not 0 < low <= min(opening, close) <= max(opening, close) <= high:
        raise ValueError("invalid_ohlc")
    if values["turnover_cny"] is not None and values["turnover_cny"] < 0:
        raise ValueError("negative_amount")
    return values


def minute_rows(body, *, after_date, end_date):
    """Return every input row with an exclusive destination and original locator.

    Older response-window rows are explicitly out of this append-only increment.
    Same-label conflicts quarantine the full label group, never last-write-win.
    """
    raw = json.loads(body)
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 1023:
        raise ValueError("invalid_bounded_minute_envelope")
    result = []
    groups = {}
    for number, row in enumerate(raw):
        item = dict(source_row=number, native=row)
        result.append(item)
        try:
            label = row["day"]
            stamp = datetime.strptime(label, "%Y-%m-%d %H:%M:%S")
            if stamp.strftime("%Y-%m-%d %H:%M:%S") != label or stamp.second:
                raise ValueError("invalid_native_minute_label")
            day = stamp.date().isoformat()
            item.update(
                trade_date=day,
                label_at=stamp.replace(tzinfo=ZoneInfo("Asia/Shanghai")),
                native_label=label,
            )
        except (KeyError, TypeError, ValueError):
            item.update(disposition="format_rejected", reason="invalid_native_minute_label")
            continue
        if day <= after_date:
            item.update(disposition="resolved_unused", reason="before_increment_window")
            continue
        if day > end_date:
            item.update(disposition="quarantined", reason="after_frozen_cutoff")
            continue
        groups.setdefault(label, []).append(item)
        try:
            item.update(normalize_values(row))
        except (TypeError, ValueError) as error:
            item.update(disposition="quarantined", reason=str(error))
            continue
        item.update(disposition="adopted", reason="new_whole_security_day_source")
    for group in groups.values():
        if len(group) > 1:
            if any(item["disposition"] != "adopted" for item in group):
                for item in group:
                    item.update(
                        disposition="quarantined", reason="invalid_or_conflicting_native_label"
                    )
                continue
            values = {tuple(item.get(k) for k in FIELDS) for item in group}
            for item in group if len(values) > 1 else group[1:]:
                item.update(
                    disposition="quarantined" if len(values) > 1 else "equal_duplicate",
                    reason="conflicting_native_label" if len(values) > 1 else "equal_native_label",
                )
    return result
