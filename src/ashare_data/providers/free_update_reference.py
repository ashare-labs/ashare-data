"""Named native fields for issuer profiles, IPO schedules and convertible bonds.

CNInfo's reviewed SDK performs its existing public request; its positional
DataFrame projection is discarded and the immutable response is parsed again.
No native null price is replaced with par value. Dates describe source terms or
published schedules, not proof that a planned event has already happened.
"""
import gzip
import hashlib
import json
import math
import re
from urllib.parse import parse_qs, urlencode, urlsplit

from ashare_data.providers.free_updates import digest

CNINFO = {"stock_profile_cninfo": "p_sysapi1133", "stock_new_ipo_cninfo": "p_sysapi1097"}
PROFILE = dict(zip(
    ["ORGNAME", "F001V", "F002V", "ASECCODE", "ASECNAME", "BSECCODE", "BSECNAME", "HSECCODE", "HSECNAME", "F044V", "MARKET", "F032V", "F003V", "F007N", "F010D", "F006D", "F011V", "F012V", "F013V", "F014V", "F004V", "F005V", "F006V", "F015V", "F016V", "F017V"],
    ["公司名称", "英文名称", "曾用简称", "A股代码", "A股简称", "B股代码", "B股简称", "H股代码", "H股简称", "入选指数", "所属市场", "所属行业", "法人代表", "注册资金", "成立日期", "上市日期", "官方网站", "电子邮箱", "联系电话", "传真", "注册地址", "办公地址", "邮政编码", "主营业务", "经营范围", "机构简介"]))
IPO = {"F108D": "摇号结果公告日", "F109D": "中签公告日", "SECNAME": "证券简称", "F006D": "上市日期",
       "F037D": "中签缴款日", "F002D": "申购日期", "F008N": "发行价", "SECCODE": "证劵代码",
       "F006N": "上网发行中签率", "F003N": "总发行数量", "F013N": "发行市盈率",
       "F043N": "上网发行数量", "F042N": "网上申购上限"}
IPO_UNITS = {"发行价": "CNY/share", "总发行数量": "10000 shares", "上网发行数量": "10000 shares",
             "网上申购上限": "source-native unit not verified", "上网发行中签率": "percent", "发行市盈率": "dimensionless"}
BOND_UNITS = {"ACTUAL_ISSUE_SCALE": "100000000 CNY", "PAR_VALUE": "CNY/bond", "ISSUE_PRICE": "CNY/bond",
              "ONLINE_GENERAL_AAU": "1000 CNY", "ONLINE_GENERAL_LWR": "percent", "FIRST_PER_PREPLACING": "CNY/share",
              "COUPON_IR": "percent_per_annum", "INITIAL_TRANSFER_PRICE": "CNY/share", "TRANSFER_PRICE": "CNY/share",
              "CONVERT_STOCK_PRICE": "CNY/share", "CURRENT_BOND_PRICE": "CNY/bond", "CURRENT_BOND_PRICENEW": "CNY/bond",
              "TRANSFER_VALUE": "CNY per 100 CNY face value", "TRANSFER_PREMIUM_RATIO": "percent"}
BOND_DATES = {"DELIST_DATE", "LISTING_DATE", "VALUE_DATE", "CEASE_DATE", "EXPIRE_DATE", "PUBLIC_START_DATE", "BOND_START_DATE",
              "SECURITY_START_DATE", "TRANSFER_START_DATE", "TRANSFER_END_DATE", "IB_START_DATE", "IB_END_DATE", "CASHFLOW_DATE"}
QUOTES = "f2~01~CONVERT_STOCK_CODE~CONVERT_STOCK_PRICE,f235~10~SECURITY_CODE~TRANSFER_PRICE,f236~10~SECURITY_CODE~TRANSFER_VALUE,f2~10~SECURITY_CODE~CURRENT_BOND_PRICE,f237~10~SECURITY_CODE~TRANSFER_PREMIUM_RATIO,f239~10~SECURITY_CODE~RESALE_TRIG_PRICE,f240~10~SECURITY_CODE~REDEEM_TRIG_PRICE,f23~01~CONVERT_STOCK_CODE~PBV_RATIO"


