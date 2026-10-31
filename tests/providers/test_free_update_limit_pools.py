import copy
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from ashare_data.providers.free_updates import free_update_limit_pools as pools
from ashare_data.providers.free_updates.free_update_semantics import numeric


def native(**changes):
    return (
        dict(
            c="600001",
            m=1,
            n="测试证券",
            p=12340,
            zdp=10,
            amount=100000,
            ltsz=1000000,
            tshare=2000000,
            hs=1.2,
            hybk="测试行业",
            lbc=2,
            fbt=92500,
            lbt=145959,
            fund=10000,
            zbc=1,
            zttj={"days": 5, "ct": 3},
        )
        | changes
    )


class Capture:
    def __init__(self, source=None, **data_changes):
        self.source = source or [native()]
        self.data_changes = data_changes
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        return json.dumps(
            {
                "rc": 0,
                "data": {"tc": len(self.source), "qdate": 20261028, "pool": self.source}
                | self.data_changes,
            }
        ).encode()


def check(rows):
    return pools.validate(
        rows,
        {"function": "stock_zt_pool_em", "parameters": {"date": "20261028"}},
        "2026-10-29",
        numeric,
    )


def test_named_fields_cannot_reverse_statistic_window_and_occurrences():
    c = Capture()
    rows = pools.collect(c, "stock_zt_pool_em", {"date": "20261028"})
    result = check(rows)
    assert rows[0]["最新价"] == "12.34" and rows[0]["首次封板时间"] == "092500"
    assert rows[0]["统计天数"] == 5 and rows[0]["涨停次数"] == 3
    assert rows[0]["source_native"] == native() and not result["gaps"]
    assert "ST" in result["source_exclusions"]
    assert rows[0]["source_effective_trading_date"] is None and "日期" not in rows[0]
    assert rows[0]["请求日期"] == "2026-10-28" and result["latest_period"] is None
    q = parse_qs(urlsplit(c.urls[0]).query)
    assert q["Pageindex"] == ["0"] and q["date"] == ["20261028"] and len(c.urls) == 1


@pytest.mark.parametrize(
    "change", [{"qdate": 20261301}, {"tc": 2}, {"tc": 10000}, {"pool": [], "tc": 0}]
)
def test_truncated_stale_or_empty_pool_cannot_pass(change):
    with pytest.raises(ValueError):
        pools.collect(Capture(**change), "stock_zt_pool_em", {"date": "20261028"})


@pytest.mark.parametrize(
    "change",
    [
        {"m": 0},
        {"c": "830001", "m": 0},
        {"fbt": 126199},
        {"lbt": 120000},
        {"p": -1},
        {"zttj": {"days": 2, "ct": 3}},
        {"zdp": -10},
    ],
)
def test_source_semantic_corruptions_fail_without_filtering(change):
    with pytest.raises(ValueError):
        check(pools.collect(Capture([native(**change)]), "stock_zt_pool_em", {"date": "20261028"}))


def test_duplicate_identity_and_rewritten_projection_are_rejected():
    with pytest.raises(ValueError):
        pools.collect(Capture([native(), native()]), "stock_zt_pool_em", {"date": "20261028"})
    rows = pools.collect(Capture(), "stock_zt_pool_em", {"date": "20261028"})
    changed = copy.deepcopy(rows)
    changed[0]["最新价"] = "123.4"
    with pytest.raises(ValueError):
        check(changed)
    with pytest.raises(ValueError):
        pools.validate(
            rows,
            {"function": "stock_zt_pool_em", "parameters": {"date": "20261028"}},
            "2026-10-27",
            numeric,
        )


def test_beijing_code_keeps_native_market_zero_without_becoming_shenzhen():
    rows = pools.collect(
        Capture([native(c="920896", m=0)]), "stock_zt_pool_em", {"date": "20261028"}
    )
    assert rows[0]["security"] == "920896.BJ" and rows[0]["source_native"]["m"] == 0
    assert not check(rows)["gaps"]
    for code, market in [("920896", 1), ("830896", 0), ("930001", 0)]:
        with pytest.raises(ValueError, match="identity"):
            pools.security(code, market)


def test_broken_pool_zero_closed_limits_is_preserved_not_removed():
    source = native(ztp=13000, zf=12, zs=0, zttj={"days": 0, "ct": 0})
    rows = pools.collect(Capture([source]), "stock_zt_pool_zbgc_em", {"date": "20261028"})
    result = pools.validate(
        rows,
        {"function": "stock_zt_pool_zbgc_em", "parameters": {"date": "20261028"}},
        "2026-10-29",
        numeric,
    )
    assert len(rows) == 1 and rows[0]["涨停次数"] == 0 and not result["gaps"]
    with pytest.raises(ValueError, match="ct/days"):
        check(pools.collect(Capture([source]), "stock_zt_pool_em", {"date": "20261028"}))


@pytest.mark.parametrize("difference", [0.001, 88.05, 10000000])
def test_native_cap_order_difference_stays_visible_without_blocking_pool_membership(difference):
    original = native(ltsz=2000000 + difference)
    rows = pools.collect(Capture([original]), "stock_zt_pool_em", {"date": "20261028"})
    result = check(rows)
    assert rows[0]["source_native"] == original and not result["gaps"]
    assert result["market_cap_discrepancies"][0]["float_cap_cny"] == original["ltsz"]


def test_mismatched_qdate_is_preserved_in_observed_snapshot_not_rewritten():
    c = Capture(qdate=20261029)
    rows = pools.collect(c, "stock_zt_pool_em", {"date": "20261028"})
    result = check(rows)
    assert rows[0]["请求日期"] == "2026-10-28"
    assert rows[0]["source_reported_qdate"] == "20261029"
    assert rows[0]["source_effective_trading_date"] is None
    assert not c.dataset_metadata["requested_qdate_matches"]
    assert not result["gaps"] and result["business_key"] == ["_snapshot_date", "source_pool", "security"]
    assert result["latest_period"] is None
    with pytest.raises(ValueError, match="future"):
        check(pools.collect(Capture(qdate=20261030), "stock_zt_pool_em", {"date": "20261028"}))
