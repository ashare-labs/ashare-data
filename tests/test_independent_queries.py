"""Synthetic protocol fixtures, never JQ answers or real market data."""
import hashlib
import json
import shutil

import pytest

from ashare_data import DataError, IndependentQueryView
from ashare_data.independent_queries import ROLE


def write(root, name, data):
    raw = data if isinstance(data, bytes) else json.dumps(data).encode()
    (root / name).write_bytes(raw)
    return {"path": name, "sha256": hashlib.sha256(raw).hexdigest()}


@pytest.fixture
def delivery(tmp_path):
    raw = write(tmp_path, "raw.js", b"synthetic fixture; no market data")
    decoder = write(tmp_path, "decoder.js", b"synthetic decoder fixture")
    rows = [{"date": d + "T00:00:00.000Z", "open": 12, "high": 14, "low": 10,
             "close": 13, "volume": 117, "amount": 1479} for d in ("2024-03-01", "2024-03-04")]
    decoded = write(tmp_path, "decoded.json", {"raw_sha256": raw["sha256"],
                    "decoder_sha256": decoder["sha256"], "rows": rows})
    source = {**decoded, "kind": "sina_daily_klc_decoded", "security": "000001.XSHE",
              "adjustment": "none", "captured_start": "2026-10-10T01:00:00+00:00",
              "captured_end": "2026-10-10T01:00:01+00:00", "raw_sha256": raw["sha256"],
              "decoder_sha256": decoder["sha256"], "evidence": [raw, decoder]}
    fact = write(tmp_path, "pause.json", {"security": "000001.XSHE", "date": "2024-03-05",
                  "full_day_paused": True, "historical_pit": False, "evidence": [raw]})
    factors = write(tmp_path, "factor.js", b'var sz000001hfq={"total":4,"data":[{"d":"2025-01-01","f":"999"},{"d":"2024-03-05","f":"4"},{"d":"2020-01-01","f":"2"},{"d":"1991-04-03","f":"1"}]}')
    products = {
        "daily": {"kind": "daily", "security": "000001.XSHE", "sources": ["sina"],
                  "start_date": "2024-03-01", "end_date": "2024-03-04"},
        "pause": {"kind": "paused_daily", "security": "000001.XSHE", "sources": ["sina"],
                  "date": "2024-03-05", "last_trade_date": "2024-03-04", "pause_fact": fact},
        "adjust": {"kind": "adjusted_daily", "security": "000001.XSHE", "sources": ["sina"],
                   "start_date": "2024-03-01", "end_date": "2024-03-04",
                   "basis_dates": ["2024-03-04", "2024-03-05"], "normalization_date": "1991-04-03",
                   "factor_source": factors, "evidence": [factors]},
    }
    b = {"schema": "ad.independent-query-binding.v1", "role": ROLE, "historical_pit": False,
         "strategy_visible": False, "sources": {"sina": source}, "products": products}
    return tmp_path, b


def view(delivery):
    root, b = delivery
    ref = write(root, "binding.json", b)
    return IndependentQueryView(root / ref["path"], expected_binding_sha256=ref["sha256"])


def test_daily_preserves_share_precision_and_unknown_fields(delivery):
    r = view(delivery).read_daily("daily", "000001.XSHE", source="sina", role=ROLE)
    assert len(r["rows"]) == 2  # no invented weekend rows
    f = r["rows"][0]["fields"]
    assert f["volume"]["value"] == "117"
    assert f["money"]["value"] == "1479"
    assert f["avg"]["origin"] == "derived" and f["avg"]["native_avg"] is False
    assert f["open_interest"]["state"] == "unsupported" and f["open_interest"]["value"] is None
    assert r["strategy_visible"] is False and r["historical_pit"] is False


def standardization(delivery, dates=None):
    root, b = delivery
    ref = write(root, "calendar.json", {"dates": dates or ["2024-03-01", "2024-03-04"]})
    b["products"]["daily"]["daily_standardization"] = {
        "policy": "raw_identity_previous_session_close.v1", "calendar": ref}