def collect_index_components(capture, params):
    """Retain the official current list; observation time builds future history."""
    from io import BytesIO
    import pandas as pd
    if set(params) != {"symbol"} or not re.fullmatch(r"\d{6}", str(params["symbol"])):
        raise ValueError("explicit CSI index code required")
    url = "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/" + params["symbol"] + "cons.xls"
    frame = pd.read_excel(BytesIO(capture.get(url)))
    headings = ["日期", "指数代码", "指数名称", "指数英文名称", "成份券代码",
                "成份券名称", "成份券英文名称", "交易所", "交易所英文名称"]
    if len(frame.columns) != len(headings) or any(
        not str(actual).strip().startswith(expected) for actual, expected in zip(frame.columns, headings)
    ):
        raise ValueError("CSI constituent source columns changed")
    frame.columns = [name.replace("成份券", "成分券") for name in headings]
    frame["日期"] = pd.to_datetime(frame["日期"], format="%Y%m%d", errors="raise").dt.date
    for field in ["指数代码", "成分券代码"]:
        frame[field] = frame[field].astype(str).str.zfill(6)
    rows = json.loads(frame.to_json(orient="records", date_format="iso", force_ascii=False))
    if not rows or any(row["指数代码"] != params["symbol"] for row in rows):
        raise ValueError("CSI index response identity differs")
    capture.dataset_metadata = {"source": "CSI official current constituent workbook",
        "index_code": params["symbol"], "source_count": len(rows), "complete_returned_window": True,
        "complete_history": False, "source_window": "complete current file for the requested index; retain each received snapshot",
        "historical_api_required": False, "membership_effective_start_inferred": False}
    return rows


def stock_market(code):
    if re.fullmatch(r"(?:60|68)\d{4}", code):
        return "SH"
    if re.fullmatch(r"(?:00|30)\d{4}", code):
        return "SZ"
    if re.fullmatch(r"(?:43|83|87|92)\d{4}", code):
        return "BJ"
    raise ValueError("unreviewed RMB stock identity")


def parse_cninfo(body, name, params):
    value = json.loads(body)
    rows = value.get("records")
    if value.get("resultcode") != 200 or not isinstance(rows, list) or not rows or len(rows) != int(value["count"]) or len(rows) != int(value["total"]):
        raise ValueError("CNInfo response rejected, empty or truncated")
    mapping = PROFILE if name == "stock_profile_cninfo" else IPO
    output = []
    for raw in rows:
        if not set(mapping) <= raw.keys():
            raise ValueError("CNInfo named native fields changed")
        row = {label: raw[key] for key, label in mapping.items()}
        row["source_native"] = raw
        code = row["A股代码"] if name == "stock_profile_cninfo" else row["证劵代码"]
        row["security"] = code+"."+stock_market(code)
        output.append(row)
    if name == "stock_profile_cninfo" and (len(rows) != 1 or rows[0]["ASECCODE"] != params["symbol"]):
        raise ValueError("CNInfo issuer identity mismatch")
    return output, {"source": "CNInfo official public company/IPO API", "api": CNINFO[name],
        "source_count": len(rows), "pages_received": 1, "native_total_reconciled": True,
        "complete_returned_window": True, "complete_universe": False, "complete_history": False,
        "scope": "one requested issuer's current profile" if name == "stock_profile_cninfo" else "all entries returned for the source's rolling 36-month IPO selection",
        "source_selection": {"scode": params["symbol"]} if name == "stock_profile_cninfo" else {"timetype": "36", "market": "ALL"},
        "date_role": "IPO dates are published schedules; listing/founded dates do not establish profile freshness",
        "native_fields_retained": True, "source_publication_time": None,
        "source_units_page": "https://webapi.cninfo.com.cn/shgs/company.html"}


