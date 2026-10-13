"""Six explicit, offline research factors over independently supplied inputs.

Named definitions describe the frozen official catalog, not an equivalence claim.
Market ratios use exact decimal inputs with no hidden price rounding or seed.
"""

from copy import deepcopy
from datetime import date
from fractions import Fraction as F
import hashlib
import json
from pathlib import Path
import re

from .f1_batch import value_text
from .f1_financial import RESEARCH_MODE as PIT_RESEARCH_MODE
from .financial_pit import FinancialPIT

DEFINITION = "f2_six_representative_factors.research.v1"
MODE = "dated_source_snapshot_research"
FIELDS = (
    "BIAS5",
    "CCI20",
    "PSY",
    "quick_ratio",
    "debt_to_asset_ratio",
    "net_operating_cash_flow_coverage",
)
LOOKBACK = {"BIAS5": 5, "CCI20": 20, "PSY": 13}


class FactorInputMissing(ValueError):
    """A requested real input or mathematical denominator is unavailable."""


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("canonical ISO day required")
    return value


def _number(value):
    if value is None:
        raise FactorInputMissing("NUMERIC_INPUT_MISSING")
    if isinstance(value, bool):
        raise ValueError("boolean numeric input")
    try:
        return F(str(value))
    except (ValueError, ZeroDivisionError) as error:
        raise ValueError("finite decimal numeric input required") from error


def _names(value, label):
    result = [value] if isinstance(value, str) else list(value)
    if not result or any(not isinstance(x, str) for x in result) or len(set(result)) != len(result):
        raise ValueError("nonempty unique " + label + " required")
    return result


def _divide(a, b):
    if b == 0:
        raise FactorInputMissing("ZERO_DENOMINATOR")
    return a / b


def _market(item, sessions, windows, day, field, provider):
    if day not in sessions:
        raise FactorInputMissing("NOT_IN_EXPLICIT_SESSION_GRID")
    i, n = sessions.index(day), LOOKBACK[field]
    selected = sessions[max(0, i + 1 - n) : i + 1]
    if len(selected) != n or not any(a <= selected[0] <= day <= b for a, b in windows):
        raise FactorInputMissing("WARMUP_OR_CONTIGUOUS_WINDOW_MISSING")
    prices, effective = [], []
    for d in selected:
        if d not in item[provider]:
            raise FactorInputMissing("SOURCE_SESSION_MISSING:" + d)
        row = item[provider][d]
        if _number(row.get("volume")) <= 0 or _number(row.get("amount")) <= 0:
            raise FactorInputMissing("NORMAL_TRADING_INPUT_REQUIRED:" + d)
        opening, high, low, close = (_number(row.get(k)) for k in ("open", "high", "low", "close"))
        if not 0 < low <= min(opening, close) <= max(opening, close) <= high:
            raise ValueError("invalid OHLC")
        eligible = [r for r in item["factors"] if r["date"] <= d]
        if not eligible:
            raise FactorInputMissing("EFFECTIVE_FACTOR_MISSING:" + d)
        chosen = max(eligible, key=lambda r: r["date"])
        factor = _number(chosen["value"])
        if factor <= 0:
            raise ValueError("positive price factor required")
        effective.append(factor)
        prices.append({"high": high * factor, "low": low * factor, "close": close * factor})
    closes = [row["close"] for row in prices]
    if field == "BIAS5":
        result = (_divide(closes[-1], sum(closes, F()) / n) - 1) * 100
    elif field == "CCI20":
        typical = [(r["high"] + r["low"] + r["close"]) / 3 for r in prices]
        mean = sum(typical, F()) / n
        deviation = sum((abs(v - mean) for v in typical), F()) / n
        result = _divide(typical[-1] - mean, F(15, 1000) * deviation)
    else:
        result = F(sum(b > a for a, b in zip(closes[:-1], closes[1:], strict=True)) * 100, 12)
    return result, {
        "window_start": selected[0],
        "observed_sessions": n,
        "within_window_factor_changes": sum(
            a != b for a, b in zip(effective[:-1], effective[1:], strict=True)
        ),
        "input_quality": "independent_raw_prices_shared_Sina_effective_factors",
    }


