"""Use-boundary regressions: preserve valid observations without look-ahead."""

import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from ashare_data.financials.expansion import encoded
from ashare_data.financials.expansion_access import SnapshotReader, SourceUseDenied, assess
from ashare_data.financials.ledger import FinancialLedger


TASK = {
    "id": "sina_income-sh600176",
    "kind": "sina_income",
    "symbol": "sh600176",
    "ipo": "1999-04-22",
    "out": None,
    "url": "https://example.invalid/source",
}


def row():
    return {
        "schema": "ad.financial-source-report.v1",
        "security": "600176.XSHG",
        "period_start": "2025-01-01",
        "period_end": "2025-09-30",
        "basis": "YTD",
        "scope": "consolidated",
        "currency": "CNY",
        "announcement_date": "2026-10-20",
        "observed_at": "2026-10-19T10:00:00+00:00",
        "period_after_code_exit": False,
        "source_revision_id": "revision",
        "response_sha256": "source-hash",
        "source_locator": "result/data/report_list/20250930",
        "historical_available_at": None,
        "metrics": {
            "net_profit": {"value": "123.450000", "state": "source_observed", "unit": "CNY"}
        },
    }


def review():
    return {
        "evidence_available_by": "2026-10-19T11:00:00+00:00",
        "disclosure_context": "later_report_comparative_column",
        "verified_fields": {"net_profit": {"value": "123.45", "pages": [8]}},
    }


