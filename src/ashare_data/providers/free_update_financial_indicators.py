"""All source pages for one issuer; report dates and announcement dates stay distinct."""
from calendar import monthrange
import json
from pathlib import Path
from urllib.parse import urlencode

from ashare_data.providers.free_update_dates import reviewed_date
from ashare_data.providers.free_update_reference import stock_market
from ashare_data.providers.free_updates import digest

FUNCTION = "stock_financial_analysis_indicator_em"
CONTRACT = json.loads(Path(__file__).with_name("financial-indicators-contract.json").read_text())
UNITS = {k: v["unit"] for k, v in CONTRACT["fields"].items()}
COMMON = {"EPSJB", "BPS", "MGJYXJJE", "TOTALOPERATEREVE", "PARENTNETPROFIT", "ROEJQ", "ZCFZL"}
SECTORS = {"通用": {"XSMLL", "LD", "SD"}, "银行": {"TOTALDEPOSITS", "GROSSLOANS", "NEWCAPITALADER", "NONPERLOAN"},
           "保险": {"EARNED_PREMIUM", "COMPENSATE_EXPENSE", "SOLVENCY_AR"}, "证券": {"JZB", "JZC", "JZBJZC"}}
PINGAN_NEW_STANDARD = "https://www.pingan.com/app_upload/images/info/upload/55e017c8-da54-42f5-81cc-b4ff9586b461.pdf"
METADATA = {"SECUCODE", "SECURITY_CODE", "SECURITY_NAME_ABBR", "SECURITY_TYPE_CODE", "ORG_CODE", "ORG_TYPE", "REPORT_DATE",
            "REPORT_TYPE", "REPORT_YEAR", "REPORT_DATE_NAME", "NOTICE_DATE", "UPDATE_DATE", "CURRENCY", "IS_BZ"}


def requested_identity(params):
    symbol = params.get("symbol", "")
    if set(params) != {"symbol", "indicator"} or params["indicator"] != "按报告期" or symbol != symbol[:6]+"."+stock_market(symbol[:6]):
        raise ValueError("financial indicators require explicit A-share identity and reporting-period basis")
    return symbol


def collect(capture, params):
    symbol = requested_identity(params)
    query = {"type": CONTRACT["report"], "sty": "APP_F10_MAINFINADATA", "quoteColumns": "", "filter": f'(SECUCODE="{symbol}")',
        "p": "1", "ps": "100", "sr": "-1", "st": "REPORT_DATE", "source": "HSF10", "client": "PC"}
    rows, hashes = [], set()
    total = None
    for page in range(1, 41):
        query["p"] = str(page)
        response = json.loads(capture.get("https://datacenter.eastmoney.com/securities/api/data/get?"+urlencode(query)))
        block = response.get("result")
        if response.get("success") is False or not isinstance(block, dict) or not isinstance(block.get("data"), list):
            raise ValueError("financial source rejected request or changed schema")
        declared = int(block["pages"]), int(block["count"])
        if total is None:
            total = declared
            if not 1 <= total[0] <= 40 or not 1 <= total[1] <= 4000:
                raise ValueError("financial source history empty or exceeds bounded pages")
        data = block["data"]
        if declared != total or not data or digest(data) in hashes:
            raise ValueError("financial pages/count changed, repeated or empty")
        hashes.add(digest(data))
        rows.extend(data)
        if page == total[0]:
            break
    if len(rows) != total[1] or len({(r.get("SECUCODE"), r.get("REPORT_DATE"), r.get("REPORT_TYPE")) for r in rows}) != len(rows):
        raise ValueError("financial source count/unique report keys disagree")
    capture.dataset_metadata = {"source_report": CONTRACT["report"], "security": symbol, "source_count": total[1], "pages_received": total[0],
        "complete_source_issuer_history": True, "complete_universe": False, "units": UNITS, "unit_evidence": CONTRACT["source"],
        "unit_evidence_sha256": CONTRACT["source_sha256"], "scope": CONTRACT["scope"],
        "native_extra_fields_retained_without_unit_claim": sorted(set().union(*(set(r) for r in rows))-UNITS.keys()-METADATA),
        "limitation": "all source-visible report periods for the explicit issuer; not all A-shares, original publication vintages or complete quarter-only history"}
    return rows