def captured_cninfo(capture, name, params):
    if len(capture.receipts) != 1:
        raise ValueError("CNInfo single public request receipt required")
    receipt = capture.receipts[0]
    url = urlsplit(receipt["url"])
    expected = {"scode": [params["symbol"]]} if name == "stock_profile_cninfo" else {"timetype": ["36"], "market": ["ALL"]}
    if receipt.get("protocol") != "http" or receipt.get("status") != 200 or url.scheme != "https" or url.hostname != "webapi.cninfo.com.cn" or url.path != "/api/sysapi/"+CNINFO[name] or parse_qs(url.query) != expected or receipt.get("request_method") != "POST":
        raise ValueError("CNInfo request source, method or scope mismatch")
    if receipt.get("request_body_sha256") != hashlib.sha256(b"").hexdigest():
        raise ValueError("unexpected CNInfo POST body")
    body = gzip.decompress((capture.root/(receipt["sha256"]+".gz")).read_bytes())
    if hashlib.sha256(body).hexdigest() != receipt["sha256"]:
        raise ValueError("CNInfo raw receipt hash mismatch")
    rows, capture.dataset_metadata = parse_cninfo(body, name, params)
    return rows



def collect_cninfo(capture, name, params, invoke):
    """Keep the reviewed public request, discard the SDK's positional projection.

    Projection/schema exceptions may be recovered only from one independently
    verified successful original response. Transport/auth exceptions propagate.
    """
    try:
        invoke()
    except (ValueError, KeyError, IndexError):
        if not capture.receipts:
            raise
    return captured_cninfo(capture, name, params)


def collect_bonds(capture, name, params):
    if name == "bond_zh_cov_info" and (set(params) != {"symbol", "indicator"} or params["indicator"] != "基本信息" or not re.fullmatch(r"\d{6}", params["symbol"])):
        raise ValueError("explicit convertible-bond basic information required")
    if name == "bond_zh_cov" and params:
        raise ValueError("full bond reference list does not accept a partial context")
    # Match the source issuance page's ordering, including its retained legacy
    # instruments. Receipt replay binds this exact public query, not an SDK
    # projection or a substituted list of currently trading securities.
    query = dict(sortColumns="PUBLIC_START_DATE", sortTypes="-1", pageSize=500, pageNumber=1,
                 reportName="RPT_BOND_CB_LIST", columns="ALL", quoteColumns=QUOTES, source="WEB", client="WEB")
    if name == "bond_zh_cov_info":
        # Retain the verified per-bond request serialization. Changing the list
        # ordering must not invalidate existing per-bond protocol receipts.
        query = dict(reportName="RPT_BOND_CB_LIST", columns="ALL", sortColumns="SECUCODE", sortTypes="1",
                     pageNumber=1, pageSize=500, quoteColumns=QUOTES, source="WEB", client="WEB")
        query["filter"] = f'(SECURITY_CODE="{params["symbol"]}")'
    rows, pages, hashes = [], None, set()
    for page in range(1, 41):
        query["pageNumber"] = page
        response = json.loads(capture.get("https://datacenter-web.eastmoney.com/api/data/v1/get?"+urlencode(query)))
        result = response.get("result")
        if response.get("success") is not True or not isinstance(result, dict) or not result.get("data"):
            raise ValueError("bond reference source rejected or empty")
        current = (int(result["pages"]), int(result["count"]))
        pages = current if pages is None else pages
        if current != pages or not 1 <= current[0] <= 40 or current[0] != math.ceil(current[1]/500):
            raise ValueError("bond reference pagination count changed or exceeds budget")
        sha = digest(result["data"])
        if sha in hashes:
            raise ValueError("bond reference page repeated")
        hashes.add(sha)
        rows.extend(result["data"])
        if page == pages[0]:
            break
    if len(rows) != pages[1] or len({r["SECUCODE"] for r in rows}) != len(rows):
        raise ValueError("bond reference all-page identities/count mismatch")
    if name == "bond_zh_cov_info" and (len(rows) != 1 or rows[0]["SECURITY_CODE"] != params["symbol"]):
        raise ValueError("bond reference issuer filter failed")
    capture.dataset_metadata = {"source": "Eastmoney RPT_BOND_CB_LIST", "source_count": len(rows), "pages_received": pages[0],
        "complete_returned_window": True, "complete_universe": name == "bond_zh_cov", "complete_history": False,
        "scope": "complete current source bond reference list including source-retained past issues; not a historical membership series" if name == "bond_zh_cov" else "one requested bond profile",
        "source_units_page": "https://data.eastmoney.com/xg/xg/?mkt=kzz", "units": BOND_UNITS,
        "price_policy": "native null/dash quotes retained; no replacement with issue or face price",
        "date_role": "native contract/schedule references, not confirmed event execution; DELIST_DATE may equal maturity despite earlier redemption and is not an active-listing or announced-exit assertion", "source_publication_time": None}
    return rows


