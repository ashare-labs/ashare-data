"""Offline, fixed-snapshot queries with explicit evidence and missingness."""
from __future__ import annotations

import json
from datetime import date, timedelta

from .model import (
    INSTRUMENT_SET_FIELDS, DataError, day, digest, request_range, require, timestamp,
)
from .storage import duck

POLICIES = {"observed": {"observed"}, "synthetic": {"synthetic"},
            "research": {"observed", "inferred", "unverified"}}


class DataView:
    def __init__(self, loaded, *, snapshot_id, manifest=None):
        self.snapshot_id, self.manifest, self.loaded = snapshot_id, manifest, loaded
        self._conn = duck()
        try:
            paths = [str(x["parquet"]) for x in loaded]
            self._conn.execute("CREATE TABLE bars AS SELECT * FROM read_parquet(?)", [paths])
            self._conn.execute("SET enable_external_access=false")
            self._calendar, self._universes, self._metadata_conflicts = {}, {}, []
            for x in loaded:
                for name, key, target in (("calendar", "date", self._calendar),
                                          ("instrument_sets", "effective_date", self._universes)):
                    for value in x["bundle"].get(name, []):
                        k = value[key]
                        if k in target and digest(target[k]) != digest(value):
                            self._metadata_conflicts.append({"dataset": name, "key": k})
                        else:
                            target[k] = value
        except Exception:
            self.close()
            raise

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _records(self, sql, params=None):
        require(self._conn is not None, "VIEW_CLOSED", "快照查询对象已经关闭")
        cursor = self._conn.execute(sql, params or [])
        names = [x[0] for x in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def conflicts(self):
        bars = self._records("SELECT symbol,bar_start,count(*) AS count FROM bars GROUP BY symbol,bar_start HAVING count(*)>1 ORDER BY symbol,bar_start")
        return self._metadata_conflicts + bars

    def quality_counts(self):
        return self._records("SELECT quality,count(*) AS count FROM bars GROUP BY quality ORDER BY quality")

    def _rows(self, syms, a, b):
        marks = ",".join("?" for _ in syms)
        return self._records(f"SELECT * FROM bars WHERE symbol IN ({marks}) AND bar_start>=? AND bar_start<? ORDER BY symbol,bar_start,source_id",
                             [*syms, a.isoformat(), b.isoformat()])

    def _coverage(self, syms, a, b, quality, as_of):
        require(quality in POLICIES, "INVALID_QUALITY", "未知查询质量策略")
        cutoff = timestamp(as_of) if as_of is not None else None
        rows = self._rows(syms, a, b)
        expected, unknown = set(), []
        d = a.date()
        while d <= (b - timedelta(microseconds=1)).date():
            k = d.isoformat()
            cal = self._calendar.get(k)
            if cal is None or (cutoff and timestamp(cal["available_at"]) > cutoff):
                unknown.append(k)
            else:
                for start, end in cal["sessions"]:
                    t, stop = max(a, timestamp(start)), min(b, timestamp(end))
                    while t < stop:
                        expected.update((s, t.isoformat()) for s in syms)
                        t += timedelta(minutes=1)
            d += timedelta(days=1)
        accepted, excluded, all_keys, duplicate_keys = [], [], set(), set()
        for row in rows:
            key = (row["symbol"], row["bar_start"])
            if key in all_keys:
                duplicate_keys.add(key)
            all_keys.add(key)
            reasons = []
            if row["quality"] not in POLICIES[quality]:
                reasons.append("quality")
            if cutoff and (row["available_at"] is None or timestamp(row["available_at"]) > cutoff):
                reasons.append("visibility_unknown_or_late")
            if cutoff and timestamp(row["bar_end"]) > cutoff:
                reasons.append("not_closed")
            if reasons:
                excluded.append({"symbol": key[0], "bar_start": key[1], "quality": row["quality"], "reasons": reasons})
            else:
                accepted.append(row)
        accepted_keys = {(r["symbol"], r["bar_start"]) for r in accepted}
        missing = sorted(expected - accepted_keys)
        unexpected = sorted(all_keys - expected)
        # With no expected minutes, an evidenced closed-day/out-of-session query can be empty.
        complete = not unknown and not missing and not duplicate_keys and not unexpected and not self._metadata_conflicts
        report = {"snapshot_id": self.snapshot_id, "quality_policy": quality,
                  "as_of": cutoff.isoformat() if cutoff else None, "expected": len(expected),
                  "present": len(all_keys), "accepted": len(accepted), "missing_count": len(missing),
                  "missing": [{"symbol": s, "bar_start": t} for s, t in missing],
                  "excluded": excluded, "unknown_calendar_dates": unknown,
                  "unexpected": [{"symbol": s, "bar_start": t} for s, t in unexpected],
                  "duplicate_keys": [{"symbol": s, "bar_start": t} for s, t in sorted(duplicate_keys)],
                  "complete": complete,
                  "scope": {"symbols": syms, "start": a.isoformat(), "end": b.isoformat()},
                  "meaning": "声明时段内的分钟槽完整性；不证明全市场覆盖、真实OHLC或供应商语义"}
        return report, accepted

    def coverage(self, symbols, start, end, *, quality="observed", as_of=None):
        syms, a, b = request_range(symbols, start, end)
        report, _ = self._coverage(syms, a, b, quality, as_of)
        return report

    def bars(self, symbols, start, end, *, quality="observed", strict=True, as_of=None,
             adjustment=None, frequency="1m"):
        require(adjustment is None, "UNSUPPORTED", "公司行动与因子不足，复权未支持")
        require(frequency == "1m", "UNSUPPORTED", "首版只支持 1m，不隐式聚合")
        require(type(strict) is bool, "INVALID_REQUEST", "strict 必须是 bool")
        syms, a, b = request_range(symbols, start, end)
        report, rows = self._coverage(syms, a, b, quality, as_of)
        if strict and not report["complete"]:
            raise DataError("COVERAGE_GAP", "覆盖未知、缺口、冲突或质量/可见性不足", report)
        require(not report["duplicate_keys"] and not self._metadata_conflicts, "SOURCE_CONFLICT",
                "多来源冲突须重新显式选择批次，不能静默挑选来源")
        for row in rows:
            row["source_fields"] = json.loads(row["source_fields"])
        return {"snapshot_id": self.snapshot_id, "rows": rows, "coverage": report,
                "units": {"price": "CNY", "volume": "shares", "amount": "CNY"},
                "frequency": "1m", "adjustment": None, "timezone": "Asia/Shanghai"}

    def calendar(self, start_date, end_date):
        a, b = date.fromisoformat(day(start_date)), date.fromisoformat(day(end_date))
        require(a <= b and (b-a).days <= 3660, "INVALID_RANGE", "日历范围须有效且不超过十年")
        require(not self._metadata_conflicts, "SOURCE_CONFLICT", "元数据来源冲突")
        rows, missing = [], []
        while a <= b:
            k = a.isoformat()
            if k in self._calendar:
                rows.append(self._calendar[k])
            else:
                missing.append(k)
            a += timedelta(days=1)
        require(not missing, "CALENDAR_UNKNOWN", "无日历证据的日期不能推断为休市", {"dates": missing})
        return {"snapshot_id": self.snapshot_id, "rows": json.loads(json.dumps(rows))}

    def sessions(self, date):
        return self.calendar(date, date)

    def instruments(self, *, as_of):
        cutoff = timestamp(as_of)
        require(not self._metadata_conflicts, "SOURCE_CONFLICT", "元数据来源冲突")
        item = self._universes.get(cutoff.date().isoformat())
        require(item is not None, "PIT_UNAVAILABLE", "无该日证券集合证据；禁止用今日股票池回填")
        require(timestamp(item["available_at"]) <= cutoff, "PIT_UNAVAILABLE", "证券集合尚不可见")
        source_fields = json.loads(json.dumps(item))
        # Project declared data fields only. Never flatten arbitrary source metadata
        # into the system envelope, even when reading an older published snapshot.
        return {"snapshot_id": self.snapshot_id, "as_of": cutoff.isoformat(),
                **{field: source_fields[field] for field in INSTRUMENT_SET_FIELDS},
                "source_fields": source_fields}

    def lineage(self):
        return {"snapshot_id": self.snapshot_id, "batches": [
            {"id": x["id"], "parquet_hash": x["parquet_hash"], "row_count": x["row_count"],
             "source": json.loads(json.dumps(x["bundle"]["source"]))} for x in self.loaded]}

    def quality(self):
        return {"snapshot_id": self.snapshot_id, "counts": self.quality_counts(),
                "validation": json.loads(json.dumps(self.manifest["report"])) if self.manifest else None}

    def corporate_actions(self, *args, **kwargs):
        raise DataError("UNSUPPORTED", "公司行动尚未导入；不能将未知解释成无事件")

    def adjustment_factors(self, *args, **kwargs):
        raise DataError("UNSUPPORTED", "因子与基准日证据不足，不提供复权")
