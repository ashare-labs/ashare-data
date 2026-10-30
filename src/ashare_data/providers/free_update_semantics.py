"""Reviewed business keys, units and publication semantics for menu observations.

Unsupported contracts remain pending. A nonempty response never supplies a
missing unit, security identity or announcement timestamp by inference.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re

from ashare_data.providers.free_updates import digest, source_date


FINANCIAL = {
    "stock_balance_sheet_by_report_em": ("TOTAL_ASSETS", "TOTAL_LIABILITIES", "TOTAL_EQUITY", "TOTAL_LIAB_EQUITY"),
    "stock_profit_sheet_by_report_em": ("OPERATE_INCOME", "NETPROFIT", "BASIC_EPS"),
    "stock_cash_flow_sheet_by_report_em": ("NETPROFIT", "END_CCE"),
}


def numeric(value):
    if value in (None, "", "N/A", "-", "--"):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid source numeric value") from exc
    if not result.is_finite():
        raise ValueError("nonfinite source numeric value")
    return result


def validate_semantics(rows, job, as_of):
    from ashare_data.providers.free_update_board_members import validate as board_members
    checked = board_members(rows, job, as_of, numeric)
    if checked is not None:
        return checked
    from ashare_data.providers.free_update_ths_bars import validate as ths_bars
    checked = ths_bars(rows, job, as_of, numeric)
    if checked is not None:
        return checked
    from ashare_data.providers.free_update_board_bars import validate as board_bars
    checked = board_bars(rows, job, as_of, numeric)
    if checked is not None:
        return checked
    from ashare_data.providers.free_update_policy_releases import validate as policy_releases
    checked = policy_releases(rows, job, as_of, numeric)
    if checked is not None:
        return checked
    from ashare_data.providers.free_update_tdx import validate as native_tdx
    checked = native_tdx(rows, job, as_of, numeric)
    if checked is not None:
        return checked
    from ashare_data.providers.free_update_legacy_reference import validate as legacy_reference
    checked = legacy_reference(rows, job, as_of, numeric)
    if checked is not None:
        return checked
    from ashare_data.providers.free_update_sina_minutes import validate as sina_minutes
    checked = sina_minutes(rows, job, as_of, numeric)
    if checked is not None:
        return checked
    from ashare_data.providers.free_update_limit_pools import validate as limit_pools
    from ashare_data.providers.free_update_business_segments import validate as business_segments
    from ashare_data.providers.free_update_market_reports import validate as market_reports
    from ashare_data.providers.free_update_futures_minutes import validate as futures_minutes
    from ashare_data.providers.free_update_province_prices import validate as province_prices
    for check in (limit_pools, business_segments, market_reports, futures_minutes, province_prices):
        result = check(rows, job, as_of, numeric)
        if result is not None:
            return result
    from ashare_data.providers.free_update_regional_prices import validate as regional
    checked = regional(rows, job, as_of, numeric)
    if checked is not None:
        return checked
    from ashare_data.providers.free_update_equities import validate as equities
    checked = equities(rows, job, as_of, numeric)
    if checked is not None:
        return checked
    from ashare_data.providers.free_update_financial_indicators import validate as financial_indicators
    checked = financial_indicators(rows, job, as_of, numeric)
    if checked is not None:
        return checked
    from ashare_data.providers.free_update_futures import validate as futures
    checked = futures(rows, job, as_of, numeric)
    if checked is not None:
        return checked
    from ashare_data.providers.free_update_reference import validate as reference
    checked = reference(rows, job, as_of, numeric)
    if checked is not None:
        return checked
    from ashare_data.providers.free_update_research import validate as research
    from ashare_data.providers.free_update_rates import validate as rates
    from ashare_data.providers.free_update_oil import validate as oil
    for validator in (research, rates, oil):
        checked = validator(rows, job, as_of, numeric)
        if checked is not None:
            return checked
    from ashare_data.providers.free_update_disclosures import validate as disclosures
    checked = disclosures(rows, job, as_of)
    if checked is not None:
        return checked
    from ashare_data.providers.free_update_valuations import validate as valuations
    checked = valuations(rows, job, as_of, numeric)
    if checked is not None:
        return checked
    from ashare_data.providers.free_update_period_tables import validate as tables
    checked = tables(rows, job, as_of, numeric)
    if checked is not None:
        return checked
    from ashare_data.providers.free_update_broadcast import validate as broadcast
    checked = broadcast(rows, job, as_of)
    if checked is not None:
        return checked
    from ashare_data.providers.free_update_shareholders import validate as holders
    checked = holders(rows, job, as_of)
    if checked is not None:
        return checked
    from ashare_data.providers.free_update_news import validate as news
    checked=news(rows,job,as_of)
    if checked is not None:
        return checked
    if job["function"] == "sina_macro_page" and job["parameters"]["dataset"] == "macro_china_passenger_load_factor":
        dates=[]
        for row in rows:
            day=source_date(row.get("统计时间"))
            if not day or day>as_of or row.get("source_units")!={"客座率":"%","载运率":"%"}:
                raise ValueError("air-transport statistical period/unit mismatch")
            if any(numeric(row[k]) is not None and not 0<=numeric(row[k])<=100 for k in ("客座率","载运率")):
                raise ValueError("air-transport percentage outside 0..100")
            dates.append(day)
        if len(set(dates))!=len(dates):
            raise ValueError("duplicate air-transport statistical periods")
        latest=max(rows,key=lambda r:source_date(r["统计时间"]))
        return {"contract_reviewed":True,"business_key":["统计时间"],"unique_keys":len(dates),
            "units":{"客座率":"percent","载运率":"percent"},"frequency":"monthly_statistical_release",
            "latest_period":max(dates),"latest_announcement":None,
            "gaps":[k+"最新期缺失" for k in ("客座率","载运率") if numeric(latest[k]) is None],
            "limitations":["统计月不是发布时间；缺值原样保留，未恢复首次发布版本"],"source_rows":len(rows)}
    from ashare_data.providers.free_update_security_events import validate as events
    from ashare_data.providers.free_update_billboard import validate as billboard
    from ashare_data.providers.free_update_spot_contracts import validate as spot
    from ashare_data.providers.free_update_commodity_reports import validate as commodities
    from ashare_data.providers.free_update_industry_flow import validate as industry_flow
    for validator in (events, spot, billboard, commodities, industry_flow):
        result = validator(rows, job, as_of, numeric, source_date)
        if result is not None:
            return result
    from ashare_data.providers.free_update_macro_contracts import validate
    macro = validate(rows, job, as_of, numeric)
    if macro is not None:
        return macro
    from ashare_data.providers.free_update_industry_contracts import validate as industry
    result = industry(rows, job, as_of, numeric)
    if result is not None:
        return result
    name, params = job["function"], job["parameters"]
    units, gaps, key_fields, dates, announcements, limitations = {}, [], [], [], [], []
    frequency = "unreviewed"
    field_coverage = {}
    missing_announcements = 0
    if name == "stock_zh_a_daily" and params.get("adjust") in {"qfq-factor", "hfq-factor"}:
        kind = params["adjust"].split("-")[0]
        field = kind+"_factor"
        key_fields = ["security", "date"]
        units = {field:"dimensionless divisor" if kind == "qfq" else "dimensionless multiplier"}
        frequency = "corporate_action_and_source_factor_revisions"
        expected_formula = "raw_price / factor" if kind == "qfq" else "raw_price * factor"
        for row in rows:
            day = source_date(row.get("date"))
            if row.get("security") != params["symbol"] or not re.fullmatch(r"(?:sh|sz)\d{6}", row["security"]):
                raise ValueError("factor security identity mismatch")
            if not day or day > as_of or row.get("baseline_sentinel") != (day == "1900-01-01"):
                raise ValueError("factor date or baseline sentinel invalid")
            if row.get("price_formula") != expected_formula or numeric(row.get(field)) is None or numeric(row[field]) <= 0:
                raise ValueError("factor direction/value invalid")
            if not row["baseline_sentinel"]:
                dates.append(day)
        limitations.extend(["1900-01-01为源基准哨兵，不是上市日或公司行动日；前复权因子作除数，后复权作乘数",
            "新浪复权基准独立保留，尚未对齐CMES历史基准；源公告时刻未知，修订按实际接收时间留存"])
    elif name == "nbs_gdp_yoy":
        frequency = "quarterly_release_and_revisions"
        key_fields = ["quarter", "publication_at"]
        units = {"value": "percent; real GDP single-quarter year-over-year"}
        for row in rows:
            day = source_date(row.get("日期"))
            publication = datetime.fromisoformat(row["publication_at"])
            if not day or not publication.tzinfo or publication.date().isoformat() > as_of or day > publication.date().isoformat():
                raise ValueError("GDP statistical period/publication date invalid")
            if row.get("definition") != "real_GDP_single_quarter_year_over_year" or row.get("unit") != "percent" or numeric(row.get("value")) is None:
                raise ValueError("GDP definition/actual value missing")
            expected = f"{day[:4]}Q{int(day[5:7])//3}"
            if row["quarter"] != expected or int(day[5:7]) not in {3,6,9,12}:
                raise ValueError("GDP quarter identity mismatch")
            dates.append(day)
            announcements.append(publication.isoformat())
        limitations.append("官方单季度实际同比及本次发布修订版本；不补金十预测值，不推定旧季度最初公布值")
    elif name == "sge_benchmark":
        frequency = "exchange_daily_am_pm_benchmark"
        key_fields = ["品种", "交易时间"]
        kind = params["kind"]
        units = {k: "CNY/gram" if kind == "gold" else "CNY/kilogram" for k in ("早盘价", "午盘价")}
        for row in rows:
            if row.get("品种") != ("SHAU" if kind == "gold" else "SHAG"):
                raise ValueError("SGE benchmark product mismatch")
            day = source_date(row.get("交易时间"))
            if not day:
                raise ValueError("invalid SGE benchmark date")
            dates.append(day)
            values = [numeric(row.get(k)) for k in units]
            if all(v is None for v in values) or any(v is not None and v <= 0 for v in values):
                raise ValueError("invalid SGE benchmark price")
        limitations.append("源毫秒时间戳按 Asia/Shanghai 转交易日；早午盘按日期外连接，单边缺值保持 null；不含成交量或公告时分")
    elif name in {"currency_boc_safe", "safe_rmb_window"}:
        frequency = "CFETS_business_day_fixing"
        key_fields = ["日期"]
        direct = {"美元","欧元","日元","港元","英镑","澳元","新西兰元","新加坡元","瑞士法郎","加元"}
        indirect = {"澳门元","林吉特","卢布","兰特","韩元","迪拉姆","里亚尔","福林","兹罗提","丹麦克朗","瑞典克朗","挪威克朗","里拉","比索","泰铢"}
        units = {**dict.fromkeys(direct,"CNY per 100 foreign currency"), **dict.fromkeys(indirect,"foreign currency per 100 CNY")}
        for row in rows:
            if set(row) != direct | indirect | {"日期"}:
                raise ValueError("SAFE currency columns changed; review quotation directions")
            day = source_date(row.get("日期"))
            if not day:
                raise ValueError("invalid SAFE fixing date")
            if name == "safe_rmb_window" and not source_date(params["start_date"]) <= day <= source_date(params["end_date"]):
                raise ValueError("SAFE fixing outside requested window")
            dates.append(day)
            for column in units:
                value = numeric(row[column])
                if value is not None and value <= 0:
                    raise ValueError("nonpositive currency fixing")
            if numeric(row["美元"]) is None:
                raise ValueError("SAFE row has no USD fixing")
        limitations.append("外管局明确的直接/间接标价分别保留；单位均为每100，历史未开通币种为空，不换算交易汇率")
    elif name == "china_government_yield":
        frequency = "ChinaBond_business_day_curve"
        key_fields = ["曲线名称", "日期"]
        units = dict.fromkeys(["3月","6月","1年","3年","5年","7年","10年","30年"],"percent_per_annum")
        for row in rows:
            day = source_date(row.get("日期"))
            if row.get("曲线名称") != "中债国债收益率曲线" or not day or not source_date(params["start_date"]) <= day <= source_date(params["end_date"]):
                raise ValueError("government curve identity/date mismatch")
            dates.append(day)
            if not any(numeric(row.get(k)) is not None for k in units):
                raise ValueError("empty government yield curve")
        limitations.append("限定中债国债收益率曲线，不混入商业银行债和中短期票据；CMES历史曲线版本仍待对齐")
    elif name == "rate_interbank":
        frequency = "Shibor_business_day_fixing"
        key_fields = ["报告日"]
        if params["market"] != "上海银行同业拆借市场" or params["symbol"] != "Shibor人民币" or params["indicator"] not in {"隔夜","1周","2周","1月","3月","6月","9月","1年"}:
            raise ValueError("unreviewed interbank market or tenor")
        units = {"利率":"percent_per_annum", "涨跌":"basis_points"}
        for row in rows:
            day = source_date(row.get("报告日"))
            if not day or numeric(row.get("利率")) is None:
                raise ValueError("Shibor date/rate missing")
            dates.append(day)
            numeric(row.get("涨跌"))
        limitations.append("币种、市场与期限按显式请求及原始回执确定；八期限各自成序列，不用隔夜冒充全部期限")
    elif name in {"macro_china_market_margin_sh", "macro_china_market_margin_sz"}:
        frequency = "exchange_business_day_margin_statistics"
        key_fields = ["日期"]
        units = {**dict.fromkeys(["融资买入额","融资余额","融券余额","融资融券余额"],"CNY"), **dict.fromkeys(["融券卖出量","融券余量"],"shares")}
        for row in rows:
            day = source_date(row.get("日期"))
            if not day:
                raise ValueError("invalid margin statistics date")
            dates.append(day)
            for k in units:
                value = numeric(row.get(k))
                if value is not None and value < 0:
                    raise ValueError("negative margin balance or flow")
            balances = [numeric(row.get(k)) for k in ["融资余额","融券余额","融资融券余额"]]
            if all(v is not None for v in balances) and abs(balances[0]+balances[1]-balances[2]) > 1:
                raise ValueError("margin total differs from component balances")
        last = max(rows, key=lambda r: r["日期"])
        missing = [k for k in units if last.get(k) is None]
        if missing:
            gaps.append("最新源记录缺少字段："+"、".join(missing))
        limitations.append("来源为金十转引交易所统计；源元/股保留，未提供首个公告时刻")
    elif name in FINANCIAL:
        key_fields = ["SECUCODE", "REPORT_DATE", "REPORT_TYPE"]
        frequency = "issuer_report_publication"
        units = {k: "CNY/share" if k.endswith("EPS") else "CNY" for k in FINANCIAL[name]}
        units["*_YOY"] = "percent"
        requested = params["symbol"]
        expected = requested[2:] + "." + requested[:2].upper()
        for row in rows:
            if row.get("SECUCODE") != expected or row.get("SECURITY_CODE") != requested[2:]:
                raise ValueError("financial security identity mismatch")
            if row.get("CURRENCY") != "CNY":
                raise ValueError("financial currency requires explicit contract")
            period, notice = source_date(row.get("REPORT_DATE")), source_date(row.get("NOTICE_DATE"))
            if not period or (notice and (period > notice or notice > as_of)):
                raise ValueError("invalid financial report/announcement date")
            dates.append(period)
            if notice:
                announcements.append(notice)
            else:
                if row.get("NOTICE_DATE") not in (None, ""):
                    raise ValueError("unparseable financial announcement date")
                missing_announcements += 1
            for k in FINANCIAL[name]:
                if k not in row:
                    raise ValueError("required financial field missing: " + k)
                numeric(row[k])
            if name == "stock_balance_sheet_by_report_em":
                assets, total = numeric(row["TOTAL_ASSETS"]), numeric(row["TOTAL_LIAB_EQUITY"])
                if assets is not None and total is not None and abs(assets-total) > max(Decimal(1), abs(assets)*Decimal("0.000001")):
                    raise ValueError("balance sheet assets do not equal liabilities and equity")
        limitations.append("历史公告只有日期，日内可见时点未知；查询使用本地实际接收时点")
        if missing_announcements:
            limitations.append(f"{missing_announcements} 条历史报告缺少公告日期，保持 null；不能用于历史公告可见时点判断")
    elif name == "treasury_rates":
        frequency = "US_business_day"
        key_fields = ["date"] + (["RATE_TYPE"] if params["kind"] == "daily_treasury_long_term_rate" else [])
        units = {"yield_fields": "percent_per_annum", "date": "America/New_York source date"}
        for row in rows:
            day = source_date(row.get("date"))
            if not day or day > as_of:
                raise ValueError("invalid Treasury observation date")
            dates.append(day)
            values = [numeric(v) for k,v in row.items() if k.startswith(("BC_", "TC_", "CS_")) or k == "RATE"]
            if not any(v is not None for v in values):
                raise ValueError("Treasury row has no numeric yield")
    elif name == "ths_hot":
        key_fields = ["market", "code"]
        frequency = "current_snapshot"
        units = {"order": "rank", "rate": "source proprietary heat score, not a return", "hot_rank_chg": "rank places"}
        for row in rows:
            if not re.fullmatch(r"\d{6}", str(row.get("code", ""))) or not row.get("name"):
                raise ValueError("invalid THS security identity")
            if int(row.get("order", 0)) < 1:
                raise ValueError("invalid THS rank")
        if len({r["order"] for r in rows}) != len(rows):
            raise ValueError("duplicate THS ranks")
    elif name in {"tdx_blocks", "tdx_members"}:
        frequency = "current_snapshot"
        key_fields = ["query_type", "block_code"] if name == "tdx_blocks" else ["block_code", "source_market", "code"]
        units = {"code": "string identifier", "source_market": "TDX native market identifier"}
        for row in rows:
            if not re.fullmatch(r"\d{6}", row.get("block_code", "")):
                raise ValueError("invalid TDX board identity")
            if name == "tdx_members":
                prefix = {0:"sz", 1:"sh", 2:"bj"}.get(row.get("source_market"))
                if not prefix or row["security"] != prefix+row["code"] or row["block_code"] != params["block_code"]:
                    raise ValueError("TDX constituent security or board mismatch")
            elif not row.get("block_name"):
                raise ValueError("missing TDX board name")
    elif name == "macro_china_lpr":
        frequency = "monthly_policy_release_or_rate_change"
        key_fields = ["TRADE_DATE"]
        units = {k:"percent_per_annum" for k in ("LPR1Y", "LPR5Y", "RATE_1", "RATE_2")}
        for row in rows:
            dates.append(source_date(row.get("TRADE_DATE")))
            if not any(numeric(row.get(k)) is not None for k in units):
                raise ValueError("LPR row has no numeric rate")
        for field in units:
            values = sorted(source_date(r.get("TRADE_DATE")) for r in rows if numeric(r.get(field)) is not None)
            field_coverage[field] = {"nonnull_records": len(values), "first_period": min(values, default=None),
                "latest_period": max(values, default=None), "role": "LPR tenor" if field.startswith("LPR") else "auxiliary interest-rate series, not LPR history"}
        if any(not field_coverage[field]["nonnull_records"] for field in ("LPR1Y", "LPR5Y")):
            gaps.append("源窗口未同时包含1年及5年LPR实际值；辅助利率不算LPR覆盖")
        limitations.append("各期限按非空值单独报告首末日期；辅助利率的更早日期不代表LPR历史")
    elif name in {"sw_index_first_info","sw_index_second_info","sw_index_third_info"}:
        frequency="current_snapshot"
        key_fields=["行业代码"]
        units={"成份个数":"count","静态股息率":"percent",
               "静态市盈率":"ratio","TTM(滚动)市盈率":"ratio","市净率":"ratio"}
        for row in rows:
            if not re.fullmatch(r"\d{6}\.SI",str(row.get("行业代码",""))) or not row.get("行业名称"):
                raise ValueError("invalid SW industry identity")
            count=numeric(row.get("成份个数"))
            if count is None or count<0 or count!=int(count):
                raise ValueError("invalid SW constituent count")
            if name!="sw_index_first_info" and not row.get("上级行业"):
                raise ValueError("SW parent industry missing")
            for column in units:
                if column not in row:
                    raise ValueError("SW measure missing: "+column)
                numeric(row[column])
        limitations.append("乐咕乐股当前申万分类及估值快照，不能代表历史成分或分类版本")
    elif name in {"sw_index_first_cons", "sw_index_second_cons", "sw_index_third_cons"}:
        frequency="current_industry_membership_snapshot"
        key_fields=["股票代码"]
        units={"股票代码":"exchange-qualified security identifier", "价格":"CNY/share",
               "市值":"100000000 CNY", "市盈率":"ratio", "市盈率ttm":"ratio", "市净率":"ratio",
               "股息率":"percent", "ROE(%)":"percent"}
        classification = f"申万{dict(sw_index_first_cons=1,sw_index_second_cons=2,sw_index_third_cons=3)[name]}级"
        for row in rows:
            if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)",str(row.get("股票代码",""))) or not row.get("股票简称") or not row.get(classification):
                raise ValueError("industry member identity or classification absent")
            for field in units:
                if field != "股票代码" and field in row:
                    value=numeric(row[field])
                    if field in {"价格","市值"} and value is not None and value<0:
                        raise ValueError("negative current constituent quote or capitalization")
        limitations.append("纳入时间原值保留，未解释为申万分类纳入日期；当前成员按实际采集时点留存，不要求历史接口")
    elif name == "index_stock_cons_csindex":
        frequency="index_constituent_publication"
        key_fields=["指数代码","日期","交易所","成分券代码"]
        units={"指数代码":"string identifier","成分券代码":"string identifier"}
        for row in rows:
            if row.get("指数代码")!=params["symbol"] or not re.fullmatch(r"\d{6}",str(row.get("成分券代码",""))):
                raise ValueError("CSI index or constituent identity mismatch")
            day=source_date(row.get("日期"))
            if not day or day>as_of or not row.get("成分券名称"):
                raise ValueError("invalid CSI constituent publication row")
            dates.append(day)
        limitations.append("仅源文件当前成分及其标注日期；历史变更和权重需独立证据")
    elif name == "tool_trade_date_hist_sina":
        frequency="exchange_calendar_publication"
        key_fields=["trade_date"]
        units={"trade_date":"Asia/Shanghai trading day"}
        for row in rows:
            if not source_date(row.get("trade_date")):
                raise ValueError("invalid trading calendar date")
        limitations.append("源日历当前覆盖范围以实际首末日为限，未来节假日修订需持续跟踪")
    elif name in {"fund_etf_scale_sse", "fund_etf_scale_szse", "fund_etf_only_szse"}:
        frequency = "exchange_daily_snapshot"
        key_fields = ["基金代码"]
        units = {"基金份额":"shares", "净值":"CNY/share"}
        for row in rows:
            if name == "fund_etf_only_szse" and row.get("基金类别") != "ETF":
                raise ValueError("non-ETF fund must not be counted as ETF coverage")
            if not re.fullmatch(r"\d{6}", str(row.get("基金代码", ""))):
                raise ValueError("invalid ETF code")
            if numeric(row.get("基金份额")) is None or numeric(row["基金份额"]) < 0:
                raise ValueError("invalid ETF shares")
        dates = [source_date(r.get("统计日期")) for r in rows if r.get("统计日期")]
        if name in {"fund_etf_scale_szse", "fund_etf_only_szse"}:
            limitations.append("深交所当期份额按真实接收时点留存；统计日期未给出，保持空值，上市日期不作统计日期")
        if name == "fund_etf_scale_szse":
            gaps.append("旧适配器返回全基金表，含LOF和不动产基金；仅供旧证据审计，不能作为ETF目录")
    elif name == "stock_fund_flow_individual":
        key_fields = ["股票代码"]
        frequency = "current_snapshot_page"
        units = {"最新价": "CNY/share", "涨跌幅": "percent", "换手率": "percent",
                 **dict.fromkeys(["流入资金(元)", "流出资金(元)", "净额(元)", "成交额(元)"], "CNY")}
        for row in rows:
            if not re.fullmatch(r"\d{6}", str(row.get("股票代码", ""))) or not row.get("股票简称"):
                raise ValueError("fund-flow security identity absent")
            for column in units:
                value = numeric(row[column])
                if column in {"最新价", "流入资金(元)", "流出资金(元)", "成交额(元)"} and value is not None and value < 0:
                    raise ValueError("negative fund-flow price/gross amount")
        limitations.append("供应商资金流分类、金额按原展示精度；跨页为不同接收时点，不是全市场原子快照")
    elif name == "energy_carbon_bj":
        key_fields = ["日期", "产品"]
        frequency = "exchange_trading_day"
        units = {"成交量": "tonne", "成交均价": "CNY/tonne", "成交额": "CNY"}
        for row in rows:
            day = source_date(row.get("日期"))
            if not day or day > as_of:
                raise ValueError("invalid carbon trading date")
            dates.append(day)
            for column in units:
                if numeric(row[column]) is None or numeric(row[column]) < 0:
                    raise ValueError("invalid carbon quantity")
            if abs(numeric(row["成交量"])*numeric(row["成交均价"])-numeric(row["成交额"])) > max(Decimal(1), numeric(row["成交量"])*Decimal("0.006")):
                raise ValueError("carbon volume/rounded price/amount inconsistent")
        limitations.append("每页明示历史覆盖及下一页；产品代码和交易日分别保留，公告时点未知")
    else:
        return {"contract_reviewed":False, "gaps":["字段口径、业务主键和单位尚未逐项验收"]}
    keys=[]
    for row in rows:
        if any(row.get(k) in (None, "") for k in key_fields):
            raise ValueError("business key field absent")
        keys.append(digest([row[k] for k in key_fields]))
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate source business keys; do not silently collapse")
    dates = [d for d in dates if d]
    latest = max(dates, default=None)
    if latest and latest > as_of:
        raise ValueError("future source observation is not current data")
    return {"contract_reviewed":True, "business_key":key_fields,
            "unique_keys":len(keys), "units":units, "frequency":frequency,
            "latest_period":latest, "latest_announcement":max(announcements,default=None),
            "age_days":(date.fromisoformat(as_of)-date.fromisoformat(latest)).days if latest else None,
            "missing_announcement_rows":missing_announcements,
            "field_value_coverage":field_coverage,
            "gaps":gaps, "limitations":limitations, "source_rows":len(rows)}
