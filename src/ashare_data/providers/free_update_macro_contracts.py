"""Reviewed raw Eastmoney statistical periods and units, not publication dates.

The pinned SDK and provider reports define these fields. Eastmoney's hgjck.js
displays customs money as 亿美元 only after zoom=-5; raw amounts are 千美元.
Eastmoney's own hjwh.js explicitly labels
both FOREX and GOLD_RESERVES as 亿美元, unlike Sina's gold volume in 万盎司. Historical receipts
are never relabeled; revisions retain their actual local reception timestamps.
"""
from datetime import date
from decimal import Decimal
import re

from ashare_data.providers.free_updates import digest, source_date

INDICATORS = {
    "ppi_yoy": {"dataset": "macro_china_ppi", "field": "BASE_SAME", "unit": "percent", "label": "工业生产者出厂价格同比"},
    "industrial_yoy": {"dataset": "macro_china_gyzjz", "field": "BASE_SAME", "unit": "percent", "label": "规模以上工业增加值当月同比"},
    "non_manufacturing_pmi": {"dataset": "macro_china_pmi", "field": "NMAKE_INDEX", "unit": "index_points", "label": "官方非制造业商务活动指数"},
    "manufacturing_pmi": {"dataset": "macro_china_pmi", "field": "MAKE_INDEX", "unit": "index_points", "label": "官方制造业采购经理指数"},
    "m2_yoy": {"dataset": "macro_china_money_supply", "field": "BASIC_CURRENCY_SAME", "unit": "percent", "label": "广义货币M2同比"},
}


def fields(names, unit):
    return dict.fromkeys(names.split(), unit)


