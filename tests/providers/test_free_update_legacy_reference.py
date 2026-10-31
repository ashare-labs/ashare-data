from copy import deepcopy
from decimal import Decimal
import gzip
import hashlib
import json
from datetime import datetime, timezone

import pytest

from ashare_data.providers.free_updates.free_update_legacy_reference import (
    collect,
    validate,
    tables,
    reviewed_url,
)
from ashare_data.providers.free_updates.free_update_coverage import CapturedResponses
from ashare_data.providers.free_updates.free_update_worker import Capture, SourceBlocked


def table(columns, values):
    return {
        "ErrorCode": 0,
        "ResultSets": [{"ColName": columns, "Count": len(values), "Content": values}],
    }


class Source:
    def __init__(self, values):
        self.values = iter(values)
        self.calls = []
        self.dataset_metadata = {}

    def post_json(self, url, params):
        self.calls.append((url, params))
        return json.dumps(next(self.values)).encode()


def identity():
    return table(["T003", "T002", "sc"], [["Example", "600000", 1]])


def test_company_native_subset_retains_unknown_fields_and_exact_identity():
    source = Source(
        [
            identity(),
            table(
                ["T031", "ssbk", "T035", "unknown"],
                [["2001-08-27", "上交所", "A股", "2.5#unmapped"]],
            ),
        ]
    )
    rows = collect(source, "tdx_f10_company", {"symbol": "sh600000"})
    assert rows[0]["native_fields"]["unknown"] == "2.5#unmapped"
    assert rows[0]["first_publication_at"] is None
    job = {"function": "tdx_f10_company", "parameters": {"symbol": "sh600000"}}
    assert validate(rows, job, "2026-10-29", Decimal)["gaps"] == []
    bad = deepcopy(rows)
    bad[0]["identity"]["sc"] = 0
    with pytest.raises(ValueError, match="identity"):
        validate(bad, job, "2026-10-29", Decimal)
    with pytest.raises(ValueError, match="exchange"):
        collect(
            Source([table(["T003", "T002", "sc"], [["Example", "600001", 1]])]),
            "tdx_f10_company",
            {"symbol": "sh600000"},
        )


def test_valuation_window_pagination_retains_ratio_sign_and_rejects_repeat():
    def page(days):
        return {
            "ErrorCode": 0,
            "ResultSets": [
                {
                    "ColDes": [{"Name": "num"}, {"Name": "total_num"}],
                    "RowNum": 1,
                    "Content": [[str(len(days)), "21"]],
                },
                {
                    "ColDes": [{"Name": n} for n in ["DATE", "PETTM", "PBMRQ", "UNKNOWN"]],
                    "RowNum": len(days),
                    "Content": [[str(d), "-1.5", "2.3", "untouched"] for d in days],
                },
            ],
        }

    first = page(range(20261001, 20261021))
    last = page([20261021])
    source = Source([identity(), first, last])
    params = {"symbol": "sh600000", "pages": 2}
    rows = collect(source, "tdx_f10_valuation", params)
    assert len(rows) == 21 and source.dataset_metadata["complete_history"]
    assert source.calls[-1][1][0]["Page"] == "1" and rows[-1]["pe_ttm"] == "-1.5"
    job = {"function": "tdx_f10_valuation", "parameters": params}
    assert validate(rows, job, "2026-10-29", Decimal)["unique_keys"] == 21
    last["ResultSets"][1]["Content"][0][0] = "20261001"
    with pytest.raises(ValueError, match="repeated"):
        collect(Source([identity(), first, last]), "tdx_f10_valuation", params)
    with pytest.raises(ValueError, match="future"):
        validate(rows, job, "2026-10-15", Decimal)


def test_business_date_binding_and_unknown_units_are_explicit():
    data = {
        "ErrorCode": 0,
        "ResultSets": [
            {
                "ColName": ["N000", "N001", "N002", "N003", "N004", "N009"],
                "Count": 1,
                "Content": [["按产品", 2, "example", "1.234", "33.333333", "--"]],
            },
            {"ColName": ["N001", "N002", "N003"], "Count": 0, "Content": []},
            {"ColName": ["rq"], "Count": 1, "Content": [["20260630"]]},
        ],
    }
    source = Source([identity(), table(["T002"], [["20260630"], ["20251231"]]), data])
    rows = collect(source, "tdx_f10_business", {"symbol": "sh600000"})
    assert rows[0]["report_period"] == "2026-06-30" and rows[0]["announcement_date"] is None
    assert rows[0]["metrics"]["revenue"]["value"] == "1.234"
    assert "scale not established" in rows[0]["metrics"]["revenue"]["unit"]
    assert rows[0]["metrics"]["revenue_share"]["value"] == "33.333333"
    assert rows[0]["metrics"]["revenue_share"]["unit"] == "percent"
    assert rows[0]["metrics"]["gross_margin"]["value"] is None
    assert rows[0]["metrics"]["gross_margin"]["native_value"] == "--"
    job = {"function": "tdx_f10_business", "parameters": {"symbol": "sh600000"}}
    assert validate(rows, job, "2026-10-29", Decimal)["contract_reviewed"]
    altered = deepcopy(rows)
    altered[0]["metrics"]["revenue"]["unit"] = "CNY"
    with pytest.raises(ValueError, match="unit"):
        validate(altered, job, "2026-10-29", Decimal)
    data["ResultSets"][2]["Content"][0][0] = "20251231"
    with pytest.raises(ValueError, match="differs"):
        collect(
            Source([identity(), table(["T002"], [["20260630"]]), data]),
            "tdx_f10_business",
            {"symbol": "sh600000"},
        )


def test_f10_does_not_open_a_host_wide_network_exception(tmp_path):
    url = "http://static.tdx.com.cn:7615/TQLEX?Entry=CWServ.tdxf10_gg_comreq"
    assert reviewed_url(url)
    c = Capture(tmp_path, interval=0)
    assert c.before(url) == "static.tdx.com.cn"
    for bad in [
        url + "&x=1",
        url.replace("7615", "80"),
        url.replace("CWServ.tdxf10_gg_comreq", "unknown"),
    ]:
        with pytest.raises(SourceBlocked):
            c.before(bad)
    with pytest.raises(ValueError):
        tables(table(["same", "same"], [[1, 2]]))


def test_json_post_replay_checks_body_not_just_url(tmp_path):
    raw = b'{"example":true}'
    sha = hashlib.sha256(raw).hexdigest()
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / (sha + ".gz")).write_bytes(gzip.compress(raw))
    now = datetime.now(timezone.utc)
    params = {"Params": ["gpquery", "600000"]}
    result = {
        "receipts": [
            {
                "url": "http://example",
                "protocol": "http",
                "status": 200,
                "received_at": now.isoformat(),
                "sha256": sha,
                "request_method": "POST",
                "request_body_sha256": hashlib.sha256(
                    json.dumps(params, ensure_ascii=False).encode()
                ).hexdigest(),
            }
        ]
    }
    assert CapturedResponses(tmp_path, result, now, now).post_json("http://example", params) == raw
    with pytest.raises(ValueError, match="body"):
        CapturedResponses(tmp_path, result, now, now).post_json(
            "http://example", {"Params": ["gpquery", "600001"]}
        )
