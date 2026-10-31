import copy
from types import SimpleNamespace

import pytest

from ashare_data.providers.free_updates import free_update_policy_releases as policy
from ashare_data.providers.free_updates.free_update_semantics import validate_semantics


def checked(rows, name):
    return validate_semantics(rows, {"function": name, "parameters": {}}, "2026-10-29")


def oil_page(content):
    return '<h2>2026年10月29日国内成品油价格按机制调整</h2><div class="TRS_Editor">'+content+'</div>'


def test_actual_oil_adjustment_is_not_theoretical_or_absolute_price():
    rows = policy.parse_ndrc(oil_page('汽、柴油价格每吨分别应上调440元、420元，调控后实际上调345元、335元。'
                                      '自2026年10月29日24时起执行。'), "https://www.ndrc.gov.cn/xwdt/xwfb/test.html")
    assert rows[0]["汽油调整"] == "345" and rows[0]["柴油调整"] == "335"
    assert rows[0]["生效时间"] == "2026-10-30T00:00:00+08:00"
    assert checked(rows, "ndrc_oil_adjustment")["latest_announcement"] == "2026-10-29"
    assert "price" not in rows[0]


def test_unchanged_policy_has_no_fabricated_effective_time():
    rows = policy.parse_ndrc(oil_page("本次汽、柴油价格不作调整。"), "https://www.ndrc.gov.cn/xwdt/xwfb/test.html")
    assert rows[0]["汽油调整"] == "0" and rows[0]["生效时间"] is None
    checked(rows, "ndrc_oil_adjustment")
    rows[0]["汽油调整"] = "1"
    with pytest.raises(ValueError, match="unchanged"):
        checked(rows, "ndrc_oil_adjustment")


@pytest.mark.parametrize("content", ["应上调440元、420元。", "实际上调10元、20元，实际下调30元、40元。",
    "本次汽、柴油价格不作调整。实际上调10元、20元。", "实际上调10元、20元，自10月30日24时起执行。"])
def test_ambiguous_oil_change_or_yearless_date_is_rejected(content):
    with pytest.raises(ValueError):
        policy.parse_ndrc(oil_page(content), "https://www.ndrc.gov.cn/xwdt/xwfb/test.html")


def flow_page():
    header = "<tr>"+"".join("<td>"+x+"</td>" for x in policy.FLOW_FIELDS)+"</tr>"
    row = "<tr><td>2026.09</td>"+"".join("<td>"+str(x)+"</td>" for x in [100, 50, -1, 2, 3, 4, 5, 6, 7, 8, 9])+"</tr>"
    blank = "<tr><td>2026.10</td>"+"<td></td>"*11+"</tr>"
    return "社会融资规模增量统计表 单位：亿元人民币<table>"+header+row+blank+"</table>"


def test_flow_blank_future_month_is_not_zero_or_publication_date():
    rows, unpublished = policy.parse_flow(flow_page(), "2026", "https://www.pbc.gov.cn/test.htm")
    assert unpublished == ["2026-10"] and len(rows) == 1
    assert rows[0]["外币贷款（折合人民币）"] == "-1" and rows[0]["publication_at"] is None
    assert checked(rows, "pbc_social_financing_flow")["units"]["人民币贷款"] == "100000000 CNY"


@pytest.mark.parametrize("before,after", [("亿元人民币", "万元人民币"), ("2026.09", "2025.09"),
    ("<td>50</td>", "<td></td>"), ("<td>委托贷款</td>", "<td>政府债券</td>")])
def test_flow_layout_unit_partial_row_and_year_are_bound(before, after):
    with pytest.raises(ValueError):
        policy.parse_flow(flow_page().replace(before, after), "2026", "https://www.pbc.gov.cn/test.htm")


def sh_page():
    return '''<meta name="ArticleTitle" content="上海市发展和改革委员会关于车用汽、柴油价格的通知（2026年10月29日）">
    自10月29日24时起执行<table><tr><td>标号</td><td>单位</td><td>最高零售价</td></tr>
    <tr><td rowspan="2">92号汽油</td><td>元/吨</td><td>11713</td></tr>
    <tr><td>元/升</td><td>8.85</td></tr></table>'''


def test_provincial_ceiling_preserves_two_native_units_without_conversion():
    rows = policy.parse_shanghai(sh_page(), "https://fgw.sh.gov.cn/fgw_jggl/test.html")
    assert [r["最高零售价"] for r in rows] == ["11713", "8.85"]
    assert {r["单位"] for r in rows} == {"元/吨", "元/升"}
    assert checked(rows, "shanghai_oil_prices")["business_key"] == ["地区", "油号", "单位", "公告日期"]
    bad = copy.deepcopy(rows)
    bad[0]["地区"] = "全国"
    with pytest.raises(ValueError, match="regional"):
        checked(bad, "shanghai_oil_prices")
    with pytest.raises(ValueError, match="incomplete"):
        policy.parse_shanghai(sh_page().replace('<tr><td>元/升</td><td>8.85</td></tr>', ''), "url")


def test_directory_uses_latest_dated_policy_and_same_official_host():
    directory = '<a href="/fgw_jggl/20261015/old.html">上海市发展和改革委员会关于车用汽、柴油价格的通知（2026年10月15日）</a>'
    directory += '<a href="http://fgw.sh.gov.cn/fgw_jggl/20261029/new.html">上海市发展和改革委员会关于车用汽、柴油价格的通知（2026年10月29日）</a>'
    requested = []
    def get(url):
        requested.append(url)
        return directory if len(requested) == 1 else sh_page()
    capture = SimpleNamespace(get=get)
    policy.collect(capture, "shanghai_oil_prices", {})
    assert requested[1] == "https://fgw.sh.gov.cn/fgw_jggl/20261029/new.html"
    assert not capture.dataset_metadata["complete_history"]
    with pytest.raises(ValueError, match="reviewed"):
        policy.link(policy.SH_DIRECTORY, "https://example.com/fgw_jggl/other", "fgw.sh.gov.cn", "/fgw_jggl/")
