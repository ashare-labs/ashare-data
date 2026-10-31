"""Synthetic protocol and gap-ownership regressions; these are not authenticated evidence."""

from decimal import Decimal
import gzip
import json
import sqlite3

import pytest

from ashare_data.financials import expansion as e
from ashare_data.financials import tushare_fundamentals as f
from ashare_data.financials import tushare_gaps as gaps
from ashare_data.financials import tushare_reference as ts


def payload(rows):
    fields = list(rows[0]) if rows else ["ts_code"]
    return e.encoded(dict(code=0, data=dict(fields=fields, items=[[row[k] for k in fields] for row in rows])))


def task(api, params, fields=None, **extra):
    return dict(id="synthetic", symbol="sh600000", kind="tushare_" + api,
                target_start="2010-01-01", target_end="2015-12-31",
                url=ts.request_url(api, dict(ts_code="600000.SH", **params), fields), **extra)


def statement(**extra):
    return dict(ts_code="600000.SH", end_date="20100331", ann_date="20100420", f_ann_date="20100421",
                report_type="1", comp_type="2", end_type="1", update_flag="0", revenue="100.000000", **extra)


@pytest.mark.parametrize(("api", "params"), [
    ("balancesheet", dict(f_ann_date="20100420", report_type="1")),
    ("dividend", dict(start_date="20100101", end_date="20101231")),
    ("fina_indicator", dict(period="20100331", report_type="1")),
    ("income", dict(period="20100331", report_type="6")),
    ("income", dict(report_type="1")),
    ("adj_factor", dict(start_date="20100101", end_date="20121231")),
    ("income_vip", dict(period="20100331", report_type="1")),
])
def test_reject_undocumented_parameters_unbounded_queries_and_vip(api, params):
    with pytest.raises(ValueError):
        task(api, params)


def test_exact_projection_reaches_post_without_entering_api_parameters():
    seen = []
    class Opener:
        def open(self, request, timeout):
            seen.append(json.loads(request.data))
            class Response:
                status, headers = 200, {}
                def __enter__(self):
                    return self
                def __exit__(self, *_):
                    pass
                def read(self, _):
                    return payload([])
            return Response()
    t = task("income", dict(period="20100331", report_type="1"), ts.FINANCIAL_ID)
    ts.Transport("synthetic-not-a-real-token", Opener())(t["url"])
    assert seen[0]["fields"] == ts.FINANCIAL_ID and "fields" not in seen[0]["params"]
    assert "revenue" not in seen[0]["fields"]
    with pytest.raises(ValueError, match="fields"):
        task("income", dict(period="20100331", report_type="1"), "ts_code,unknown_field")


def test_financial_versions_dates_and_precision_never_collapse_or_imply_pit():
    t = task("income", dict(start_date="20100401", end_date="20100430", report_type="1"), out="20100301")
    original = statement()
    revised = original | dict(update_flag="1", f_ann_date="20100425", revenue="120.000000")
    rows, coverage = f.observations(payload([original, revised, original]), t)
    assert len(rows) == 3 and rows[0]["source_ordinal"] != rows[2]["source_ordinal"]
    assert rows[0]["source_revision_id"] != rows[1]["source_revision_id"]
    assert rows[0]["fields"]["revenue"]["value"] == "100.000000"
    assert rows[0]["fields"]["revenue"]["unit"] == "provider_native_amount_unit_unverified"
    assert all(r["period_after_code_exit"] and r["historical_available_at"] is None for r in rows)
    assert rows[0]["announcement_date"] == "2010-04-20" and rows[0]["actual_announcement_date"] == "2010-04-21"
    assert not coverage["revision_history_complete"]
    with pytest.raises(ValueError, match="report type"):
        f.observations(payload([original | {"report_type": "6"}]), t)


