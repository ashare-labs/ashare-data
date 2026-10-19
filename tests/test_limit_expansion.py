"""Reference accuracy, lifespan bounds, and resume without a database copy."""
import copy
import gzip
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ashare_data.financials import expansion as e
from ashare_data.financials import limit_expansion as p
from ashare_data.core.model import DataError

TASK = {"symbol":"sh600000", "start":"2010-01-01", "end":"2012-09-26"}
TASK["url"] = p.url_for(TASK["symbol"], TASK["start"], TASK["end"])


def body(rows=None):
    if rows is None:
        rows = [{"ts_code":"600000.SH", "trade_date":"20100104", "high_limit":23.86,
                 "low_limit":19.52,"is_st":0,"is_paused":0,"factor":2.9692}]
    return e.encoded({"code":200,"data":{"ts_code":"600000.SH","candle_mode":0,"count":len(rows),"list":rows}})


class ReferenceParserTests(unittest.TestCase):
    def test_preserves_tokens_and_no_ohlcv_or_official_promotion(self):
        raw = body().replace(b'23.86', b'23.860000')
        rows, cov = p.parse(raw, TASK)
        self.assertEqual(rows[0]["fields"]["high_limit"]["value"], "23.860000")
        self.assertEqual(rows[0]["fields"]["factor"]["value"], "2.9692")
        self.assertFalse(rows[0]["official_limit_certified"])
        self.assertNotIn("close", rows[0])
        self.assertFalse(cov["full_history_proven"])

    def test_empty_is_unknown_and_missing_zero_null_are_distinct(self):
        rows, cov = p.parse(body([]), TASK)
        self.assertFalse(cov["absence_proved"]); self.assertEqual(rows, [])
        payload = json.loads(body()); row = payload["data"]["list"][0]
        row.pop("factor"); row["high_limit"] = None; row["low_limit"] = 0
        row["is_st"] = 0
        fields = p.parse(e.encoded(payload), TASK)[0][0]["fields"]
        self.assertEqual(fields["factor"]["state"], "field_missing")
        self.assertEqual(fields["high_limit"]["state"], "source_null")
        self.assertEqual(fields["low_limit"]["state"], "invalid_numeric_or_flag")
        self.assertEqual(fields["is_st"]["value"], 0)

    def test_count_identity_bounds_and_duplicate_day_rejected(self):
        original = json.loads(body())
        edits = [lambda x: x["data"].update(count=2),
                 lambda x: x["data"]["list"][0].update(ts_code="600001.SH"),
                 lambda x: x["data"]["list"][0].update(trade_date="20091231"),
                 lambda x: x["data"].update(count=2,list=x["data"]["list"]*2)]
        for edit in edits:
            payload = copy.deepcopy(original); edit(payload)
            with self.assertRaises(DataError):p.parse(e.encoded(payload), TASK)

    def test_requests_cannot_cross_scope_or_change_adjustment(self):
        with self.assertRaises(ValueError): p.url_for("sh600000", "2010-01-01", "2012-09-27")
        for url in [TASK["url"].replace("candle_mode=0","candle_mode=1"), TASK["url"]+"&limit=1000",
                    TASK["url"].replace("https://api.","https://attacker.")]:
            with self.assertRaises(ValueError):p.validate_url(url)


class ReferenceBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="limit-expansion-test-")
        self.root = Path(self.tmp.name)
        daily = self.root / "daily.db"
        with sqlite3.connect(daily) as con:
            con.execute("CREATE TABLE instruments(symbol, name, ipo, out, board)")
            con.execute("INSERT INTO instruments VALUES('sh600000','synthetic','1999-11-10','2012-09-28','main')")
            con.execute("CREATE TABLE code_events(id, old_symbol, new_symbol, effective_date)")
            con.execute("CREATE TABLE metadata(key,value)")
        self.baseline = self.root / "baseline"
        e.prepare(self.baseline, daily, "2010-01-01", "2026-10-19")
        self.batch = self.root / "reference"
        p.prepare(self.batch, self.baseline)

    def tearDown(self):self.tmp.cleanup()

    def test_nonoverlapping_windows_stop_before_code_exit(self):
        _, manifest, tasks = e.load_batch(self.batch)
        self.assertEqual([(t["start"],t["end"]) for t in tasks],
                         [("2010-01-01","2012-09-26"),("2012-09-27","2012-09-27")])
        self.assertEqual(manifest["target_codes"],1)
        self.assertEqual(p.prepare(self.batch,self.baseline),manifest)

    def test_success_reused_and_provider_parse_error_retained(self):
        with patch.object(p, "fetch", return_value=(200,body(),{})) as fetch:
            p.run(self.batch,max_jobs=1)
            p.run(self.batch,max_jobs=1)
            # Second window is a source/date mismatch, retained as parse_failed.
            result = p.run(self.batch)
            self.assertEqual(fetch.call_count,2)
        self.assertEqual(result["task_states"],{"complete":1,"parse_failed":1})
        self.assertEqual(e.audit(self.batch,verify=True)["integrity_errors"],[])
        self.assertFalse(list(self.batch.rglob('*.tmp')))

    def test_http_access_error_stops_remaining_tasks_on_same_host(self):
        with patch.object(p, "fetch", return_value=(403,b"denied",{})) as fetch:
            result = p.run(self.batch)
            self.assertEqual(fetch.call_count,1)
        state = json.loads((self.batch/'source-state.json').read_text())
        self.assertIn('blocked',state['api.zizizaizai.com'])
        self.assertEqual(result['task_states']['pending'],1)

    def test_read_keeps_current_reference_separate_from_pit_and_absence(self):
        from ashare_data.financials.expansion_access import SourceUseDenied
        with patch.object(p, "fetch", return_value=(200,body(),{})):
            p.run(self.batch,max_jobs=1)
        reader = p.ReferenceReader(self.batch)
        result = reader.read("sh600000", "2010-01-04", "high_limit", as_of="2030-01-01T00:00:00Z")
        self.assertEqual(result["value"], "23.86")
        self.assertFalse(result["official_limit_certified"])
        with self.assertRaises(SourceUseDenied):
            reader.read("sh600000", "2010-01-04", "high_limit", purpose="historical_pit", as_of="2030-01-01T00:00:00Z")
        with self.assertRaises(SourceUseDenied):
            reader.read("sh600000", "2010-01-05", "is_paused", as_of="2030-01-01T00:00:00Z")
        with self.assertRaises(SourceUseDenied):
            reader.read("sh600000", "2010-01-04", "high_limit", as_of="2010-01-04T18:00:00Z")
        audit = reader.read("sh600000", "2010-01-04", "high_limit", purpose="source_audit")
        self.assertFalse(audit["calculation_value_exposed"])


if __name__ == '__main__':unittest.main()
