"""Public all-market membership and bounded recent-minute acquisition."""

from datetime import date, datetime, timedelta
import json
import math
from pathlib import Path
import re
import threading
from urllib.parse import urlencode

from ashare_data.providers.cmes import normalize_security, native_security
from ashare_data.market.increment import minute_rows, minute_window
from ashare_data.providers.daily_sources import PublicHTTP, SourceBlocked
from ashare_data.storage.market_release import atomic_write, canonical

UNIVERSE_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center."
MINUTE_URL = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?"


class IncrementHTTP(PublicHTTP):
    """Retain source denial across scheduled runs; never auto-clear an access block."""

    def __init__(self, root, state_root, **kwargs):
        super().__init__(root, **kwargs)
        self.block_path = Path(state_root) / "blocked-http-sources.json"
        self.block_save_lock = threading.Lock()
        if self.block_path.exists():
            self.blocked.update(json.loads(self.block_path.read_bytes()))

    def get(self, url, referer=None):
        try:
            return super().get(url, referer)
        except SourceBlocked:
            with self.block_save_lock, self.lock:
                atomic_write(self.block_path, canonical(self.blocked))
            raise


def equity_security(value):
    """Explicit venue plus A-equity prefix, including STAR, ChiNext and Beijing."""
    match = re.fullmatch(r"(sh|sz|bj)\.?([0-9]{6})", str(value).lower())
    if not match:
        return None
    venue, code = match.groups()
    prefixes = {"sh": ("60", "68"), "sz": ("00", "30"), "bj": ("43", "83", "87", "88", "92")}
    return normalize_security(venue.upper() + "." + code) if code.startswith(prefixes[venue]) else None


def seed_files(directory):
    """Names only: retain downloaded historical identities without scanning their bars."""
    values = []
    for path in sorted(Path(directory).iterdir()):
        if path.is_file() and path.suffix.lower() in {".zip", ".csv"}:
            security = equity_security(path.stem)
            if security:
                values.append(dict(security=security, source="cmes_file_inventory",
                                   locator=path.name, membership="historical_observed"))
    return values


def universe(http):
    """Freeze a paginated current A-share list, including the source's BJ membership."""
    count_url = UNIVERSE_URL + "getHQNodeStockCount?node=hs_a"
    body, receipts = http.get(count_url, "https://finance.sina.com.cn/")
    total = int(json.loads(body))
    if not 1 <= total <= 20000:
        raise ValueError("unexpected all-market membership count")
    rows, seen, excluded = [], set(), []
    for page in range(1, math.ceil(total / 80) + 1):
        url = UNIVERSE_URL + f"getHQNodeData?node=hs_a&sort=symbol&asc=1&page={page}&num=80"
        body, recs = http.get(url, "https://finance.sina.com.cn/")
        receipts.extend(recs)
        values = json.loads(body)
        if not isinstance(values, list) or len(values) != min(80, total - (page - 1) * 80):
            raise ValueError("incomplete all-market membership page")
        for index, row in enumerate(values):
            symbol = row["symbol"]
            if symbol in seen:
                raise ValueError("duplicate member across source pages; retry a new snapshot")
            seen.add(symbol)
            security = equity_security(symbol)
            if security is None:
                excluded.append(dict(symbol=symbol, reason="not_supported_A_equity_identifier"))
            else:
                rows.append(dict(security=security, name=row["name"], source="sina_current_membership",
                                 source_symbol=symbol, source_asset_id=recs[-1]["sha256"],
                                 source_row=index, membership="current", observed_at=recs[-1]["received_at"]))
    if len(seen) != total:
        raise ValueError("all-market membership count differs")
    return dict(rows=rows, receipts=receipts, source_count=total, excluded=excluded,
                coverage="source-declared current A-share list; historical membership not inferred")


def fetch_minute(http, security, window):
    window = minute_window(window)
    symbol = native_security(security).replace(".", "").lower()
    url = MINUTE_URL + urlencode(dict(symbol=symbol, scale=1, ma="no", datalen=window))
    body, receipts = http.get(url, "https://finance.sina.com.cn/")
    return body, receipts[-1]


def session_labels(day):
    """Modern regular-session labels; extra source labels are retained separately.

    This assesses observed label coverage, not universal bar-boundary semantics or
    intraday suspension. Fewer labels remain a visible source/session limitation.
    """
    labels = set()
    for start, end in (("09:31", "11:30"), ("13:01", "15:00")):
        cursor = datetime.fromisoformat(day + "T" + start)
        stop = datetime.fromisoformat(day + "T" + end)
        while cursor <= stop:
            labels.add(cursor.strftime("%Y-%m-%d %H:%M:%S"))
            cursor += timedelta(minutes=1)
    return labels


def assess_minutes(body, day, window, *, trade_status=None):
    start = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    rows = minute_rows(body, after_date=start, end_date=day, max_rows=window)
    for row in rows:
        if row.get("reason") == "after_frozen_cutoff":
            row.update(disposition="resolved_unused", reason="outside_requested_day")
    accepted = [r for r in rows if r["disposition"] == "adopted"]
    labels = {r["native_label"] for r in accepted}
    native = json.loads(body) or []
    source_labels = sorted(r.get("day", "") for r in native if isinstance(r, dict) and r.get("day"))
    missing = sorted(session_labels(day) - labels)
    rejected = sum(r["disposition"] in {"quarantined", "format_rejected"} for r in rows)
    state = "complete" if not missing and not rejected else "partial"
    if not labels:
        state = "suspended" if trade_status == "0" else "empty_unknown"
    if source_labels and source_labels[0][:10] > day:
        state = "window_expired"
    if not labels and source_labels and source_labels[-1][:10] < day:
        state = "not_yet_available"
    suspended = not labels and trade_status == "0"
    if suspended:
        state, missing = "suspended", []
    if rejected:
        state = "quality_gap"
    counts = {key: sum(r["disposition"] == key for r in rows) for key in
              ("adopted", "equal_duplicate", "resolved_unused", "quarantined", "format_rejected")}
    return rows, dict(state=state, observed_count=len(labels), expected_regular_labels=0 if suspended else 240,
                      missing_regular_labels=len(missing), missing_labels=missing,
                      extra_labels=sorted(labels - session_labels(day)),
                      first_source_label=source_labels[0] if source_labels else None,
                      last_source_label=source_labels[-1] if source_labels else None,
                      requested_window=window, returned_rows=len(native),
                      source_window_days_not_guaranteed=True, trade_status=trade_status,
                      interval_semantics="source labels preserved; boundary semantics unverified",
                      input_rows=len(rows), **counts)