def test_statement_window_is_announcement_but_indicator_window_is_period():
    rows, _ = f.observations(payload([statement()]), task("income", dict(start_date="20100401", end_date="20100430", report_type="1")))
    assert rows[0]["period_end"] == "2010-03-31"
    with pytest.raises(ValueError, match="window"):
        f.observations(payload([statement()]), task("income", dict(start_date="20100301", end_date="20100331", report_type="1")))
    row = dict(ts_code="600000.SH", ann_date="20100420", end_date="20100331", update_flag="0", netprofit_margin="10.5")
    rows, _ = f.observations(payload([row]), task("fina_indicator", dict(start_date="20100301", end_date="20100331")))
    assert rows[0]["fields"]["netprofit_margin"]["value"] == "10.5"


def test_dividend_tax_date_and_share_units_remain_distinct():
    t = task("dividend", dict(ex_date="20100701"))
    row = dict(ts_code="600000.SH", end_date="20091231", ann_date="20100420", imp_ann_date="20100620",
               record_date="20100630", ex_date="20100701", pay_date="20100702", div_proc="预案",
               cash_div="0.9", cash_div_tax="1.0", stk_div="0.2", stk_bo_rate="0.1", stk_co_rate="0.1", base_share="100.1234")
    result = f.observations(payload([row]), t)[0][0]
    assert not result["implemented"]
    assert result["fields"]["base_share"]["value"] == "1001234.0000"
    assert result["fields"]["cash_div_tax"]["tax_basis"] == "gross_pre_tax"
    assert result["fields"]["cash_div"]["value"] == "0.9"
    assert result["fields"]["ann_date"]["value"] != result["fields"]["imp_ann_date"]["value"]
    with pytest.raises(ValueError, match="filter"):
        f.observations(payload([row | {"ex_date": "20100702"}]), t)


def test_factors_are_positive_separate_and_missing_days_explicit():
    t = task("adj_factor", dict(start_date="20100104", end_date="20100105"), expected_sessions=["2010-01-04", "2010-01-05"])
    row = dict(ts_code="600000.SH", trade_date="20100104", adj_factor="10.123400")
    rows, coverage = f.observations(payload([row]), t)
    assert not rows[0]["compatible_with_sina_hfq"] and not rows[0]["carry_forward_authorized"]
    assert coverage["missing_evidenced_sessions"] == ["2010-01-05"]
    for bad in ("0", "-1", "NaN", "Infinity"):
        with pytest.raises(ValueError):
            f.observations(payload([row | {"adj_factor": bad}]), t)
    with pytest.raises(ValueError, match="Duplicate"):
        f.observations(payload([row, row]), t)


def test_capped_statements_split_announcements_not_report_periods():
    t = task("income", dict(start_date="20100401", end_date="20100430", report_type="1"), periods=["2010-03-31"])
    _, coverage = f.observations(payload([statement()] * 100), t)
    assert coverage["row_cap_reached"] and not coverage["guard_is_documented_source_limit"]
    children = f.split_task(t)
    assert children[0]["periods"] == ["2010-03-31"]
    assert [ts.request_identity(c["url"])[1]["end_date"] for c in children] == ["20100415", "20100430"]
    assert not f.split_task(task("income", dict(period="20100331", report_type="1")))


@pytest.fixture
def existing(tmp_path):
    db = tmp_path / "daily.db"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE instruments(symbol,name,ipo,out,board)")
        con.execute("INSERT INTO instruments VALUES('sh600000','synthetic','1999-01-01',NULL,'main')")
        con.execute("INSERT INTO instruments VALUES('sh600001','synthetic retired','1992-01-01','2000-01-01','main')")
        con.execute("CREATE TABLE code_events(id,old_symbol,new_symbol,effective_date)")
        con.execute("CREATE TABLE metadata(key,value)")
    root = tmp_path / "existing"
    e.prepare(root, db, "2010-01-01", "2010-12-31")
    _, manifest, tasks = e.load_batch(root)
    for t in tasks:
        rows = []
        if t["kind"] == "sina_income":
            rows = [dict(period_end="2010-03-31", scope="consolidated", currency="CNY", source_revision_id="original",
                         source_locator="report/0", announcement_date="2010-04-20", metrics={canonical:
                         dict(value="100" if field == "revenue" else "10", unit="CNY", source_field=field)
                         for field, canonical in f.METRICS["income"].items()})]
        artifact = e.artifact(root, "normalized/" + t["id"] + ".jsonl.gz", gzip.compress(b"".join(e.encoded(r) for r in rows)))
        e.atomic(root / "receipts" / (t["id"] + ".json"), e.encoded(dict(state="complete" if rows else "empty_unknown", normalized=artifact)))
    return root, manifest