CONTRACTS = {
    "macro_china_hgjck": {**fields("EXIT_BASE IMPORT_BASE", "1000 USD, current month"),
        **fields("EXIT_ACCUMULATE IMPORT_ACCUMULATE", "1000 USD, year-to-date"),
        **fields("EXIT_BASE_SAME IMPORT_BASE_SAME EXIT_BASE_SEQUENTIAL IMPORT_BASE_SEQUENTIAL EXIT_ACCUMULATE_SAME IMPORT_ACCUMULATE_SAME", "percent"),
        "TRADE_BALANCE_100M_USD": "100000000 USD, current month; calculated exports minus imports"},
    "macro_china_fx_gold": {**fields("GOLD_RESERVES FOREX", "100000000 USD"),
        **fields("GOLD_RESERVES_SAME GOLD_RESERVES_SEQUENTIAL FOREX_SAME FOREX_SEQUENTIAL", "percent")},
    "macro_china_qyspjg": {**fields("BASE FARM_BASE MINERAL_BASE ENERGY_BASE", "source_index_points"),
        **fields("BASE_SAME BASE_SEQUENTIAL FARM_BASE_SAME FARM_BASE_SEQUENTIAL MINERAL_BASE_SAME MINERAL_BASE_SEQUENTIAL ENERGY_BASE_SAME ENERGY_BASE_SEQUENTIAL", "percent")},
    "macro_china_gyzjz": fields("BASE_SAME BASE_ACCUMULATE", "percent"),
    "macro_china_consumer_goods_retail": {**fields("RETAIL_TOTAL RETAIL_TOTAL_ACCUMULATE", "100000000 CNY"),
        **fields("RETAIL_TOTAL_SAME RETAIL_TOTAL_SEQUENTIAL RETAIL_ACCUMULATE_SAME", "percent")},
    "macro_china_cpi": {**fields("NATIONAL_BASE NATIONAL_ACCUMULATE CITY_BASE CITY_ACCUMULATE RURAL_BASE RURAL_ACCUMULATE", "index: corresponding previous-year period=100"),
        **fields("NATIONAL_SAME NATIONAL_SEQUENTIAL CITY_SAME CITY_SEQUENTIAL RURAL_SAME RURAL_SEQUENTIAL", "percent")},
    "macro_china_czsr": {**fields("BASE BASE_ACCUMULATE", "100000000 CNY"),
        **fields("BASE_SAME BASE_SEQUENTIAL ACCUMULATE_SAME", "percent")},
    "macro_china_enterprise_boom_index": {**fields("BOOM_INDEX FAITH_INDEX", "index_points"),
        **fields("BOOM_INDEX_SAME BOOM_INDEX_SEQUENTIAL FAITH_INDEX_SAME FAITH_INDEX_SEQUENTIAL", "source_native_change; definition unverified")},
    "macro_china_gdp": {**fields("DOMESTICL_PRODUCT_BASE FIRST_PRODUCT_BASE SECOND_PRODUCT_BASE THIRD_PRODUCT_BASE", "100000000 CNY, nominal year-to-date"),
        **fields("SUM_SAME FIRST_SAME SECOND_SAME THIRD_SAME", "percent, real year-to-date yoy")},
    "macro_china_gdzctz": {**fields("BASE BASE_ACCUMULATE", "100000000 CNY"), **fields("BASE_SAME BASE_SEQUENTIAL", "percent")},
    "macro_china_money_supply": {**fields("BASIC_CURRENCY CURRENCY FREE_CASH", "100000000 CNY"),
        **fields("BASIC_CURRENCY_SAME BASIC_CURRENCY_SEQUENTIAL CURRENCY_SAME CURRENCY_SEQUENTIAL FREE_CASH_SAME FREE_CASH_SEQUENTIAL", "percent")},
    "macro_china_national_tax_receipts": {"TAX_INCOME": "100000000 CNY, year-to-date", **fields("TAX_INCOME_SAME TAX_INCOME_SEQUENTIAL", "percent")},
    "macro_china_new_financial_credit": {**fields("RMB_LOAN RMB_LOAN_ACCUMULATE", "100000000 CNY"),
        **fields("RMB_LOAN_SAME RMB_LOAN_SEQUENTIAL LOAN_ACCUMULATE_SAME", "percent")},
    "macro_china_pmi": {**fields("MAKE_INDEX NMAKE_INDEX", "index_points"), **fields("MAKE_SAME NMAKE_SAME", "percent")},
    "macro_china_ppi": {**fields("BASE BASE_ACCUMULATE", "index: corresponding previous-year period=100"), "BASE_SAME": "percent"},
    "macro_china_wbck": {**fields("BASE BASE_ACCUMULATE", "100000000 CNY"), **fields("BASE_SAME BASE_SEQUENTIAL", "percent")},
    "macro_china_whxd": {**fields("BASE BASE_ACCUMULATE", "100000000 USD"), **fields("BASE_SAME BASE_SEQUENTIAL", "percent")},
    "macro_china_xfzxx": {**fields("CONSUMERS_FAITH_INDEX CONSUMERS_ASTIS_INDEX CONSUMERS_EXPECT_INDEX", "index_points"),
        **fields("FAITH_INDEX_SAME FAITH_INDEX_SEQUENTIAL ASTIS_INDEX_SAME ASTIS_INDEX_SEQUENTIAL EXPECT_INDEX_SAME EXPECT_INDEX_SEQUENTIAL", "percent")},
}


def require_customs_units(function, units):
    """Retire stale derived labels without rewriting immutable old evidence."""
    if function != "macro_china_hgjck":
        return
    amounts = ("EXIT_BASE", "IMPORT_BASE", "EXIT_ACCUMULATE", "IMPORT_ACCUMULATE")
    if any(units.get(key) != CONTRACTS[function][key] for key in amounts):
        raise ValueError("retired customs amount-unit contract: raw amounts are 1000 USD; reparse original receipts with the current adapter")


def customs_actuals(rows):
    """Keep native thousand-dollar fields; add an explicitly calculated balance.

    Provider hgjck.js calls fieldfmt(v.EXIT_BASE, {omit:false, zoom:-5})
    under its 亿美元 heading. It applies the same factor to all four amount
    fields. Do not reuse that display heading as the raw API field's unit.
    """
    output = []
    for row in rows:
        values = [None if row[k] in (None, '', '--', '-') else Decimal(str(row[k]))
                  for k in ('EXIT_BASE', 'IMPORT_BASE')]
        if any(v is not None and (not v.is_finite() or v < 0) for v in values):
            raise ValueError('customs monthly amount invalid')
        balance = None if any(v is None for v in values) else str((values[0]-values[1])/Decimal('100000'))
        output.append(row | {'TRADE_BALANCE_100M_USD': balance,
            'trade_balance_formula': '(EXIT_BASE - IMPORT_BASE) / 100000',
            'customs_raw_amount_unit': '1000 USD'})
    return output


