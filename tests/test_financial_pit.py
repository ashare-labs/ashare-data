"""Query/visibility contract tests; real official disclosures are tested separately."""

from copy import deepcopy
from datetime import date, timedelta
import hashlib
import json

import pytest

from ashare_data import FinancialPIT
from ashare_data.financials.validation import AcceptanceError
from ashare_data.financials.ledger import RESEARCH_MODE


def make_pack():
    reports, observations = {}, []
    for security, scale in (("000001.XSHE", 1), ("600000.XSHG", 10)):
        for year in (2022, 2023, 2024):
            for month, day, value in ((3, 31, 10), (6, 30, 30), (9, 30, 60), (12, 31, 100)):
                if year == 2024 and month > 3:
                    continue
                period = f"{year}-{month:02d}-{day}"
                release = date.fromisoformat(period) + timedelta(days=20)
                rid = f"{security}:{period}"
                reports[rid] = {
                    "security": security,
                    "period_end": period,
                    "published_date": release.isoformat(),
                    "publication_precision": "timestamp",
                    "published_at": release.isoformat() + "T18:00:00+08:00",
                    "available_at": release.isoformat() + "T18:05:00+08:00",
                    "historical_first_publication_proven": True,
                    "complete_revision_history": True,
                    "sha256": "0" * 64,
                }
                observations.append(
                    {
                        "record_id": rid,
                        "report_id": rid,
                        "security": security,
                        "period_start": f"{year}-01-01",
                        "period_end": period,
                        "basis": "YTD",
                        "scope": "consolidated",
                        "currency": "CNY",
                        "unit": "CNY",
                        "field": "net_profit",
                        "value": str(value * scale),
                        "page": 1,
                        "replaces_in_this_pack": None,
                    }
                )
    return {"schema": "ad.financial-ledger.v1", "reports": reports, "observations": observations}


def query(p=None, **kwargs):
    return FinancialPIT(make_pack() if p is None else p).query(
        kwargs.pop("securities", "000001.XSHE"),
        kwargs.pop("fields", "net_profit"),
        **{"as_of": "2024-04-21", "mode": RESEARCH_MODE, **kwargs},
    )


def test_multi_company_latest_query_and_quarter_ttm_source_lineage():
    result = query(securities=["000001.XSHE", "600000.XSHG"], basis="TTM")
    assert [r["value"] for r in result["rows"]] == ["100", "1000"]
    for row in result["rows"]:
        assert row["period_start"] == "2023-04-01"
        assert row["period_end"] == "2024-03-31"
        assert row["quarter_sum_equals_bridge"]
        assert len(row["quarters"]) == 4
        assert [t["coefficient"] for t in row["terms"]] == [1, 1, -1]
        assert all(t["security"] == row["security"] for t in row["terms"])
    assert result["source_files_verified"] is False
    assert result["historical_pit_certified"] is False


@pytest.mark.parametrize(
    "cutoff,status",
    [
        ("2024-04-20", "missing"),
        ("2024-04-20T23:59:59+08:00", "missing"),
        ("2024-04-20T16:00:00Z", "ok"),
        ("2024-04-21", "ok"),
    ],
)
def test_date_only_boundary_is_next_shanghai_calendar_day(cutoff, status):
    row = query(as_of=cutoff, period="2024-03-31")["rows"][0]
    assert row["status"] == status
    if status == "ok":
        assert row["terms"][0]["available_from"] == "2024-04-21T00:00:00+08:00"


def test_strict_timestamp_is_gated_by_actual_availability():
    assert (
        query(as_of="2024-04-20T18:04:59+08:00", mode="strict", period="2024-03-31")["rows"][0][
            "status"
        ]
        == "missing"
    )
    row = query(as_of="2024-04-20T18:05:00+08:00", mode="strict", period="2024-03-31")["rows"][0]
    assert row["value"] == "10"
    assert row["terms"][0]["available_from"] == "2024-04-20T18:05:00+08:00"


def test_date_only_proof_returns_visibility_unproven_in_strict_mode():
    pack = make_pack()
    report = pack["reports"]["000001.XSHE:2024-03-31"]
    report.update(
        publication_precision="date_only",
        published_at=None,
        available_at=None,
        historical_first_publication_proven=False,
    )
    assert (
        query(pack, as_of="2024-04-21T09:00:00+08:00", mode="strict", period="2024-03-31")["rows"][
            0
        ]["status"]
        == "visibility_unproven"
    )