def test_inventory_reuses_existing_amounts_derives_common_ratio_and_bounds_factor_tail(existing, tmp_path):
    root, manifest = existing
    snapshot, summary = gaps.inventory(root, tmp_path / "inventory")
    assert summary["existing_amount_fields"] == 4 and summary["derived_indicator_values"] == 1
    _, repeat = gaps.inventory(root, tmp_path / "inventory")
    assert repeat["entries_refreshed"] == 0
    factors = dict(schema="ad.existing-factor-inventory.v1", universe_sha256=manifest["universe_sha256"],
                   codes={"sh600000": dict(covered_through="2010-12-28", basis="sina_hfq")})
    plan = gaps.build_plan(root, snapshot, final=True, factor_inventory=factors)
    income = [t for t in plan["tasks"] if t["kind"] == "tushare_income"]
    assert len(income) == 3
    for t in income:
        report = ts.request_identity(t["url"])[1]["report_type"]
        if report == "1":
            assert not set(f.METRICS["income"]) & set(ts.request_fields(t["url"]).split(","))
    assert not [t for t in plan["tasks"] if t["kind"] == "tushare_fina_indicator"]
    factor = next(t for t in plan["tasks"] if t["kind"] == "tushare_adj_factor")
    assert (factor["start"], factor["end"]) == ("2010-12-29", "2010-12-31")
    assert not any(d.get("symbol") == "sh600001" for d in plan["deferred"])
    derived = plan["existing_fields"][0]["derived"][0]
    assert Decimal(derived["value"]) == 10 and not derived["historical_pit_certified"]


def test_pending_source_defers_its_requests_and_final_plan_cannot_overlap(existing, tmp_path):
    root, _ = existing
    path = root / "receipts/sina_income-sh600000.json"
    path.unlink()
    snapshot, _ = gaps.inventory(root, tmp_path / "inventory")
    preview = gaps.build_plan(root, snapshot)
    assert not [t for t in preview["tasks"] if t["kind"] == "tushare_income"]
    assert any(d["state"] == "pending_existing_source" for d in preview["deferred"])
    with pytest.raises(ValueError, match="still pending"):
        gaps.build_plan(root, snapshot, final=True)
    with pytest.raises(ValueError, match="closed-source"):
        gaps.prepare(tmp_path / "batch", root, preview)


@pytest.mark.parametrize(("api", "target", "native", "title"), [
    ("income", "n_income_attr_p", "NETPARECOMPPROF", "归属于母公司的净利润"),
    ("income", "n_income_attr_p", "NETPARESHARPROF", "归属于母公司的净利润"),
    ("cashflow", "c_cash_equ_end_period", "CASHEQUFINBALA", "期末现金及现金等价物余额"),
    ("cashflow", "c_cash_equ_end_period", "CASHEQUFINBALA", "期末现金及现金等价物"),
])
def test_existing_raw_amount_aliases_are_reused_without_recollection(api, target, native, title):
    row = dict(period_end="2010-03-31", scope="consolidated", currency="CNY", source_revision_id="source-version",
               source_locator="report/0", metrics={}, fields=[dict(source_field=native, source_title=title,
               ordinal=7, value="123.456789012345678901", state="source_observed")])
    summary = gaps.summarize([row], api)
    period = summary["periods"][0]
    assert period["present"] == [target]
    assert period["reused_raw_fields"][target] == dict(source_field=native, source_title=title, ordinal=7)
    entry = dict(api=api, **summary)
    manifest = dict(target_start="2010-01-01", target_end="2010-12-31")
    tasks = gaps.financial_tasks(entry, dict(symbol="sh600000"), manifest, {"2010-03-31"})
    current = [t for t in tasks if t["reason"] == "missing_current_amount_fields"]
    assert all(target not in t["gaps"] and target not in ts.request_fields(t["url"]).split(",") for t in current)
    assert not row["metrics"]  # The immutable original extraction remains unchanged.
    for invalid in (row | {"fields": row["fields"] * 2},
                    row | {"fields": [row["fields"][0] | {"source_title": "同名含义未经核实"}]},
                    row | {"fields": [row["fields"][0] | {"value": None}]},
                    row | {"fields": [row["fields"][0] | {"value": "NaN"}]}):
        assert target not in gaps.summarize([invalid], api)["periods"][0]["present"]
    for invalid in (row | {"currency": "USD"}, row | {"scope": "parent"}, row | {"period_after_code_exit": True}):
        assert not gaps.summarize([invalid], api)["periods"]


