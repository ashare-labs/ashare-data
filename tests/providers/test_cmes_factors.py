"""Synthetic edge cases plus a small numerical regression from retained CMES packages."""

from copy import deepcopy
from decimal import Decimal, localcontext, ROUND_DOWN
import json
from pathlib import Path
import zipfile

import pytest

from ashare_data import DataError, Store
from ashare_data.core.model import canonical, digest
from ashare_data.providers.cmes_factors import CMESFactorView, adjust_prices, import_cmes_factors

SECURITY = "600000.XSHG"
MEMBER = "SH.600000.csv"
HFQ = "date,hfq_factor\n2024-01-03,4\n2024-01-02,2\n1900-01-01,1\n"
QFQ = "date,qfq_factor\n2024-01-03,1\n2024-01-02,2\n1900-01-01,4\n"


def archives(root, hfq=HFQ, qfq=QFQ):
    paths = []
    for kind, value in (("hfq", hfq), ("qfq", qfq)):
        path = root / (kind + ".zip")
        with zipfile.ZipFile(path, "w") as z:
            z.writestr(MEMBER, value)
            z.writestr("ignored.csv", "unrelated data is not parsed")
        paths.append(path)
    return paths


def load(store, paths, through="2024-01-07"):
    identity = import_cmes_factors(
        store, hfq_path=paths[0], qfq_path=paths[1], members={SECURITY: MEMBER}, through=through
    )
    return CMESFactorView(store, identity)


@pytest.fixture
def factors(tmp_path):
    return load(Store.init(tmp_path / "store"), archives(tmp_path))


def bars():
    rows = [
        dict(
            security=SECURITY,
            date=date,
            open=price,
            high=price,
            low=price,
            close=price,
            volume="100",
            amount=None,
            preclose="9",
            source="fixture",
        )
        for date, price in (("2024-01-02", "10"), ("2024-01-03", "5"))
    ]
    receipt = dict(rows_sha256=digest(rows), source="synthetic")
    receipt["receipt_sha256"] = digest(receipt)
    return dict(
        schema="ad-local-daily-prices.v1",
        price_basis="raw",
        rows=rows,
        start="2024-01-02",
        end="2024-01-03",
        receipt=receipt,
        gaps=[dict(code="MISSING_REFERENCE")],
        limitations=["Raw source limitation"],
    )


def test_raw_does_not_require_or_read_factors():
    raw = bars()
    assert adjust_prices(raw) == raw
    with pytest.raises(DataError, match="no adjustment basis"):
        adjust_prices(raw, basis_date="2024-01-03")


def test_direction_inclusive_asof_and_fixed_anchors(factors):
    raw = bars()
    before = deepcopy(raw)
    pre = factors.adjust_prices(raw, adjustment="pre", basis_date="2024-01-06")
    post = factors.adjust_prices(raw, adjustment="post", basis_date="2024-01-06")
    assert [Decimal(r["close"]) for r in pre["rows"]] == [Decimal(5), Decimal(5)]
    assert [Decimal(r["close"]) for r in post["rows"]] == [Decimal(20), Decimal(20)]
    # CMES qfq is a divisor: the earlier price is 10/2, never 10*2.
    assert Decimal(pre["rows"][0]["close"]) == Decimal(10) / Decimal(2)
    assert pre["adjustment"]["price_anchor_dates"] == {SECURITY: "2024-01-06"}
    assert pre["adjustment"]["anchor_effective_dates"] == {SECURITY: "2024-01-03"}
    assert post["adjustment"]["price_anchor_dates"] == {SECURITY: "1900-01-01"}
    assert factors.factor_at(SECURITY, "2024-01-02")["hfq_factor"] == "2"
    assert factors.factor_at(SECURITY, "2024-01-01")["effective_date"] == "1900-01-01"
    assert factors.factor_at(SECURITY, "2024-01-07")["effective_date"] == "2024-01-03"
    assert raw == before
    assert pre["gaps"] == raw["gaps"]
    for output in (pre, post):
        assert output["price_basis"] in ("pre", "post")
        assert output["receipt"]["raw_receipt"] == raw["receipt"]
        assert output["receipt"]["rows_sha256"] == digest(output["rows"])
        assert output["receipt"]["receipt_sha256"] == digest(
            {k: v for k, v in output["receipt"].items() if k != "receipt_sha256"}
        )
        assert output["adjustment"]["execution_permission"] is False
        for row in output["rows"]:
            assert (row["volume"], row["amount"], row["preclose"]) == ("100", None, "9")


