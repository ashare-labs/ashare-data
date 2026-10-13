"""Synthetic safety contracts. Real-source acceptance uses the external sealed pack."""

from copy import deepcopy
from contextlib import contextmanager
import hashlib
import json
from urllib.parse import urlencode

import pytest

from ashare_data import DataError, MinuteEvidenceView, seal_minute_evidence
from ashare_data.historical_market import DATA_SCHEMA, SCHEMA
from ashare_data.live import KLINE_URL
from ashare_data.model import canonical, digest


@contextmanager
def error(code):
    with pytest.raises(DataError) as caught:
        yield
    assert caught.value.code == code


def fixture_files():
    files = {"evidence.txt": b"Synthetic test evidence; never a real market assertion"}

    def save(name, value):
        files[name] = canonical(value)

    def http(name, url):
        save(
            name + ".receipt.json",
            {
                "status": 200,
                "url": url,
                "bytes": len(files[name]),
                "sha256": hashlib.sha256(files[name]).hexdigest(),
                "captured_start": "2026-10-13T02:00:00+00:00",
                "captured_end": "2026-10-13T02:00:01+00:00",
            },
        )

    http("evidence.txt", "https://example.invalid/test")
    symbol, label = "000001.XSHE", "2026-10-09"

    def fact(value):
        return {
            "value": value,
            "status": "source_observed" if value is not None else "unavailable",
            "reason": "synthetic fixture",
            "historical_available_at": None,
            "source_refs": ["evidence.txt"],
        }

    state = {
        "security": symbol,
        "date": label,
        "listed": fact(True),
        "suspended": fact(False),
        "is_st": fact(False),
        "source_preclose": fact("9.7000"),
        "previous_close": fact("9.700"),
        "previous_session": "2026-10-08",
        "reference_price": fact(None),
        "upper_limit": fact(None),
        "lower_limit": fact(None),
        "rule": {"kind": "unknown", "depends_on_st": True, "source_refs": ["evidence.txt"]},
        "source_claimed_limits": {
            "validation": "source_missing",
            "execution_usable": False,
            "source_refs": ["evidence.txt"],
        },
        "source_quality": {"execution_permission": False, "source_refs": ["evidence.txt"]},
        "historical_available_at": None,
        "replay_effective_at": None,
        "intraday_halts_verified": False,
        "limit_model_candidate": {
            "upper_limit": "10.67",
            "lower_limit": "8.73",
            "ratio": "0.10",
            "tick": "0.01",
            "rounding": "ROUND_HALF_UP_minimum_one_tick",
            "assumed_reference": "9.700",
            "source_refs": ["evidence.txt"],
            "execution_usable": False,
            "dependencies_unproved": ["synthetic_reference_assumption"],
        },
    }
    prices = []
    for frequency, scale, labels in (
        ("1m", 1, [label + " 09:31:00", label + " 09:32:00", label + " 15:00:00"]),
        ("daily", 240, ["2026-10-08", label]),
    ):
        rows = [
            {
                "day": t,
                "open": "9.700",
                "high": "9.710",
                "low": "9.690",
                "close": "9.700",
                "volume": "3",
                **({"amount": "29.100001"} if scale == 1 else {}),
            }
            for t in labels
        ]
        body = frequency + ".json"
        save(body, rows)
        receipt = {
            "schema": 1,
            "url": KLINE_URL
            + "?"
            + urlencode({"symbol": "sz000001", "scale": scale, "ma": "no", "datalen": 1023}),
            "http_status": 200,
            "sha256": hashlib.sha256(files[body]).hexdigest(),
            "request_started_at": "2026-10-13T02:00:00+00:00",
            "observed_at": "2026-10-13T02:00:01+00:00",
            "supersedes_observation": None,
        }
        receipt["observation_id"] = digest(receipt)
        save(frequency + ".observation.json", receipt)
        prices.append(
            {
                "security": symbol,
                "frequency": frequency,
                "body": body,
                "receipt": frequency + ".observation.json",
            }
        )
    save(
        "calendar.json",
        {
            "data": [
                {"jyrq": d, "jybz": "0" if d in ("2026-10-10", "2026-10-11") else "1"}
                for d in (
                    "2026-10-08",
                    "2026-10-09",
                    "2026-10-10",
                    "2026-10-11",
                    "2026-10-12",
                    "2026-10-13",
                )
            ]
        },
    )
    http(
        "calendar.json",
        "https://www.szse.cn/api/report/exchange/onepersistenthour/monthList?month=2026-10",
    )
    queries = []
    for f10 in (False, True):
        report = "RPT_F10_DIVIDEND_MAIN" if f10 else "RPT_SHAREBONUS_DET"
        name = report + ".json"
        save(
            name,
            {
                "success": True,
                "result": {
                    "pages": 1,
                    "count": 1,
                    "data": [
                        {
                            "SECURITY_CODE": "000001",
                            "EQUITY_RECORD_DATE": None,
                            "EX_DIVIDEND_DATE": "2026-09-01 00:00:00",
                            **({"PAY_CASH_DATE": None} if f10 else {}),
                        }
                    ],
                },
            },
        )
        params = {
            "reportName": report,
            "columns": "ALL",
            "pageSize": "20",
            "pageNumber": "1",
            "sortColumns": "REPORT_TIME,NOTICE_DATE" if f10 else "REPORT_DATE",
            "sortTypes": "-1,-1" if f10 else "-1",
            "filter": '(SECUCODE="000001.SZ")' if f10 else '(SECURITY_CODE="000001")',
            "source": "HSF10" if f10 else "WEB",
            "client": "PC" if f10 else "WEB",
        }
        url = (
            "https://datacenter.eastmoney.com/securities/api/data/v1/get"
            if f10
            else "https://datacenter-web.eastmoney.com/api/data/v1/get"
        )
        http(name, url + "?" + urlencode(params))
        queries.append(
            {
                "security": symbol,
                "report": report,
                "pages": [{"body": name, "receipt": name + ".receipt.json"}],
            }
        )
    extension = {
        "schema": "ad.minute-source-evidence.v1",
        "request_id": "synthetic",
        "securities": [symbol],
        "dates": [label],
        "sessions": [["09:30", "11:30"], ["13:00", "14:57"]],
        "historical_pit": False,
        "execution_permission": False,
        "price_sources": prices,
        "calendar_range": ["2026-10-08", "2026-10-13"],
        "calendars": [
            {
                "market": "XSHE",
                "body": "calendar.json",
                "receipt": "calendar.json.receipt.json",
                "source_refs": ["calendar.json"],
            }
        ],
        "actions": {
            "start": "2026-10-08",
            "end": "2026-10-12",
            "absence_proved": False,
            "complete": False,
            "source_refs": ["evidence.txt"],
        },
        "action_queries": queries,
        "assessments": [
            {
                "gate_id": f"G{i:02d}",
                "status": "blocked",
                "owner": "test",
                "reason": "synthetic",
                "source_refs": ["evidence.txt"],
            }
            for i in range(12)
        ],
        "opening_comparisons": [
            {
                "security": symbol,
                "date": label,
                "timepoint_0930_volume": 1,
                "timepoint_0931_volume": 2,
                "source_refs": ["evidence.txt"],
            }
        ],
    }
    data = {
        "schema": DATA_SCHEMA,
        "scope": {"synthetic": True},
        "states": [state],
        "instruments": [
            {
                "security": symbol,
                "initial_listing_date": fact(None),
                "delisting_date": fact(None),
                "source_refs": ["evidence.txt"],
            }
        ],
        "universes": [],
        "actions": [],
        "minute_evidence": extension,
    }
    return files, data


