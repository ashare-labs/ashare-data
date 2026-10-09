"""Finite owner/codec acceptance. Real sealed source replay, no RQ/native trading run."""

import copy
import json
import os
import shutil
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ashare_data import (
    DataError,
    M2AD08Call,
    M2ConsumerBinding,
    M2Read,
    Store,
)
from ashare_data.cli import main
from ashare_data.m2 import MODE
from ashare_data.m2_codec import (
    consume_previous,
    history_array,
    native_ad08_call,
    normalize_native_previous,
)
from ashare_data.m2_payloads import economic_payload
from ashare_data.m2_source import check_window, import_inputs, parse_inputs, policy
from ashare_data.m2_types import canonical_bytes, canonical_hash, exact_int, json_loads


def rejects(code, fn):
    with pytest.raises(DataError) as error:
        fn()
    assert error.value.code == code


@pytest.fixture(scope="session")
def bundle_path():
    path = Path(
        os.environ.get(
            "ASHARE_M2_BUNDLE", Path(__file__).parents[2] / "m2-product-evidence/input-bundle"
        )
    )
    if not path.is_dir():
        pytest.skip(
            "M2 sealed real fixture absent; set ASHARE_M2_BUNDLE to delivered offline bundle"
        )
    return path


@pytest.fixture(scope="session")
def real_objects(bundle_path):
    manifest = json.loads((bundle_path / "manifest.json").read_text())
    return {n: (bundle_path / n).read_bytes() for n in manifest["files"]}


@pytest.fixture(scope="session")
def prepared_m2(bundle_path, tmp_path_factory):
    s = Store.init(tmp_path_factory.mktemp("m2") / "store")
    sid = s.import_m2(bundle_path)
    profiles = {p.to_dict()["version"]: p.to_dict()["profile_sha256"] for p in s.m2_profiles(sid)}
    return s, sid, profiles


def use_for(prepared, window="w1", *, run="run-1", request=None):
    s, sid, profiles = prepared
    version = "m2.daily.600000.m2a.v1" if window == "m2a" else "m2.daily.600000.multiday.v1"
    view = s.m2(sid, profile_sha256=profiles[version], mode=MODE)
    plan = next(
        p.to_dict() for p in view.windows() if p.to_dict()["geometry"]["window_id"] == window
    )
    h = canonical_hash({"kind": "synthetic_consumer_declaration_not_execution"})
    b = M2ConsumerBinding(
        view.descriptor().sha256,
        window,
        plan["window_plan_sha256"],
        h,
        run,
        request or h,
        "previous-close-band.v1",
        h,
    )
    ack = [d.to_dict() for d in view.assumptions(window)]
    return view, view.use(window, assumption_ack=ack, consumer_binding=b), b


def context(
    use,
    day="2020-01-03",
    previous="2020-01-02",
    phase="DECIDE",
    generation=0,
    epoch=0,
    consumer=None,
):
    clock = "09:00:00" if phase == "DECIDE" else "08:00:00" if phase == "INITIALIZE" else "15:00:00"
    cutoff = (
        previous + "T23:59:59.999999+08:00"
        if phase in ("DECIDE", "INITIALIZE")
        else day + "T15:00:00+08:00"
    )
    return use.context(
        current_date=day,
        logical_at=day + "T" + clock + "+08:00",
        phase=phase,
        query_end=cutoff,
        visibility="assumed",
        consumer=consumer or ("strategy" if phase == "DECIDE" else "engine"),
        event_cursor=0,
        restore_generation=generation,
        epoch=epoch,
    )


def call(day="2020-01-03", previous="2020-01-02"):
    return M2AD08Call("600000.XSHG", day, previous, day, "1d", "close", 1, False, False, "pre")


@pytest.mark.parametrize("value", [100.0, True, "100", Decimal(100), np.int64(100), -1])
def test_strict_integer(value):
    rejects("M2_INTEGER", lambda: exact_int(value))


def test_generic_encoder_does_not_guess_typed_string_fields():
    assert (
        canonical_bytes(
            {"v": Decimal("12.4700"), "at": datetime(2020, 1, 3, 1, tzinfo=timezone.utc)}
        )
        == b'{"at":"2020-01-03T01:00:00.000000Z","v":"12.47"}'
    )
    rejects("M2_ENCODING", lambda: canonical_bytes({"n": 100.0}))
    rejects("M2_ENCODING", lambda: json_loads('{"n":1,"n":2}'))
    rejects("M2_ENCODING", lambda: json_loads('{"n":NaN}'))


