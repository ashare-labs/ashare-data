"""Native monthly tables with preserved headings, units and statistical periods.

These are current public series, not point-in-time releases. Source revisions
remain distinct stored observations; period labels never become announcement dates.
"""
from functools import lru_cache
from datetime import datetime
import hashlib
import importlib.metadata
import importlib.util
from io import StringIO
import json
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from ashare_data.providers.free_updates import catalog, digest, source_date

SINA = json.loads(Path(__file__).with_name("sina-macro-contracts.json").read_text())["datasets"]
THS = {
    "macro_rmb_deposit": ("rmb", "人民币存款余额"),
    "macro_rmb_loan": ("loan", "新增人民币贷款"),
    "macro_stock_finance": ("finance", "股票筹资"),
}
TABLES = {*SINA, *THS}
REQUIRED = {
    "macro_china_central_bank_balance": ["总资产", "总负债"],
    "macro_china_society_electricity": ["全社会用电量", "第一产业用电量", "第二产业用电量", "第三产业用电量", "城乡居民生活用电量合计"],
    "macro_china_supply_of_money": ["货币和准货币（广义货币M2）", "货币(狭义货币M1)", "流通中现金(M0)"],
    "macro_china_foreign_exchange_gold": ["黄金储备", "国家外汇储备"],
    "macro_china_postal_telecommunicational": ["函件总数", "包件", "特快专递"],
    "macro_china_freight_index": ["波罗的海综合运价指数BDI "],
    "macro_china_insurance": ["原保险保费收入"],
    "macro_china_society_traffic_volume": ["货运量同比增长"],
}
SCOPED_TABLES = {"macro_china_postal_telecommunicational", "macro_china_freight_index",
                 "macro_china_insurance", "macro_china_society_traffic_volume"}


