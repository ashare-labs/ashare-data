"""Synthetic regression tests: financial data loss and resumability boundaries."""
import copy
import fcntl
import gzip
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ashare_data.financials import expansion as e


def response(value="12.300000", currency="CNY"):
    return {"result": {"status": {"code": 0}, "data": {
        "report_count": "1", "report_date": [{"date_value": "20200331"}],
        "report_list": {"20200331": {
            "rType": "合并期末", "rCurrency": currency, "publish_date": "20200428",
            "update_time": 1700000000, "data": [
                {"item_field": "BIZINCO", "item_title": "营业收入", "item_value": value},
                {"item_field": "OTHER", "item_title": "其他", "item_value": None},
            ]}}}}}


TASK = {"kind": "sina_income", "symbol": "sh600000", "ipo": "1999-11-10", "out": None}


class FinancialParserTests(unittest.TestCase):
    def parse(self, payload, task=TASK):
        return e.parse_financial(e.encoded(payload), task, "2010-01-01", "2020-12-31")

    def test_precision_missing_and_pit_boundary(self):
        rows, coverage = self.parse(response("9007199254740993.123456"))
        row = rows[0]
        self.assertEqual(row["metrics"]["operating_revenue"]["value"], "9007199254740993.123456")
        self.assertIsNone(row["fields"][1]["value"])
        self.assertFalse(row["historical_pit_certified"])
        self.assertFalse(row["first_publication_proven"])
        self.assertIsNone(row["historical_available_at"])
        self.assertEqual(row["announcement_date"], "2020-04-28")
        self.assertIn("2020-06-30", coverage["quarters_without_observation"])

    def test_null_zero_and_invalid_are_distinct(self):
        for raw, expected, state in [(None, None, "source_missing"), ("0", "0", "source_observed"),
                                     ("NaN", None, "invalid_source_numeric")]:
            rows, _ = self.parse(response(raw))
            self.assertEqual((rows[0]["fields"][0]["value"], rows[0]["fields"][0]["state"]), (expected, state))

    def test_incomplete_pagination_and_duplicate_period_rejected(self):
        payload = response()
        payload["result"]["data"]["report_count"] = "2"
        with self.assertRaisesRegex(ValueError, "pagination"):
            self.parse(payload)
        payload["result"]["data"]["report_date"].append({"date_value": "20200331"})
        with self.assertRaisesRegex(ValueError, "pagination"):
            self.parse(payload)

    def test_repeated_unknown_titles_preserved_but_ambiguous_mapping_rejected(self):
        payload = response()
        fields = payload["result"]["data"]["report_list"]["20200331"]["data"]
        fields.append(copy.deepcopy(fields[1]))
        rows, _ = self.parse(payload)
        self.assertEqual(len(rows[0]["fields"]), 3)
        fields.append(copy.deepcopy(fields[0]))
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            self.parse(payload)

    def test_code_exit_flag_does_not_extend_listing(self):
        task = dict(TASK, out="2016-05-13")
        rows, coverage = self.parse(response(), task)
        self.assertTrue(rows[0]["period_after_code_exit"])
        self.assertEqual(coverage["after_code_exit_reports"], 1)
        self.assertIsNone(rows[0]["supersedes"])

    def test_current_revision_not_promoted_to_original_publication(self):
        rows, _ = self.parse(response())
        other, _ = self.parse(response("13.300000"))
        self.assertNotEqual(rows[0]["source_revision_id"], other[0]["source_revision_id"])
        self.assertEqual(rows[0]["revision_status"], other[0]["revision_status"])
        self.assertIsNone(other[0]["supersedes"])

    def test_no_data_and_provider_error_differ(self):
        payload = response()
        payload["result"]["data"] = {"report_count": "0", "report_date": [], "report_list": []}
        rows, coverage = self.parse(payload)
        self.assertEqual(rows, [])
        self.assertFalse(coverage["absence_proved"])
        payload["result"]["status"]["code"] = 1
        with self.assertRaises(ValueError):
            self.parse(payload)


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reference-expansion-test-")
        self.base = Path(self.temp.name)
        self.daily = self.base / "daily.sqlite3"
        with sqlite3.connect(self.daily) as con:
            con.execute("CREATE TABLE instruments(symbol, name, ipo, out, board, metadata_source, observed_at, receipt)")
            con.execute("INSERT INTO instruments VALUES('sh600000','synthetic','1999-11-10',NULL,'main','synthetic','2020','abc')")
            con.execute("CREATE TABLE code_events(id, old_symbol, new_symbol, effective_date)")
            con.execute("CREATE TABLE metadata(key,value)")
        self.root = self.base / "batch"
        e.prepare(self.root, self.daily, "2010-01-01", "2020-12-31")

    def tearDown(self):
        self.temp.cleanup()

    def test_prepare_is_idempotent_and_does_not_mutate_daily(self):
        original = self.daily.read_bytes()
        a = e.prepare(self.root, self.daily, "2010-01-01", "2020-12-31")
        b = e.prepare(self.root, self.daily, "2010-01-01", "2020-12-31")
        self.assertEqual(a, b)
        self.assertEqual(original, self.daily.read_bytes())
        with self.assertRaisesRegex(ValueError, "identity"):
            e.prepare(self.root, self.daily, "2011-01-01", "2020-12-31")

    def test_completed_capture_is_not_redownloaded(self):
        with patch.object(e, "fetch", return_value=(200, e.encoded(response()), {})) as fetch:
            result = e.run(self.root, max_jobs=1)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(result["task_states"]["complete"], 1)
        first = json.loads((self.root / "receipts/sina_balance-sh600000.json").read_text())
        e.verify_receipt(self.root, first)
        (self.root / "STOP").touch()
        with patch.object(e, "fetch") as fetch:
            e.run(self.root)
            fetch.assert_not_called()
        normalized = self.root / first["normalized"]["path"]
        normalized.write_bytes(b"tampered")
        report = e.audit(self.root, verify=True)
        self.assertEqual(len(report["integrity_errors"]), 1)

    def test_rate_limit_persists_host_cooldown_and_reduces_rate(self):
        # Other source hosts remain independently usable; keep this test to one request.
        with patch.object(e, "fetch", return_value=(429, b"rate limited", {"Retry-After": "7200"})):
            e.run(self.root, max_jobs=1)
        state = json.loads((self.root / "source-state.json").read_text())["quotes.sina.cn"]
        self.assertEqual(state["interval"], 10)
        self.assertGreater(state["cooldown_until"], e.time.time() + 7100)
        self.assertFalse(list(self.root.rglob("*.tmp")))

    def test_access_denial_blocks_host_without_auth_retry(self):
        with patch.object(e, "fetch", return_value=(403, b"denied", {})):
            e.run(self.root, max_jobs=1)
        state = json.loads((self.root / "source-state.json").read_text())["quotes.sina.cn"]
        self.assertIn("403", state["blocked"])
        self.assertFalse(list((self.root / "raw").iterdir()))

    def test_crashed_capture_reused_after_last_network_attempt(self):
        task = json.loads((self.root / "tasks.json").read_text())[0]
        raw = e.encoded(response())
        artifact = e.artifact(self.root, "raw/" + task["id"] + ".json.gz", gzip.compress(raw, mtime=0))
        receipt = {"task_id": task["id"], "state": "captured", "raw": artifact,
                   "observed_at": e.now(), "attempts": [{"response_sha256": e.digest(raw)}] * 3}
        e.atomic(self.root / "receipts" / (task["id"] + ".json"), e.encoded(receipt))
        with patch.object(e, "fetch", return_value=(429, b"later", {})) as fetch:
            e.run(self.root, max_jobs=1)
        completed = json.loads((self.root / "receipts" / (task["id"] + ".json")).read_text())
        self.assertEqual(completed["state"], "complete")
        self.assertEqual(fetch.call_count, 1)  # Only the next task contacted the network.

    def test_changed_manifest_tasks_rejected(self):
        (self.root / "tasks.json").write_bytes(b"[]\n")
        with self.assertRaisesRegex(ValueError, "identity"):
            e.audit(self.root)

    def test_offline_replay_never_calls_network(self):
        with patch.object(e, "fetch", return_value=(200, e.encoded(response()), {})):
            e.run(self.root, max_jobs=1)
        with patch.object(e, "fetch") as fetch:
            result = e.replay(self.root, all_captured=True)
        fetch.assert_not_called()
        self.assertTrue(result["checksums_verified"])
        self.assertEqual(result["integrity_errors"], [])

    def test_report_writer_cannot_race_active_collector(self):
        with (self.root / "writer.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                e.audit(self.root, write=True)
            self.assertEqual(e.audit(self.root, write=False)["planned_tasks"], 5)


class CalendarTests(unittest.TestCase):
    def payload(self):
        return {"data": [{"jyrq": f"2020-02-{d:02}", "jybz": "1" if d == 3 else "0"}
                         for d in range(1, 30)]}

    def parse(self, payload):
        return e.parse_calendar(e.encoded(payload), {"month": "2020-02"}, "2020-02-01", "2020-02-20")

    def test_explicit_calendar_flags_not_weekday_inference(self):
        rows, coverage = self.parse(self.payload())
        self.assertEqual(len(rows), 20)
        self.assertEqual(coverage["source_natural_days"], 29)
        self.assertEqual(coverage["target_open_days"], 1)
        self.assertFalse(rows[3]["is_open"])  # Synthetic closed Tuesday is preserved.
        self.assertIsNone(rows[0]["historical_available_at"])

    def test_missing_day_preserves_other_observations_without_guessing(self):
        data = self.payload()
        data["data"].pop(0)
        rows, coverage = self.parse(data)
        self.assertEqual(len(rows), 19)
        self.assertEqual(coverage["source_missing_dates"], ["2020-02-01"])
        self.assertEqual(coverage["source_status"], "partial_source_month")
        self.assertFalse(coverage["source_month_complete"])
        self.assertFalse(any(r["date"] == "2020-02-01" for r in rows))

    def test_duplicate_and_unsupported_enum_rejected(self):
        data = self.payload()
        data["data"][-1] = data["data"][0]
        with self.assertRaises(ValueError):
            self.parse(data)
        data = self.payload()
        data["data"][0]["jybz"] = "2"
        with self.assertRaises(ValueError):
            self.parse(data)

    def test_existing_official_source_is_replayed_with_original_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            original = folder / "existing"
            original.mkdir()
            raw = e.encoded(self.payload())
            url = "https://www.szse.cn/api/report/exchange/onepersistenthour/monthList?month=2020-02"
            (original / "szse-2020-02.json").write_bytes(raw)
            (original / "szse-2020-02.json.receipt.json").write_bytes(e.encoded({
                "status": 200, "sha256": e.digest(raw), "url": url, "captured_end": "2021-01-01T00:00:00+00:00"}))
            with patch.object(e, "fetch") as fetch:
                e.prepare_calendar(folder / "batch", "2020-02-01", "2020-02-29", original)
            fetch.assert_not_called()
            receipt = json.loads((folder / "batch/receipts/szse_calendar-2020-02.json").read_text())
            self.assertEqual(receipt["observed_at"], "2021-01-01T00:00:00+00:00")
            self.assertEqual(receipt["row_count"], 29)
            self.assertTrue(receipt["attempts"][0]["local_replay"])


if __name__ == "__main__":
    unittest.main()