@pytest.mark.parametrize(
    "change,code",
    [
        ({"bar_count": True}, "M2_INTEGER"),
        ({"bar_count": 1.0}, "M2_INTEGER"),
        ({"bar_count": 2}, "M2_INTEGER"),
        ({"include_now": 0}, "M2_UNREVIEWED_AD08"),
        ({"skip_suspended": True}, "M2_UNREVIEWED_AD08"),
        ({"field": "open"}, "M2_UNREVIEWED_AD08"),
        ({"frequency": "5m"}, "M2_UNREVIEWED_AD08"),
        ({"adjustment_requested": "none"}, "M2_UNREVIEWED_AD08"),
        ({"trade_date": datetime(2020, 1, 3)}, "M2_DATE"),
        ({"adjust_orig": "2020-01-06"}, "M2_UNREVIEWED_AD08"),
        ({"security": "000001.XSHE"}, "M2_UNREVIEWED_AD08"),
    ],
)
def test_exact_ad08_shape(change, code):
    rejects(code, lambda: M2AD08Call.from_dict({**call().to_dict(), **change}))


def test_real_import_idempotent_and_source_semantics(prepared_m2, bundle_path):
    s, sid, _ = prepared_m2
    assert s.import_m2(bundle_path) == sid
    v, u, _ = use_for(prepared_m2)
    assert len(v.windows()) == 2
    assert v.quality().to_dict()["daily_query_complete"] is False
    assert v.quality().to_dict()["available_at"] is None
    assert not u.envelope().to_dict()["backend_execution_authorized"]
    assert v.lineage().to_dict()["expansion_annex"]["jan20_rule"][
        "ordinary_share_T_plus_1_unchanged_by_this_amendment"
    ]
    rejects("M2_BACKEND_ADMISSION_REQUIRED", u.require_execution)


@pytest.mark.parametrize("mode", ["strict", "paper", "received", "verified", True, None])
def test_opt_in_does_not_promote_strict_or_paper(prepared_m2, mode):
    s, sid, p = prepared_m2
    rejects(
        "M2_EXPLICIT_MODE_REQUIRED",
        lambda: s.m2(sid, profile_sha256=next(iter(p.values())), mode=mode),
    )


def test_ack_scope_and_exact_content_required(prepared_m2):
    v, u, b = use_for(prepared_m2)
    ack = [d.to_dict() for d in v.assumptions("w1")]
    for wrong in [
        [],
        ack[:-1],
        ack + ack[:1],
        list(reversed(ack)),
        [{**ack[0], "content_sha256": "0" * 64}, *ack[1:]],
    ]:
        rejects(
            "M2_ASSUMPTION_REQUIRED", lambda: v.use("w1", assumption_ack=wrong, consumer_binding=b)
        )
    rejects(
        "M2_IDENTITY_MISMATCH",
        lambda: v.use(
            "w2", assumption_ack=[d.to_dict() for d in v.assumptions("w2")], consumer_binding=b
        ),
    )