def repair_customs_snapshot(source, destination):
    """Derive a corrected immutable copy; never relabel or overwrite old evidence.

    The four stored amounts already contain the provider's native thousand-dollar
    numbers. Only their retired semantic labels and the explicit derived balance
    need correction. Original receipt/run times remain the observation times.
    """
    import hashlib
    from pathlib import Path

    from ashare_data.providers.free_update_manual import read_snapshot, write_snapshot
    from ashare_data.providers.free_update_semantics import validate_semantics
    from ashare_data.providers.free_updates import implementation_identity

    source, destination = Path(source).resolve(), Path(destination).resolve()
    manifest, rows = read_snapshot(source)
    if manifest["function"] != "macro_china_hgjck" or manifest["excel_row"] not in {147, 148, 149, 163}:
        raise ValueError("only reviewed customs native snapshots can be repaired")
    if any((destination / name).exists() for name in ("CURRENT", "CURRENT.json", "releases")):
        raise ValueError("production root is forbidden")
    old_units = manifest["semantics"].get("units", {})
    amounts = ("EXIT_BASE", "IMPORT_BASE", "EXIT_ACCUMULATE", "IMPORT_ACCUMULATE")
    if any(old_units.get(k) != CONTRACTS[manifest["function"]][k].replace("1000 USD", "100000000 USD")
           for k in amounts):
        raise ValueError("snapshot does not have the exact retired customs unit contract")
    corrected = customs_actuals(rows)
    job = {"function": manifest["function"], "parameters": {},
           "repair_kind": "native-customs-amount-units-v1", "supersedes_snapshot": manifest["snapshot_id"],
           "implementation_sha256": implementation_identity()}
    semantics = validate_semantics(corrected, job, manifest["observed_at"][:10])
    replacement = {"supersedes_snapshot": manifest["snapshot_id"],
        "source_manifest_sha256": hashlib.sha256((source / "manifest.json").read_bytes()).hexdigest(),
        "source_records_sha256": manifest["files"]["records.json"],
        "kind": job["repair_kind"], "native_values_unchanged": True,
        "added_fields": ["TRADE_BALANCE_100M_USD", "trade_balance_formula", "customs_raw_amount_unit"],
        "new_source_request": False}
    check = {k: manifest[k] for k in ("run_id", "function", "observed_at", "coverage", "source_urls")}
    check.update(job_id=digest(job), records=len(corrected), semantics=semantics,
                 execution_kind="derived_unit_correction", correction=replacement,
                 snapshot_observation_date=manifest.get("snapshot_observation_date"))
    destination.mkdir(parents=True, exist_ok=True)
    return write_snapshot(destination, manifest["excel_row"], job, corrected, check)


