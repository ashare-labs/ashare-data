"""Synthetic video/text combinations protect partial transcript availability."""
from copy import deepcopy
from datetime import datetime, timezone
import gzip
import hashlib
import json

import pytest

from ashare_data.providers.free_update_broadcast import article, collect, refresh_missing, receipt_window_start, transcript_summary, validate

DAY = "2026-10-28"
URL = "https://tv.cctv.com/2026/10/28/VIDETEST261028.shtml"
JOB = {"function": "news_cctv", "parameters": {"date": "20261028"}}


def page(identity="VIDETEST261028", text=""):
    return f'<meta name="contentid" content="{identity}"><h3>Test video</h3><div class="content_area">{text}</div>'.encode()


@pytest.fixture(autouse=True)
def html_dependencies():
    pytest.importorskip("bs4", reason="free-updates extra required for HTML contracts")


def test_video_only_article_retains_identity_without_inventing_text():
    row = article(page(), URL, DAY)
    assert row["content"] == "" and row["transcript_status"] == "source_text_absent"
    assert row["source_id"] == "VIDETEST261028" and row["publication_at"] is None
    with pytest.raises(ValueError, match="transcript/identity"):
        article(page(identity="OTHER"), URL, DAY)


def test_partial_broadcast_retains_complete_directory_and_available_text():
    other = URL.replace("TEST", "SECOND")
    index = (f'<ul><li><a title="《新闻联播》 20261028" href="{URL.replace("TEST", "FULL")}">Full</a></li>'
             f'<li><a href="{URL}">Text</a></li><li><a href="{other}">Video</a></li></ul>').encode()

    class Capture:
        def get(self, url):
            return {"https://tv.cctv.com/lm/xwlb/day/20261028.shtml": index,
                    URL: page(text="Actual source text"), other: page(identity="VIDESECOND261028")}[url]

    capture = Capture()
    rows = collect(capture, JOB["parameters"])
    check = validate(rows, JOB, DAY)
    assert len(rows) == 2 and check["available_transcripts"] == 1 and not check["gaps"]
    assert capture.dataset_metadata["complete_requested_broadcast_index"]
    assert not capture.dataset_metadata["complete_transcript_text"]
    assert capture.dataset_metadata["missing_transcript_ids"] == ["VIDESECOND261028"]
    assert "transcript_status" not in rows[0]


def test_all_missing_text_does_not_count_as_available_transcripts():
    with pytest.raises(ValueError, match="no available transcript"):
        validate([article(page(), URL, DAY)], JOB, DAY)


def test_acceptance_summary_rejects_stale_missing_text_count_after_repair():
    rows = [article(page(text="Actual retained transcript"), URL, DAY)]
    coverage = {"broadcast_date": DAY, "expected_articles": 1, "received_articles": 1,
                "available_transcripts": 1, "missing_transcript_ids": [], "complete_transcript_text": True}
    summary = transcript_summary(rows, coverage)
    assert summary["available_transcripts"] == 1 and "1篇完整文字稿" in summary["description"]
    with pytest.raises(ValueError, match="contradict"):
        transcript_summary(rows, coverage | {"available_transcripts": 0})


@pytest.mark.parametrize("mutation", ["missing_marker", "false_marker"])
def test_missing_text_marker_must_match_actual_content(mutation):
    available = article(page(text="Actual source text"), URL, DAY)
    invalid = deepcopy(available)
    if mutation == "missing_marker":
        invalid["content"] = ""
    else:
        invalid["transcript_status"] = "source_text_absent"
    with pytest.raises(ValueError, match="marked|contradicts"):
        validate([invalid], JOB, DAY)


def retry_source(root):
    other = URL.replace("TEST", "SECOND")
    index_url = "https://tv.cctv.com/lm/xwlb/day/20261028.shtml"
    index = (f'<li><a title="《新闻联播》 20261028" href="{URL.replace("TEST", "FULL")}">Full</a></li>'
             f'<li><a href="{URL}">Text</a></li><li><a href="{other}">Video</a></li>').encode()
    (root / "raw").mkdir(exist_ok=True)
    receipts = []
    for url, body in [(index_url, index), (URL, page(text="Kept text")), (other, page(identity="VIDESECOND261028"))]:
        sha = hashlib.sha256(body).hexdigest()
        (root / "raw" / (sha+".gz")).write_bytes(gzip.compress(body))
        receipts.append({"url": url, "sha256": sha, "protocol": "http", "status": 200,
                         "received_at": "2026-10-28T12:00:00+00:00"})
    return other, {"receipts": receipts, "status": "received"}


@pytest.mark.parametrize("text", ["Late source transcript", ""])
def test_retry_only_fetches_absent_text_and_keeps_complete_directory(tmp_path, text):
    other, original = retry_source(tmp_path)
    calls = []

    class Capture:
        receipts = []

        def get(self, url):
            assert url == other
            calls.append(url)
            body = page(identity="VIDESECOND261028", text=text)
            sha = hashlib.sha256(body).hexdigest()
            (tmp_path / "raw" / (sha+".gz")).write_bytes(gzip.compress(body))
            self.receipts.append({"url": url, "sha256": sha, "status": 200, "protocol": "http",
                                  "received_at": "2026-10-29T12:00:00+00:00"})
            return body

    old_start = datetime(2026, 10, 28, 11, tzinfo=timezone.utc)
    old_end = datetime(2026, 10, 28, 13, tzinfo=timezone.utc)
    rows, coverage, receipts, reused = refresh_missing(Capture(), tmp_path, original, JOB["parameters"], old_start, old_end)
    assert calls == [other] and reused == [0, 1] and rows[0]["content"] == "Kept text"
    assert len(rows) == 2 and rows[1]["content"] == text and len(receipts) == 3
    assert coverage["available_transcripts"] == (2 if text else 1)
    assert coverage["missing_transcript_ids"] == ([] if text else ["VIDESECOND261028"])


def test_cached_receipts_require_existing_run_hash_and_actual_new_capture_time(tmp_path):
    from ashare_data.storage.free_updates import writer
    with writer(tmp_path):
        pass
    _, original = retry_source(tmp_path)
    (tmp_path / "runs").mkdir(exist_ok=True)
    body = json.dumps(original).encode()
    (tmp_path / "runs" / "original.json").write_bytes(body)
    with writer(tmp_path) as db:
        with db:
            db.execute("INSERT INTO runs(run_id,job_id,as_of,started_at,finished_at,status,job_json,result_json) VALUES(?,?,?,?,?,?,?,?)",
                ("original", "test", DAY, "2026-10-28T11:00:00+00:00", "2026-10-28T13:00:00+00:00", "received", json.dumps(JOB), "{}"))
    result = deepcopy(original)
    result["receipts"][2]["received_at"] = "2026-10-29T12:00:00+00:00"
    result["receipt_reuse"] = {"source_run_id": "original", "source_result_sha256": hashlib.sha256(body).hexdigest(), "indices": [0, 1]}
    run = {"started_at": "2026-10-29T11:00:00+00:00", "finished_at": "2026-10-29T13:00:00+00:00"}
    assert receipt_window_start(tmp_path, result, run).date().isoformat() == DAY
    tampered = deepcopy(result)
    tampered["receipts"][0]["sha256"] = "0"*64
    with pytest.raises(ValueError, match="cached receipt"):
        receipt_window_start(tmp_path, tampered, run)
    tampered = deepcopy(result)
    tampered["receipts"][2]["received_at"] = "2026-10-28T12:00:00+00:00"
    with pytest.raises(ValueError, match="actual execution"):
        receipt_window_start(tmp_path, tampered, run)