def test_dividend_alignment_requires_all_anchors_and_never_force_joins():
    row = f.observations(payload([dict(ts_code="600000.SH", div_proc="实施", ex_date="20100701", record_date="20100630",
                                     cash_div_tax="1.0", stk_bo_rate="0", stk_co_rate="0")]), task("dividend", dict(ex_date="20100701")))[0][0]
    event = dict(date="2010-07-01", cash_basis="gross_pre_tax_per_old_eligible_share", anchors=dict(
                 cash_div_tax="1", stk_bo_rate="0", stk_co_rate="0", record_date="2010-06-30"))
    assert gaps.dividend_matches(row, event)
    assert not gaps.dividend_matches(row, event | {"cash_basis": "after_tax"})
    assert not gaps.dividend_matches(row, event | {"anchors": event["anchors"] | {"record_date": None}})


def test_reconciliation_aggregates_split_windows_and_keeps_units_unverified(existing, tmp_path):
    baseline, manifest = existing
    instrument = dict(symbol="sh600000", out=None)
    parent = gaps.make_task("income", instrument, dict(start_date="20100401", end_date="20100430", report_type="5"),
                           ["revenue"], manifest, gaps=["revenue"], periods=["2010-03-31"], reason="prior_provider_versions_unknown")
    base = tmp_path / "parent"
    ts._prepare(base, baseline, [parent], {})
    row = statement() | {"report_type": "5"}
    ts.run(base, lambda _: (200, payload([row] * 100), {}))
    child = tmp_path / "child"
    gaps.prepare_splits(child, baseline, base)
    def fetch(url):
        _, params = ts.request_identity(url)
        return 200, payload([row] if params["end_date"] == "20100430" else []), {}
    ts.run(child, fetch)
    plan = {"existing_fields": []}
    result = gaps.reconcile(plan, [base, child], tmp_path / "reconcile")
    assert result["states"] == {"observed_native_amount_unit_unverified": 1}
    assert result["records"] == 1
    ledger = json.loads(gzip.decompress((tmp_path / "reconcile/field-reconciliation.jsonl.gz").read_bytes()))
    assert ledger["observations"][0]["report_type"] == "5"
    assert len(ledger["leaf_receipts"]) == 2 and not list((tmp_path / "reconcile").glob("*.tmp"))