def test_r1_nested_listing_hashes_and_canonical_numbers_cli(prepared_m2, tmp_path, capsys):
    _, u, _ = use_for(prepared_m2)
    c = context(u, phase="SUBMIT_MATCH")
    r = u.trading_state(c)
    p = r.result.to_dict()
    assert p["buy_round_lot"] == 100
    assert "receipt" not in p["listing"]
    assert canonical_hash(p) == r.evidence.to_dict()["result_sha256"]
    for forbidden in ("receipt", "authorization", "context", "evidence"):
        bad = copy.deepcopy(p)
        bad["listing"][forbidden] = {}
        rejects("M2_SCHEMA", lambda: economic_payload(bad))
    for invalid in (100.0, True, "100"):
        rejects("M2_INTEGER", lambda: economic_payload({**p, "buy_round_lot": invalid}))
    prev = u.decision_prev_close(context(u), call=call()).result.to_dict()
    assert canonical_hash(economic_payload({**prev, "value": "12.4700"})) == canonical_hash(prev)
    request = tmp_path / "canonical.json"
    request.write_text(json.dumps({"kind": "result", "payload": {**prev, "value": "12.4700"}}))
    assert main(["m2", "canonical", "--request", str(request)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["sha256"] == canonical_hash(prev) and out[
        "canonical_utf8"
    ].encode() == canonical_bytes(prev)


def test_r2_p_only_and_engine_scope(prepared_m2):
    _, u, _ = use_for(prepared_m2)
    c = context(u)
    r = u.decision_prev_close(c, call=call())
    assert r.value == Decimal("12.47")
    assert all(
        "state_row_sha256" not in x and "raw_price_fields" not in x
        for x in r.sources.to_dict()["rows"]
    )
    for method in (u.execution_bar, u.trading_state, u.valuation_bar):
        rejects("M2_ROLE_SCOPE", lambda: method(c))
        rejects(
            "M2_ROLE_SCOPE", lambda: method(context(u, phase="SUBMIT_MATCH", consumer="strategy"))
        )
    e = context(u, phase="SUBMIT_MATCH")
    assert u.execution_bar(e).result.to_dict()["close"] == "12.6"
    assert u.execution_bar(e).result.to_dict()["volume"] == 38018810
    assert u.valuation_bar(context(u, phase="AFTER_TRADING")).result.to_dict()["close"] == "12.6"
    rejects(
        "M2_ROLE_SCOPE",
        lambda: u.execution_bar(context(u, "2020-01-17", "2020-01-16", phase="SUBMIT_MATCH")),
    )


def test_timezone_hashes_and_clock_checks(prepared_m2):
    _, u, _ = use_for(prepared_m2)
    c = context(u)
    d = replace(
        c, logical_at="2020-01-03T01:00:00.000000Z", query_end="2020-01-02T15:59:59.999999Z"
    )
    assert c.to_dict() == d.to_dict()
    assert c.to_dict()["logical_at"].endswith(".000000Z")
    for changes, code in [
        ({"logical_at": "2020-01-03T09:00:00"}, "M2_CLOCK_INVALID"),
        ({"logical_at": "2020-01-03T15:00:00+08:00"}, "M2_CLOCK_INVALID"),
        ({"query_end": "2020-01-03T15:00:00+08:00"}, "M2_LOOKAHEAD"),
        ({"restore_generation": True}, "M2_INTEGER"),
        ({"visibility": "received"}, "M2_VISIBILITY"),
    ]:
        rejects(code, lambda: u.decision_prev_close(replace(c, **changes), call=call()))


def test_full_receipt_revalidation_stable_sources_and_identity(prepared_m2):
    _, u, _ = use_for(prepared_m2)
    c = context(u)
    r = u.decision_prev_close(c, call=call())
    c1 = replace(c, restore_generation=1, epoch=2)
    rejects("M2_RECEIPT_MISMATCH", lambda: u.verify(r, context=c1))
    fresh = r.with_authorization(u.revalidate(r, context=c1))
    assert fresh.evidence == r.evidence and fresh.sources == r.sources and fresh.result == r.result
    assert fresh.authorization != r.authorization
    assert u.verify(fresh, context=c1) == fresh
    # Persist through JSON; history remains evidence, not current permission.
    restored = M2Read.from_dict(json.loads(canonical_bytes(r.to_dict())))
    assert u.verify(restored, context=c) == r
    rejects("M2_RECEIPT_MISMATCH", lambda: u.verify(restored, context=c1))
    for section, key in [
        ("sources", "rows"),
        ("evidence", "assumption_refs"),
        ("evidence", "owner_pin_sha256"),
    ]:
        wire = r.to_dict()
        wire[section][key] = [] if key in ("rows", "assumption_refs") else "0" * 64
        forged = M2Read.from_dict(wire)
        rejects("M2_RECEIPT_MISMATCH", lambda: u.revalidate(forged, context=c1))


@pytest.mark.parametrize(
    "field",
    [
        "run_id",
        "consumer_request_sha256",
        "strategy_version_id",
        "assumption_ack_bundle_sha256",
        "run_binding_sha256",
        "owner_pin_sha256",
        "window_plan_sha256",
    ],
)
def test_cross_declaration_rejected_before_read(prepared_m2, field, monkeypatch):
    _, u, _ = use_for(prepared_m2)
    c = context(u)
    bad = replace(
        c, **{field: ("foreign" if field in ("run_id", "strategy_version_id") else "1" * 64)}
    )
    monkeypatch.setattr(
        u._view, "_fresh", lambda: pytest.fail("must reject binding before loading data")
    )
    rejects("M2_IDENTITY_MISMATCH", lambda: u.decision_prev_close(bad, call=call()))


def test_selected_window_not_any_registered_window(prepared_m2, monkeypatch):
    _, u1, _ = use_for(prepared_m2, "w1")
    _, u2, _ = use_for(prepared_m2, "w2")
    c2 = context(u2, "2020-01-06", "2020-01-03")
    r2 = u2.decision_prev_close(c2, call=call("2020-01-06", "2020-01-03"))
    monkeypatch.setattr(
        u1._view,
        "_fresh",
        lambda: pytest.fail("foreign selected window must fail before source read"),
    )
    rejects(
        "M2_IDENTITY_MISMATCH",
        lambda: u1.decision_prev_close(c2, call=call("2020-01-06", "2020-01-03")),
    )
    rejects(
        "M2_IDENTITY_MISMATCH",
        lambda: u1.revalidate(r2, context=context(u1, "2020-01-06", "2020-01-03", generation=1)),
    )


def test_wrong_call_date_and_rehashed_result_rejected(prepared_m2):
    _, u, _ = use_for(prepared_m2)
    c = context(u)
    r = u.decision_prev_close(c, call=call())
    wire = r.to_dict()
    wire["query"]["call"] = call("2020-01-06", "2020-01-03").to_dict()
    rejects("M2_UNREVIEWED_AD08", lambda: u.verify(wire, context=c))
    wire = r.to_dict()
    wire["result"]["value"] = "12.48"
    wire["evidence"]["result_sha256"] = canonical_hash(wire["result"])
    rejects("M2_RECEIPT_MISMATCH", lambda: u.verify(wire, context=c))


@pytest.mark.parametrize(
    "candidate,n",
    [
        ("2020-01-16", 1),
        ("2020-01-18", 1),
        ("2020-01-20", 1),
        ("2020-01-17", True),
        ("2020-01-17", 1.0),
        ("2020-01-17", 2),
    ],
)
def test_strict_terminal_successor(prepared_m2, candidate, n):
    _, u, _ = use_for(prepared_m2)
    c = context(u, "2020-01-16", "2020-01-15", phase="SETTLEMENT")
    rejects(
        "M2_INTEGER" if type(n) is not int or n != 1 else "M2_ROLE_SCOPE",
        lambda: u.successor(c, candidate_date=candidate, n=n),
    )


def test_terminal_listing_no_extra_price_or_event(prepared_m2):
    _, u, _ = use_for(prepared_m2, "w2")
    c = context(u, "2020-01-17", "2020-01-16", phase="SETTLEMENT")
    r = u.successor(c, candidate_date="2020-01-20")
    assert r.result.to_dict()["absolute_delisting_date"] is None
    assert r.query.to_dict()["target_date"] == "2020-01-20"
    rejects(
        "M2_ROLE_SCOPE",
        lambda: u.valuation_bar(context(u, "2020-01-20", "2020-01-17", phase="SETTLEMENT")),
    )


@pytest.mark.parametrize(
    "fault",
    [
        "missing_calendar",
        "same_successor",
        "missing_row",
        "unknown_st",
        "suspended",
        "related_late_payment",
        "unknown_event_date",
        "nonunit_adjustment",
    ],
)
def test_fact_conflicts_cannot_be_covered_by_ack(real_objects, fault):
    data = parse_inputs(real_objects)
    spec = copy.deepcopy(policy()["windows"]["w1"])
    if fault == "missing_calendar":
        data["calendar"].pop("2020-01-04")
    if fault == "same_successor":
        spec["calendar_links"][-1]["next_open"] = "2020-01-16"
    if fault == "missing_row":
        data["bars"].pop("2020-01-10")
    if fault == "unknown_st":
        data["states"]["2020-01-10"]["isST"] = None
    if fault == "suspended":
        data["bars"]["2020-01-10"]["tradestatus"] = "0"
    if fault == "related_late_payment":
        data["events"][0].update(record_date="2020-01-10", pay_date="2020-07-23")
    if fault == "unknown_event_date":
        data["events"][0]["record_date"] = None
    if fault == "nonunit_adjustment":
        data["states"]["2020-01-10"]["preclose"] = "11.5"
    with pytest.raises(DataError):
        check_window(data, spec)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        True,
        12,
        Decimal("12.47"),
        np.float32(12.47),
        float("nan"),
        float("inf"),
        0.0,
        -1.0,
        np.array([], dtype="float64"),
        np.array([12.47, 12.47]),
        np.array([[12.47]]),
        np.array([12.47], dtype="float32"),
    ],
)
def test_native_codec_rejects_shapes_and_types(bad):
    rejects("M2_NATIVE_VALUE", lambda: normalize_native_previous(bad))