def test_null_prices_are_preserved(factors):
    raw = bars()
    raw["rows"][0]["open"] = None
    raw["receipt"]["rows_sha256"] = digest(raw["rows"])
    assert (
        factors.adjust_prices(raw, adjustment="pre", basis_date="2024-01-03")["rows"][0]["open"]
        is None
    )


def test_decimal_context_and_return_mutation_do_not_change_future_results(factors):
    raw = bars()
    for field in ("open", "high", "low", "close"):
        raw["rows"][0][field] = "10.123456789123456789"
    raw["receipt"]["rows_sha256"] = digest(raw["rows"])
    expected = factors.adjust_prices(raw, adjustment="pre", basis_date="2024-01-03")
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 3, ROUND_DOWN
        actual = factors.adjust_prices(raw, adjustment="pre", basis_date="2024-01-03")
    assert actual == expected
    assert Decimal(actual["rows"][0]["close"]) == Decimal("5.0617283945617283945")
    actual["adjustment"]["policy"]["decimal_precision"] = 1
    actual["receipt"]["raw_receipt"]["source"] = "mutated"
    assert factors.adjust_prices(raw, adjustment="pre", basis_date="2024-01-03") == expected


@pytest.mark.parametrize(
    "changes",
    [
        {"adjustment": "qfq", "basis_date": "2024-01-03"},
        {"adjustment": "pre", "basis_date": None},
        {"adjustment": "post", "basis_date": "2024-01-02"},
        {"adjustment": "pre", "basis_date": "2024-01-08"},
    ],
)
def test_invalid_or_future_basis_is_rejected(factors, changes):
    with pytest.raises(DataError):
        factors.adjust_prices(bars(), **changes)


def test_adjusted_input_tampered_raw_missing_security_and_duplicate_rows_fail(factors):
    variants = []
    raw = bars()
    raw["price_basis"] = "pre"
    variants.append(raw)
    raw = bars()
    raw["rows"][0]["close"] = "11"
    variants.append(raw)
    raw = bars()
    raw["rows"][0]["security"] = "000001.XSHE"
    raw["receipt"]["rows_sha256"] = digest(raw["rows"])
    variants.append(raw)
    raw = bars()
    raw["rows"].append(deepcopy(raw["rows"][0]))
    raw["receipt"]["rows_sha256"] = digest(raw["rows"])
    variants.append(raw)
    for raw in variants:
        with pytest.raises(DataError):
            factors.adjust_prices(raw, adjustment="pre", basis_date="2024-01-03")


def test_no_backfill_or_future_factor_lookup(factors):
    for security, date in (
        (SECURITY, "1899-12-31"),
        (SECURITY, "2024-01-08"),
        ("000001.XSHE", "2024-01-02"),
    ):
        with pytest.raises(DataError):
            factors.factor_at(security, date)


@pytest.mark.parametrize(
    "hfq,qfq",
    [
        (HFQ.replace("2024-01-03,4", "2024-01-03,0"), QFQ),
        (HFQ.replace("2024-01-03,4", "2024-01-03,NaN"), QFQ),
        (HFQ.replace("2024-01-03,4", "2024-01-03,-4"), QFQ),
        (HFQ.replace("2024-01-03,4", "2024-01-03,Infinity"), QFQ),
        (HFQ + "2024-01-03,4\n", QFQ),
        (HFQ.replace("date,hfq_factor", "date,factor"), QFQ),
        (HFQ.replace("2024-01-03,4", "2024-01-03,4,extra"), QFQ),
        (HFQ, QFQ.replace("2024-01-03", "2024-01-04")),
        (HFQ, QFQ.replace("2024-01-02,2", "2024-01-02,3")),
        (HFQ.replace("2024-01-03", "2024-02-30"), QFQ),
    ],
)
def test_invalid_csv_and_inconsistent_pairs_are_not_published(tmp_path, hfq, qfq):
    store = Store.init(tmp_path / "store")
    with pytest.raises(DataError):
        load(store, archives(tmp_path, hfq, qfq))
    assert not (store.root / "cmes_factors").exists()


