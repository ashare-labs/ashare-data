"""Source parsing fixtures; none of these clock values are actual observations."""

from datetime import datetime
from urllib.parse import parse_qs, urlparse

import pytest


from ashare_data import phase_observation as phase
from ashare_data.calendar_scope import cash_inventory
from ashare_data.model import DataError, canonical
from test_phase_observation import install

pytestmark = pytest.mark.usefixtures("synthetic_action_baseline")


def market(monkeypatch, *, wrong_date=False, incomplete=False, wrong_code=False, day="2026-10-14"):
    at = day + "T08:55:00+08:00"
    install(monkeypatch, at)
    old = phase.fetch

    def fetch(url, referer):
        params = parse_qs(urlparse(url).query)
        if "COMMON_SSE_CP" in url:
            code = params["STOCK_CODE"][0]
            record = dict(
                A_STOCK_CODE="999999" if wrong_code else code,
                STOCK_TYPE="1",
                LIST_BOARD="1",
                DELIST_DATE="-",
                LIST_DATE="20000101",
            )
            return canonical(dict(result=[record], pageHelp=dict(total=2 if incomplete else 1)))
        if "push2.eastmoney.com" in url:
            code = params["secid"][0][2:]
            ref = {"600000": 10, "600036": 20, "601318": 30}[code]
            t = datetime.fromisoformat(at.replace(day[5:], "10-12") if wrong_date else at)
            return canonical(
                dict(
                    rc=0,
                    data=dict(
                        f57=code, f86=int(t.timestamp()), f60=ref, f51=ref * 1.1, f52=ref * 0.9
                    ),
                )
            )
        return old(url, referer)

    monkeypatch.setattr(phase, "fetch", fetch)


def test_source_market_fields_are_derived_not_hardcoded_unknown(tmp_path, monkeypatch):
    market(monkeypatch)
    attempt = phase.capture(tmp_path, "admission", "2026-10-14")
    result = phase.read(tmp_path, attempt["capture_id"])["assessment"]
    for row in result["rows"]:
        assert row["eligible"] is True and row["is_st"] is False and row["suspended"] is False
        assert row["lower_limit"] and row["upper_limit"] and row["rule_version"]
        assert row["cash_inventory"]["cash_history_complete"] is True
        assert row["action_scope"]["price_share_code_review"] is None
    assert result["execution_permission"] is False
    assert all(x.startswith("SCOPED_PRICE_SHARE_CODE_REVIEW_MISSING") for x in result["blockers"])


@pytest.mark.parametrize("case", ["wrong_date", "incomplete", "wrong_code"])
def test_stale_limits_incomplete_or_wrong_identity_block(tmp_path, monkeypatch, case):
    market(monkeypatch, **{case: True})
    attempt = phase.capture(tmp_path, "admission", "2026-10-14")
    result = phase.read(tmp_path, attempt["capture_id"])["assessment"]
    assert result["execution_permission"] is False
    assert any(x.startswith("CURRENT_LIMITS_UNVERIFIED") for x in result["blockers"])
    if case != "wrong_date":
        assert any(x.startswith("CURRENT_IDENTITY_UNVERIFIED") for x in result["blockers"])


def cash(records):
    return dict(success=True, code=0, result=dict(pages=1, count=len(records), data=records))


def test_cash_dates_support_zero_entitlement_but_proposal_is_not_implementation():
    rows = [
        dict(
            SECURITY_CODE="601318",
            ASSIGN_PROGRESS="实施分配",
            EQUITY_RECORD_DATE="2026-09-09 00:00:00",
            EX_DIVIDEND_DATE="2026-09-10 00:00:00",
            NOTICE_DATE="2026-09-03 00:00:00",
            PRETAX_BONUS_RMB=9.8,
        ),
        dict(
            SECURITY_CODE="601318",
            ASSIGN_PROGRESS="预披露",
            EQUITY_RECORD_DATE=None,
            EX_DIVIDEND_DATE=None,
        ),
    ]
    result = cash_inventory(cash(rows), "601318", "2026-10-13")
    assert result["events"][0]["record_before_pilot_genesis"] is True
    assert result["events"][0]["pay_date"] is None
    assert len(result["proposals"]) == 1 and result["unresolved"] == []
    rows[0]["EX_DIVIDEND_DATE"] = "2026-10-13"
    assert (
        "CURRENT_OR_FUTURE_PRICE_ACTION_REVIEW_REQUIRED"
        in cash_inventory(cash(rows), "601318", "2026-10-13")["unresolved"]
    )
    rows[0]["SECURITY_CODE"] = "600000"
    with pytest.raises(DataError):
        cash_inventory(cash(rows), "601318", "2026-10-13")