def test_all_three_windows_real_public_codec(prepared_m2):
    counts = {}
    for wid in ("m2a", "w1", "w2"):
        v, u, _ = use_for(prepared_m2, wid)
        spec = v.profile().to_dict()["window_specs"][wid]
        for link in spec["calendar_links"]:
            day, previous = link["current"], link["previous_open"]
            c = context(u, day, previous)
            actual = native_ad08_call(
                security="600000.XSHG",
                trade_date=day,
                dt=pd.Timestamp(previous),
                adjust_orig=datetime.fromisoformat(day),
                frequency="1d",
                fields="close",
                bar_count=1,
                include_now=False,
                skip_suspended=False,
                adjust_type="pre",
                time_policy="rq641_shanghai_midnight",
            )
            read = u.decision_prev_close(c, call=actual)
            array = history_array(read, use=u, context=c)
            consumed = consume_previous(array[0], read=read, use=u, context=c)
            assert consumed.value == Decimal(str(float(array[0]))) == read.value
            assert not consumed.codec_observation.to_dict()["native_call_observed"]
            rejects(
                "M2_NATIVE_MISMATCH",
                lambda: consume_previous(float(array[0]) + 0.01, read=read, use=u, context=c),
            )
        counts[wid] = len(spec["calendar_links"])
    assert counts == {"m2a": 2, "w1": 10, "w2": 10}