def validate(rows, job, as_of, numeric):
    from ashare_data.providers.free_update_dates import reviewed_date
    name, params = job["function"], job["parameters"]
    if name == "stock_zh_a_st_em":
        return validate_st(rows, params)
    if name not in set(CNINFO) | {"bond_zh_cov", "bond_zh_cov_info"}:
        return None
    keys, dates, missing, gaps, observations = [], [], {}, [], []
    if name == "stock_profile_cninfo":
        units, fields, frequency = {"注册资金": "10000 CNY"}, ["security"], "issuer_profile_current_snapshot"
        for row in rows:
            code = row.get("A股代码", "")
            market = stock_market(code)
            if code != params["symbol"] or row.get("security") != code+"."+market or not row.get("公司名称") or not row.get("A股简称"):
                raise ValueError("company profile identity/name mismatch")
            expected = {"SH": "上交所", "SZ": "深交所", "BJ": "北交所"}[market]
            allowed_markets = {expected}
            if market == "SZ" and code.startswith("30"):
                allowed_markets.add("深交所创业板")
            if row.get("所属市场") not in allowed_markets:
                raise ValueError("company profile market does not match security code")
            founded, listed = reviewed_date(row.get("成立日期")), reviewed_date(row.get("上市日期"))
            if not founded or not listed or not founded <= listed <= as_of or numeric(row.get("注册资金")) is None or numeric(row["注册资金"]) <= 0:
                raise ValueError("company profile dates/registered capital invalid")
    elif name == "stock_new_ipo_cninfo":
        units, fields, frequency = IPO_UNITS, ["security", "申购日期"], "published_ipo_schedule_and_revisions"
        for row in rows:
            code = row.get("证劵代码", "")
            if row.get("security") != code+"."+stock_market(code) or not row.get("证券简称") or not reviewed_date(row.get("申购日期")):
                raise ValueError("IPO identity or subscription schedule missing")
            for field in IPO_UNITS:
                value = numeric(row.get(field))
                if value is not None and field != "发行市盈率" and value < 0:
                    raise ValueError("negative IPO amount/price/rate")
            total, online = numeric(row.get("总发行数量")), numeric(row.get("上网发行数量"))
            if total is None or total <= 0 or online is not None and online > total:
                raise ValueError("IPO offering volume invalid/inconsistent")
    else:
        units, fields, frequency = BOND_UNITS, ["SECUCODE"], "published_convertible_bond_reference_and_schedule"
        for row in rows:
            code = row.get("SECUCODE", "")
            if not re.fullmatch(r"(?:1[12]\d{4}\.(?:SH|SZ)|404\d{3}\.NQ)", code) or row.get("SECURITY_CODE") != code[:6] or not row.get("SECURITY_NAME_ABBR"):
                raise ValueError("bond reference source identity/name is malformed")
            if row.get("TRADE_MARKET") != {"SH":"CNSESH","SZ":"CNSESZ","NQ":"STAS00"}[code[-2:]]:
                raise ValueError("convertible bond market disagrees with identity")
            if name == "bond_zh_cov_info" and row["SECURITY_CODE"] != params["symbol"]:
                raise ValueError("bond reference belongs to another issuer")
            ordinary = bool(re.fullmatch(r"(?:11[0138]\d{3}\.SH|12[378]\d{3}\.SZ)", code))
            underlying = row.get("CONVERT_STOCK_CODE", "")
            if not isinstance(underlying, str) or not re.fullmatch(r"\d{6}", underlying):
                raise ValueError("bond underlying source code is malformed")
            if ordinary and stock_market(underlying) != code[-2:]:
                raise ValueError("convertible bond and underlying stock exchange disagree")
            if not ordinary:
                observations.append({"security": code, "kind": "source_retained_legacy_identity",
                    "native_market": row["TRADE_MARKET"], "underlying_native_code": underlying,
                    "instrument_type": "not inferred from code; native reference terms retained",
                    "ordinary_convertible_equivalence_claimed": False})
            for key in BOND_DATES:
                if row.get(key) not in (None, "", "-") and reviewed_date(row[key]) is None:
                    raise ValueError("invalid published bond term date: "+key)
            for first, last in [("VALUE_DATE", "EXPIRE_DATE"), ("TRANSFER_START_DATE", "TRANSFER_END_DATE"), ("IB_START_DATE", "IB_END_DATE")]:
                start, end = reviewed_date(row.get(first)), reviewed_date(row.get(last))
                if start and end and start > end:
                    raise ValueError("bond term starts after it ends: "+first+"/"+last)
                if (first == "VALUE_DATE" and not (start and end)) or bool(start) != bool(end):
                    observations.append({"security": code, "kind": "incomplete_contract_dates",
                        "fields": [first, last], "values": [row.get(first), row.get(last)]})
            for key in ("ACTUAL_ISSUE_SCALE", "PAR_VALUE", "ISSUE_PRICE"):
                if numeric(row.get(key)) is None or numeric(row[key]) <= 0:
                    raise ValueError("bond essential issue terms absent/nonpositive")
            for key in BOND_UNITS:
                value = numeric(row.get(key))
                missing[key] = missing.get(key, 0)+(value is None)
                if value is not None and key not in {"TRANSFER_PREMIUM_RATIO"} and value < 0:
                    raise ValueError("negative bond reference measure: "+key)
    for row in rows:
        if any(row.get(k) in (None, "") for k in fields):
            raise ValueError("reference business key missing")
        keys.append(digest([row[k] for k in fields]))
    if not rows or len(keys) != len(set(keys)):
        raise ValueError("reference response empty or duplicated")
    return {"contract_reviewed": True, "business_key": fields, "unique_keys": len(keys), "source_rows": len(rows),
        "units": units, "frequency": frequency, "latest_period": max(dates, default=None), "latest_announcement": None,
        "missing_field_observations": missing, "gaps": sorted(set(gaps)),
        "field_observations": observations,
        "limitations": ["当前资料/发行日程按真实接收时点留存；不从成立、上市、到期或申购日期推断资料最新更新时间",
            "全名单仅为来源当前可见范围；不推定CMES全部历史或历日证券身份",
            "原生字段保留，未声明单位的扩展字段仍是未解释源值；不以面值补缺失市场报价",
            "债券目录中的旧代码、转板代码及未知债券类别按源身份保留；不一律视作当前普通可转债，不推测转板前后映射",
            "IPO F042N 网上申购上限单位未核实，保留原值及单位未知标注，不作万股换算"]}