@lru_cache(maxsize=1)
def data_parser():
    # Loading this reviewed, standard-library-only parser does not initialize the
    # SDK or execute source JSONP. The parser file itself must match the pin.
    from ashare_data.providers.free_updates.free_update_macro_pages import PARSER_SHA256
    dist = importlib.metadata.distribution("akshare")
    path = Path(dist.locate_file("akshare/utils/demjson.py"))
    if dist.version != catalog()["akshare_version"] or hashlib.sha256(path.read_bytes()).hexdigest() != PARSER_SHA256:
        raise ValueError("macro data parser differs from reviewed source")
    spec = importlib.util.spec_from_file_location("ashare_reviewed_macro_parser", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.decode


def parse_sina(payload, name):
    from ashare_data.providers.free_updates.free_update_macro_pages import parse_page
    definition = SINA[name]
    if payload["config"]["all"] != definition["columns"]:
        raise ValueError("Sina macro field/unit schema changed")
    rows, coverage = parse_page(payload, offset=0, requested=1000)
    if not rows or (name not in SCOPED_TABLES and
                    (not coverage["complete_history"] or len(rows) != coverage["source_total"])):
        raise ValueError("Sina whole-series count exceeds bounded single table")
    allowed_sections = {"累计", "非累计"} if name in {"macro_china_postal_telecommunicational", "macro_china_society_traffic_volume"} else {"source"}
    if not {r["source_section"] for r in rows} <= allowed_sections:
        raise ValueError("Sina table unexpectedly acquired cumulative/other sections")
    if name in SCOPED_TABLES:
        expected = min(1000, coverage['source_total'])
        if any(not 0 < n <= expected+128 for n in coverage['section_row_counts'].values()):
            raise ValueError('source current section window count mismatch')
        coverage['declared_count_minus_returned_by_section'] = {k:coverage['source_total']-n for k,n in coverage['section_row_counts'].items()}
        if any(n != expected for n in coverage['section_row_counts'].values()):
            coverage['complete_history'] = False
    identity = digest(definition["columns"])
    rows = [dict(r, source_schema_sha256=identity, source_units=coverage["units"]) for r in rows]
    coverage.update(source_contract=name, complete_returned_window=True,
                    scope="all currently returned statistical periods; not CMES full history",
                    source_schema_sha256=identity)
    if name in SCOPED_TABLES:
        coverage.update(unique_observations=len(rows),
                        scope='latest 1000 source periods per named section; missing fields retained separately')
    if name == 'macro_china_insurance':
        coverage.update(raw_returned_rows=len(rows), selection={'省市地区':'全国'},
                        excluded_scope='regional rows remain in raw response; duplicate province labels are not guessed as named cities')
        rows=[row for row in rows if row['省市地区']=='全国']
        if not rows:
            raise ValueError('insurance source window has no explicit national aggregate')
        coverage.update(unique_observations=len(rows),scope='explicit 全国 rows in the returned latest regional table window')
    return rows, coverage


def ths_columns(name):
    if name == "macro_stock_finance":
        return ["月份", *[k+"(亿元)" for k in ("募集资金", "首发募集资金", "增发募集资金", "配股募集资金")]]
    if name == "macro_rmb_loan":
        return [("月份", "月份"), ("新增人民币贷款", "总额(亿元)"), ("新增人民币贷款", "同比"),
                ("新增人民币贷款", "环比"), ("累计人民币贷款", "总额(亿元)"), ("累计人民币贷款", "同比")]
    return [("月份", "月份"), *[(head+"(亿元)", field) for head in
            ("新增存款", "新增企业存款", "新增储蓄存款", "新增其他存款") for field in ("数量", "同比", "环比")]]


def parse_ths(body, name):
    from bs4 import BeautifulSoup
    import pandas as pd
    soup = BeautifulSoup(body, "lxml")
    if THS[name][1] not in soup.get_text():
        raise ValueError("THS macro page title missing/mismatched")
    tables = pd.read_html(StringIO(str(soup)), keep_default_na=False)
    if not tables or list(tables[0].columns) != ths_columns(name):
        raise ValueError("THS macro native headings/units changed")
    frame = tables[0]
    if not 1 <= len(frame) <= 1000:
        raise ValueError("THS table empty or exceeds bounded table size")
    columns = ths_columns(name)
    labels = [c if isinstance(c, str) else (c[0] if c[0] == c[1] else "/".join(c)) for c in columns]
    units = {c: "percent" if c.endswith(("/同比", "/环比")) else "100 million CNY" for c in labels[1:]}
    identity = digest(columns)
    rows = []
    for values in frame.itertuples(index=False, name=None):
        row = dict(zip(labels, [None if str(v).strip() in {"", "--", "-"} else str(v).strip() for v in values]))
        rows.append(dict(row, source_schema_sha256=identity, source_units=units))
    return rows, {"source_contract": name, "source_count": len(rows), "source_schema_sha256": identity,
                  "units": units, "complete_returned_window": True, "complete_history": False,
                  "history_limit": "HTML only exposes a recent table (currently 30 months); no pre-window history claimed",
                  "publication_time": "not provided; statistical month is not announcement date"}


def financing_observed_months(rows, coverage, received_at):
    """Keep valid months; quarantine future labels against the original receipt.

    Replaying yesterday's body next month must not promote its future rows. The
    receipt is immutable HTTP evidence, never the current processing clock or a
    user-supplied query date. Quarantine keeps the complete native row, its source
    position and reason alongside the raw response.
    """
    observed = datetime.fromisoformat(received_at)
    if observed.tzinfo is None:
        raise ValueError("financing receipt needs an explicit timezone")
    cutoff = observed.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    accepted, quarantined, seen = [], [], set()
    for position, row in enumerate(rows, 1):
        month = source_date(row.get("月份"))
        if not month or month[8:] != "01" or month in seen:
            raise ValueError("invalid or duplicate financing month")
        seen.add(month)
        if month > cutoff:
            quarantined.append({"source_row_number": position, "record": row,
                                "reason": "statistical_month_after_original_receipt"})
        else:
            accepted.append(row)
    if not accepted:
        raise ValueError("financing table contains no observed current/past month")
    return accepted, dict(coverage, source_count=len(accepted), raw_source_count=len(rows),
                         source_observed_at=received_at, accepted_month_cutoff=cutoff,
                         quarantined_count=len(quarantined), quarantined_records=quarantined,
                         complete_source_table=not quarantined,
                         scope="all valid months visible at original receipt; future labels separately quarantined",
                         current_month_status="source current-to-date value; not a finalized monthly release")


def collect(capture, name, params):
    if params:
        raise ValueError("native period table takes no arbitrary parameters")
    if name in SINA:
        definition = SINA[name]
        url = "https://quotes.sina.cn/mac/api/jsonp_v3.php/SINAREMOTECALLCALLBACK1601651495761/MacPage_Service.get_pagedata?"
        body = capture.get(url+urlencode(dict(cate=definition["cate"], event=definition["event"],
                                             **{"from": 0}, num=1000, condition="")))
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            text = body.decode("gb18030")
        payload = data_parser()(text[text.index("{"):text.rindex("}")+1])
        rows, coverage = parse_sina(payload, name)
    else:
        rows, coverage = parse_ths(capture.get("https://data.10jqka.com.cn/macro/"+THS[name][0]+"/"), name)
        if name == "macro_stock_finance":
            rows, coverage = financing_observed_months(rows, coverage, capture.receipts[-1]["received_at"])
    capture.dataset_metadata = coverage
    return rows


def validate(rows, job, as_of, numeric):
    name = job["function"]
    if name not in TABLES:
        return None
    field = "截止日期" if name == "macro_china_freight_index" else "统计时间" if name in SINA else "月份"
    columns = SINA[name]["columns"] if name in SINA else ths_columns(name)
    units = ({c[1]: c[2] for c in columns if len(c) == 3} if name in SINA else
             {c if isinstance(c, str) else "/".join(c): "percent" if not isinstance(c, str) and c[-1] in {"同比", "环比"}
              else "100 million CNY" for c in columns[1:]})
    days, keys, missing = [], [], {}
    for row in rows:
        day = source_date(row.get(field))
        if not day or day > as_of or (name != "macro_china_freight_index" and day[8:] != "01") or row.get("source_schema_sha256") != digest(columns) or row.get("source_units") != units:
            raise ValueError("period table date/schema/unit mismatch")
        days.append(day)
        dimension = '省市地区' if name=='macro_china_insurance' else '统计对象' if name=='macro_china_society_traffic_volume' else None
        if dimension and (not row.get(dimension) or (name=='macro_china_insurance' and row[dimension]!='全国')):
            raise ValueError('macro scope identity missing or outside national insurance selection')
        keys.append((day, row.get("source_section", "source"), row.get(dimension) if dimension else None))
        for key, unit in units.items():
            raw = row[key]
            if unit == "percent" and raw is not None:
                if not isinstance(raw, str) or not raw.endswith("%"):
                    raise ValueError("THS percentage lost its source percent scale")
                raw = raw[:-1]
            value = numeric(raw)
            if value is None:
                missing[key] = missing.get(key, 0)+1
        if name == "macro_china_central_bank_balance":
            a, b = numeric(row["总资产"]), numeric(row["总负债"])
            if a is not None and b is not None and abs(a-b) > max(abs(a), abs(b))*numeric("0.00001")+numeric("0.1"):
                raise ValueError("central bank assets and liabilities do not reconcile")
    if not rows or len(set(keys)) != len(rows):
        raise ValueError("duplicate/empty macro periods")
    latest = rows[days.index(max(days))]
    required = REQUIRED.get(name, [next(iter(units))])
    gaps = [k+"最新期缺失" for k in required if numeric(latest.get(k)) is None]
    notes = ["统计月不是发布日期；修订以本机首次接收记录，未恢复历史首次发布版本", "历史缺值保留，不补零；可选字段缺失数逐列披露"]
    roles = {}
    if name in SCOPED_TABLES:
        notes.append("最新非空字段可单独查询；缺失字段不补值，按字段披露最近有效期")
    if name == 'macro_china_freight_index':
        roles = {k:'source index level; native unit label empty and index base unspecified, not a currency quotation' for k in units}
        notes.append('截止日期沿用源标签；HRCI/BHMI缺失不阻断BDI等已返回指数，未知基期不猜补')
    elif name == 'macro_china_postal_telecommunicational':
        roles = {'source_section':'原生累计/非累计分区分别留存，不合并或互相补值', '长途电话通话时长':'源单位仅写万，原样保留；不推测分钟或次'}
    elif name == 'macro_china_insurance':
        roles={'省市地区':'只接受源明确标记全国的合计；不汇总省市以避免重复省名与计划单列市重叠',
               '统计时间':'源观察月份；不自行解释为当月流量，不用差分猜月值'}
        notes.append('全国原保险保费及险种字段按源万元保存；原始地区表留存，当前入口不宣称所有地区身份已核')
    elif name == 'macro_china_society_traffic_volume':
        roles={'统计对象':'运输方式及航线维度分开，禁止跨方式相加',
               'source_section':'累计与非累计分开；原单位标签原样留在source_units'}
        notes.append('同比字段按源百分数可用；数量与周转量的泛化表头在各方式间尺度未逐一核实，均保留原值，不按亿吨/亿人统一换算')
        units={k:'percent' if u=='%' else 'source_native_scale_per_transport_mode_unverified' for k,u in units.items()}
    if name == "macro_rmb_deposit":
        roles = {k: "outstanding balance, despite source heading 新增; not a monthly inflow" for k in units if k.endswith("/数量")}
        notes.append("页面名人民币存款余额与表头新增存款不一致；保留原表头并明确为余额，不解释成当月新增")
    elif name == "macro_rmb_loan":
        roles = {"新增人民币贷款/总额(亿元)": "monthly net new loans; may be negative",
                 "累计人民币贷款/总额(亿元)": "outstanding loan balance, not year-to-date net new loans"}
    elif name == "macro_stock_finance":
        notes.append("当前月为截至采集时的筹资额，月内继续更新；不把配股缺值当零")
        notes.append("超过原始接收日的月份原样隔离在coverage.quarantined_records；不会随离线重放日期改变自动转为有效月")
    elif name == "macro_china_supply_of_money":
        notes.append("M1统计定义存在官方调整；保留源历史序列，不声明跨口径可比或自行回算")
    elif name == "macro_china_society_electricity":
        notes.append("数量按源表万千瓦时保留；不将月度观察累加成人为年度值")
    field_latest = {key:max((source_date(r[field]) for r in rows if numeric(r.get(key)) is not None), default=None)
                    for key in units} if name in SCOPED_TABLES else {}
    business_key=[field, "source_section"] if name in SINA else [field]
    if name=='macro_china_insurance':
        business_key.append('省市地区')
    elif name=='macro_china_society_traffic_volume':
        business_key.append('统计对象')
    return {"contract_reviewed": True, "business_key": business_key,
            "unique_keys": len(keys), "source_rows": len(rows), "units": units, "field_roles": roles,
            "frequency": "daily_source_index_and_revisions" if name == 'macro_china_freight_index' else "monthly_source_release_and_revisions", "latest_period": max(days),
            "field_latest_nonnull_periods": field_latest,
            "latest_announcement": None, "missing_field_observations": missing, "gaps": gaps, "limitations": notes}