def test_standardization_is_explicit_and_preserves_precision(delivery):
    standardization(delivery)
    v = view(delivery)
    plain = v.read_daily("daily", "000001.XSHE", source="sina", role=ROLE)
    normalized = v.read_daily("daily", "000001.XSHE", source="sina", role=ROLE, standardize=True)
    assert plain["rows"][1]["fields"]["pre_close"]["state"] == "unsupported"
    fields = normalized["rows"][1]["fields"]
    assert fields["pre_close"]["value"] == "13" and fields["pre_close"]["previous_date"] == "2024-03-01"
    assert fields["pre_close"]["exchange_ex_reference"] is False
    assert fields["factor"]["value"] == "1" and fields["factor"]["corporate_action_factor"] is False
    for key in ("money", "volume", "avg", "paused", "high_limit", "low_limit", "open_interest", "vwap"):
        assert fields[key] == plain["rows"][1]["fields"][key]


def test_previous_close_never_jumps_over_missing_session(delivery):
    standardization(delivery, ["2024-03-01", "2024-03-02", "2024-03-04"])
    r = view(delivery).read_daily("daily", "000001.XSHE", source="sina", role=ROLE, standardize=True)
    assert r["rows"][1]["fields"]["pre_close"]["state"] == "unsupported"
    assert r["rows"][1]["fields"]["pre_close"]["previous_date"] == "2024-03-02"


def test_previous_calendar_is_pinned_and_must_be_ordered(delivery):
    standardization(delivery, ["2024-03-04", "2024-03-01"])
    with pytest.raises(DataError, match="calendar"):
        view(delivery).read_daily("daily", "000001.XSHE", source="sina", role=ROLE, standardize=True)
    standardization(delivery)
    v = view(delivery)
    (delivery[0] / "calendar.json").write_text('{}')
    with pytest.raises(DataError, match="digest"):
        v.read_daily("daily", "000001.XSHE", source="sina", role=ROLE, standardize=True)


def test_standardization_requires_bound_contract(delivery):
    with pytest.raises(DataError, match="not bound"):
        view(delivery).read_daily("daily", "000001.XSHE", source="sina", role=ROLE, standardize=True)
    with pytest.raises(DataError, match="boolean"):
        view(delivery).read_daily("daily", "000001.XSHE", source="sina", role=ROLE, standardize=1)


@pytest.fixture
def actions_delivery(delivery):
    root, b = delivery
    record = {"F020D": "2024-03-04", "F012N": 2.5, "F010N": None, "F011N": None,
              "F006D": "2024-02-26", "F018D": "2024-03-01", "F023D": "2024-03-04",
              "F025D": None, "F007V": "synthetic plan"}
    ref = write(root, "actions.json", {"resultcode": 200, "total": 2,
                 "records": [record, dict(record, F020D="2025-01-01")]})
    b["sources"]["sina"].update(ref, kind="cninfo_dividends")
    b["products"]["daily"]["kind"] = "corporate_actions"
    return delivery


def test_corporate_ledger_excludes_future_and_preserves_null(actions_delivery):
    r = view(actions_delivery).read_corporate_actions("daily", "000001.XSHE", source="sina", role=ROLE)
    assert len(r["rows"]) == 1
    f = r["rows"][0]["fields"]
    assert f["cash_per_10_shares"]["value"] == "2.5"
    assert f["bonus_shares_per_10"]["state"] == "unsupported"
    assert not r["historical_action_chain_complete"] and not r["adjustment_factor_certified"]


@pytest.mark.parametrize("change", ["count", "duplicate", "value", "status"])
def test_invalid_corporate_ledgers_refused(actions_delivery, change):
    root, b = actions_delivery
    d = json.loads((root / "actions.json").read_text())
    if change == "count":
        d["total"] = 3
    elif change == "duplicate":
        d["records"][1]["F020D"] = "2024-03-04"
    elif change == "value":
        d["records"][0]["F012N"] = -1
    else:
        d["resultcode"] = 500
    b["sources"]["sina"].update(write(root, "actions.json", d))
    with pytest.raises(DataError):
        view(actions_delivery).read_corporate_actions("daily", "000001.XSHE", source="sina", role=ROLE)