def _latest(reports, kind, day):
    eligible = [r for r in reports if r["kind"] == kind and r["notice_date"] < day]
    if not eligible:
        raise FactorInputMissing("NO_VISIBLE_REPORT:" + kind)
    # Do not fall back to an older report merely because a newer field is null.
    return max(eligible, key=lambda r: r["period"])


def _quarter(reports, current, field, day):
    value = _number(current["values"].get(field))
    if current["period"].endswith("03-31"):
        return value, [current]
    previous_end = {"06-30": "03-31", "09-30": "06-30", "12-31": "09-30"}[current["period"][5:]]
    period = current["period"][:5] + previous_end
    previous = [
        r
        for r in reports
        if r["kind"] == current["kind"] and r["period"] == period and r["notice_date"] < day
    ]
    if not previous:
        raise FactorInputMissing("PREVIOUS_YTD_COMPONENT_MISSING:" + period)
    return value - _number(previous[0]["values"].get(field)), [current, previous[0]]


def _snapshot(item, day, field):
    reports = item["reports"]
    if field in {"quick_ratio", "debt_to_asset_ratio"}:
        current = _latest(reports, "balance", day)
        values, terms = current["values"], [current]
        if field == "quick_ratio":
            assets, inventory, denominator = (
                _number(values.get(k))
                for k in ("TOTAL_CURRENT_ASSETS", "INVENTORY", "TOTAL_CURRENT_LIAB")
            )
            if assets < 0 or inventory < 0:
                raise ValueError("invalid balance-sheet signs")
            numerator = assets - inventory
        else:
            numerator = _number(values.get("TOTAL_LIABILITIES"))
            denominator = _number(values.get("TOTAL_ASSETS"))
        if denominator < 0 or numerator < 0:
            raise ValueError("invalid balance-sheet signs")
    else:
        cash = _latest(reports, "cash", day)
        income = _latest(reports, "income", day)
        if cash["period"] != income["period"]:
            raise FactorInputMissing("LATEST_REPORT_PERIODS_DIFFER")
        numerator, terms = _quarter(reports, cash, "NETCASH_OPERATE", day)
        denominator, other = _quarter(reports, income, "PARENT_NETPROFIT", day)
        terms += other
        current = cash
    result = _divide(numerator, denominator)
    return result, {
        "period_end": current["period"],
        "input_quality": MODE,
        "negative_denominator": denominator < 0,
        "component_updates_after_cutoff": sum(r["update_date"] > day for r in terms),
        "financial_sources": [
            {k: r[k] for k in ("kind", "period", "notice_date", "update_date", "source_path")}
            for r in terms
        ],
    }