def validate(rows, job, as_of, numeric):
    if job["function"] != FUNCTION:
        return None
    symbol = requested_identity(job["parameters"])
    keys, periods, notices, organizations = [], [], [], set()
    missing, latest_values = dict.fromkeys(UNITS, 0), dict.fromkeys(UNITS)
    missing_notice = 0
    ratio_checks, ratio_disagreements = 0, []
    for row in rows:
        if row.get("SECUCODE") != symbol or row.get("SECURITY_CODE") != symbol[:6] or row.get("CURRENCY") != "CNY":
            raise ValueError("financial indicator issuer or currency mismatch")
        period, notice = reviewed_date(row.get("REPORT_DATE")), reviewed_date(row.get("NOTICE_DATE"))
        if not period or period > as_of or int(period[5:7]) not in {3, 6, 9, 12} or int(period[8:]) != monthrange(int(period[:4]), int(period[5:7]))[1]:
            raise ValueError("financial indicator reporting period invalid/future")
        if str(row.get("REPORT_YEAR")) != period[:4] or row.get("REPORT_TYPE") != {3: "一季报", 6: "中报", 9: "三季报", 12: "年报"}[int(period[5:7])]:
            raise ValueError("financial report year/type inconsistent")
        if row.get("NOTICE_DATE") not in (None, "") and not notice or notice and not period <= notice <= as_of:
            raise ValueError("financial announcement must follow report period and precede observation")
        if notice:
            notices.append(notice)
        else:
            missing_notice += 1
        organizations.add(row.get("ORG_TYPE"))
        for field in UNITS:
            if field not in row:
                raise ValueError("financial main-panel field absent: "+field)
            value = numeric(row[field])
            if value is None:
                missing[field] += 1
            else:
                latest_values[field] = max(latest_values[field] or period, period)
        capital, assets, percentage = [numeric(row.get(k)) for k in ("JZB", "JZC", "JZBJZC")]
        if all(v is not None for v in (capital, assets, percentage)) and assets != 0:
            ratio_checks += 1
            implied = capital/assets*100
            if abs(implied-percentage) > numeric("0.011"):
                ratio_disagreements.append({"report_period": period, "source_percent": str(percentage), "component_implied_percent": str(implied)})
        periods.append(period)
        keys.append((symbol, period, row["REPORT_TYPE"]))
    if not rows or len(keys) != len(set(keys)):
        raise ValueError("financial reports empty or duplicate")
    latest = max(periods)
    last_rows = [r for r, p in zip(rows, periods) if p == latest]
    gaps = ["面板字段原生尺度仍未核实："+field for field in ("ZYGPGMJZC", "ZYGDSYLZQJZB") if missing[field] != len(rows)]
    if ratio_disagreements:
        gaps.append("净资本/净资产百分数与组件不完全对账；保留原值和差异，未重算覆盖")
    applicability, label_conflicts = [], []
    if symbol == "600030.SH":
        label_conflicts.append({"field": "ZYGDSYLZQJZB", "matched_report_period": "2026-09-30",
            "vendor_label": CONTRACT["fields"]["ZYGDSYLZQJZB"]["label"],
            "official_label": "自营非权益类证券及其衍生品/净资本", "official_value_percent": "327.98",
            "source": "https://www.cs.ecitic.com/newsite/tzzgx/ggyth/gg/aggg/202610/P020261026642041978763.pdf",
            "scope": "single issuer/report corroboration only; cannot rename every historical/vendor record"})
        gaps.append("券商面板ZYGDSYLZQJZB标签与官方2026三季报分母/范围冲突，保留原值待逐期映射")
    for row in last_rows:
        sector = row.get("ORG_TYPE")
        if sector not in SECTORS:
            gaps.append("尚未审定行业专属字段："+str(sector))
        required = COMMON | SECTORS.get(sector, set())
        if symbol == "601318.SH" and sector == "保险" and latest >= "2023-03-31":
            # Explicit issuer evidence, not an inference that all insurers or all
            # missing values have the same accounting/publishing behaviour.
            required = required - {"COMPENSATE_EXPENSE", "SOLVENCY_AR"}
            applicability.append({"field": "COMPENSATE_EXPENSE", "status": "legacy_presentation_retired",
                "effective_from": "2023-01-01", "evidence": PINGAN_NEW_STANDARD,
                "meaning": "旧保险准则赔付支出不要求出现在新准则面板；不能用保险服务费用替代"})
            half_period = latest[:4]+("-06-30" if latest[5:7] in {"06", "09"} else "-12-31")
            if latest[5:7] == "03":
                half_period = str(int(latest[:4])-1)+"-12-31"
            half_rows = [r for r in rows if reviewed_date(r["REPORT_DATE"]) == half_period]
            available = bool(half_rows) and all(numeric(r.get("SOLVENCY_AR")) is not None for r in half_rows)
            applicability.append({"field": "SOLVENCY_AR", "status": "observed_issuer_half_year_panel",
                "expected_report_period": half_period, "value_available": available,
                "not_filled_into_latest_quarter": True,
                "meaning": "该发行人源面板半年/年度值独立保留；不声明季度偿付能力无披露义务，也不以旧值填季度"})
            if not available:
                gaps.append("保险集团源面板应有的半年/年度偿付能力值缺失："+half_period)
            # The vendor keeps the legacy earned-premium label while reporting
            # under the new standard. Do not silently call the new value the old
            # measure or pretend this known semantic gap has been resolved.
            gaps.append("新保险准则下EARNED_PREMIUM源旧标签与保险服务收入的逐期映射待核；不按旧已赚保费解释")
        for field in sorted(required):
            if numeric(row.get(field)) is None:
                gaps.append("最新报告的必要指标缺失："+field)
        if not reviewed_date(row.get("NOTICE_DATE")):
            gaps.append("最新报告缺少公告日；不能以报告期或接收时间补公告日")
    return {"contract_reviewed": True, "business_key": ["SECUCODE", "REPORT_DATE", "REPORT_TYPE"], "unique_keys": len(keys),
        "units": UNITS, "field_labels": {k: v["label"] for k, v in CONTRACT["fields"].items()},
        "frequency": "issuer_report_publication_and_revisions", "latest_period": latest, "latest_announcement": max(notices, default=None),
        "source_rows": len(rows), "organization_types": sorted(organizations), "missing_announcement_rows": missing_notice,
        "missing_value_counts": missing, "field_latest_nonnull_periods": latest_values,
        "uninterpreted_native_extra_fields": sorted(set().union(*(set(r) for r in rows))-UNITS.keys()-METADATA),
        "ratio_scale_checks": ratio_checks, "ratio_scale_disagreements": ratio_disagreements,
        "field_applicability": applicability,
        "source_label_conflicts": label_conflicts,
        "gaps": sorted(set(gaps)), "limitations": ["单位覆盖来源按报告期主要指标面板；源额外字段原样保留，未核单位的不作为已解释指标",
            "银行、保险、券商和一般企业分别校验最新必要字段；不要求银行具有制造业库存周转指标",
            "报告期、公告日与实际接收时间分开。日期级公告无法恢复日内首次可见时刻；历史无公告日保持空",
            "每次按来源全部可见报告期复查修订，按内容去重；未完成全市场发行人目录或历史PIT版本"]}
