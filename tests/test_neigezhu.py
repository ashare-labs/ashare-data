"""Consumer safety on tiny native-label fixtures, without source/network access."""

import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import shutil

import duckdb
import numpy as np

from ashare_data import DataError, NeigezhuMinuteView
from ashare_data.market.neigezhu_quality import cumulative_100_feasible, diagnostics


class NativeViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "data/stock_1m/SH/600000.parquet"
        self.source.parent.mkdir(parents=True)
        with duckdb.connect() as con:
            con.execute(
                "CREATE TABLE bars(timestamp TIMESTAMP,open DOUBLE,high DOUBLE,low DOUBLE,close DOUBLE,volume BIGINT,turnover DOUBLE)"
            )
            con.executemany(
                "INSERT INTO bars VALUES(?,?,?,?,?,?,?)",
                [
                    (datetime(2026, 6, 2, 9, 30), 10, 10, 10, 10, 100, 1000),
                    (datetime(2026, 6, 2, 9, 31), 10, 9, 8, 10, 100, 1000),
                    (datetime(2026, 6, 2, 9, 32), 0, 0, 0, 0, 0, 0),
                    (datetime(2026, 6, 2, 9, 33), 10, 10, 10, 10, 0, 0),
                ],
            )
            con.execute("COPY bars TO ? (FORMAT PARQUET)", [str(self.source)])
        self.entry = self.root / "deployment.json"
        self.entry.write_text(
            json.dumps({"assets": {"minute-hf-download": {"path": str(self.root)}}})
        )
        self.manifest = self.root / "manifest.csv"
        with self.manifest.open("w", newline="") as f:
            row = dict(
                path="data/stock_1m/SH/600000.parquet",
                expected_bytes=self.source.stat().st_size,
                hash_matches="True",
                input_unchanged_after_scan="True",
                status="scanned_with_errors",
                computed_hash=hashlib.sha256(self.source.read_bytes()).hexdigest(),
            )
            writer = csv.DictWriter(f, fieldnames=list(row))
            writer.writeheader()
            writer.writerow(row)
        self.sha = hashlib.sha256(self.manifest.read_bytes()).hexdigest()

    def tearDown(self):
        self.temp.cleanup()

    def view(self):
        return NeigezhuMinuteView(
            entry=self.entry, manifest=self.manifest, expected_manifest_sha256=self.sha
        )

    def read(self, view, **extra):
        return view.get_price(
            "600000.XSHG",
            mode="retrospective",
            start="2026-06-02T09:30:00+08:00",
            end="2026-06-02T09:34:00+08:00",
            **extra,
        )

    def test_raw_opening_bad_and_zero_rows_remain_visible(self):
        result = self.read(self.view())
        self.assertEqual(result.data.source_row.tolist(), [0, 1, 2, 3])
        self.assertEqual(result.data.source_label.iloc[0], "2026-06-02T09:30:00+08:00")
        self.assertEqual(
            result.data.price_status.tolist(), ["unverified", "invalid", "invalid", "unverified"]
        )
        self.assertFalse(result.data.execution_admitted.any())
        self.assertFalse(result.data.new_valuation_admitted.any())
        self.assertEqual(result.data.volume.tolist(), [100, 100, 0, 0])
        self.assertEqual(result.report["repaired_rows"], 0)

    def test_explicit_missing_label_is_reported_not_filled(self):
        result = self.read(self.view(), labels=["2026-06-02T09:34:00+08:00"])
        self.assertTrue(result.data.empty)
        self.assertEqual(result.report["missing_requested_labels"], ["2026-06-02T09:34:00+08:00"])
        self.assertIsNone(result.report["coverage_basis"]["actual_missing_trading_days"])

    def test_relative_asset_follows_deployment_data_root_after_migration(self):
        portable = self.root / "portable"
        target = portable / "assets/hf/data/stock_1m/SH/600000.parquet"
        target.parent.mkdir(parents=True)
        shutil.copyfile(self.source, target)
        entry = portable / "deployment.json"
        entry.write_text(
            json.dumps({"data_root": "assets", "assets": {"minute-hf-download": {"path": "hf"}}})
        )
        moved = self.root / "moved"
        shutil.copytree(portable, moved)
        for config in (entry, moved / "deployment.json"):
            view = NeigezhuMinuteView(
                entry=config, manifest=self.manifest, expected_manifest_sha256=self.sha
            )
            self.assertEqual(view.root, (config.parent / "assets/hf").resolve())
            self.assertEqual(len(self.read(view).data), 4)

    def test_nonfinite_null_and_negative_values_keep_raw_but_deny_relevant_uses(self):
        with duckdb.connect() as con:
            con.execute(
                "CREATE TABLE bad(timestamp TIMESTAMP,open DOUBLE,high DOUBLE,low DOUBLE,close DOUBLE,volume BIGINT,turnover DOUBLE)"
            )
            con.executemany(
                "INSERT INTO bad VALUES(?,?,?,?,?,?,?)",
                [
                    (datetime(2026, 6, 2, 9, 30), 10, float("nan"), 10, 10, 100, 1000),
                    (datetime(2026, 6, 2, 9, 31), 10, float("inf"), 10, 10, 100, 1000),
                    (datetime(2026, 6, 2, 9, 32), 10, 10, 10, 10, -100, -1000),
                    (datetime(2026, 6, 2, 9, 33), None, 10, 10, 10, None, None),
                ],
            )
            con.execute("COPY bad TO ? (FORMAT PARQUET)", [str(self.source)])
        rows = list(csv.DictReader(self.manifest.open()))
        rows[0].update(
            expected_bytes=self.source.stat().st_size,
            computed_hash=hashlib.sha256(self.source.read_bytes()).hexdigest(),
        )
        with self.manifest.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=rows[0])
            writer.writeheader()
            writer.writerows(rows)
        self.sha = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        result = self.read(self.view())
        self.assertEqual(
            result.data.price_status.tolist(), ["invalid", "invalid", "unverified", "invalid"]
        )
        self.assertEqual(
            result.data.activity_status.tolist()[-2:], ["invalid_volume", "unknown_volume"]
        )
        self.assertTrue(np.isnan(result.data.high.iloc[0]))
        self.assertTrue(np.isposinf(result.data.high.iloc[1]))
        self.assertEqual(result.data.volume.iloc[2], -100)
        rows = result.to_dict()["rows"]
        json.dumps(result.to_dict(), allow_nan=False)
        self.assertEqual([rows[i]["high"] for i in (0, 1)], ["NaN", "+Infinity"])
        self.assertIsNone(rows[3]["open"])
        self.assertIn("null_volume", rows[3]["diagnostics"])
        self.assertTrue(all(r["use_decision"]["execution"] == "deny" for r in rows))
        self.assertEqual(rows[2]["use_decision"]["fresh_valuation"], "not_granted")

    def test_modified_source_and_manifest_are_rejected(self):
        view = self.view()
        self.source.write_bytes(b"x" * self.source.stat().st_size)
        with self.assertRaises(DataError) as error:
            self.read(view)
        self.assertEqual(error.exception.code, "NEIGEZHU_SOURCE_INTEGRITY")
        self.manifest.write_text(self.manifest.read_text() + "\n")
        with self.assertRaises(DataError) as error:
            self.view()
        self.assertEqual(error.exception.code, "NEIGEZHU_MANIFEST_INTEGRITY")

    def test_ohlc_conflicts_are_independent_of_nonfinite_activity(self):
        cases = [
            (9, float("nan"), 1000, "invalid", "deny", {"nonfinite_volume"}),
            (9, 100, None, "invalid", "deny", {"nonfinite_amount", "null_amount"}),
            (10, float("nan"), 1000, "unverified", "deny", {"nonfinite_volume"}),
            (10, 100, None, "unverified", "deny", {"nonfinite_amount", "null_amount"}),
            (10, 100, 1000, "unverified", "not_granted", set()),
        ]
        with duckdb.connect() as con:
            con.execute(
                "CREATE TABLE mixed(symbol VARCHAR,exchange VARCHAR,timestamp TIMESTAMP,"
                "open DOUBLE,high DOUBLE,low DOUBLE,close DOUBLE,volume DOUBLE,turnover DOUBLE)"
            )
            con.executemany(
                "INSERT INTO mixed VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    ("600000", "SH", datetime(2026, 6, 2, 9, 30 + i), 10, 10, 10, c, v, a)
                    for i, (c, v, a, *_expected) in enumerate(cases)
                ],
            )
            con.execute("COPY mixed TO ? (FORMAT PARQUET)", [str(self.source)])
        source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        records = list(csv.DictReader(self.manifest.open()))
        records[0].update(expected_bytes=self.source.stat().st_size, computed_hash=source_hash)
        with self.manifest.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=records[0])
            writer.writeheader()
            writer.writerows(records)
        self.sha = hashlib.sha256(self.manifest.read_bytes()).hexdigest()

        rows = self.read(self.view()).to_dict()["rows"]
        self.assertEqual(len(rows), len(cases))
        for i, (c, _v, _a, status, execution, activity_flags) in enumerate(cases):
            with self.subTest(case=i):
                row = rows[i]
                bad_price = status == "invalid"
                expected_flags = activity_flags | ({"ohlc_envelope_error"} if bad_price else set())
                self.assertEqual(set(row["diagnostics"]), expected_flags)
                self.assertEqual(row["price_status"], status)
                self.assertEqual(row["price_structurally_usable"], not bad_price)
                self.assertEqual(row["use_decision"]["execution"], execution)
                self.assertEqual(
                    row["use_decision"]["fresh_valuation"], "deny" if bad_price else "not_granted"
                )
                self.assertEqual(row["close"], c)
                self.assertEqual(row["source_row"], i)
        self.assertEqual([rows[i]["volume"] for i in (0, 2)], ["NaN", "NaN"])
        self.assertTrue(all(rows[i]["amount"] is None for i in (1, 3)))
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), source_hash)

    def test_results_do_not_rewrite_view_or_source(self):
        view = self.view()
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = self.read(view)
        result.data.iloc[1]["diagnostics"].clear()
        self.assertIn("ohlc_envelope_error", self.read(view).data.iloc[1]["diagnostics"])
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)

    def test_pinned_conflict_overlay_never_changes_or_drops_prices(self):
        review = self.root / "review.json"
        review.write_text(
            json.dumps(
                {
                    "schema": "neigezhu-quality-overlay.v1",
                    "manifest_sha256": self.sha,
                    "ranges": [
                        {
                            "path": "data/stock_1m/SH/600000.parquet",
                            "source_sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
                            "first_row": 0,
                            "last_row": 1,
                            "rule": "daily_reference_conflict",
                            "price_status": "conflict",
                            "evidence": {"reference_sha256": "a" * 64},
                        }
                    ],
                }
            )
        )
        view = NeigezhuMinuteView(
            entry=self.entry,
            manifest=self.manifest,
            expected_manifest_sha256=self.sha,
            review=review,
            expected_review_sha256=hashlib.sha256(review.read_bytes()).hexdigest(),
        )
        result = self.read(view)
        self.assertEqual(
            result.data.price_status.tolist(), ["conflict", "invalid", "invalid", "unverified"]
        )
        self.assertEqual(result.data.open.tolist(), [10, 10, 0, 10])
        self.assertEqual(len(result.data), 4)
        self.assertTrue(result.data.review_evidence.iloc[0])