def test_rights_identity_and_date_roles(actions_delivery):
    root, b = actions_delivery
    record = {"SECCODE": "000001", "SUBID": 7, "F012D": "2024-03-04", "F005N": 8,
              "F004N": 3, "DECLAREDATE": "2024-02-20", "F011D": "2024-03-01",
              "F013D": "2024-03-04", "F014D": "2024-03-08", "F038D": "2024-03-20"}
    d = {"resultcode": 200, "total": 1, "records": [record]}
    b["sources"]["sina"].update(write(root, "actions.json", d), kind="cninfo_rights")
    r = view(actions_delivery).read_corporate_actions("daily", "000001.XSHE", source="sina", role=ROLE)
    assert r["rows"][0]["payment_end_date"] == "2024-03-08"
    assert r["rows"][0]["rights_listing_date"] == "2024-03-20"
    record["SECCODE"] = "000002"
    b["sources"]["sina"].update(write(root, "actions.json", d))
    with pytest.raises(DataError, match="security"):
        view(actions_delivery).read_corporate_actions("daily", "000001.XSHE", source="sina", role=ROLE)


@pytest.mark.parametrize("change", [None, "half_day", "wrong_security", "missing", "duplicate"])
def test_halt_events_require_explicit_complete_evidence(delivery, change):
    root, b = delivery
    row = {"SECURITY_CODE": "000001", "SECUCODE": "000001.SZ", "EVENT_TYPE_CODE": "023",
           "NOTICE_DATE": "2024-03-05 00:00:00",
           "LEVEL1_CONTENT": "因股东大会停牌一天，2024年03月05日 9:30-2024年03月05日 15:00停牌"}
    rows = [row]
    if change == "half_day":
        row["LEVEL1_CONTENT"] = row["LEVEL1_CONTENT"].replace("15:00", "11:30")
    elif change == "wrong_security":
        row["SECURITY_CODE"] = "000002"
    elif change == "missing":
        row["EVENT_TYPE_CODE"] = "001"
    elif change == "duplicate":
        rows.append(dict(row))
    raw = ('var pagedata = ' + json.dumps({"sjyl": {"result": {"data": rows}}}) + '; throw Error("not executable");').encode()
    b["sources"]["sina"].update(write(root, "events.html", raw), kind="eastmoney_stock_calendar_html")
    b["products"]["daily"].update(kind="halt_events", dates=["2024-03-05"])
    v = view(delivery)
    if change:
        with pytest.raises(DataError):
            v.read_halt_events("daily", "000001.XSHE", role=ROLE)
    else:
        r = v.read_halt_events("daily", "000001.XSHE", role=ROLE)
        assert r["rows"][0]["full_day_paused"] is True
        assert r["rows"][0]["official_announcement_chain_verified"] is False
        assert r["matching_feed"] is False


@pytest.mark.parametrize("role", [None, "strategy", "matching", "engine_initialization"])
def test_wrong_roles_rejected(delivery, role):
    with pytest.raises(DataError, match="role required"):
        view(delivery).read_daily("daily", "000001.XSHE", source="sina", role=role)


@pytest.mark.parametrize("product,security,source", [
    ("outside", "000001.XSHE", "sina"), ("daily", "000002.XSHE", "sina"),
    ("daily", "000001.XSHE", "unbound")])
def test_scope_rejected(delivery, product, security, source):
    with pytest.raises(DataError):
        view(delivery).read_daily(product, security, source=source, role=ROLE)


@pytest.mark.parametrize("file", ["raw.js", "decoder.js", "decoded.json", "binding.json"])
def test_every_read_rechecks_digests(delivery, file):
    v = view(delivery)
    v.read_daily("daily", "000001.XSHE", source="sina", role=ROLE)
    (delivery[0] / file).write_bytes(b"changed")
    with pytest.raises(DataError, match="digest mismatch"):
        v.read_daily("daily", "000001.XSHE", source="sina", role=ROLE)