def validate(rows, job, as_of, numeric):
    name, params = job["function"], job["parameters"]
    if name not in CONTRACTS and name not in {"macro_indicator_em", "cpi_rate_em"}:
        return None
    derived = name not in CONTRACTS
    expected = None
    if derived:
        expected = INDICATORS[params["indicator"]] if name == "macro_indicator_em" else {
            "field": {"mom": "NATIONAL_SEQUENTIAL", "yoy": "NATIONAL_SAME"}[params["indicator"]], "unit": "percent"}
        units, key, day_field = {"value": expected["unit"]}, ["statistical_month", "indicator"], "日期"
    else:
        units, key, day_field = CONTRACTS[name], ["REPORT_DATE"], "REPORT_DATE"
    dates, keys = [], []
    missing = dict.fromkeys(units, 0)
    last_value = dict.fromkeys(units, None)
    for row in rows:
        day = source_date(row.get(day_field))
        if not day or day > as_of or day[8:] != "01":
            raise ValueError("invalid macro statistical period")
        dates.append(day)
        if derived:
            if (row.get("statistical_month") != day[:7] or row.get("indicator") != params["indicator"]
                    or row.get("source_field") != expected["field"] or row.get("publication_date") is not None):
                raise ValueError("macro indicator identity/date mapping mismatch")
        else:
            label = row.get("TIME", "")
            month = source_date(label)
            quarter = re.fullmatch(r"(\d{4})年第(?:1-)?([1-4])季度", label)
            if month != day and not (quarter and int(quarter[1]) == int(day[:4]) and int(quarter[2])*3 == int(day[5:7])):
                raise ValueError("macro TIME label contradicts REPORT_DATE")
        if name == 'macro_china_hgjck' and row != customs_actuals([row])[0]:
            raise ValueError('customs balance formula or native unit differs')
        for field in units:
            if field not in row:
                raise ValueError("required macro measure missing: " + field)
            value = numeric(row[field])
            if value is None:
                missing[field] += 1
            else:
                last_value[field] = max(last_value[field] or day, day)
                if units[field].startswith("index") and value < 0:
                    raise ValueError("negative macro index level")
        if name == "macro_china_gdp":
            total = numeric(row["DOMESTICL_PRODUCT_BASE"])
            parts = [numeric(row[k]) for k in ("FIRST_PRODUCT_BASE", "SECOND_PRODUCT_BASE", "THIRD_PRODUCT_BASE")]
            if total is not None and all(p is not None for p in parts) and abs(total-sum(parts)) > max(Decimal(1), abs(total)*Decimal("0.00001")):
                raise ValueError("GDP sector totals inconsistent")
        if name == "macro_china_pmi" and any(numeric(row[k]) is not None and not 0 <= numeric(row[k]) <= 100 for k in ("MAKE_INDEX", "NMAKE_INDEX")):
            raise ValueError("PMI index outside 0..100")
        if any(row.get(k) in (None, "") for k in key):
            raise ValueError("macro business key absent")
        keys.append(digest([row[k] for k in key]))
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate macro statistical-period keys")
    latest = max(dates)
    gaps = [f"{field} 最新非空期 {period}，落后于表最新期 {latest}；未填充" for field, period in last_value.items() if period != latest]
    availability_notes = []
    if name == "macro_china_enterprise_boom_index":
        availability_notes, gaps = gaps, []
        required = ['BOOM_INDEX']
        gaps = [k+'没有任何有效值' for k in required if last_value[k] is None]
        if name == "macro_china_enterprise_boom_index":
            availability_notes.append("源同比字段反复等于指数减100，未证实跨年同比定义；按源字段保留，不作同比计算")
    limitations = ["统计期和源修订值按实际取得时点入库，发布日期未提供；不能还原历史首次发布版本",
                  "增量为周期性重新读取源可得序列并按内容去重，保留修订版本，不制造每日新值"]
    if name == "macro_china_gyzjz" or (name == "macro_indicator_em" and params["indicator"] == "industrial_yoy"):
        limitations.append("工业增加值1—2月合并发布；缺少当月同比时保持 null，不拿累计增长补单月同比")
    if name == "macro_china_gdp":
        limitations.append("本表同比为年初至当季累计口径；不能替代单季GDP同比")
    if name == "macro_china_fx_gold":
        limitations.append("黄金储备与国家外汇储备均为亿美元金额；不能与新浪万盎司黄金数量序列互换")
    if name == 'macro_china_hgjck':
        limitations.extend(['接口原始出口、进口金额为千美元；网页乘10^-5后显示亿美元，原值不改写',
            'TRADE_BALANCE_100M_USD为同月出口减进口再除100000的显式计算值；不用累计值代替当月，不提供预测或原始公告时刻'])
    if derived:
        limitations.append("原候选报告中的预测值/公告日不由此替代源提供；仅同口径实际指标")
    return {"contract_reviewed": True, "business_key": key, "unique_keys": len(keys),
            "units": units, "frequency": "statistical_release", "latest_period": latest,
            "latest_announcement": None, "age_days": (date.fromisoformat(as_of)-date.fromisoformat(latest)).days,
            "missing_value_counts": missing, "field_latest_nonnull_periods": last_value,
            "field_availability_notes": availability_notes,
            "gaps": gaps, "limitations": limitations, "source_rows": len(rows)}