def test_late_correction_does_not_leak_and_updates_all_derived_terms_at_same_cutoff():
    pack = make_pack()
    original = pack["observations"][8]
    correction = {
        **original,
        "record_id": "correction",
        "report_id": "correction",
        "value": "19",
        "replaces_in_this_pack": original["record_id"],
    }
    pack["observations"].append(correction)
    pack["reports"]["correction"] = {
        **pack["reports"][original["report_id"]],
        "published_date": "2024-05-02",
        "published_at": "2024-05-02T17:00:00+08:00",
        "available_at": "2024-05-02T17:01:00+08:00",
    }
    for cutoff, expected in (("2024-05-02", "100"), ("2024-05-03", "109")):
        row = query(pack, as_of=cutoff, basis="TTM")["rows"][0]
        assert row["value"] == expected
        assert sum(int(q["value"]) for q in row["quarters"]) == int(expected)
    row = query(pack, as_of="2024-05-03", basis="quarter")["rows"][0]
    assert row["value"] == "19"
    assert row["terms"][0]["replaces_record_id"] == original["record_id"]


def test_missing_quarter_does_not_succeed_using_only_annual_bridge():
    pack = make_pack()
    pack["observations"] = [r for r in pack["observations"] if r["period_end"] != "2023-06-30"]
    assert query(pack, basis="TTM")["rows"][0]["status"] == "missing"


def test_latest_fields_cannot_mix_periods_or_silently_fallback():
    pack = make_pack()
    old = pack["observations"][7]
    pack["observations"].append({**old, "record_id": "revenue", "field": "operating_revenue"})
    rows = query(pack, fields=["net_profit", "operating_revenue"])["rows"]
    assert all(r["status"] == "missing" and r["value"] is None for r in rows)
    assert "different latest periods" in rows[0]["reason"]
    rows = query(pack, fields=["net_profit", "operating_revenue"], period="2023-12-31")["rows"]
    assert all(r["status"] == "ok" for r in rows)


def test_unknown_security_is_explicit_missing_not_zero():
    rows = query(securities=["000001.XSHE", "600999.XSHG"])["rows"]
    assert rows[0]["status"] == "ok"
    assert rows[1]["status"] == "missing" and rows[1]["value"] is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"fields": "balance_total"},
        {"securities": []},
        {"fields": ["net_profit"] * 2},
        {"basis": "cumulative"},
        {"mode": "guess"},
        {"mode": "strict"},
        {"as_of": "2024-04-21T09:00:00"},
        {"period": "2023-12-30"},
    ],
)
def test_invalid_query_contract_is_rejected(kwargs):
    with pytest.raises(AcceptanceError):
        query(**kwargs)


def test_pack_and_returned_values_are_detached_from_caller_mutations():
    pack = make_pack()
    view = FinancialPIT(pack)
    pack["observations"][-10]["value"] = "99999"
    args = {"as_of": "2024-04-21", "mode": RESEARCH_MODE}
    first = view.query("000001.XSHE", "net_profit", **args)
    first["rows"][0]["terms"][0]["value"] = "99999"
    assert view.query("000001.XSHE", "net_profit", **args)["rows"][0]["value"] == "10"


def test_report_security_cannot_contaminate_other_company():
    pack = make_pack()
    pack["reports"]["000001.XSHE:2024-03-31"]["security"] = "600000.XSHG"
    with pytest.raises(AcceptanceError, match="security mismatch"):
        FinancialPIT(pack)


def test_loader_pins_pack_and_verifies_all_evidence(tmp_path):
    pack = make_pack()
    raw = b"synthetic source for loader integrity test\n"
    sha = hashlib.sha256(raw).hexdigest()
    (tmp_path / "source").write_bytes(raw)
    pack["sources"] = [{"path": "source", "sha256": sha, "bytes": len(raw)}]
    for report in pack["reports"].values():
        for path_key, sha_key in (
            ("path", "sha256"),
            ("text_path", "text_sha256"),
            ("index_path", "index_sha256"),
        ):
            report[path_key], report[sha_key] = "source", sha
    path = tmp_path / "pack.json"
    path.write_text(json.dumps(pack))
    pack_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    view = FinancialPIT.from_file(path, evidence_root=tmp_path, expected_sha256=pack_sha)
    assert view.coverage()["source_files_verified"]
    assert view.coverage()["pack_sha256"] == pack_sha
    with pytest.raises(AcceptanceError, match="pack hash"):
        FinancialPIT.from_file(path, evidence_root=tmp_path, expected_sha256="0" * 64)
    (tmp_path / "source").write_bytes(b"tampered")
    with pytest.raises(AcceptanceError, match="hash/size"):
        FinancialPIT.from_file(path, evidence_root=tmp_path)


def test_ambiguous_version_chain_raises_not_missing_or_best_effort():
    pack = make_pack()
    other = deepcopy(pack["observations"][8])
    other["record_id"] = "unlinked"
    pack["observations"].append(other)
    with pytest.raises(AcceptanceError, match="ambiguous"):
        query(pack)