class AccessPolicyTests(unittest.TestCase):
    def test_future_date_blocks_calculation_but_record_remains_auditable(self):
        r = row()
        result = assess(
            r,
            TASK,
            purpose="source_research",
            as_of="2026-10-19T12:00:00+00:00",
            field="net_profit",
        )
        self.assertFalse(result["allowed"])
        self.assertIn("announcement_date_after_requested_day", result["reasons"])
        self.assertTrue(assess(r, TASK, purpose="source_audit")["allowed"])
        self.assertFalse(result["record_invalidated"])

    def test_passing_midnight_does_not_resolve_unreviewed_future_date(self):
        result = assess(
            row(),
            TASK,
            purpose="source_research",
            as_of="2026-11-01T00:00:00+08:00",
            field="net_profit",
        )
        self.assertIn("source_announcement_after_capture_day_requires_review", result["reasons"])

    def test_official_observation_allows_only_verified_current_research_fields(self):
        r = row()
        before = assess(
            r,
            TASK,
            purpose="source_research",
            as_of="2026-10-19T10:30:00+00:00",
            field="net_profit",
            review=review(),
        )
        after = assess(
            r,
            TASK,
            purpose="source_research",
            as_of="2026-10-19T11:00:00+00:00",
            field="net_profit",
            review=review(),
        )
        self.assertFalse(before["allowed"])
        self.assertTrue(after["allowed"])
        self.assertIsNone(r["historical_available_at"])
        wrong_field = assess(
            r,
            TASK,
            purpose="source_research",
            as_of="2026-10-19T12:00:00+00:00",
            field="operating_revenue",
            review=review(),
        )
        self.assertFalse(wrong_field["allowed"])

    def test_official_capture_is_never_first_release_or_full_pit(self):
        r = row()
        r.update(historical_pit_certified=True, first_publication_proven=True)
        result = assess(
            r,
            TASK,
            purpose="historical_pit",
            as_of="2026-10-21T12:00:00+00:00",
            field="net_profit",
            review=review(),
        )
        self.assertIn("field_has_no_verified_historical_binding", result["reasons"])
        with self.assertRaisesRegex(ValueError, "unsupported input pack"):
            FinancialLedger(r)

    def test_china_local_calendar_day_not_utc_day(self):
        r = row()
        r["observed_at"] = "2026-10-19T17:00:00+00:00"
        result = assess(
            r,
            TASK,
            purpose="source_research",
            as_of="2026-10-19T18:00:00+00:00",
            field="net_profit",
        )
        self.assertTrue(result["allowed"])
        self.assertNotIn("source_date_label_after_capture_day", result["findings"])

    def test_issuer_reports_after_code_exit_are_retained_but_not_old_code_metrics(self):
        r = row()
        r["announcement_date"] = "2026-08-28"
        r["period_after_code_exit"] = True
        task = dict(TASK, out="2018-02-28")
        allowed = assess(
            r,
            task,
            purpose="source_research",
            as_of="2026-10-19T12:00:00+00:00",
            field="net_profit",
        )
        denied = assess(
            r,
            task,
            purpose="listed_security",
            as_of="2026-10-19T12:00:00+00:00",
            field="net_profit",
        )
        self.assertTrue(allowed["allowed"])
        self.assertFalse(denied["allowed"])
        self.assertFalse(allowed["code_mapping_applied"])

    def test_late_disclosure_of_pre_exit_period_is_not_post_exit_period(self):
        r = row()
        r.update(period_end="2017-12-31", period_start="2017-01-01", announcement_date="2018-04-30")
        task = dict(TASK, out="2018-02-28")
        decision = assess(
            r,
            task,
            purpose="listed_security",
            as_of="2026-10-19T12:00:00+00:00",
            field="net_profit",
        )
        self.assertTrue(decision["allowed"])

    def test_malformed_purpose_and_naive_cutoff_rejected(self):
        with self.assertRaises(ValueError):
            assess(row(), TASK, purpose="backtest_anyway")
        with self.assertRaises(ValueError):
            assess(row(), TASK, purpose="source_research", as_of="2026-10-19", field="net_profit")

    def test_share_overlap_blocks_components_without_discarding_total(self):
        r = {
            "schema": "ad.corporate-source-observation.v1",
            "security": "600176.XSHG",
            "observed_at": "2026-10-19T10:00:00+00:00",
            "category": "shares",
            "observation": {
                "date": "2025-12-31",
                "announcement_date": "2026-03-20",
                "fields": {
                    "total_shares": "1000",
                    "float_a_shares": "1000",
                    "float_h_shares": "400",
                },
                "source_quantum_shares": {
                    "total_shares": "1",
                    "float_a_shares": "1",
                    "float_h_shares": "1",
                },
            },
        }
        a = assess(
            r,
            TASK,
            purpose="source_research",
            as_of="2026-10-19T12:00:00+00:00",
            field="float_a_shares",
        )
        total = assess(
            r,
            TASK,
            purpose="source_research",
            as_of="2026-10-19T12:00:00+00:00",
            field="total_shares",
        )
        self.assertFalse(a["allowed"])
        self.assertTrue(total["allowed"])
        # At this source precision, a 0.4-share sum excess is not a proven conflict.
        r["observation"]["fields"].update(float_a_shares="600.4")
        rounded = assess(
            r,
            TASK,
            purpose="source_research",
            as_of="2026-10-19T12:00:00+00:00",
            field="float_a_shares",
        )
        self.assertTrue(rounded["allowed"])


class ReaderIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="expansion-access-")
        self.asset = Path(self.temp.name)
        self.root = self.asset / "batches" / "test"
        self.root.mkdir(parents=True)
        universe = encoded({"code_events": []})
        tasks = encoded([TASK])
        (self.root / "universe.json").write_bytes(universe)
        (self.root / "tasks.json").write_bytes(tasks)
        (self.root / "manifest.json").write_bytes(
            encoded(
                {
                    "schema": "ad.financial-reference-batch.v1",
                    "universe_sha256": hashlib.sha256(universe).hexdigest(),
                    "tasks_sha256": hashlib.sha256(tasks).hexdigest(),
                }
            )
        )
        r = row()
        (self.root / "normalized").mkdir()
        (self.root / "receipts").mkdir()
        payload = gzip.compress(encoded(r))
        path = "normalized/" + TASK["id"] + ".jsonl.gz"
        (self.root / path).write_bytes(payload)
        (self.root / "receipts" / (TASK["id"] + ".json")).write_bytes(
            encoded(
                {
                    "task_id": TASK["id"],
                    "state": "complete",
                    "row_count": 1,
                    "observed_at": r["observed_at"],
                    "attempts": [{"response_sha256": r["response_sha256"]}],
                    "normalized": {
                        "path": path,
                        "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    },
                }
            )
        )

    def tearDown(self):
        self.temp.cleanup()

    def install_review(self):
        evidence = b"synthetic official evidence"
        (self.asset / "official-proof").write_bytes(evidence)
        rv = dict(
            review(),
            task_id=TASK["id"],
            **{k: row()[k] for k in ("response_sha256", "source_revision_id", "source_locator")},
            outcome="official_current_document_values_verified_no_first_release_claim",
            evidence=[
                {
                    "path": "official-proof",
                    "bytes": len(evidence),
                    "sha256": hashlib.sha256(evidence).hexdigest(),
                    "received_at": "2026-10-19T11:00:00+00:00",
                }
            ],
        )
        (self.asset / "usage-reviews.json").write_bytes(
            encoded({"schema": "ad.expansion-usage-reviews.v1", "records": [rv]})
        )

    def read(self, purpose):
        return SnapshotReader(self.root).read(
            TASK["id"],
            period="2025-09-30",
            field="net_profit",
            purpose=purpose,
            as_of="2026-10-19T12:00:00+00:00",
        )

    def test_entry_point_consumes_review_but_keeps_pit_denied(self):
        with self.assertRaises(SourceUseDenied):
            self.read("source_research")
        self.install_review()
        result = self.read("source_research")
        self.assertEqual(result["value"], "123.450000")
        self.assertNotIn("source_record", result)
        with self.assertRaises(SourceUseDenied):
            self.read("historical_pit")

    def test_evidence_tampering_fails_closed(self):
        self.install_review()
        (self.asset / "official-proof").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.read("source_research")

    def test_review_for_other_source_revision_cannot_unlock_value(self):
        self.install_review()
        p = self.asset / "usage-reviews.json"
        data = json.loads(p.read_text())
        data["records"][0]["source_revision_id"] = "another-version"
        p.write_bytes(encoded(data))
        with self.assertRaises(SourceUseDenied):
            self.read("source_research")

    def test_audit_retains_original_without_exposing_a_calculation_value(self):
        result = self.read("source_audit")
        self.assertEqual(result["source_record"]["announcement_date"], "2026-10-20")
        self.assertFalse(result["calculation_value_exposed"])


class HistoricalBindingTests(unittest.TestCase):
    """A qualified field may work while the rest of the captured batch is unknown."""

    setUp = ReaderIntegrationTests.setUp
    tearDown = ReaderIntegrationTests.tearDown

    def install_history(self, *, unproved_neighbor=False):
        def ref(name, raw):
            (self.asset / name).write_bytes(raw)
            return {"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}

        evidence = ref("report.pdf", b"synthetic original report")
        text = ref("report.txt", b"synthetic transcribed amount 123.45")
        index = ref("index.json", b"synthetic official version inventory")
        release = ref("archive.json", b"synthetic contemporaneous release capture")
        report = {
            "security": row()["security"],
            "period_end": "2025-09-30",
            "published_date": "2025-10-31",
            "publication_precision": "timestamp",
            "published_at": "2025-10-31T18:00:00+08:00",
            "available_at": "2025-10-31T18:05:00+08:00",
            "historical_first_publication_proven": True,
            "complete_revision_history": True,
            "path": evidence["path"],
            "sha256": evidence["sha256"],
            "text_path": text["path"],
            "text_sha256": text["sha256"],
            "index_path": index["path"],
            "index_sha256": index["sha256"],
        }
        observation = {
            "record_id": "official-cell",
            "report_id": "official-report",
            **{k: row()[k] for k in ("security", "period_start", "period_end", "basis", "scope")},
            "currency": "CNY",
            "unit": "CNY",
            "field": "net_profit",
            "value": "123.45",
            "page": 1,
            "replaces_in_this_pack": None,
        }
        pack = {
            "schema": "ad.financial-ledger.v1",
            "reports": {"official-report": report},
            "observations": [observation],
            "sources": [evidence, text, index],
        }
        if unproved_neighbor:
            other = dict(
                report, historical_first_publication_proven=False, complete_revision_history=False
            )
            pack["reports"]["unproved-other-field"] = other
            pack["observations"].append(
                dict(
                    observation,
                    record_id="other-cell",
                    report_id="unproved-other-field",
                    field="operating_revenue",
                    value="500",
                )
            )
        ledger = ref("ledger.json", encoded(pack))
        ledger["evidence_root"] = "."
        proof = {
            "schema": "ad.official-report-release-proof.v1",
            "report_id": "official-report",
            "document_sha256": report["sha256"],
            "published_at": report["published_at"],
            "available_at": report["available_at"],
            "historical_first_publication_proven": True,
            "evidence_kind": "contemporaneous_release_capture",
            "revision_inventory_complete_through": "2026-10-19T12:00:00+00:00",
            "sources": [release],
        }
        proof_ref = ref("release-proof.json", encoded(proof))
        binding = {
            "task_id": TASK["id"],
            "field": "net_profit",
            **{k: row()[k] for k in ("response_sha256", "source_revision_id", "source_locator")},
            "official_record_id": "official-cell",
            "ledger": ledger,
            "release_proof": proof_ref,
        }
        (self.asset / "historical-bindings.json").write_bytes(
            encoded({"schema": "ad.expansion-historical-bindings.v1", "bindings": [binding]})
        )

    def historical(self, field="net_profit", as_of="2025-11-01T00:00:00+08:00"):
        return SnapshotReader(self.root).read(
            TASK["id"], period="2025-09-30", field=field, as_of=as_of
        )

    def test_verified_field_is_usable_before_present_day_capture(self):
        self.install_history()
        result = self.historical()
        self.assertEqual(result["value"], "123.450000")
        self.assertTrue(result["decision"]["historical_field_evidence"]["allowed"])
        self.assertFalse(result["decision"]["historical_pit_certified"])

    def test_unknown_neighbor_field_does_not_block_verified_field(self):
        self.install_history(unproved_neighbor=True)
        self.assertEqual(self.historical()["field"], "net_profit")
        with self.assertRaises(SourceUseDenied):
            self.historical("operating_revenue")

    def test_before_release_and_after_inventory_bound_are_denied(self):
        self.install_history()
        with self.assertRaises(SourceUseDenied):
            self.historical(as_of="2025-10-31T18:04:59+08:00")
        with self.assertRaises(SourceUseDenied):
            self.historical(as_of="2026-10-20T12:00:00+00:00")

    def test_historical_release_evidence_tampering_is_rejected(self):
        self.install_history()
        (self.asset / "archive.json").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.historical()

    def test_other_provider_version_cannot_inherit_field_binding(self):
        self.install_history()
        path = self.asset / "historical-bindings.json"
        obj = json.loads(path.read_text())
        obj["bindings"][0]["source_revision_id"] = "different-source-version"
        path.write_bytes(encoded(obj))
        with self.assertRaises(SourceUseDenied):
            self.historical()

    def rewrite_bound_file(self, key, mutate):
        registry_path = self.asset / "historical-bindings.json"
        registry = json.loads(registry_path.read_text())
        ref = registry["bindings"][0][key]
        path = self.asset / ref["path"]
        data = json.loads(path.read_text())
        mutate(data)
        raw = encoded(data)
        path.write_bytes(raw)
        ref.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        registry_path.write_bytes(encoded(registry))

    def test_official_value_mismatch_cannot_be_approved_by_release_proof(self):
        self.install_history()
        self.rewrite_bound_file("ledger", lambda p: p["observations"][0].update(value="999"))
        with self.assertRaisesRegex(ValueError, "value or reporting scope mismatch"):
            self.historical()

    def test_date_only_and_current_index_are_not_precise_release_proof(self):
        self.install_history()
        self.rewrite_bound_file(
            "ledger",
            lambda p: p["reports"]["official-report"].update(
                publication_precision="date_only", published_at=None, available_at=None
            ),
        )
        with self.assertRaises(SourceUseDenied):
            self.historical()
        self.install_history()
        self.rewrite_bound_file(
            "release_proof", lambda p: p.update(evidence_kind="current_index_label")
        )
        with self.assertRaisesRegex(ValueError, "current index labels"):
            self.historical()


if __name__ == "__main__":
    unittest.main()
