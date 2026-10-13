"""Source scope and completeness boundaries for the offline supplement builder."""

import hashlib
import json
from pathlib import Path
import runpy

import pytest



@pytest.fixture(scope="module")
def builder():
    return runpy.run_path(str(Path(__file__).parents[1] / "scripts/build_daily_execution.py"))


def write_history(root, *, rows=(), count=None, pages=1, code="000333", status=200):
    (root / "raw").mkdir(exist_ok=True)
    path = root / "raw/szse-names-000333.json"
    path.write_text(
        json.dumps(
            [
                dict(
                    error=None,
                    metadata=dict(
                        tabkey="tab2",
                        catalogid="SSGSGMXX",
                        recordcount=len(rows) if count is None else count,
                        pagecount=pages,
                    ),
                    data=list(rows),
                )
            ]
        )
    )
    receipt = dict(
        status=status,
        url="https://www.szse.cn/api/report/ShowReport/data?CATALOGID=SSGSGMXX"
        "&TABKEY=tab2&txtZzrq=2024-03-29&txtZqdm=" + code,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    path.with_name(path.name + ".receipt.json").write_text(json.dumps(receipt))


def test_complete_empty_history_is_distinct_from_missing_or_truncated_response(tmp_path, builder):
    write_history(tmp_path)
    assert builder["official_name_history"](tmp_path, "000333.XSHE")[0] == []
    write_history(tmp_path, count=1)
    with pytest.raises(ValueError, match="Incomplete"):
        builder["official_name_history"](tmp_path, "000333.XSHE")
    write_history(tmp_path, pages=2)
    with pytest.raises(ValueError, match="Incomplete"):
        builder["official_name_history"](tmp_path, "000333.XSHE")


@pytest.mark.parametrize(
    "change",
    [
        {"code": "000651"},
        {"status": 403},
        {"rows": [dict(zqdm="000651", bgrq="2019-01-01")]},
        {"rows": [dict(zqdm="000333", bgrq="2024-04-01")]},
    ],
)
def test_history_must_match_security_cutoff_and_successful_query(tmp_path, builder, change):
    write_history(tmp_path, **change)
    with pytest.raises(ValueError):
        builder["official_name_history"](tmp_path, "000333.XSHE")


def test_execution_output_cannot_silently_replace_another_revision(tmp_path, builder):
    path = tmp_path / "binding.json"
    builder["fixed"](path, b"first")
    builder["fixed"](path, b"first")
    with pytest.raises(ValueError, match="Refusing to replace"):
        builder["fixed"](path, b"second")
    assert path.read_bytes() == b"first"
