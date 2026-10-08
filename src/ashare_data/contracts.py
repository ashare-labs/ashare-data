"""Bounded, evidence-declared source label grids. No universal 240-bar assumption.

This validates supplied assertions, not the truth of the evidence they cite. Real
Sina label/session/status evidence is not bundled or inferred from returned bars.
"""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta

from .model import DataError, TZ, canonical, day, digest, nonempty, require, symbol, timestamp


def label_time(value):
    if isinstance(value, str) and len(value) == 10:
        return datetime.combine(datetime.fromisoformat(day(value)).date(), time.min, TZ)
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    require(isinstance(value, datetime), "INVALID_TIME", "标签必须是日期或时间")
    return value.replace(tzinfo=TZ) if value.tzinfo is None else value.astimezone(TZ)


class CoverageContract:
    """Immutable per-provider/frequency calendar, session mapping and status facts."""

    def __init__(self, data):
        try:
            # Detach caller-owned containers. No input object is retained or returned.
            data = json.loads(canonical(data))
            require(type(data["schema_version"]) is int and data["schema_version"] in {1, 2},
                    "INVALID_CONTRACT", "支持覆盖契约 v1 整日状态和 v2 区间状态")
            for key in ("id", "provider", "trading_scope", "evidence"):
                nonempty(data[key], key)
            require(data["frequency"] in {"daily", "1m", "5m"}, "INVALID_CONTRACT", "未知频率")
            require(data["evidence_kind"] in {"synthetic", "observed"}, "INVALID_CONTRACT", "须标明合成或实测证据")
            require(data["label_semantics"] in {"verified", "unverified"}, "INVALID_CONTRACT", "须标明标签语义")
            require(isinstance(data["calendar"], list) and 0 < len(data["calendar"]) <= 366,
                    "INVALID_CONTRACT", "一次契约限定 1..366 个显式日期")
            require(isinstance(data["statuses"], list) and len(data["statuses"]) <= 3660,
                    "INVALID_CONTRACT", "状态记录过多或格式错误")
            self._days, self._status, self._slots = {}, {}, {}
            self._intervals = {}
            for cal in data["calendar"]:
                d = day(cal["date"])
                require(d not in self._days and type(cal["is_open"]) is bool,
                        "INVALID_CONTRACT", "重复日期或非法开市状态")
                nonempty(cal["evidence"], "calendar.evidence")
                timestamp(cal["available_at"])
                require(isinstance(cal["sessions"], list) and bool(cal["sessions"]) == cal["is_open"],
                        "INVALID_CONTRACT", "开市须有时段，休市不得有时段")
                slots, previous_end, ids = [], None, set()
                for session in cal["sessions"]:
                    nonempty(session["id"], "session.id")
                    require(session["id"] not in ids, "INVALID_CONTRACT", "重复 session id")
                    ids.add(session["id"])
                    a, b = timestamp(session["start"], minute=True), timestamp(session["end"], minute=True)
                    require(a < b and a.date().isoformat() == b.date().isoformat() == d
                            and (previous_end is None or a >= previous_end), "INVALID_CONTRACT", "时段跨日、无序或重叠")
                    previous_end = b
                    if data["frequency"] == "daily":
                        continue
                    if session["mapping"] == "end_labels":
                        step = timedelta(minutes=int(data["frequency"][:-1]))
                        require((b-a) % step == timedelta(0), "INVALID_CONTRACT", "规则时段不能整除周期")
                        t = a
                        while t < b:
                            slots.append({"label": t+step, "start": t, "end": t+step, "session": session["id"]})
                            t += step
                    else:
                        require(session["mapping"] == "explicit" and session["slots"],
                                "INVALID_CONTRACT", "特殊竞价标签必须显式声明，不能补造")
                        last = a
                        for slot in session["slots"]:
                            x, y = timestamp(slot["start"], minute=True), timestamp(slot["end"], minute=True)
                            label = timestamp(slot["label"], minute=True)
                            require(last == x < y <= b and a <= label <= b,
                                    "INVALID_CONTRACT", "显式映射须无缝覆盖声明时段，标签须在时段内")
                            slots.append({"label": label, "start": x, "end": y, "session": session["id"]})
                            last = y
                        require(last == b, "INVALID_CONTRACT", "显式映射缺少时段尾部")
                if data["frequency"] == "daily" and cal["is_open"]:
                    slots = [{"label": label_time(d), "start": timestamp(cal["sessions"][0]["start"]),
                              "end": timestamp(cal["sessions"][-1]["end"]), "session": "declared_daily_scope"}]
                require(len({s["label"] for s in slots}) == len(slots), "INVALID_CONTRACT", "标签重复")
                self._days[d], self._slots[d] = cal, slots
            for state in data["statuses"]:
                security = symbol(state["security"])
                timestamp(state["available_at"])
                nonempty(state["evidence"], "status.evidence")
                require(state["state"] in {"trading", "suspended", "unknown"},
                        "INVALID_CONTRACT", "未知状态；缺失不可视作正常交易")
                if data["schema_version"] == 1:
                    key = (security, day(state["date"]))
                    require(key not in self._status, "INVALID_CONTRACT", "重复证券日状态")
                    self._status[key] = state
                    a = label_time(state["date"])
                    b = a + timedelta(days=1)
                else:
                    require("date" not in state, "INVALID_CONTRACT", "v2 状态使用生效区间，不混用整日 date")
                    a, b = timestamp(state["effective_start"]), timestamp(state["effective_end"])
                    require(a < b and b-a <= timedelta(days=366), "INVALID_CONTRACT", "状态区间须为正且不超过366天")
                self._intervals.setdefault(security, []).append((a, b, state))
            for intervals in self._intervals.values():
                intervals.sort(key=lambda entry: entry[0])
                require(all(left[1] <= right[0] for left, right in zip(intervals, intervals[1:])),
                        "INVALID_CONTRACT", "证券状态区间不得重叠；修订须建立新契约，不能静默择一")
            self._data, self.contract_id = data, digest(data)
        except (KeyError, TypeError, ValueError) as exc:
            raise DataError("INVALID_CONTRACT", "覆盖契约字段缺失或格式错误") from exc

    @property
    def frequency(self):
        return self._data["frequency"]

    def _state_at(self, security, clock, cutoff):
        for a, b, fact in self._intervals.get(security, []):
            if a <= clock < b:
                if timestamp(fact["available_at"]) <= cutoff and fact["state"] != "unknown":
                    return fact["state"], {"effective_start": a.isoformat(), "effective_end": b.isoformat(),
                                          "available_at": fact["available_at"], "evidence": fact["evidence"]}
                break
        return "unknown", None

    def _state_span(self, security, start, end, cutoff):
        """Cover the full half-open interval; unknown facts never bridge gaps."""
        cursor, states = start, set()
        for a, b, fact in self._intervals.get(security, []):
            if b <= cursor or a >= end:
                continue
            if a > cursor or fact["state"] == "unknown" or (cutoff and timestamp(fact["available_at"]) > cutoff):
                return "unknown"
            states.add(fact["state"])
            cursor = min(b, end)
            if cursor == end:
                return next(iter(states)) if len(states) == 1 else "mixed"
        return "unknown"

    def _slot_state(self, security, slot, cutoff):
        if self.frequency != "daily":
            return self._state_span(security, slot["start"], slot["end"], cutoff)
        # A daily aggregate may span known intraday halts, but unknown portions
        # of an active session cannot be filled by a later resumption fact.
        spans = self._days[slot["label"].date().isoformat()]["sessions"]
        states = [self._state_span(security, timestamp(s["start"]), timestamp(s["end"]), cutoff) for s in spans]
        if "unknown" in states:
            return "unknown"
        return "suspended" if set(states) == {"suspended"} else "trading"

    def trading_status(self, security, clock):
        """Offline, evidence-declared eligibility, separate from data freshness."""
        symbol(security)
        clock = timestamp(clock)
        cal = self._days.get(clock.date().isoformat())
        state, reason, session, evidence = "unknown", "CALENDAR_UNKNOWN", None, None
        if cal and timestamp(cal["available_at"]) <= clock:
            if not cal["is_open"]:
                state, reason = "closed_market", "MARKET_CLOSED"
            else:
                instrument, evidence = self._state_at(security, clock, clock)
                if instrument == "unknown":
                    reason = "TRADING_STATUS_UNKNOWN"
                elif instrument == "suspended":
                    state, reason = "suspended", "SUSPENDED"
                else:
                    spans = cal["sessions"]
                    session = next((s["id"] for s in spans if timestamp(s["start"]) <= clock < timestamp(s["end"])), None)
                    if session is not None:
                        state, reason = "tradable", None
                    elif timestamp(spans[0]["start"]) <= clock < timestamp(spans[-1]["end"]):
                        state, reason = "session_break", "SESSION_BREAK"
                    else:
                        state, reason = "closed_market", "MARKET_CLOSED"
        return {"state": state, "tradable": state == "tradable", "as_of": clock.isoformat(),
                "reasons": [reason] if reason else [], "session_id": session, "status_evidence": evidence,
                "calendar_evidence": cal["evidence"] if cal and timestamp(cal["available_at"]) <= clock else None,
                "contract_id": self.contract_id, "evidence_kind": self._data["evidence_kind"],
                "meaning": "按可见声明时段及证券状态判断；不证明成交、撮合、PIT或真实来源资质"}

    def _expected(self, security, start, end, cutoff=None):
        require(start <= end and (end-start).days <= 366, "INVALID_RANGE", "覆盖窗口须有效且不超过一年")
        expected, unknown = [], []
        d = start.date()
        while d <= end.date():
            key = d.isoformat()
            cal = self._days.get(key)
            if cal is None or (cutoff and timestamp(cal["available_at"]) > cutoff):
                unknown.append({"date": key, "code": "CALENDAR_UNKNOWN"})
            elif cal["is_open"]:
                for slot in self._slots[key]:
                    if start <= slot["label"] <= end:
                        state = self._slot_state(security, slot, cutoff)
                        if state in {"unknown", "mixed"}:
                            issue = {"date": key, "code": "PARTIAL_BAR_STATUS" if state == "mixed" else "TRADING_STATUS_UNKNOWN"}
                            if issue not in unknown:
                                unknown.append(issue)
                        elif state == "trading":
                            expected.append(slot)
            d += timedelta(days=1)
        return sorted(expected, key=lambda s: s["label"]), unknown

    def assess(self, security, labels, start, end, *, count=None, as_of=None):
        symbol(security)
        cutoff = timestamp(as_of) if as_of is not None else None
        start, end = label_time(start), label_time(end)
        slots, unknown = self._expected(security, start, end, cutoff)
        if self._data["label_semantics"] != "verified":
            unknown.append({"code": "SESSION_SCOPE_UNKNOWN"})
        if count is not None:
            require(type(count) is int and 0 < count <= 1000, "INVALID_COUNT", "无效 count")
            if cutoff:
                slots = [s for s in slots if s["end"] <= cutoff]
            if len(slots) < count:
                unknown.append({"code": "CALENDAR_COVERAGE", "required_count": count})
            slots = slots[-count:]
        expected = {s["label"] for s in slots}
        labels = [label_time(t) for t in labels]
        selected = [t for t in labels if start <= t <= end]
        outside = [t.isoformat() for t in labels if not start <= t <= end]
        if count is not None:
            selected = selected[-count:]
        present = set(selected)
        missing = []
        for t in sorted(expected-present):
            peers = sorted(x for x in present & expected if x.date() == t.date())
            kind = "whole_day" if not peers else "head" if t < peers[0] else "tail" if t > peers[-1] else "internal"
            missing.append({"label": t.isoformat(), "kind": kind})
        unexpected = [t.isoformat() for t in sorted(present-expected)]
        duplicates = len(selected) != len(present)
        complete = not unknown and not missing and not unexpected and not duplicates
        empty_state = None
        if complete and not expected:
            calendars = [cal for d, cal in self._days.items() if start.date().isoformat() <= d <= end.date().isoformat()]
            candidate_slots = [s for d, slots in self._slots.items() for s in slots if start <= s["label"] <= end]
            if calendars and all(not cal["is_open"] for cal in calendars):
                empty_state = "closed_market"
            elif candidate_slots and all(self._slot_state(security, s, cutoff) == "suspended" for s in candidate_slots):
                empty_state = "suspended"
            else:
                empty_state = "session_break"
        return {"contract_id": self.contract_id, "provider": self._data["provider"],
                "frequency": self.frequency, "trading_scope": self._data["trading_scope"],
                "evidence_kind": self._data["evidence_kind"], "evidence": self._data["evidence"],
                "status": "unknown" if unknown else empty_state or ("complete" if complete else "incomplete"),
                "complete": complete, "grid_complete": complete, "trade_totals_verified": False,
                "tradable": bool(expected) if not unknown else None,
                "tradable_basis": "queried range contains declared trading slots; not current-time permission",
                "trading_status": self.trading_status(security, cutoff) if cutoff else None,
                "expected_count": len(expected), "present_count": len(present),
                "missing": missing, "unexpected": unexpected, "duplicates": duplicates, "unknown": unknown,
                "out_of_request_labels": outside, "assessment_scope": "requested inclusive source-label range",
                "range": {"start": start.isoformat(), "end": end.isoformat(), "as_of": cutoff.isoformat() if cutoff else None},
                "meaning": "仅证据声明的标签槽覆盖；不认证证据真伪、成交全量、PIT或供应商最终值"}

    def bounds(self, label, *, as_of=None):
        t = label_time(label)
        if self._data["label_semantics"] != "verified":
            return None
        cal = self._days.get(t.date().isoformat())
        if cal is None or (as_of is not None and timestamp(cal["available_at"]) > timestamp(as_of)):
            return None
        for slot in self._slots.get(t.date().isoformat(), []):
            if slot["label"] == t:
                return {"bar_start": slot["start"].isoformat(), "bar_end": slot["end"].isoformat(),
                        "basis": "declared_contract", "contract_id": self.contract_id,
                        "evidence_kind": self._data["evidence_kind"]}
        return None

    def _latest_expected(self, security, clock):
        if self._data["label_semantics"] != "verified":
            return None, "SESSION_SCOPE_UNKNOWN"
        d, first = clock.date(), label_time(min(self._days)).date()
        # Only facts at/after the newest eligible slot can change the latest
        # watermark. Unknown old history must not hide a known resumed session.
        while d >= first:
            key = d.isoformat()
            cal = self._days.get(key)
            if cal is None or timestamp(cal["available_at"]) > clock:
                return None, "CALENDAR_UNKNOWN"
            for slot in reversed(self._slots[key]):
                if slot["end"] > clock:
                    continue
                state = self._slot_state(security, slot, clock)
                if state in {"unknown", "mixed"}:
                    return None, "PARTIAL_BAR_STATUS" if state == "mixed" else "TRADING_STATUS_UNKNOWN"
                if state == "trading":
                    return slot["label"], None
            d -= timedelta(days=1)
        return None, "NO_CLOSED_TRADABLE_BAR"

    def freshness(self, security, labels, clock):
        symbol(security)
        clock = timestamp(clock)
        decision = self.trading_status(security, clock)
        expected, reason = self._latest_expected(security, clock)
        eligible, future = [], []
        for value in labels:
            label = label_time(value)
            bounds = self.bounds(value, as_of=clock)
            if label > clock or (bounds and timestamp(bounds["bar_end"]) > clock):
                future.append(label.isoformat())
            else:
                eligible.append(label)
        actual = max(eligible, default=None)
        if future and actual is None:
            watermark, reason = "unknown", "FUTURE_SOURCE_LABEL"
        else:
            watermark = "unknown" if expected is None else "fresh" if actual == expected else "stale"
        # Retain stale diagnostics outside market hours. A fresh watermark alone
        # cannot permit paper use during a break, closure or suspension.
        if watermark == "stale":
            status = "stale"
        elif decision["state"] in {"suspended", "closed_market", "session_break"}:
            status = decision["state"]
        elif decision["state"] == "unknown" or watermark == "unknown":
            status = "unknown"
        else:
            status = "fresh"
        return {"status": status, "source_watermark_status": watermark,
                "expected_latest_label": expected.isoformat() if expected else None,
                "last_eligible_label": actual.isoformat() if actual else None,
                "admissible": status == "fresh", "trading_status": decision,
                "future_source_labels": future, "reasons": ([reason] if reason else []) + decision["reasons"],
                "contract_id": self.contract_id}