def collect_st(capture, params):
    """All pages of the reviewed Shanghai/Shenzhen risk-warning list, by code."""
    if params:
        raise ValueError('ST source filter is fixed; no implicit market change')
    query = {'pn':'1','pz':'100','po':'1','np':'1','ut':'bd1d9ddb04089700cf9c27f6f7426281',
        'fltt':'2','invt':'2','fid':'f12','fs':'m:0 f:4,m:1 f:4','fields':'f12,f13,f14'}
    rows, total = [], None
    for page in range(1, 21):
        query['pn'] = str(page)
        value = json.loads(capture.get('https://40.push2.eastmoney.com/api/qt/clist/get?'+urlencode(query)))
        data = value.get('data')
        if value.get('rc') != 0 or not isinstance(data, dict) or not isinstance(data.get('diff'), list):
            raise ValueError('ST native list rejected or changed schema')
        count = data.get('total')
        if not isinstance(count, int) or not 1 <= count <= 2000 or total is not None and total != count:
            raise ValueError('ST list count changed or outside reviewed bound')
        total = count
        expected = min(100, total-(page-1)*100)
        if len(data['diff']) != expected:
            raise ValueError('ST page size/count mismatch')
        for native in data['diff']:
            if not {'f12','f13','f14'} <= native.keys():
                raise ValueError('ST identity fields absent')
            code = native['f12']
            market, share_class = st_identity(code)
            rows.append({'代码':code,'名称':native['f14'],'security':code+'.'+market,'share_class':share_class,
                'source_native':native,'source_market_filter':query['fs']})
        if len(rows) >= total:
            break
    validate_st(rows, params)
    capture.dataset_metadata = {'source_count':total,'pages_received':page,'complete_returned_window':True,
        'complete_history':False,'complete_universe':False,'source_effective_time':None,
        'scope':'all source-visible risk-warning names; SH/SZ A/B and 92 BJ identities mapped, other BJ/NEEQ codes kept as unresolved native identities; no historical membership',
        'source_filter':query['fs'],'source_sort':'security code; count and unique identities reconciled',
        'source_fields':{'f12':'security code','f13':'native routing identifier; 0 also includes BJ/NEEQ, not an exchange identity','f14':'source name'},
        'limitation':'observation time records receipt, not ST entry/exit effective date; this list makes no quote-value acceptance claim'}
    return rows


