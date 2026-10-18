"""Small constructed contracts; no downloaded market data in the package."""

from copy import deepcopy
from decimal import Context, localcontext

import pytest

from ashare_data import DataError, Store, export_minute_research
from ashare_data.core.model import digest


@pytest.fixture
def snapshot(tmp_path):
    source = dict(
        id="constructed-contract",
        kind="local_reconstruction",
        location="fixture:minute",
        observed_at="2025-07-01T16:00:00+08:00",
        label="end",
        time_semantics="unverified",
        time_evidence="Constructed protocol test, not a real market assertion",
        volume_unit="shares",
        amount_unit="CNY",
        price_unit="CNY",
        price_basis="unadjusted",
        unit_evidence="Explicit test units",
    )
    day = "2025-07-01"
    metadata = dict(
        real_market_data=True,
        securities=["601234.XSHG"],
        days=[day],
        sessions={day: [[day + "T09:30:00+08:00", day + "T09:32:00+08:00"]]},
    )
    bundle = dict(
        schema_version=1,
        source=source,
        calendar=[
            dict(
                date=day,
                is_open=True,
                sessions=metadata["sessions"][day],
                evidence="Constructed session",
                available_at=source["observed_at"],
            )
        ],
        bars=[
            dict(
                symbol=metadata["securities"][0],
                timestamp=day + f"T09:{n}:00+08:00",
                open="10.10",
                high="10.20",
                low="10.00",
                close="10.15",
                volume="100",
                amount="1015",
                quality="unverified",
                quality_reason="constructed",
                available_at=None,
                original={"label": n, "text": "原始字段", "derivation": "explicit-test-model"},
            )
            for n in [31, 32]
        ],
    )
    with localcontext(Context(prec=28)):
        store = Store.init(tmp_path / "store")
        batch = store.import_bundle(bundle)
        a, b = metadata["sessions"][day][0]
        report = store.validate(
            [batch],
            requirements=[dict(symbols=metadata["securities"], start=a, end=b)],
            policy="research",
        )
        assert report["passed"]
        sid = store.publish(report["id"])
    with store.snapshot(sid) as view:
        yield view, metadata


def test_projection_keeps_source_identity_quality_and_unknowns(snapshot):
    view, metadata = snapshot
    before = deepcopy(metadata)
    doc = export_minute_research(view, metadata)
    assert metadata == before
    assert len(doc["minute"]) == 2
    assert doc["content_sha256"] == digest({k: v for k, v in doc.items() if k != "content_sha256"})
    assert doc["source_refs"]["ad_lineage"] == view.lineage()
    row = doc["minute"][0]
    assert row["quality"] == "unverified"
    assert all(
        row[k] is None for k in ["bar_start", "bar_end", "available_at", "historical_available_at"]
    )
    assert row["evidence"]["ad_row"]["source_fields"]["original"]["text"] == "原始字段"
    assert row["replay_bar_end"] == row["source_label"]
    assert export_minute_research(view, metadata) == doc
    a, b = metadata["sessions"][metadata["days"][0]][0]
    with pytest.raises(DataError, match="覆盖未知"):
        view.bars(metadata["securities"], a, b, quality="observed")


@pytest.mark.parametrize("field", ["minute", "schema", "producer", "content_sha256"])
def test_reserved_fields_cannot_replace_owner_projection(snapshot, field):
    view, metadata = snapshot
    metadata[field] = "forged"
    with pytest.raises(DataError) as exc:
        export_minute_research(view, metadata)
    assert exc.value.code == "MINUTE_RESEARCH_RESERVED_FIELDS"


def test_annotations_cannot_replace_prices_or_disappear(snapshot):
    view, metadata = snapshot
    key = "601234.XSHG|2025-07-01T09:31:00+08:00"
    metadata["bar_annotations"] = {key: {"close": "999"}}
    with pytest.raises(DataError) as exc:
        export_minute_research(view, metadata)
    assert exc.value.code == "MINUTE_RESEARCH_ANNOTATION_FIELDS"
    metadata["bar_annotations"] = {"outside": {}}
    with pytest.raises(DataError) as exc:
        export_minute_research(view, metadata)
    assert exc.value.code == "MINUTE_RESEARCH_ANNOTATION_OUT_OF_SCOPE"