class CumulativeModelTests(unittest.TestCase):
    def test_saved_reference_counterexample_is_conditional_not_a_price_denial(self):
        from ashare_data.research.minute_policy import native_denials

        flags, _ = diagnostics([[9.21, 9.22, 9.21, 9.22]], [683000], [6289257])
        self.assertTrue(flags["vwap_beyond_storage_envelope"][0])
        self.assertEqual(
            native_denials(
                {"price_status": "unverified", "diagnostics": ["vwap_beyond_storage_envelope"]}
            )["execution"],
            "not_granted",
        )

    def test_empty_integer_interval_is_infeasible_not_unknown(self):
        result = cumulative_100_feasible([10, 12], [11, 11], [0, 0])
        self.assertTrue(result["eligible"])
        self.assertFalse(result["feasible"])
        self.assertEqual(result["reason"], "empty_integer_volume_interval")
        self.assertEqual(result["first_infeasible_offset"], 1)

    def test_actual_extrema_detect_impossible_high_inside_vwap(self):
        flags, _ = diagnostics([[6.78, 2326.53, 6.77, 6.77]], [5900], [40020])
        self.assertFalse(flags["vwap_beyond_storage_envelope"][0])
        self.assertTrue(flags["new_extrema_beyond_99_share_and_one_yuan_hypotheses"][0])

    def test_constructed_real_trades_satisfy_extrema_condition(self):
        rng = np.random.default_rng(31)
        for _ in range(300):
            ticks = rng.integers(1, 10000, size=8)
            volumes = rng.integers(1, 3000, size=8)
            price = [ticks[0] / 100, ticks.max() / 100, ticks.min() / 100, ticks[-1] / 100]
            flags, _ = diagnostics([price], [volumes.sum()], [(ticks * volumes).sum() / 100])
            self.assertFalse(flags["actual_extrema_exact_volume_conditional"][0])

    def test_feasibility_against_independent_enumeration(self):
        rng = np.random.default_rng(19)
        for _ in range(300):
            lo = rng.integers(0, 115, size=4)
            hi = lo + rng.integers(0, 5, size=4)
            reported = rng.integers(0, 3, size=4) * 100
            possible = {0}
            total = 0
            for low, high, value in zip(lo, hi, reported):
                total += int(value)
                possible = {
                    s + v
                    for s in possible
                    for v in range(int(low), int(high) + 1)
                    if total - 50 <= s + v <= total + 49
                }
            result = cumulative_100_feasible(lo, hi, reported)
            self.assertTrue(result["eligible"])
            self.assertEqual(result["feasible"], bool(possible))

    def test_non_lot_reporting_is_outside_hypothesis(self):
        self.assertFalse(cumulative_100_feasible([1], [200], [101])["eligible"])

    def test_known_integer_paths_are_always_feasible(self):
        rng = np.random.default_rng(27)
        for _ in range(300):
            true = rng.integers(0, 1000, size=8)
            cumulative = np.cumsum(true)
            reported = np.diff(np.r_[0, 100 * ((cumulative + 50) // 100)])
            result = cumulative_100_feasible(np.maximum(0, true - 2), true + 2, reported)
            self.assertTrue(result["eligible"])
            self.assertTrue(result["feasible"])
            self.assertLessEqual(result["terminal_true_volume_min"], cumulative[-1])
            self.assertGreaterEqual(result["terminal_true_volume_max"], cumulative[-1])


if __name__ == "__main__":
    unittest.main()