def unknown_coverage():
    return {"status": "unknown", "complete": False, "grid_complete": False,
            "trade_totals_verified": False, "expected_count": None,
            "unknown": [{"code": "CALENDAR_UNKNOWN"}, {"code": "SESSION_SCOPE_UNKNOWN"}, {"code": "TRADING_STATUS_UNKNOWN"}],
            "meaning": "缺来源标签映射、版本化时段或证券状态，不能从返回根数推算全量覆盖"}


def data_age(raw, chosen, meta, clock, *, daily=False):
    clock = timestamp(clock)
    last = label_time(raw[-1]["day"])
    returned = label_time(chosen[-1]["day"]) if chosen else None
    return {"last_source_label": raw[-1]["day"], "last_returned_label": chosen[-1]["day"] if chosen else None,
            "source_label_age_seconds": (clock-last).total_seconds(),
            "returned_label_age_seconds": (clock-returned).total_seconds() if returned else None,
            "response_observed_at": meta["observed_at"],
            "http_observation_age_seconds": (clock-timestamp(meta["observed_at"])).total_seconds(),
            "age_basis": "daily source date at midnight; not publication time" if daily else "source label; not vendor publication time",
            "status": "unknown", "reason": "仅标签年龄不构成交易日历/停牌感知的新鲜度保证"}
