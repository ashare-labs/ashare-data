"""Integrity reports must not promote the early mixed-source comparison probe."""

import json
from pathlib import Path
import runpy

from ashare_data.providers.baostock_collection import import_captures
from ashare_data.storage.local_daily import LocalDailyStore


def test_audit_uses_named_sample_snapshot_and_excludes_legacy_comparison(tmp_path):
    store = LocalDailyStore(tmp_path)
    store.set_meta("start", "2010-01-01")
    store.set_meta("end", "2010-01-04")
    (tmp_path / "reports").mkdir()
    import_captures(store)
    store.close()
    (tmp_path / "reports/crosscheck.json").write_text('{"comparisons": "unverified legacy probe"}')
    report = dict(
        at="2026-10-14T18:00:00+00:00",
        bar_snapshot_at="2026-10-14T17:59:00+00:00",
        read_consistency="One SELECT per security during ongoing collection.",
        scope_note="Includes retained pre-merger SME history.",
        sample_definition_report="reports/acceptance-progress.json",
        sample_definition_report_at="2026-10-14T17:50:00+00:00",
        sample_symbols=["sh600001", "sz000001"],
        window_count=3,
        disagreements=[dict(symbol="sh600001", date="2010-01-04", field="close")],
        request_errors=[],
        counts=dict(baostock_consistent=1, sina_different=1),
        interpretation="Synthetic source consistency check, not a truth certificate.",
    )
    path = tmp_path / "reports/sample-price-crosscheck.json"
    path.write_text(json.dumps(report))
    audit = runpy.run_path(str(Path(__file__).parents[1] / "scripts/audit_local_daily.py"))["audit"]
    result = audit(tmp_path)
    summary = result["independent_crosscheck"]
    assert summary["at"] == report["at"]
    assert summary["bar_snapshot_at"] == report["bar_snapshot_at"]
    for key in (
        "read_consistency",
        "scope_note",
        "sample_definition_report",
        "sample_definition_report_at",
    ):
        assert summary[key] == report[key]
    assert summary["securities"] == 2 and summary["third_source_windows"] == 3
    assert summary["checked_price_field_disagreements"] == 1
    assert summary["source_consistency_counts"] == report["counts"]
    assert "pairs" not in summary
    path.unlink()
    assert "independent_crosscheck" not in audit(tmp_path)
