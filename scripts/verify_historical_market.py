"""Offline acceptance of the documented finite multi-board public-source batch."""

import argparse
import json

from ashare_data import DataError, HistoricalMarketView


def verify(binding, sha):
    view = HistoricalMarketView(binding, expected_binding_sha256=sha)
    checks = []

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    def rejected(name, code, call):
        try:
            call()
        except DataError as exc:
            check(name, exc.code == code)
        else:
            raise AssertionError(name + " was accepted")

    halt = view.state("600518.XSHG", "2019-05-20")
    st = view.state("600518.XSHG", "2019-05-21")
    check(
        "kangmei_one_day_halt_then_ST",
        halt["suspended"]["value"] is True
        and st["suspended"]["value"] is False
        and st["is_st"]["value"] is True,
    )
    check(
        "kangmei_5pct_issuer_rule",
        st["upper_limit"]["value"] == "6.97" and st["lower_limit"]["value"] == "6.31",
    )
    before = view.state("300750.XSHE", "2020-08-21")
    after = view.state("300750.XSHE", "2020-08-24")
    check(
        "chinext_transition_10_to_20",
        before["rule"]["ratio"] == "0.10" and after["rule"]["ratio"] == "0.20",
    )
    check(
        "bad_source_10pct_claim_quarantined",
        after["source_claimed_limits"]["raw_fields"]["up_limit"] == "211.45"
        and after["upper_limit"]["value"] == "230.68"
        and after["lower_limit"]["value"] == "153.78"
        and after["source_claimed_limits"]["execution_usable"] is False,
    )
    check(
        "explicit_minute_consumer_daily_model",
        view.order_state(
            "300750.XSHE",
            as_of="2020-08-24T09:31:00+08:00",
            side="BUY",
            accept_daily_replay=True,
            accept_limit_model=True,
        )["ready"],
    )
    check(
        "implicit_model_rejected",
        not view.order_state("300750.XSHE", as_of="2020-08-24T09:31:00+08:00", side="BUY")["ready"],
    )
    for label in ("2020-07-16", "2020-07-17", "2020-07-20", "2020-07-21", "2020-07-22"):
        check(
            "star_no_daily_band_" + label,
            view.state("688981.XSHG", label)["upper_limit"]["status"] == "not_applicable",
        )
    check(
        "star_after_5_sessions_20pct",
        view.state("688981.XSHG", "2020-07-23")["upper_limit"]["value"] == "95.48",
    )
    check(
        "bse_ipo_date_correction",
        view.instruments(["832982.XBSE"], on_date="2023-07-19")[0]["lifecycle_state"]
        == "not_yet_listed",
    )
    check(
        "bse_no_fake_daily_status",
        view.state("832982.XBSE", "2023-07-20")["suspended"]["value"] is None,
    )
    check(
        "bse_no_band_not_zero",
        view.state("832982.XBSE", "2023-07-20")["upper_limit"]["status"] == "not_applicable",
    )
    check(
        "bse_subsequent_30pct_reference_missing",
        view.state("832982.XBSE", "2023-07-21")["rule"]["ratio"] == "0.30"
        and view.state("832982.XBSE", "2023-07-21")["upper_limit"]["status"] == "unavailable",
    )
    check(
        "dated_delisting",
        view.instruments(["600087.XSHG"], on_date="2014-06-05")[0]["delisting_date"]["value"]
        == "2014-06-05",
    )
    check(
        "delisted_security_survives_historical_population",
        view.universe("2014-06-04", candidates=["600087.XSHG"])["rows"][0]["security"]
        == "600087.XSHG",
    )
    for label, count in (
        ("2014-06-04", 2505),
        ("2019-05-21", 3607),
        ("2020-07-15", 3882),
        ("2020-07-16", 3885),
    ):
        p = view.universe(label)
        check(
            "full_archive_pagination_" + label,
            len(p["rows"]) == count and p["source_query_complete"] and not p["market_complete"],
        )
    check(
        "IPO_absence_before_is_unknown",
        bool(view.universe("2020-07-15", candidates=["688981.XSHG"])["unknown"]),
    )
    check(
        "IPO_present_on_first_date",
        len(view.universe("2020-07-16", candidates=["688981.XSHG"])["rows"]) == 1,
    )
    check(
        "empty_2024_source_not_empty_market",
        view.universe("2024-01-02")["coverage_state"] == "unavailable",
    )
    check(
        "2024_ST_not_backfilled", view.state("600000.XSHG", "2024-01-02")["is_st"]["value"] is None
    )
    rejected(
        "strict_PIT_rejected",
        "PIT_UNAVAILABLE",
        lambda: view.state("300750.XSHE", "2020-08-24", visibility="verified"),
    )
    rejected(
        "historical_receipt_clock_rejected",
        "RECEIPT_NOT_VISIBLE",
        lambda: view.state(
            "300750.XSHE",
            "2020-08-24",
            visibility="received",
            knowledge_at="2020-08-24T09:00:00+08:00",
        ),
    )
    rows = view.corporate_actions(
        ["000651.XSHE", "300750.XSHE"], start="2022-01-01", end="2023-12-31"
    )["rows"]
    by_symbol = {r["security"]: r for r in rows}
    gree, catl = by_symbol["000651.XSHE"], by_symbol["300750.XSHE"]
    check(
        "gree_gross_vs_reference_deduction",
        gree["cash_per_share"] == "1" and gree["ex_price_cash_deduction"] == "0.936124",
    )
    check(
        "gree_no_fake_intraday_cash",
        gree["payment_date"] == "2022-04-08" and gree["cash_available_at"] is None,
    )
    check(
        "gree_payment_date_is_depository_only",
        "CSDC Shenzhen" in gree["payment_date_scope"]
        and "only" in gree["payment_date_scope"]
        and "company_self_payment_date" in gree
        and gree["company_self_payment_date"] is None,
    )
    check(
        "catl_units_per_old_share",
        catl["cash_per_share"] == "2.52"
        and catl["transfer_per_share"] == "0.8"
        and catl["bonus_per_share"] == "0",
    )
    check(
        "catl_credit_vs_trading",
        catl["share_credit_date"] == catl["new_share_trading_date"] == "2023-04-26"
        and catl["share_credit_scope"] != catl["new_share_trading_scope"]
        and catl["share_credit_at"] is None,
    )
    check("catl_self_paid_unknown", catl["company_self_payment_date"] is None)
    rejected(
        "intraday_action_time_required_rejected",
        "HISTORICAL_ACTION_TIME_UNKNOWN",
        lambda: view.corporate_actions(
            ["300750.XSHE"], start="2023-04-01", end="2023-04-30", require_share_credit_time=True
        ),
    )
    check(
        "announcement_status_events_available",
        len(view.security_events(["600518.XSHG"], start="2019-05-17", end="2019-05-23")["rows"])
        == 2,
    )
    return dict(
        binding_sha256=sha,
        checks_passed=len(checks),
        checks=checks,
        coverage=view.coverage(),
        network_used=False,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("binding")
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.binding, args.sha256), ensure_ascii=False, indent=2))