def st_identity(code):
    # This source board includes B shares. Keep them separate and do not expand
    # the A-share identity contract used by issuer financial-report requests.
    if re.fullmatch(r'900\d{3}', code):
        return 'SH', 'B'
    if re.fullmatch(r'200\d{3}', code):
        return 'SZ', 'B'
    if re.fullmatch(r'(?:43|83|87)\d{4}', code):
        return 'NATIVE_0', 'unresolved_BJ_or_NEEQ'
    return stock_market(code), 'A'


def validate_st(rows, params):
    if params or not rows:
        raise ValueError('ST list empty or unexpected parameters')
    identities = []
    for row in rows:
        code, name = row.get('代码',''), row.get('名称','')
        market, share_class = st_identity(code)
        native = row.get('source_native', {})
        if row.get('security') != code+'.'+market or native.get('f13') != {'SH':1,'SZ':0,'BJ':0,'NATIVE_0':0}[market] or row.get('share_class') != share_class:
            raise ValueError('ST source market/security mismatch')
        if not isinstance(name,str) or 'ST' not in name.upper() or native.get('f12') != code or native.get('f14') != name:
            raise ValueError('ST source name or identity projection mismatch')
        if row.get('source_market_filter') != 'm:0 f:4,m:1 f:4':
            raise ValueError('ST source membership filter changed')
        identities.append(row['security'])
    if len(identities) != len(set(identities)):
        raise ValueError('ST duplicate identity across pages')
    return {'contract_reviewed':True,'business_key':['security'],'unique_keys':len(identities),'source_rows':len(rows),
        'units':{},'frequency':'source_membership_snapshot_and_revisions','latest_period':None,'latest_announcement':None,
        'share_classes':sorted({r['share_class'] for r in rows}),
        'gaps':[],'limitations':['源风险警示名单含沪深A/B股、92号北交所及43/83/87号股票；后者保留源路由身份，不擅自全部认作北交所',
            '以接收时点留存快照，不能把今日采集时间当作ST资格生效日',
            '本条验收股票列表身份；未请求或宣称验证该接口的报价附加字段']}