@pytest.mark.parametrize("coalesce", [False, True])
def test_finite_runner_collects_all_authorized_stages_and_resumes_without_requests(existing, tmp_path, monkeypatch, coalesce):
    import importlib.util
    from pathlib import Path
    scripts = Path(__file__).parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("fundamentals_runner_fixture", scripts / "run_tushare_reference.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    baseline, _ = existing
    root = tmp_path / "run"
    calendar, limits, capped = [root / name for name in ("calendar", "limits", "capped-targets")]
    ts.prepare_calendar(calendar, baseline)
    monkeypatch.setattr(runner, "wait_source", lambda *a: True)
    monkeypatch.setattr(ts, "run", lambda root, fetch, **k: e.run(root, interval=0.0001, minimum_interval=0.0001,
                       fetcher=fetch, normalizer=ts.normalize, authenticated=True, **k))
    calls = []
    def fetch(url):
        api, params = ts.request_identity(url)
        calls.append(api)
        if api == "trade_cal":
            return 200, payload([dict(exchange=params["exchange"], cal_date="20100104", is_open=1, pretrade_date=None)]), {}
        if api == "stk_limit":
            return 200, payload([dict(ts_code="600000.SH", trade_date="20100104", up_limit="12", down_limit="10")]), {}
        if api in f.STATEMENTS:
            row = statement() | {"report_type": params["report_type"]}
            row.update({k: "10.00" for k in f.METRICS[api]})
            return 200, payload([row]), {}
        return 200, payload([]), {}
    runner.collect(root, baseline, fetch, False, calendar, limits, capped, coalesce_dividends=coalesce)
    completion = json.loads((root / "completion.json").read_text())
    assert completion["state"] == "source_pass_finished_with_explicit_gaps"
    assert {"income", "balancesheet", "cashflow", "dividend"} <= set(calls)
    assert completion["reconciliation"]["records"] > 0 and not completion["database_imported"]
    runner.collect(root, baseline, lambda _: pytest.fail("Completed task fetched on resume"), False, calendar, limits, capped,
                   coalesce_dividends=coalesce)
    assert not list(root.rglob("*.tmp"))


def test_dividend_reconciliation_refuses_many_original_events_to_one_provider_row(existing, tmp_path):
    baseline, manifest = existing
    event = dict(date="2010-07-01", source_revision_id="original-a", cash_basis="gross_pre_tax_per_old_eligible_share",
                 anchors=dict(cash_div_tax="1", stk_bo_rate="0", stk_co_rate="0", record_date="2010-06-30"))
    planned = gaps.make_task("dividend", dict(symbol="sh600000"), dict(ex_date="20100701"), ["pay_date"], manifest,
                             gaps=["pay_date"], reason="missing_existing_distribution_fields",
                             matching_events=[event, event | {"source_revision_id": "original-b"}])
    root = tmp_path / "dividend"
    ts._prepare(root, baseline, [planned], {})
    row = dict(ts_code="600000.SH", div_proc="实施", ex_date="20100701", record_date="20100630", pay_date="20100702",
               cash_div_tax="1", stk_bo_rate="0", stk_co_rate="0")
    ts.run(root, lambda _: (200, payload([row]), {}))
    result = gaps.reconcile({"existing_fields": []}, [root], tmp_path / "reconciliation")
    assert result["states"] == {"unresolved_ambiguous_event_match": 1}


def test_response_proven_new_period_gets_one_gap_request_without_inventing_next_quarter(existing, tmp_path):
    baseline, manifest = existing
    planned = gaps.make_task("income", dict(symbol="sh600000"),
                             dict(start_date="20100101", end_date="20101231", report_type="1"), [], manifest,
                             periods=["2010-03-31"], gaps=["f_ann_date", "update_flag"],
                             reason="missing_version_and_actual_announcement_metadata")
    root = tmp_path / "metadata"
    ts._prepare(root, baseline, [planned], {})
    new = statement() | dict(end_date="20100630", ann_date="20100801", f_ann_date="20100801")
    ts.run(root, lambda _: (200, payload([statement(), new]), {}))
    receipt = gaps.read(root / "receipts" / (planned["id"] + ".json"))
    assert receipt["row_count"] == 2
    target = tmp_path / "discovered"
    out = gaps.prepare_discovered_periods(target, baseline, [root])
    assert out["planned_tasks"] == 1 and not out["quarterly_filing_obligation_inferred"]
    tasks = e.load_batch(target)[2]
    assert tasks[0]["periods"] == ["2010-06-30"]
    assert ts.request_identity(tasks[0]["url"])[1] == {"ts_code": "600000.SH", "period": "20100630", "report_type": "1"}
    assert set(f.METRICS["income"]) <= set(ts.request_fields(tasks[0]["url"]).split(","))
