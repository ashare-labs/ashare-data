"""Current stock/ETF depth, with native timestamps and quantities kept explicit.

eltdx 3.0.3 quotes.get_depth uses a zero-cursor 0x0547 refresh. Its SDK docs
define quote prices and lot quantities; this adapter does not extend their
interpretation to convertible bonds or reconstruct an exchange timestamp.
"""
from datetime import datetime
from decimal import Decimal


def checked_record(record, symbol):
    if record["exchange"] + record["code"] != symbol or record["market_id"] != int(symbol.startswith("sh")):
        raise ValueError("native depth security mismatch")
    prices = [Decimal(str(record[k])) for k in ("last_price", "last_close_price", "open_price", "high_price", "low_price")]
    if any(not p.is_finite() or p < 0 for p in prices):
        raise ValueError("native depth nonfinite or negative price")
    if all(prices[k] for k in (0, 2, 3, 4)) and not prices[4] <= min(prices[0],prices[2]) <= max(prices[0],prices[2]) <= prices[3]:
        raise ValueError("native depth OHLC range mismatch")
    for side in ("buy_levels", "sell_levels"):
        levels = record[side]
        if len(levels) != 5:
            raise ValueError("native depth does not contain five levels")
        nonzero=[]
        for level in levels:
            price, volume=Decimal(str(level["price"])), level["volume"]
            if not price.is_finite() or price < 0 or type(volume) is not int or volume < 0:
                raise ValueError("invalid depth price/quantity")
            if price:
                nonzero.append(price)
        if nonzero != sorted(nonzero,reverse=side == "buy_levels"):
            raise ValueError("native depth level ordering differs")
    for field in ("total_hand", "current_hand", "inside_dish", "outer_disc"):
        if type(record[field]) is not int or record[field] < 0:
            raise ValueError("invalid native quote lot count")


def parse(native, params, received):
    symbol=params["symbol"]
    records=native["records"]
    if native["requested_codes"] != [symbol] or len(records) != 1:
        raise ValueError("native depth requested/returned scope differs")
    record=records[0]
    wire=bytes.fromhex(native["raw_payload"])
    decoded=bytes.fromhex(native["decoded_payload"])
    record_bytes=bytes.fromhex(record["record_hex"])
    if not wire or not record_bytes or decoded.count(record_bytes) != 1:
        raise ValueError("native depth original/decoded record bytes absent")
    checked_record(record,symbol)
    return [{"security":symbol,"asset_type":params["asset_type"],"observed_at":received,
        "exchange_timestamp":None,"source_time_raw":record["update_time_raw"],
        "last_price":record["last_price"],"previous_close":record["last_close_price"],
        "bids":record["buy_levels"],"asks":record["sell_levels"],
        "native_fields":record}]


def validate(rows, params, as_of):
    if len(rows) != 1:
        raise ValueError("one explicitly requested quote required")
    row=rows[0]
    native=row["native_fields"]
    checked_record(native,params["symbol"])
    received=datetime.fromisoformat(row["observed_at"])
    if (row["security"] != params["symbol"] or row["asset_type"] != params["asset_type"]
            or received.utcoffset() is None or received.date().isoformat() > as_of
            or row["exchange_timestamp"] is not None or row["source_time_raw"] != native["update_time_raw"]
            or row["last_price"] != native["last_price"] or row["previous_close"] != native["last_close_price"]
            or row["bids"] != native["buy_levels"] or row["asks"] != native["sell_levels"]):
        raise ValueError("quote identity, values or observation/source time changed")
    return {"contract_reviewed":True,"business_key":["security"],"gaps":[],
        "source_rows":1,"unique_keys":1,"latest_period":None,"latest_announcement":None,
        "frequency":"current quote snapshot","units":{"prices":"CNY per stock share / ETF unit",
            "depth_volume":"native lots (手), not converted to shares", "native_volume":"named lot fields retained"},
        "limitations":["receipt time is not exchange trading date/time; raw source clock is retained",
            "zero levels after close or while inactive stay zero; not invented orders",
            "stock and ETF scope only; bond quote precision remains unresolved",
            "native binary decoding uses the explicitly pinned existing SDK, not an independent decoder"]}