class FactorResearchView:
    """A rectangular offline factor query, with optional FinancialPIT cash-quality inputs.

    ``from_file`` checks captured bytes; neither that check nor a computed value
    certifies historical visibility, source independence, or JoinQuant equality.
    """

    def __init__(self, dataset, *, financial_pit=None):
        if (
            dataset.get("schema") != "ad.f2-independent-inputs.v1"
            or dataset.get("input_contains_JQ_values") is not False
            or dataset.get("raw_price_adjustment") != "none"
            or dataset.get("units")
            != {"price": "CNY/share", "volume": "shares", "financial": "consolidated_CNY"}
        ):
            raise ValueError("explicit independent raw units/schema required")
        if financial_pit is not None and not isinstance(financial_pit, FinancialPIT):
            raise ValueError("FinancialPIT instance required")
        self._data = deepcopy(dataset)
        self._pit = financial_pit
        self._verified = False
        self._digest = None
        sessions = self._data["sessions"]
        if not sessions or sessions != sorted(set(sessions)):
            raise ValueError("ordered unique explicit sessions required")
        for day in sessions:
            _day(day)
        for a, b in self._data["input_windows"]:
            if _day(a) > _day(b):
                raise ValueError("invalid input window")
        self._items = {}
        for item in self._data["items"]:
            security = item["security"]
            if not re.fullmatch(r"\d{6}\.X(SHG|SHE)", security) or security in self._items:
                raise ValueError("canonical unique security required")
            self._items[security] = item
            seen = set()
            for r in item["factors"]:
                key = _day(r["date"])
                if key in seen:
                    raise ValueError("duplicate effective factor date")
                seen.add(key)
            seen = set()
            for r in item["reports"]:
                key = r["kind"], _day(r["period"])
                if (
                    key in seen
                    or r["period"][5:] not in {"03-31", "06-30", "09-30", "12-31"}
                    or _day(r["notice_date"]) < r["period"]
                ):
                    raise ValueError("invalid or ambiguous financial snapshot")
                _day(r["update_date"])
                seen.add(key)

    @classmethod
    def from_file(cls, path, *, evidence_root, expected_sha256=None, financial_pit=None):
        content = Path(path).read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise ValueError("input projection hash mismatch")
        dataset = json.loads(content)
        result = cls(dataset, financial_pit=financial_pit)
        root, seen = Path(evidence_root).resolve(), set()
        if not dataset.get("sources"):
            raise ValueError("captured sources required")
        for source in dataset["sources"]:
            target = (root / source["path"]).resolve()
            if not target.is_relative_to(root) or target in seen:
                raise ValueError("unsafe or duplicate source path")
            seen.add(target)
            raw = target.read_bytes()
            if len(raw) != source["bytes"] or hashlib.sha256(raw).hexdigest() != source["sha256"]:
                raise ValueError("captured source hash/size mismatch")
        result._verified, result._digest = True, digest
        return result

    def _pit_quality(self, security, day):
        response = self._pit.query(
            [security],
            ["net_operate_cash_flow", "parent_net_profit"],
            as_of=day,
            basis="quarter",
            mode=PIT_RESEARCH_MODE,
        )
        if any(r["status"] != "ok" for r in response["rows"]):
            raise FactorInputMissing("FINANCIAL_PIT_INPUT_UNAVAILABLE")
        cash, profit = response["rows"]
        result = _divide(_number(cash["value"]), _number(profit["value"]))
        return result, {
            "period_end": cash["period_end"],
            "input_quality": "FinancialPIT_versioned_report_archive",
            "financial_pit_source_files_verified": response["source_files_verified"],
            "financial_pit_pack_sha256": response["pack_sha256"],
            "financial_sources": cash["terms"] + profit["terms"],
            "negative_denominator": _number(profit["value"]) < 0,
        }

    def query(self, securities, fields, *, dates, definition, mode, provider="sina"):
        if definition != DEFINITION or mode != MODE or provider not in {"sina", "tencent"}:
            raise ValueError("explicit supported research definition/mode/provider required")
        securities, fields, dates = (
            _names(securities, "securities"),
            _names(fields, "fields"),
            _names(dates, "dates"),
        )
        if set(fields) - set(FIELDS):
            raise ValueError("unsupported factor")
        if any(not re.fullmatch(r"\d{6}\.X(SHG|SHE)", s) for s in securities):
            raise ValueError("canonical security required")
        for d in dates:
            _day(d)
        rows = []
        for security in securities:
            for day in dates:
                for field in fields:
                    row = {
                        "security": security,
                        "date": day,
                        "field": field,
                        "value": None,
                        "status": "missing",
                        "definition": DEFINITION,
                        "mode": MODE,
                        "price_provider": provider,
                        "production_eligible": False,
                        "historical_pit_certified": False,
                        "jq_equivalent": False,
                    }
                    try:
                        # Explicit PIT inputs do not depend on a market/snapshot item.
                        if field == "net_operating_cash_flow_coverage" and self._pit is not None:
                            value, detail = self._pit_quality(security, day)
                        elif security not in self._items:
                            raise FactorInputMissing("SECURITY_INPUT_MISSING")
                        elif field in LOOKBACK:
                            value, detail = _market(
                                self._items[security],
                                self._data["sessions"],
                                self._data["input_windows"],
                                day,
                                field,
                                provider,
                            )
                        else:
                            value, detail = _snapshot(self._items[security], day, field)
                        row.update(value=value_text(value), status="ok", **detail)
                    except FactorInputMissing as error:
                        row["reason"] = str(error)
                    rows.append(row)
        return {
            "schema": "ad.f2-factor-query.v1",
            "rows": rows,
            "source_files_verified": self._verified,
            "dataset_sha256": self._digest,
            "production_eligible": False,
            "historical_pit_certified": False,
            "jq_equivalent": False,
        }
