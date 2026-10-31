"""Reviewed current inputs for the separately authorized second menu batch.

Sina wire positions reuse market.client.reconcile_day. CNI's current-sample
download is distinct from its monthly historical-weight workbook. Native F10
topic dates and flags remain native rather than invented disclosure dates.
"""

from datetime import datetime
from decimal import Decimal
from io import BytesIO
import re
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from ashare_data.providers.free_updates.free_update_coverage import require
from ashare_data.providers.free_updates.free_update_legacy_reference import call

TZ = ZoneInfo("Asia/Shanghai")
FUNCTIONS = {"sina_current_quotes", "cni_current_members", "tdx_f10_hot_topics"}
CSI_CODES = {"000300", "000016", "000688", "000905", "000906", "000852", "000010"}


def stock(value):
    require(isinstance(value, str) and re.fullmatch(r"(?:sh60|sh68|sz00|sz30)\d{4}", value),
            "explicit SH/SZ A-share identity required")
    return value


def parse_quotes(body, symbols, observed_at):
    require(0 < len(symbols) <= 100 and len(set(symbols)) == len(symbols),
            "quote batch needs 1..100 distinct securities")
    for symbol in symbols:
        stock(symbol)
    text = body.decode("gb18030")
    pattern = re.compile(r'var hq_str_((?:sh|sz)\d{6})="([^"\r\n]*)";\s*')
    matches = list(pattern.finditer(text))
    require(not pattern.sub("", text).strip(), "unexpected quote response content")
    labels = [m.group(1) for m in matches]
    require(len(set(labels)) == len(labels) and set(labels) == set(symbols),
            "quote response security set differs from request")
    observed = datetime.fromisoformat(observed_at)
    require(observed.tzinfo is not None, "receipt time needs timezone")
    records, empty = [], []
    for match in matches:
        symbol, native = match.groups()
        if not native:
            empty.append(symbol)
            continue
        fields = native.split(",")
        require(len(fields) >= 32 and fields[0], "quote fields missing")
        stamp = datetime.strptime(fields[30] + " " + fields[31], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
        require(stamp <= observed, "future source quote timestamp")
        values = {k: fields[p] for k, p in
                  {"open": 1, "previous_close": 2, "close": 3, "high": 4,
                   "low": 5, "volume": 8, "amount": 9}.items()}
        numbers = {k: Decimal(v) for k, v in values.items()}
        require(all(v.is_finite() and 0 <= v < Decimal("1e18") for v in numbers.values()),
                "invalid quote value")
        no_trade = all(numbers[k] == 0 for k in ("open", "high", "low", "volume"))
        require(no_trade or (0 < numbers["low"] <= min(numbers["open"], numbers["close"])
                            <= max(numbers["open"], numbers["close"]) <= numbers["high"]),
                "inconsistent quote OHLC")
        require(numbers["volume"] == numbers["volume"].to_integral_value(), "fractional stock volume")
        records.append(dict(security=symbol, name=fields[0], asset_type="stock",
                            date=stamp.date().isoformat(), quote_time=stamp.isoformat(),
                            **values, price_basis="unadjusted", volume_unit="share",
                            amount_unit="CNY", quote_kind="current_cumulative_quote",
                            zero_trade_fields=no_trade, finality="not_attested",
                            trading_scope="source-native cumulative, tail retained separately",
                            native_fields=fields))
    return records, dict(requested_securities=symbols, returned_securities=labels,
                         empty_securities=empty, complete_response_set=True,
                         source_count=len(records), complete_returned_window=True,
                         source_window="all explicitly requested current security quotes; native timestamp retained")


def parse_cni(body, as_of):
    import pandas as pd
    frame = pd.read_excel(BytesIO(body), dtype=str).fillna("")
    expected = ["日期", "样本代码", "样本简称", "所属行业", "权重（%）"]
    require(list(frame.columns) == expected, "CNI current-sample workbook columns changed")
    require(len(frame) > 0, "empty current CNI sample workbook")
    records = []
    for native in frame.to_dict("records"):
        code = native["样本代码"].zfill(6)
        # 399006's published investment universe is Shenzhen ChiNext. This
        # narrow contract does not guess exchanges for arbitrary CNI indices.
        require(re.fullmatch(r"30\d{4}", code) and native["样本简称"], "non-ChiNext member identity")
        day = datetime.strptime(native["日期"], "%Y-%m-%d").date().isoformat()
        require(day <= as_of, "future CNI membership date")
        weight = None if native["权重（%）"] in {"", "--", "-"} else Decimal(native["权重（%）"])
        require(weight is None or (weight.is_finite() and 0 <= weight <= 100), "invalid CNI percent weight")
        records.append({"指数代码": "399006", "指数名称": "创业板指", "日期": day,
                        "成分券代码": code, "成分券名称": native["样本简称"],
                        "交易所": "深圳证券交易所", "所属行业": native["所属行业"],
                        "权重百分比": str(weight) if weight is not None else None, "source_native": native})
    require(len({r["成分券代码"] for r in records}) == len(records), "duplicate CNI member")
    require(len({r["日期"] for r in records}) == 1, "mixed CNI membership dates")
    return records, dict(source_count=len(records), complete_returned_window=True,
                         source_window="official CNI current-sample download for 399006",
                         monthly_weight_file_used=False, weight_unit="percent",
                         membership_effective_start_inferred=False,
                         available_weights=sum(r["权重百分比"] is not None for r in records))


def parse_topics(tables, symbol):
    stock(symbol)
    require(len(tables) == 1, "F10 topic table count changed")
    records = []
    for raw in tables[0]["rows"]:
        require({"bflag", "ztrq", "ztmc", "gld", "rxsj", "ztnr", "arec", "id", "sslb"} <= raw.keys()
                and raw["id"] is not None and raw["ztmc"], "F10 topic identity/text schema changed")
        records.append(dict(security=symbol, topic_id=str(raw["id"]), topic_name=raw["ztmc"],
                            topic_description=raw["ztnr"], taxonomy="TDX F10",
                            source_native=raw, first_publication_at=None))
    require(len({r["topic_id"] for r in records}) == len(records), "duplicate F10 topic identity")
    return records, dict(source_count=len(records), complete_returned_window=True,
                         source_window="complete source-returned current topics for specified issuer",
                         native_dates_and_flags="uninterpreted; not disclosure times or verified heat scores")


def collect(capture, function, parameters, as_of):
    if function == "sina_current_quotes":
        require(set(parameters) == {"symbols"}, "explicit batch securities required")
        body = capture.get("https://hq.sinajs.cn/list=" + ",".join(parameters["symbols"]),
                           {"Referer": "https://finance.sina.com.cn/"})
        return parse_quotes(body, parameters["symbols"], capture.receipts[-1]["received_at"])
    if function == "cni_current_members":
        require(parameters == {"symbol": "399006"}, "only reviewed ChiNext index supported")
        body = capture.get("https://www.cnindex.com.cn/sample-detail/download?" +
                           urlencode(dict(indexcode="399006", dateStr=as_of[:7])))
        return parse_cni(body, as_of)
    if function == "tdx_f10_hot_topics":
        require(set(parameters) == {"symbol"}, "explicit topic issuer required")
        symbol = stock(parameters["symbol"])
        identity = call(capture, "CWServ.tdxf10_gg_comreq", {"Params": ["gpquery", symbol[2:]]})
        require(len(identity) == 1 and len(identity[0]["rows"]) == 1
                and identity[0]["rows"][0].get("T002") == symbol[2:]
                and identity[0]["rows"][0].get("sc") == int(symbol.startswith("sh")),
                "F10 topic issuer identity mismatch")
        return parse_topics(call(capture, "CWServ.tdxf10_gg_rdtc",
                                 {"Params": [symbol[2:], "zttzbkz"]}), symbol)
    if function == "index_stock_cons_csindex":
        from ashare_data.providers.free_updates.free_update_reference import collect_index_components
        require(parameters.get("symbol") in CSI_CODES, "index outside the second-batch scope")
        rows = collect_index_components(capture, parameters)
        return rows, capture.dataset_metadata
    if function == "sina_raw_daily":
        from ashare_data.providers.free_updates.free_update_equities import collect as equities
        stock(parameters["symbol"])
        rows = equities(capture, function, parameters)
        return rows, capture.dataset_metadata
    raise ValueError("unreviewed supplemental source")


def semantics(function, records, parameters, as_of):
    if function in {"index_stock_cons_csindex", "sina_raw_daily"}:
        from ashare_data.providers.free_updates.free_update_semantics import validate_semantics
        return validate_semantics(records, dict(function=function, parameters=parameters), as_of)
    if function == "cni_current_members":
        key = ["指数代码", "日期", "交易所", "成分券代码"]
        units = {"权重百分比": "percent", "codes": "identifiers"}
        dates = [r["日期"] for r in records]
        limits = ["official current file, no inferred effective-from date"]
    elif function == "sina_current_quotes":
        key = ["security", "quote_time"]
        units = {"OHLC": "CNY/share", "volume": "share", "amount": "CNY"}
        dates = [r["date"] for r in records]
        limits = ["source timestamp and unadjusted current cumulative fields; finality not attested",
                  "zero-trade fields retained, no invented suspension reason"]
    elif function == "tdx_f10_hot_topics":
        key = ["security", "topic_id"]
        units, dates = {"topic_id": "identifier"}, []
        limits = ["TDX issuer topics subset, not a market-wide heat ranking",
                  "native ztrq/rxsj/gld/flags retained without inferred roles"]
    else:
        raise ValueError("unknown supplemental source semantics")
    return dict(contract_reviewed=True, gaps=[], business_key=key, units=units,
                latest_period=max(dates) if dates else None,
                limitations=limits, frequency="current_snapshot")