def test_pause_modes_and_no_fake_trade(delivery):
    v = view(delivery)
    filled = v.read_paused("pause", "000001.XSHE", skip_paused=False, fill_paused=True, role=ROLE)
    f = filled["rows"][0]["fields"]
    assert f["open"]["value"] == "13" and f["volume"]["value"] == "0"
    assert f["close"]["origin"] == "synthetic_paused_fill" and not f["close"]["traded"]
    assert not filled["raw_row_present"]
    unfilled = v.read_paused("pause", "000001.XSHE", skip_paused=False, fill_paused=False, role=ROLE)
    assert unfilled["rows"][0]["fields"]["close"]["state"] == "nan"
    assert unfilled["rows"][0]["fields"]["paused"]["value"] is True
    assert v.read_paused("pause", "000001.XSHE", skip_paused=True, fill_paused=True, role=ROLE)["rows"] == []


def test_pause_cannot_fill_a_real_trading_bar(delivery):
    root, b = delivery
    p = b["products"]["pause"]
    p["date"] = "2024-03-04"
    f = json.loads((root / "pause.json").read_text())
    f["date"] = p["date"]
    p["pause_fact"] = write(root, "pause.json", f)
    with pytest.raises(DataError, match="conflicts"):
        view(delivery).read_paused("pause", "000001.XSHE", skip_paused=False, fill_paused=True, role=ROLE)


def test_pause_requires_last_observed_anchor(delivery):
    delivery[1]["products"]["pause"]["last_trade_date"] = "2024-03-01"
    with pytest.raises(DataError, match="anchor"):
        view(delivery).read_paused("pause", "000001.XSHE", skip_paused=False, fill_paused=True, role=ROLE)


def test_basis_controls_price_ratio_and_future_factors_do_not_leak(delivery):
    v = view(delivery)
    before = v.read_adjusted("adjust", "000001.XSHE", basis_date="2024-03-04", fq="pre", role=ROLE)
    after = v.read_adjusted("adjust", "000001.XSHE", basis_date="2024-03-05", fq="pre", role=ROLE)
    post = v.read_adjusted("adjust", "000001.XSHE", basis_date="2024-03-05", fq="post", role=ROLE)
    assert before["rows"][0]["fields"]["close"]["value"] == "13"
    assert after["rows"][0]["fields"]["close"]["value"] == "6.5"
    assert post["rows"][0]["fields"]["close"]["value"] == "26"
    assert after["rows"][0]["fields"]["volume"]["value"] == "117"
    assert all(x["date"] <= "2024-03-05" for x in after["factor_history_through_basis"])
    assert after["historical_action_chain_complete"] is False


def test_unbound_factor_basis_refused(delivery):
    with pytest.raises(DataError):
        view(delivery).read_adjusted("adjust", "000001.XSHE", basis_date="2026-01-01", fq="pre", role=ROLE)


def test_relocation_and_source_traversal(delivery, tmp_path):
    v = view(delivery)
    destination = tmp_path / "relocated"
    destination.mkdir()
    for p in delivery[0].iterdir():
        if p.is_file():
            shutil.copy2(p, destination / p.name)
    moved = IndependentQueryView(destination / "binding.json", expected_binding_sha256=v.sha256)
    assert moved.read_daily("daily", "000001.XSHE", source="sina", role=ROLE)["rows"]
    delivery[1]["sources"]["sina"]["path"] = "../outside.json"
    with pytest.raises(DataError, match="inside"):
        view(delivery).read_daily("daily", "000001.XSHE", source="sina", role=ROLE)


def test_eastmoney_volume_is_lots_not_fake_share_precision(delivery):
    root, b = delivery
    r = write(root, "eastmoney.json", {"rc": 0, "data": {"market": 0, "code": "000001",
        "klines": ["2024-03-01,12,13,14,10,7,8912.34,0,0,0,0"]}})
    b["sources"]["sina"].update(r, kind="eastmoney_daily_klines")
    f = view(delivery).read_daily("daily", "000001.XSHE", source="sina", role=ROLE)["rows"][0]["fields"]
    assert f["volume"]["value"] == "700" and f["money"]["value"] == "8912.34"