def test_missing_and_duplicate_zip_members_fail(tmp_path):
    store = Store.init(tmp_path / "store")
    paths = archives(tmp_path)
    with zipfile.ZipFile(paths[0], "a") as z, pytest.warns(UserWarning, match="Duplicate name"):
        z.writestr(MEMBER, HFQ)
    with pytest.raises(DataError, match="Missing or duplicate"):
        load(store, paths)
    paths = archives(tmp_path)
    with pytest.raises(DataError, match="Missing or duplicate"):
        import_cmes_factors(
            store,
            hfq_path=paths[0],
            qfq_path=paths[1],
            members={"000001.XSHE": "SZ.000001.csv"},
            through="2024-01-03",
        )


def test_fixed_versions_survive_append_and_revision(tmp_path):
    store = Store.init(tmp_path / "store")
    paths = archives(tmp_path)
    old = load(store, paths)
    expected = canonical(old.adjust_prices(bars(), adjustment="pre", basis_date="2024-01-03"))
    assert load(store, paths).dataset_id == old.dataset_id
    # A later action renormalizes every qfq entry, while hfq history stays unchanged.
    future_h = HFQ.replace("date,hfq_factor\n", "date,hfq_factor\n2025-01-02,8\n")
    future_q = "date,qfq_factor\n2025-01-02,1\n2024-01-03,2\n2024-01-02,4\n1900-01-01,8\n"
    appended = load(store, archives(tmp_path, future_h, future_q), through="2025-01-02")
    assert appended.dataset_id != old.dataset_id
    assert (
        appended.adjust_prices(bars(), adjustment="pre", basis_date="2024-01-03")["rows"]
        == json.loads(expected)["rows"]
    )
    revised = load(
        store,
        archives(
            tmp_path,
            future_h.replace("2024-01-02,2", "2024-01-02,2.5"),
            future_q.replace("2024-01-02,4", "2024-01-02,3.2"),
        ),
        through="2025-01-02",
    )
    assert (
        revised.adjust_prices(bars(), adjustment="pre", basis_date="2024-01-03")["rows"][0]["close"]
        != "5.0"
    )
    for view in (old, CMESFactorView(store, old.dataset_id)):
        assert (
            canonical(view.adjust_prices(bars(), adjustment="pre", basis_date="2024-01-03"))
            == expected
        )


def test_tampering_is_rejected_on_reopen(tmp_path):
    store = Store.init(tmp_path / "store")
    factors = load(store, archives(tmp_path))
    path = store.root / "cmes_factors" / (factors.dataset_id + ".json")
    path.chmod(0o644)
    path.write_text("{}")
    with pytest.raises(DataError, match="hash differs"):
        CMESFactorView(store, factors.dataset_id)
    with pytest.raises(DataError, match="SHA256"):
        CMESFactorView(store, "latest")


def test_actual_ex_date_numerical_regressions(tmp_path):
    fixture = json.loads((Path(__file__).resolve().parents[1] / "fixtures/cmes-factors/windows.json").read_text())
    for i, case in enumerate(fixture["windows"]):
        root = tmp_path / str(i)
        root.mkdir()
        source = case["events"]
        hfq = "date,hfq_factor\n" + "".join(
            r["date"] + "," + r["hfq_factor"] + "\n" for r in source
        )
        qfq = "date,qfq_factor\n" + "".join(
            r["date"] + "," + r["qfq_factor"] + "\n" for r in source
        )
        view = load(Store.init(root / "store"), archives(root, hfq, qfq), through=case["ex_date"])
        raw = bars()
        raw["start"], raw["end"] = case["previous_date"], case["ex_date"]
        for row, label, close in zip(
            raw["rows"],
            (case["previous_date"], case["ex_date"]),
            (case["previous_close"], case["ex_close"]),
        ):
            row.update(date=label, **dict.fromkeys(("open", "high", "low", "close"), close))
        raw["receipt"]["rows_sha256"] = digest(raw["rows"])
        pre = view.adjust_prices(raw, adjustment="pre", basis_date=case["ex_date"])
        post = view.adjust_prices(raw, adjustment="post", basis_date=case["ex_date"])
        previous_adjusted = Decimal(pre["rows"][0]["close"])
        assert abs(previous_adjusted - Decimal(case["ex_reference_price"])) < Decimal("0.005")
        assert previous_adjusted < Decimal(case["previous_close"])
        assert Decimal(pre["rows"][1]["close"]) == Decimal(case["ex_close"])
        assert Decimal(post["rows"][1]["close"]) > Decimal(case["ex_close"])