def test_native_time_codec_does_not_truncate_intraday():
    kwargs = dict(
        security="600000.XSHG",
        trade_date="2020-01-03",
        dt=datetime(2020, 1, 2, 12),
        adjust_orig=datetime(2020, 1, 3),
        frequency="1d",
        fields="close",
        bar_count=1,
        include_now=False,
        skip_suspended=False,
        adjust_type="pre",
        time_policy="rq641_shanghai_midnight",
    )
    rejects("M2_NATIVE_TIME", lambda: native_ad08_call(**kwargs))


@pytest.mark.parametrize("fault", ["after_prepare", "after_manifest"])
def test_publication_interruption_recovery(bundle_path, tmp_path, fault):
    s = Store.init(tmp_path / "interrupted")
    with pytest.raises(RuntimeError):
        import_inputs(s, bundle_path, _fault=fault)
    pending = s.m2_snapshots()[0]
    assert pending["status"] == "prepared"
    sid = pending["dataset_id"]
    assert s.recover_m2() == [{"dataset_id": sid, "status": "published", "error": None}]
    assert s.import_m2(bundle_path) == sid and s.recover_m2() == []


def test_mutated_source_requires_new_review_and_old_view_rechecks(
    bundle_path, prepared_m2, tmp_path
):
    candidate = tmp_path / "mutant"
    shutil.copytree(bundle_path, candidate)
    f = candidate / "old/daily.json"
    f.write_bytes(f.read_bytes() + b" ")
    s = Store.init(tmp_path / "store")
    rejects("M2_INTEGRITY", lambda: s.import_m2(candidate))
    manifest = json.loads((candidate / "manifest.json").read_text())
    manifest["files"]["old/daily.json"]["bytes"] += 1
    (candidate / "manifest.json").write_bytes(canonical_bytes(manifest))
    rejects("M2_UNREVIEWED_INPUT", lambda: s.import_m2(candidate))
    original, sid, profiles = prepared_m2
    isolated = tmp_path / "isolated"
    shutil.copytree(original.root, isolated)
    prepared = (Store(isolated), sid, profiles)
    _, u, _ = use_for(prepared)
    victim = next((isolated / "m2-objects").iterdir())
    victim.chmod(0o600)
    victim.write_bytes(b"changed")
    with pytest.raises(DataError):
        u.decision_prev_close(context(u), call=call())