def test_opening_snapshot_is_not_an_order_matching_proof(delivery):
    root, b = delivery
    tick = {"time_label": "09:25", "event_kind": "opening_match", "status_raw": 2,
            "price": 13, "price_milli": 13000, "volume": 7, "order_count": 2, "record_hex": "0011"}
    r = write(root, "opening.json", {"code": "000001", "exchange": "sz", "trading_date": "2024-03-04", "ticks": [tick]})
    b["sources"]["sina"].update(r, kind="tdx_history_trades")
    b["products"]["daily"].update(kind="opening_match", date="2024-03-04")
    x = view(delivery).read_opening("daily", "000001.XSHE", role=ROLE)
    assert x["rows"][0]["price"] == "13" and x["rows"][0]["money"] is None
    assert not x["independent_matching_verified"] and not x["order_participation_verified"]
    tick["status_raw"] = 8
    r = write(root, "opening.json", {"code": "000001", "exchange": "sz", "trading_date": "2024-03-04", "ticks": [tick]})
    b["sources"]["sina"].update(r)
    with pytest.raises(DataError, match="one actual opening"):
        view(delivery).read_opening("daily", "000001.XSHE", role=ROLE)


@pytest.fixture
def index_delivery(delivery):
    root, b = delivery
    ref = write(root, "index.csv", b"symbol,name,opt-in,opt-out\nSZ000001,A,2020-01-01,2024-03-01\nSZ000002,B,2024-03-01,\nSH600000,C,2019-01-01,\n")
    b["sources"]["index"] = {**ref, "kind": "index_membership_intervals",
        "security": "000300.XSHG", "adjustment": "none", "official_chain_verified": False,
        "captured_start": "2026-10-10T01:00:00+00:00", "captured_end": "2026-10-10T01:00:01+00:00"}
    b["products"]["index"] = {"kind": "index_members", "security": "000300.XSHG",
        "sources": ["index"], "dates": ["2020-03-02", "2024-03-01"], "expected_members": 2}
    return delivery


def index_read(v, **kw):
    return v.read_index_members("index", "000300.XSHG", as_of_date=kw.pop("as_of_date", "2024-03-01"),
                               source="index", role=ROLE, **kw)


def test_index_interval_boundaries_and_uncertified_labels(index_delivery):
    v = view(index_delivery)
    before = index_read(v, as_of_date="2020-03-02", allow_uncertified=True)
    after = index_read(v, allow_uncertified=True)
    assert [r["security"] for r in before["rows"]] == ["000001.XSHE", "600000.XSHG"]
    assert [r["security"] for r in after["rows"]] == ["000002.XSHE", "600000.XSHG"]
    assert not after["official_chain_verified"] and not after["complete_membership_certified"]
    assert not after["historical_name_verified"] and not after["strategy_visible"]


@pytest.mark.parametrize("flag", [False, 1, "true", None])
def test_index_requires_explicit_candidate_acceptance(index_delivery, flag):
    with pytest.raises(DataError, match="allow_uncertified"):
        index_read(view(index_delivery), allow_uncertified=flag)


def test_index_date_scope_and_source_integrity(index_delivery):
    v = view(index_delivery)
    with pytest.raises(DataError, match="Unbound"):
        index_read(v, as_of_date="2024-03-02", allow_uncertified=True)
    (index_delivery[0] / "index.csv").write_text("changed")
    with pytest.raises(DataError, match="digest mismatch"):
        index_read(v, allow_uncertified=True)


@pytest.mark.parametrize("suffix", ["SZ000002,D,2023-01-01,\n", "SH600001,D,2023-01-01,\n"])
def test_index_duplicate_or_extra_member_refused(index_delivery, suffix):
    root, b = index_delivery
    raw = (root / "index.csv").read_bytes() + suffix.encode()
    b["sources"]["index"].update(write(root, "index.csv", raw))
    with pytest.raises(DataError, match="unexpected index members"):
        index_read(view(index_delivery), allow_uncertified=True)


def test_index_snapshot_retains_original_code_and_rejects_wrong_day(index_delivery):
    root, b = index_delivery
    ref = write(root, "snapshot.csv", b"Symbol,Name\n000001.SZ,A\n600000.SS,C\n")
    b["sources"]["index"].update(ref, kind="index_dated_snapshot", snapshot_date="2024-03-01")
    result = index_read(view(index_delivery), allow_uncertified=True)
    assert result["rows"][1]["source_symbol"] == "600000.SS"
    with pytest.raises(DataError, match="Snapshot date"):
        index_read(view(index_delivery), as_of_date="2020-03-02", allow_uncertified=True)
