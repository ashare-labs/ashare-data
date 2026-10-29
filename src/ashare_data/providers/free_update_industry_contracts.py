"""Exact public industry indicator identities, native index levels and percent changes.

The provider's hyzslist.js defines rate fields as percentages. Indicator names
and identifiers are returned by RPT_INDUSTRY_INDEX, also shown on its public
hyzs_list_<ID>.html pages. Phone shipment units are cross-checked against CAICT's
monthly report, not handset sales or cumulative shipments. No unverified unit
is assigned to insurance premiums. Index base changes are not silently rebased.
"""
from datetime import date

from ashare_data.providers.free_updates import source_date


INDUSTRY = {
    "macro_china_mobile_number": ("EMI00225823", "手机出货量:当月值", "10000 handsets; domestic market monthly shipments", "monthly"),
    "macro_china_bank_financing": ("EMI01516267", "银行理财产品发行数量:当月值", "products", "monthly"),
    "macro_china_lpi_index": ("EMI00352262", "物流景气指数", "index_points", "monthly"),
    "macro_china_construction_index": ("EMI00662541", "建材指数", "source_index_points", "source_business_day"),
    "macro_china_bdti_index": ("EMI00107668", "原油运输指数(BDTI)", "source_index_points", "source_business_day"),
    "macro_china_bsi_index": ("EMI00107667", "超灵便型船运价指数(BSI)", "source_index_points", "source_business_day"),
    "macro_china_energy_index": ("EMI00662539", "能源指数", "source_index_points", "source_business_day"),
    "macro_china_agricultural_index": ("EMI00662543", "农副指数", "source_index_points", "source_business_day"),
    "macro_china_agricultural_product": ("EMI00009274", "农产品批发价格总指数", "source_index_points", "source_business_day"),
    "macro_china_commodity_price_index": ("EMI00662535", "大宗商品价格指数", "source_index_points", "source_business_day"),
    "macro_china_vegetable_basket": ("EMI00009275", "菜篮子产品批发价格指数", "source_index_points", "source_business_day"),
}
RATE_FIELDS = "CHANGE_RATE CHANGERATE_3M CHANGERATE_6M CHANGERATE_1Y CHANGERATE_2Y CHANGERATE_3Y".split()


def validate(rows, job, as_of, numeric):
    if job["function"] not in INDUSTRY:
        return None
    identity, label, unit, frequency = INDUSTRY[job["function"]]
    units = {"INDICATOR_VALUE":unit, **dict.fromkeys(RATE_FIELDS,"percent")}
    dates=[]
    latest_nonnull=dict.fromkeys(units)
    missing=dict.fromkeys(units,0)
    for row in rows:
        if row.get("INDICATOR_ID") != identity or row.get("INDICATOR_NAME") != label:
            raise ValueError("industry indicator identity/name mismatch")
        day=source_date(row.get("REPORT_DATE"))
        if not day or day > as_of or (frequency == "monthly" and not day.endswith("-01")):
            raise ValueError("industry statistical date invalid")
        dates.append(day)
        for field in units:
            if field not in row:
                raise ValueError("industry measure absent: "+field)
            value=numeric(row[field])
            if value is None:
                missing[field]+=1
            else:
                latest_nonnull[field]=max(latest_nonnull[field] or day,day)
                if field == "INDICATOR_VALUE":
                    if value < 0 or (unit == "products" and value != value.to_integral_value()):
                        raise ValueError("industry level/count unit invalid")
                    if identity == "EMI00352262" and value > 100:
                        raise ValueError("logistics diffusion index outside 0..100")
                elif value < -100:
                    raise ValueError("industry percentage change below -100")
    if not dates or len(set(dates)) != len(dates):
        raise ValueError("industry duplicate or empty statistical-period keys")
    latest=max(dates)
    extra = {}
    if job["function"] == "macro_china_mobile_number":
        months = sorted({int(d[:4])*12+int(d[5:7])-1 for d in dates})
        missing_months = [f"{m//12:04}-{m%12+1:02}" for m in range(months[0], months[-1]+1) if m not in months]
        extra = {"unavailable_statistical_months": missing_months,
            "rate_definitions": {"CHANGE_RATE": "native change versus previous available observation; not guaranteed adjacent-month MoM",
                **{k: "native percent; named lookback retained, not reconstructed" for k in RATE_FIELDS if k != "CHANGE_RATE"}},
            "unit_evidence": "https://www.caict.ac.cn/kxyj/qwfb/qwsj/202607/P020260729496941180492.pdf"}
    return {"contract_reviewed":True,"business_key":["INDICATOR_ID","REPORT_DATE"], **extra,
        "unique_keys":len(dates),"units":units,"frequency":frequency,
        "indicator_id":identity,"indicator_name":label,"latest_period":latest,
        "latest_announcement":None,"age_days":(date.fromisoformat(as_of)-date.fromisoformat(latest)).days,
        "missing_value_counts":missing,"field_latest_nonnull_periods":latest_nonnull,
        "gaps":[f"{k} 最新非空期 {v}，落后于表最新期 {latest}；未填充" for k,v in latest_nonnull.items() if v != latest],
        "limitations":["原生单位按逐指标契约；涨跌幅为源百分数，近3/6月及1/2/3年列分别保留，不改为日同比",
            "统计期不是公告日；不重建首次发布版本。指数换基和编制方法变化保留源值，未重新定基",
            "源重复板块关联仅在全部投影字段完全一致时去重；冲突期拒绝入库"] + (["手机按国内市场当月出货量、单位万部；非零售销量，不以累计数替代。缺月保留，CHANGE_RATE不保证相邻月份环比"] if extra else []),
        "source_rows":len(rows)}
