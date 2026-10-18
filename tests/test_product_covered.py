"""Synthetic reference packages exercise ordinary publication, never market coverage."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from ashare_data import DataError, Store
from ashare_data.core.model import canonical, digest
from ashare_data.product import LocalDailyProduct, validate_covered_input
from ashare_data.product.configuration import resolve
from ashare_data.storage.local_daily import BAR_FIELDS, LocalDailyStore
from test_corporate_history import fixture_data, seal
from test_daily_execution import package, write_supplement  # noqa: F401
from test_daily_research import data  # noqa: F401
from test_historical_market import (
    dataset as historical_dataset,
    fact,
    package as historical_package,
)

SID = "000001.XSHE"


@pytest.fixture
def covered(package, tmp_path):  # noqa: F811
    base, payload, execution_root = package
    execution = write_supplement(execution_root, base, payload)
    dates = base.descriptor()["windows"][0]["sessions"]
    corpus = fixture_data()
    corpus["actions"][0]["fields"].update(
        announcement_date=dates[120],
        record_date=dates[121],
        ex_date=dates[122],
        payment_date=dates[122],
        cash_per_share="1",
    )
    binding, sha = seal(tmp_path / "corporate", corpus)
    refs = dict(
        name="test-evidence",
        kind="daily_execution",
        research=dict(binding_path=str(base.path), binding_sha256=base.binding_sha256),
        execution=dict(binding_path=str(execution.path), binding_sha256=execution.binding_sha256),
        corporate=dict(binding_path=str(binding), binding_sha256=sha),
    )
    root = tmp_path / "library"
    store = LocalDailyStore(root)
    receipts = [
        dict(
            url="https://fixture.invalid/synthetic-only",
            sha256="a" * 64,
            received_at="2026-01-01T00:00:00+00:00",
            status=200,
        )
    ]
    receipt = store.receipts(receipts)[0]
    store.save_universe(
        [
            dict(
                symbol="sz000001",
                name="Synthetic",
                ipo="1990-01-01",
                out=None,
                board="main",
                source="synthetic",
                receipt="a" * 64,
            )
        ],
        receipts,
        dates[0],
        dates[-1],
    )
    store.save_calendar(dates, receipts)
    for day in dates:
        row = dict(
            date=day,
            open="10.50",
            high="10.60",
            low="10.40",
            close="10.55",
            volume="12345",
            amount="130239.75",
            preclose="10.05",
            tradestatus="1",
            is_st="0",
        )
        store.db.execute(
            "INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "sz000001",
                day,
                "baostock",
                *(row[k] for k in BAR_FIELDS),
                digest(row),
                receipt,
                receipts[0]["received_at"],
            ),
        )
    store.db.commit()
    store.close()
    return dict(
        product=LocalDailyProduct(root, references=refs),
        refs=refs,
        root=root,
        dates=dates,
        start=dates[120],
        end=dates[123],
        package=package,
        corpus=corpus,
        corporate_root=binding.parent,
        output=tmp_path / "release",
    )


def publish(c, **kwargs):
    return c["product"].publish(c["output"], [SID], c["start"], c["end"], **kwargs)


def view(c, result):
    return Store(c["output"]).local_daily_product(result["dataset_id"])


def change_main(c, clause, params=()):
    with sqlite3.connect(c["root"] / "daily.sqlite3") as db:
        db.execute("UPDATE bars SET " + clause + " WHERE date=?", (*params, c["start"]))


def update_execution(c, edit):
    base, payload, root = c["package"]
    edit(payload)
    updated = write_supplement(root, base, payload)
    c["refs"]["execution"]["binding_sha256"] = updated.binding_sha256


def test_coverage_is_read_only_and_full_identity_is_deterministic(covered):
    c = covered
    before = (c["root"] / "daily.sqlite3").stat().st_mtime_ns
    a = c["product"].coverage([SID], c["start"], c["end"])
    assert a["ready"] and not a["gaps"]
    assert a == c["product"].coverage([SID], c["start"], c["end"])
    assert a["coverage_sha256"] == digest({k: v for k, v in a.items() if k != "coverage_sha256"})
    assert a["scope"] == dict(securities=[SID], start=c["start"], end=c["end"], source="auto")
    assert not c["output"].exists()
    assert (c["root"] / "daily.sqlite3").stat().st_mtime_ns == before


def test_ordinary_default_freezes_main_prices_and_original_separate_corporate(covered):
    c = covered
    result = publish(c)
    assert result == publish(c)
    v = view(c, result)
    doc, schedule, coverage = v.read(), v.read_corporate_schedule(), v.read_coverage()
    assert result["mode"] == "covered_research" and coverage["ready"]
    assert "research_model" not in doc and "corporate_schedule" not in doc
    assert doc["actions"] == [] and len(schedule["rows"]) == 1
    assert set(r["close"] for r in doc["daily"]) == {"10.55"}  # Distinct from snapshot prices.
    assert len(doc["reference_evidence"]["market"]["rows"]) == 4
    assert len(doc["reference_evidence"]["market"]["admission"]["requests"]) == 8
    assert doc["reference_evidence"]["market"]["rows"][0]["upper_limit"]["status"] == "derived"
    assert schedule["execution_permission"] is schedule["historical_pit"] is False
    assert validate_covered_input(doc, schedule)
    assert (
        hashlib.sha256(Path(result["portfolio_input_path"]).read_bytes()).hexdigest()
        == result["portfolio_input_sha256"]
    )


def test_missing_references_blocks_default_without_diagnostic_fallback(covered):
    c = covered
    p = LocalDailyProduct(c["root"])
    result = p.coverage([SID], c["start"], c["end"])
    assert not result["ready"] and result["gaps"][0]["code"] == "PRODUCT_REFERENCE_MISSING"
    with pytest.raises(DataError) as error:
        p.publish(c["output"], [SID], c["start"], c["end"])
    assert error.value.code == "PRODUCT_COVERAGE_GAPS" and not c["output"].exists()
    result = p.publish(c["output"], [SID], c["start"], c["end"], mode="price_only_no_limits")
    assert view(c, result).read()["research_model"] == "price_only_no_actions"
    assert view(c, result).read_corporate_schedule() is None


@pytest.mark.parametrize(
    "change,code",
    [
        ("preclose='10.06'", "PRODUCT_REFERENCE_PRICE_CONFLICT"),
        ("is_st='1'", "PRODUCT_SOURCE_STATE_CONFLICT"),
        ("tradestatus='0'", "PRODUCT_SOURCE_STATE_CONFLICT"),
        ("high='12'", "PRODUCT_PRICE_LIMIT_CONFLICT"),
    ],
)
def test_price_and_state_source_conflicts_are_not_relabeled(covered, change, code):
    change_main(covered, change)
    c = covered
    result = c["product"].coverage([SID], c["start"], c["end"])
    assert not result["ready"] and code in {g["code"] for g in result["gaps"]}
    with pytest.raises(DataError):
        publish(c)
    assert not c["output"].exists()


def test_unknown_unselected_observation_is_warning_but_selected_unknown_blocks(covered):
    c = covered

    def edit(payload):
        for field in ("is_st", "upper_limit", "lower_limit"):
            payload["states"][-1][field].update(value=None, status="unavailable", assumption_ids=[])

    update_execution(c, edit)
    first = c["product"].publish(c["output"], [SID], c["start"], c["start"])
    admission = view(c, first).read()["reference_evidence"]["market"]["admission"]
    assert admission["ready"] and not admission["whole_window_ready"]
    assert any(
        i.get("usage") == "observation_only" and not i["blocking"] for i in admission["issues"]
    )
    result = c["product"].coverage([SID], c["start"], c["end"])
    assert not result["ready"]
    assert any(g["code"] == "FIELD_UNAVAILABLE" and g["date"] == c["end"] for g in result["gaps"])


def test_known_suspension_is_preserved_not_replaced_by_volume_inference(covered):
    c = covered
    update_execution(c, lambda payload: payload["states"][0]["suspended"].update(value=True))
    change_main(c, "tradestatus='0',volume='0'")
    result = publish(c)
    row = next(r for r in view(c, result).read()["daily"] if r["date"] == c["start"])
    assert row["suspended"] is True and row["state_origin"] == "daily_execution"


def test_reference_tampering_and_corporate_missing_date_fail_before_any_output(covered):
    c = covered
    path = Path(c["refs"]["execution"]["binding_path"]).parent / "data.json"
    raw = path.read_bytes()
    path.write_bytes(raw + b" ")
    result = c["product"].coverage([SID], c["start"], c["end"])
    assert not result["ready"] and any("INTEGRITY" in g["code"] for g in result["gaps"])
    path.write_bytes(raw)
    c["corpus"]["actions"][0]["fields"].pop("payment_date")
    _, sha = seal(c["corporate_root"], c["corpus"])
    c["refs"]["corporate"]["binding_sha256"] = sha
    result = c["product"].coverage([SID], c["start"], c["end"])
    assert not result["ready"] and any(
        g.get("owner_issue", {}).get("field") == "payment_date" for g in result["gaps"]
    )
    assert not c["output"].exists()


def test_missing_warmup_calendar_and_reference_scope_report_specific_gaps(covered):
    c = covered
    result = c["product"].coverage([SID], c["dates"][119], c["end"])
    assert result["gaps"][0]["code"] == "PRODUCT_WARMUP_UNAVAILABLE"
    # Warmup exists, but the selected reference output cannot cover a successor day.
    result = c["product"].coverage([SID], c["start"], c["dates"][-1])
    assert not result["ready"] and result["gaps"][0]["code"] == "PRODUCT_WARMUP_UNAVAILABLE"
    with sqlite3.connect(c["root"] / "daily.sqlite3") as db:
        db.execute("DELETE FROM calendar WHERE date=?", (c["dates"][121],))
    result = c["product"].coverage([SID], c["start"], c["end"])
    assert not result["ready"] and any(
        g["code"] == "PRODUCT_CALENDAR_CONFLICT" for g in result["gaps"]
    )


def test_same_request_concurrent_publication_creates_one_consistent_release(covered):
    c = covered
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(lambda _: publish(c), range(3)))
    assert results[0] == results[1] == results[2]
    assert view(c, results[0]).read_coverage()["ready"]
    with sqlite3.connect(c["output"] / "catalog.sqlite") as db:
        assert db.execute("SELECT count(*) FROM local_daily_products").fetchone()[0] == 1


@pytest.mark.parametrize(
    "name", ["input.json", "corporate-schedule.json", "coverage.json", "manifest.json"]
)
def test_every_frozen_file_is_checked_including_after_restore(covered, tmp_path, name):
    c = covered
    result = publish(c)
    restored = tmp_path / "restored"
    shutil.copytree(c["output"], restored)
    shutil.rmtree(c["root"])
    shutil.rmtree(c["corporate_root"])
    v = Store(restored).local_daily_product(result["dataset_id"])
    assert v.read_coverage()["ready"] and len(v.read_corporate_schedule()["rows"]) == 1
    target = restored / "local_daily_products" / result["dataset_id"] / name
    target.chmod(0o600)
    target.write_bytes(b"{}")
    with pytest.raises(DataError):
        v.read()


def test_frozen_projection_requires_source_facts_and_matching_model(covered):
    c = covered
    v = view(c, publish(c))
    doc, schedule = v.read(), v.read_corporate_schedule()
    mutations = [
        lambda d: d.pop("reference_evidence"),
        lambda d: d["daily"][120].update(upper_limit="999"),
        lambda d: d["reference_evidence"]["source_bindings"]["corporate"].update(
            binding_sha256="0" * 64
        ),
        lambda d: d["reference_evidence"]["market"]["admission"].update(requests=[]),
    ]
    for edit in mutations:
        altered = deepcopy(doc)
        edit(altered)
        with pytest.raises(DataError):
            validate_covered_input(altered, schedule)


def test_configuration_explicit_reference_choice_and_path_safety(covered, tmp_path):
    c = covered
    assets = {
        "main": dict(kind="local_daily", path=str(c["root"])),
        "release": dict(kind="store", path="out"),
    }
    for role, spec in c["refs"].items():
        if role in {"name", "kind"}:
            continue
        assets[role] = dict(
            kind={
                "research": "daily_research",
                "execution": "daily_execution",
                "corporate": "corporate_history",
            }[role],
            path=str(Path(spec["binding_path"]).parent),
            binding="binding.json",
            binding_sha256=spec["binding_sha256"],
        )
    profile = dict(
        kind="product",
        daily_asset="main",
        release_store_asset="release",
        reference_sets={
            "selected": dict(
                kind="daily_execution",
                research_asset="research",
                execution_asset="execution",
                corporate_history_asset="corporate",
            )
        },
        default_reference_set="selected",
    )
    path = tmp_path / "deployment.json"
    config = dict(
        schema="ashare-deployment.v1", data_root=".", assets=assets, profiles={"product": profile}
    )
    path.write_bytes(canonical(config))
    r = resolve(path)
    assert r["references"]["name"] == "selected" and set(r["selected_assets"]) == set(assets)
    assert LocalDailyProduct.from_deployment(path).coverage([SID], c["start"], c["end"])["ready"]
    with pytest.raises(DataError):
        resolve(path, reference_set="another")
    config["assets"]["research"]["binding"] = "../outside.json"
    path.write_bytes(canonical(config))
    with pytest.raises(DataError):
        resolve(path)


@pytest.mark.parametrize(
    "case,code",
    [
        ("valid", None),
        ("suspended", None),
        ("unlimited", "PRODUCT_STATE_UNAVAILABLE"),
        ("not_listed", "NOT_YET_LISTED"),
        ("limit_conflict", "PRODUCT_LIMIT_SOURCE_CONFLICT"),
    ],
)
def test_historical_reference_is_explicit_and_preserves_boundaries(covered, tmp_path, case, code):
    c = covered
    sample = historical_dataset()
    template = sample["states"][0]
    template.update(
        security=SID,
        reference_price=fact("10.05"),
        upper_limit=fact("11.06", "derived"),
        lower_limit=fact("9.05", "derived"),
    )
    template["rule"].update(ratio="0.10")
    template["source_claimed_limits"].update(
        raw_fields={"up_limit": "11.06", "down_limit": "9.05"}, validation="model_agreement"
    )
    sample["states"] = [{**deepcopy(template), "date": day} for day in c["dates"][120:124]]
    sample.update(
        actions=[],
        universes=[],
        instruments=[
            dict(
                security=SID,
                initial_listing_date=fact("1990-01-01"),
                delisting_date=fact(None),
                source_refs=["raw.txt"],
            )
        ],
    )
    if case == "suspended":
        sample["states"][0]["suspended"] = fact(True)
        change_main(c, "tradestatus='0',volume='0'")
    elif case == "not_listed":
        sample["instruments"][0]["initial_listing_date"] = fact(c["end"])
    elif case == "unlimited":
        sample["states"][0]["rule"].update(kind="unlimited", ratio=None)
        for field in ("upper_limit", "lower_limit"):
            sample["states"][0][field] = {**fact(None), "status": "not_applicable"}
        sample["states"][0]["source_claimed_limits"].update(
            raw_fields={}, validation="model_unavailable"
        )
    elif case == "limit_conflict":
        sample["states"][0]["source_claimed_limits"].update(
            raw_fields={"up_limit": "12.00", "down_limit": "8.00"},
            validation="conflict_with_rule_model",
        )
    root = tmp_path / "historical"
    root.mkdir()
    path, sha = historical_package(root, sample)
    refs = dict(
        name="explicit-historical",
        kind="historical_market",
        market=dict(binding_path=str(path), binding_sha256=sha),
        corporate=c["refs"]["corporate"],
    )
    report = c["product"].coverage([SID], c["start"], c["end"], references=refs)
    if code:
        assert not report["ready"] and code in {g["code"] for g in report["gaps"]}
    else:
        assert report["ready"], report["gaps"]
        result = publish(c, references=refs)
        doc = view(c, result).read()
        assert doc["reference_evidence"]["market"]["kind"] == "historical_market"
        assert len(doc["reference_evidence"]["market"]["admissions"]) == 8


def test_rehashed_price_projection_still_fails_the_source_mapping(covered):
    c = covered
    result = publish(c)
    old = c["output"] / "local_daily_products" / result["dataset_id"]
    doc = json.loads((old / "input.json").read_bytes())
    doc["daily"][120]["upper_limit"] = "999"
    raw = canonical(doc)
    manifest = json.loads((old / "manifest.json").read_bytes())
    manifest.update(
        portfolio_input_sha256=hashlib.sha256(raw).hexdigest(), portfolio_input_bytes=len(raw)
    )
    new_id = digest(manifest)
    for name, body in [("input.json", raw), ("manifest.json", canonical(manifest))]:
        (old / name).chmod(0o600)
        (old / name).write_bytes(body)
    old.rename(old.with_name(new_id))
    with sqlite3.connect(c["output"] / "catalog.sqlite") as db:
        db.execute(
            "UPDATE local_daily_products SET dataset_id=?, input_sha256=? WHERE dataset_id=?",
            (new_id, manifest["portfolio_input_sha256"], result["dataset_id"]),
        )
    with pytest.raises(DataError, match="Projected state value"):
        Store(c["output"]).local_daily_product(new_id)


def test_release_reference_symlink_cannot_escape_release(covered, tmp_path):
    c = covered
    result = publish(c)
    root = c["output"] / "local_daily_products" / result["dataset_id"]
    target = root / "corporate-schedule.json"
    outside = tmp_path / "outside.json"
    target.rename(outside)
    target.symlink_to(outside)
    with pytest.raises(DataError) as error:
        view(c, result)
    assert error.value.code == "PRODUCT_INTEGRITY"
