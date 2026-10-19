"""Synthetic protocol, credential redaction and bounded collection regressions."""

import gzip
import json
import sqlite3
from pathlib import Path

import pytest

from ashare_data.financials import expansion as e
from ashare_data.financials import tushare_reference as ts


@pytest.fixture
def baseline(tmp_path):
    db = tmp_path / "daily.db"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE instruments(symbol, name, ipo, out, board)")
        con.executemany("INSERT INTO instruments VALUES(?,?,?,?,?)", [
            ("sh600000", "synthetic", "1999-01-01", None, "main"),
            ("sz000001", "synthetic", "1999-01-01", None, "main"),
        ])
        con.execute("CREATE TABLE code_events(id, old_symbol, new_symbol, effective_date)")
        con.execute("CREATE TABLE metadata(key,value)")
    root = tmp_path / "baseline"
    e.prepare(root, db, "2010-01-01", "2010-01-05")
    return root


def payload(fields, items):
    return e.encoded(dict(code=0, msg=None, data=dict(fields=fields.split(","), items=items)))


def test_transport_uses_tls_post_and_never_exposes_runtime_secret():
    secret = "SYNTHETIC_SECRET_ONLY_FOR_OFFLINE_TEST"

    class Response:
        status, headers = 200, {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            return payload("trade_date,ts_code,up_limit,down_limit", [])

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "https://api.tushare.pro" and request.method == "POST"
            assert json.loads(request.data)["token"] == secret
            return Response()

    transport = ts.Transport(secret, Opener())
    assert secret not in repr(transport)
    assert transport(ts.request_url("stk_limit", {"trade_date": "20100104"}))[0] == 200
    with pytest.raises(ValueError, match="TLS"):
        transport("http://api.tushare.pro/?api_name=stk_limit&trade_date=20100104")


def test_permission_and_frequency_are_distinct_business_failures():
    class Opener:
        def __init__(self, message):
            self.message = message

        def open(self, request, timeout):
            class Response:
                status, headers = 200, {}

                def __enter__(inner):
                    return inner

                def __exit__(inner, *args):
                    pass

                def read(inner, limit):
                    return e.encoded(dict(code=2002, msg=self.message))
            return Response()

    url = ts.request_url("stk_limit", {"trade_date": "20100104"})
    for message, expected in [("每分钟最多访问200次", 429), ("没有访问该接口的权限", 403)]:
        status, _, headers = ts.Transport("synthetic-secret-123456", Opener(message))(url)
        assert status == expected and headers["_transport_status"] == 200 and headers["_provider_status"] == 2002


def test_calendar_creates_only_evidenced_open_days_and_resume_reuses_cache(tmp_path, baseline):
    root = tmp_path / "calendar"
    ts.prepare_calendar(root, baseline)
    calls = []

    def fetch(url):
        api, params = ts.request_identity(url)
        calls.append(api)
        return 200, payload(ts.FIELDS[api], [[params["exchange"], f"2010010{i}", int(i in (4, 5)), None]
                                            for i in range(1, 6)]), {}

    result = ts.run(root, fetch)
    assert result["task_states"] == {"complete": 2}
    ts.run(root, lambda _: pytest.fail("cached success requested twice"))
    target = tmp_path / "limits"
    manifest = ts.prepare_limits(target, baseline, root)
    assert manifest["planned_tasks"] == 2 and not manifest["unknown_calendar_dates"]
    tasks = e.load_batch(target)[2]
    assert [t["start"] for t in tasks] == ["2010-01-05", "2010-01-04"]
    receipt = json.loads(next((root / "receipts").glob("*.json")).read_text())
    assert receipt["attempts"][0]["authenticated"] is True


def test_partial_calendar_keeps_missing_dates_unknown(tmp_path, baseline):
    root = tmp_path / "calendar"
    ts.prepare_calendar(root, baseline)
    task = e.load_batch(root)[2][0]
    rows, cov = ts.calendar_rows(payload(ts.FIELDS["trade_cal"], [["SSE", "20100104", 1, None]]), task)
    assert len(rows) == 1 and len(cov["missing_calendar_dates"]) == 4


def test_collection_stop_is_honored_after_inflight_receipt_is_saved(tmp_path, baseline):
    root, stop = tmp_path / "calendar", tmp_path / "STOP"
    ts.prepare_calendar(root, baseline)
    calls = []

    def fetch(url):
        api, params = ts.request_identity(url)
        calls.append(api)
        stop.touch()
        return 200, payload(ts.FIELDS[api], [[params["exchange"], "20100104", 1, None]]), {}

    result = ts.run(root, fetch, stop_paths=(stop,))
    assert len(calls) == 1 and result["task_states"] == {"partial_source": 1, "pending": 1}
    assert json.loads((root / "worker-exit.json").read_text())["stop_requested"]
    assert len(list((root / "raw").glob("*.gz"))) == 1


def test_limits_keep_null_zero_and_decimal_without_inventing_unlimited_status():
    universe = {"instruments": [{"symbol": "sh600000", "ipo": "1999-01-01", "out": None}]}
    task = {"url": ts.request_url("stk_limit", {"trade_date": "20100104"})}
    raw = b'{"code":0,"data":{"fields":["trade_date","ts_code","up_limit","down_limit"],"items":[["20100104","600000.SH",23.860000,0]]}}'
    rows, coverage = ts.limit_rows(raw, task, universe)
    assert rows[0]["fields"]["up_limit"]["value"] == "23.860000"
    assert rows[0]["fields"]["down_limit"]["state"] == "zero_semantics_unverified"
    assert rows[0]["fields"]["pre_close"]["value"] is None
    assert not rows[0]["no_price_limit_inferred"] and coverage["target_all_codes_returned"]


def test_limit_identity_duplicates_nonfinite_and_inverted_values_fail():
    universe = {"instruments": [{"symbol": "sh600000", "ipo": "1999-01-01", "out": None}]}
    task = {"url": ts.request_url("stk_limit", {"trade_date": "20100104"})}
    fields = "trade_date,ts_code,up_limit,down_limit"
    for rows in [ [["20100105", "600000.SH", 12, 10]],
                  [["20100104", "600000.SH", 12, 10]] * 2,
                  [["20100104", "600000.SH", 10, 12]] ]:
        with pytest.raises(ValueError):
            ts.limit_rows(payload(fields, rows), task, universe)


def test_credential_echo_never_reaches_raw_storage(tmp_path, baseline):
    root = tmp_path / "calendar"
    ts.prepare_calendar(root, baseline)
    secret = "synthetic-secret-123456"

    class Opener:
        def open(self, request, timeout):
            class Response:
                status, headers = 200, {}

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

                def read(self, limit):
                    return e.encoded(dict(code=2002, msg=secret))
            return Response()

    ts.run(root, ts.Transport(secret, Opener()), max_jobs=1)
    assert not list((root / "raw").iterdir())
    assert all(secret.encode() not in p.read_bytes() for p in root.rglob("*") if p.is_file())


def test_capped_day_only_creates_missing_target_code_supplement(tmp_path, baseline):
    limit_root = tmp_path / "limits"
    task = dict(id="tushare_stk_limit-2010-01-04", kind="tushare_stk_limit", symbol="", start="2010-01-04", end="2010-01-04",
                url=ts.request_url("stk_limit", {"trade_date": "20100104"}))
    ts._prepare(limit_root, baseline, [task], {})
    rows = [["20100104", "000001.SZ", 12, 10]] + [["20100104", f"{500000+i}.SH", 12, 10] for i in range(5799)]
    ts.run(limit_root, lambda _: (200, payload("trade_date,ts_code,up_limit,down_limit", rows), {}))
    receipt = json.loads((limit_root / "receipts" / (task["id"] + ".json")).read_text())
    assert receipt["state"] == "partial_source" and receipt["coverage"]["row_cap_reached"]
    assert len(gzip.decompress((limit_root / receipt["normalized"]["path"]).read_bytes()).splitlines()) == 1
    target = tmp_path / "capped"
    ts.prepare_capped_targets(target, baseline, limit_root)
    tasks = e.load_batch(target)[2]
    assert len(tasks) == 1 and ts.request_identity(tasks[0]["url"])[1]["ts_code"] == "600000.SH"


def runner(monkeypatch):
    import importlib.util
    scripts = Path(__file__).parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("tushare_runner_fixture", scripts / "run_tushare_reference.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hidden_prompt_fails_closed_when_echo_cannot_be_disabled(monkeypatch, tmp_path, baseline):
    import warnings
    module = runner(monkeypatch)
    root = tmp_path / "runtime"
    monkeypatch.setattr(module.sys, "argv", ["runner", "--root", str(root), "--baseline", str(baseline), "--prompt-token"])
    monkeypatch.setattr(module.sys.stdin, "isatty", lambda: True)
    def unsafe_prompt(_):
        warnings.warn("synthetic echo failure", module.getpass.GetPassWarning)
        pytest.fail("Plain-text fallback must never be reached")
    monkeypatch.setattr(module.getpass, "getpass", unsafe_prompt)
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2 and not (root / "worker.json").exists()


def test_existing_token_environment_is_removed_from_process_and_never_persisted(monkeypatch, tmp_path, baseline):
    module = runner(monkeypatch)
    root, secret = tmp_path / "runtime", "synthetic-secret-value-123456"
    monkeypatch.setattr(module.sys, "argv", ["runner", "--root", str(root), "--baseline", str(baseline), "--token-env", "TEST_AUTH_REFERENCE"])
    monkeypatch.setenv("TEST_AUTH_REFERENCE", secret)
    monkeypatch.setattr(module, "collect", lambda *a: None)
    assert module.main() == 0
    assert "TEST_AUTH_REFERENCE" not in module.os.environ
    assert all(secret.encode() not in p.read_bytes() for p in root.rglob("*") if p.is_file())