def assemble(tmp_path, mutate=None, file_mutate=None):
    files, data = fixture_files()
    if mutate:
        mutate(data)
    if file_mutate:
        file_mutate(files)
    files["data.json"] = canonical(data)
    for name, raw in files.items():
        (tmp_path / name).write_bytes(raw)
    binding = {
        "schema": SCHEMA,
        "data": "data.json",
        "historical_pit": False,
        "execution_permission": False,
        "received_at": "2026-10-13T02:00:02+00:00",
        "files": [
            {"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            for name, raw in files.items()
        ],
    }
    raw = canonical(binding)
    (tmp_path / "binding.json").write_bytes(raw)
    return MinuteEvidenceView(
        tmp_path / "binding.json", expected_binding_sha256=hashlib.sha256(raw).hexdigest()
    )


def test_source_replay_preserves_decimal_text_tail_and_unknown_intervals(tmp_path):
    view = assemble(tmp_path)
    rows = view.bars("000001.XSHE", "2026-10-09")
    assert rows[0]["source_row"]["amount"] == "29.100001"
    assert len(rows) == 3 and rows[-1]["source_label"].endswith("15:00:00")
    assert rows[-1]["in_target_label_grid"] is False
    assert all(
        r["interval_start"] is r["interval_end"] is r["historical_available_at"] is None
        for r in rows
    )
    coverage = view.label_coverage()[0]
    assert coverage["target_labels"] == 237 and coverage["matched_labels"] == 2
    assert not coverage["label_grid_complete"] and not coverage["interval_coverage_verified"]
    assert view.calendars()["XSHE"]["days"]["2026-10-11"] is False
    assert view.state("000001.XSHE", "2026-10-09")["previous_close"]["value"] == "9.700"


def test_unknown_dates_do_not_become_empty_action_certification(tmp_path):
    view = assemble(tmp_path)
    result = view.corporate_actions(["000001.XSHE"], start="2026-10-08", end="2026-10-12")
    assert result["rows"] == [] and not result["absence_proved"] and not result["complete"]
    assert result["source_queries"][1]["milestones"]["PAY_CASH_DATE"]["null_count"] == 1
    assert not view.admission_report()["ready_for_execution"]
    with error("MINUTE_GATES_BLOCKED"):
        view.export_backtest()


def test_detached_queries_and_historical_visibility_refused(tmp_path):
    view = assemble(tmp_path)
    view.state("000001.XSHE", "2026-10-09")["listed"]["value"] = False
    view.bars("000001.XSHE", "2026-10-09")[0]["source_row"]["close"] = "1"
    assert view.state("000001.XSHE", "2026-10-09")["listed"]["value"] is True
    assert view.bars("000001.XSHE", "2026-10-09")[0]["source_row"]["close"] == "9.700"
    for kwargs, code in [
        ({"visibility": "verified"}, "PIT_UNAVAILABLE"),
        (
            {"visibility": "received", "knowledge_at": "2026-10-09T09:31:00+08:00"},
            "RECEIPT_NOT_VISIBLE",
        ),
        ({"require_intervals": True}, "MINUTE_INTERVAL_UNVERIFIED"),
    ]:
        with error(code):
            view.bars("000001.XSHE", "2026-10-09", **kwargs)


@pytest.mark.parametrize(
    "mutation,code",
    [
        (
            lambda d: d["states"][0].update(historical_available_at="2026-10-09T09:00:00+08:00"),
            "MINUTE_EVIDENCE_CLOCK",
        ),
        (
            lambda d: d["states"][0].update(replay_effective_at="2026-10-09T09:00:00+08:00"),
            "MINUTE_EVIDENCE_CLOCK",
        ),
        (
            lambda d: d["states"][0]["previous_close"].update(value="9.69"),
            "MINUTE_REFERENCE_MISMATCH",
        ),
        (
            lambda d: d["states"][0].update(previous_session="2026-10-09"),
            "MINUTE_REFERENCE_MISMATCH",
        ),
        (
            lambda d: d["minute_evidence"]["assessments"][3].update(status="obtained"),
            "MINUTE_INTERVAL_UNVERIFIED",
        ),
        (
            lambda d: d["minute_evidence"]["actions"].update(absence_proved=True),
            "MINUTE_ACTION_COVERAGE",
        ),
        (
            lambda d: d["minute_evidence"]["opening_comparisons"][0].update(
                timepoint_0930_volume=0
            ),
            "MINUTE_OPENING_MISMATCH",
        ),
        (
            lambda d: d["minute_evidence"]["opening_comparisons"][0].update(
                timepoint_0931_volume=1
            ),
            "MINUTE_OPENING_MISMATCH",
        ),
        (
            lambda d: d["minute_evidence"]["price_sources"][0].update(security="600000.XSHG"),
            "MINUTE_EVIDENCE_SOURCE",
        ),
        (lambda d: d["minute_evidence"]["action_queries"].pop(), "MINUTE_ACTION_COVERAGE"),
        (
            lambda d: d["states"][0]["limit_model_candidate"].update(execution_usable=True),
            "MINUTE_LIMIT_CANDIDATE",
        ),
        (
            lambda d: d["states"][0]["limit_model_candidate"].update(upper_limit="10.68"),
            "MINUTE_LIMIT_CANDIDATE",
        ),
        (
            lambda d: d["states"][0]["limit_model_candidate"].update(dependencies_unproved=[]),
            "MINUTE_LIMIT_CANDIDATE",
        ),
        (
            lambda d: d["states"][0].update(intraday_halts_verified=True),
            "HISTORICAL_SCHEMA",
        ),
    ],
)
def test_semantic_promotions_and_crosswired_scope_are_rejected(tmp_path, mutation, code):
    with error(code):
        assemble(tmp_path, mutate=mutation)


def test_receipt_digest_is_recomputed(tmp_path):
    def mutate(files):
        receipt = json.loads(files["1m.observation.json"])
        receipt["url"] = receipt["url"].replace("sz000001", "sh600000")
        files["1m.observation.json"] = canonical(receipt)

    with error("MINUTE_EVIDENCE_INTEGRITY"):
        assemble(tmp_path, file_mutate=mutate)


def test_source_bytes_tamper_detected_after_binding(tmp_path):
    view = assemble(tmp_path)
    (tmp_path / "1m.json").write_bytes(b"[]")
    with error("HISTORICAL_INTEGRITY"):
        MinuteEvidenceView(
            tmp_path / "binding.json", expected_binding_sha256=view.descriptor()["binding_sha256"]
        )


@pytest.mark.parametrize("mutation", ["count", "pages", "security", "date_filter"])
def test_rehashed_action_scope_cannot_hide_missing_or_filtered_history(tmp_path, mutation):
    def mutate(files):
        name = "RPT_F10_DIVIDEND_MAIN.json"
        source = json.loads(files[name])
        receipt = json.loads(files[name + ".receipt.json"])
        if mutation in {"count", "pages"}:
            source["result"][mutation] = 2
        elif mutation == "security":
            source["result"]["data"][0]["SECURITY_CODE"] = "000002"
        else:
            receipt["url"] += "&startDate=2026-10-08"
        files[name] = canonical(source)
        receipt.update(bytes=len(files[name]), sha256=hashlib.sha256(files[name]).hexdigest())
        files[name + ".receipt.json"] = canonical(receipt)

    with error("MINUTE_ACTION_COVERAGE"):
        assemble(tmp_path, file_mutate=mutate)


def test_unknown_calendar_flag_rejected_even_with_valid_hashes(tmp_path):
    def mutate(files):
        source = json.loads(files["calendar.json"])
        source["data"][0]["jybz"] = "unknown"
        files["calendar.json"] = canonical(source)
        receipt = json.loads(files["calendar.json.receipt.json"])
        receipt.update(
            bytes=len(files["calendar.json"]),
            sha256=hashlib.sha256(files["calendar.json"]).hexdigest(),
        )
        files["calendar.json.receipt.json"] = canonical(receipt)

    with error("MINUTE_EVIDENCE_SOURCE"):
        assemble(tmp_path, file_mutate=mutate)


@pytest.mark.parametrize("role", ["calendar", "action"])
def test_receipt_role_cannot_bypass_raw_source_hash_validation(tmp_path, role):
    source = "calendar.json" if role == "calendar" else "RPT_F10_DIVIDEND_MAIN.json"

    def mutate_data(data):
        spec = data["minute_evidence"]
        entry = (
            spec["calendars"][0] if role == "calendar" else spec["action_queries"][1]["pages"][0]
        )
        entry["receipt"] = "unchecked.json"

    def mutate_files(files):
        files["unchecked.json"] = files[source + ".receipt.json"]

    with error("MINUTE_EVIDENCE_INTEGRITY"):
        assemble(tmp_path, mutate=mutate_data, file_mutate=mutate_files)


def test_sealing_preserves_hashes_and_refuses_overwrite(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    view = assemble(source)
    shutil_binding = (source / "binding.json").read_bytes()
    (source / "inventory.json").write_bytes(shutil_binding)
    out = tmp_path / "sealed"
    sealed = seal_minute_evidence(source, out)
    assert sealed["binding_sha256"] == view.descriptor()["binding_sha256"]
    assert (out / "binding.json").read_bytes() == shutil_binding
    with error("DESTINATION_EXISTS"):
        seal_minute_evidence(source, out)


def test_missing_reference_fails_required_fields(tmp_path):
    view = assemble(tmp_path)
    with error("HISTORICAL_UNKNOWN"):
        view.state("000001.XSHE", "2026-10-09", require_fields=["reference_price", "upper_limit"])


@pytest.mark.parametrize("required", [["suspended"], ["listed"]])
def test_minute_required_fields_inherit_historical_conflict_quarantine(tmp_path, required):
    def mutate(data):
        state = data["states"][0]
        pool = {k: deepcopy(state[k]) for k in ("security", "suspended", "is_st")}
        pool["is_st"]["value"] = True
        data["universes"] = [
            {
                "date": state["date"],
                "source_row_count": 1,
                "source_query_complete": True,
                "market_complete": False,
                "source_refs": ["evidence.txt"],
                "rows": [pool],
            }
        ]

    view = assemble(tmp_path, mutate=mutate)
    assert view.state("000001.XSHE", "2026-10-09")["dependency_conflicts"]
    with error("HISTORICAL_DEPENDENCY_CONFLICT"):
        view.state("000001.XSHE", "2026-10-09", require_fields=required)


def test_sealer_disallows_reserved_binding_path(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    assemble(source)
    inventory = json.loads((source / "binding.json").read_bytes())
    bad = deepcopy(inventory)
    bad["files"].append({"path": "binding.json", "bytes": 0, "sha256": "0" * 64})
    (source / "inventory.json").write_bytes(canonical(bad))
    with error("HISTORICAL_FILE"):
        seal_minute_evidence(source, tmp_path / "sealed")
    assert not (tmp_path / "sealed").exists()
