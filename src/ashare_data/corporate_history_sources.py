"""Strict, inert parsers for free corporate-action and share-history responses.

Provider nulls, reported dates, units and original rows survive normalization.
No price factors are used to manufacture cash or share entitlements.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import json
import re


def number(value, divisor=1):
    if value is None or value in ("", "--", "-", "暂无数据"):
        return None
    try:
        n = Decimal(str(value).replace(",", "")) / Decimal(divisor)
    except InvalidOperation as exc:
        raise ValueError("Invalid decimal source value") from exc
    if not n.is_finite() or not 0 <= n < Decimal("1e20") or n.as_tuple().exponent < -24:
        raise ValueError("Invalid decimal source value")
    return format(n, "f")


def source_day(value):
    if value is None or value in ("", "--", "-", "19000101", "1900-01-01", "1900-01-01 00:00:00"):
        return None
    value = str(value)
    if re.fullmatch(r"\d{8}", value):
        value = value[:4] + "-" + value[4:6] + "-" + value[6:]
    if not re.fullmatch(r"\d{4}-\d\d-\d\d(?: 00:00:00)?", value):
        raise ValueError("Unrecognized source date")
    result = value[:10]
    if date.fromisoformat(result).isoformat() != result:
        raise ValueError("Invalid source date")
    return result


class SourceTables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables = {}
        self.active = None
        self.depth = 0
        self.cell = None
        self.row = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            ident = attrs.get("id", "")
            if self.active:
                self.depth += 1
            elif ident in {"sharebonus_1", "sharebonus_2", "sharebonusdetail"} or re.fullmatch(
                r"StockStructureNewTable\d+", ident
            ):
                if ident in self.tables:
                    raise ValueError("Duplicate source table")
                self.active, self.depth = ident, 1
                self.tables[ident] = []
        if self.active and self.depth == 1:
            if tag == "tr":
                self.row = []
            if tag in ("td", "th"):
                self.cell = ""

    def handle_data(self, text):
        if self.cell is not None and self.depth == 1:
            self.cell += text

    def handle_endtag(self, tag):
        if not self.active:
            return
        if self.depth == 1:
            if tag in ("td", "th") and self.cell is not None:
                self.row.append(" ".join(self.cell.split()))
                self.cell = None
            if tag == "tr" and self.row:
                self.tables[self.active].append(self.row)
        if tag == "table":
            self.depth -= 1
            if not self.depth:
                self.active = None


def sina_tables(raw, security):
    text = raw.decode("gb18030")
    code = ("sh" if security.endswith("XSHG") else "sz") + security[:6]
    identities = re.findall(r'var\s+(?:page_symbol|fullcode)\s*=\s*"([a-z]{2}\d{6})"\s*;', text)
    if not identities or set(identities) != {code}:
        raise ValueError("Missing or mismatched Sina security identity")
    p = SourceTables()
    p.feed(text)
    return p.tables


def parse_sina_actions(raw, security):
    tables = sina_tables(raw, security)
    if not {"sharebonus_1", "sharebonus_2"} <= tables.keys():
        raise ValueError("Missing dividend/rights tables")
    headers = {cell for rows in tables.values() for row in rows for cell in row}
    if (
        not {"分红方案(每10股)", "派息(税前)(元)", "配股方案(每10股配股股数)", "基准股本(股)"}
        <= headers
    ):
        raise ValueError("Dividend/rights units changed")
    actions, nonimplemented, unparsed = [], [], []
    known_headers = {
        "分红",
        "配股",
        "公告日期",
        "分红方案(每10股)",
        "进度",
        "除权除息日",
        "股权登记日",
        "红股上市日",
        "查看详细",
        "送股(股)",
        "转增(股)",
        "派息(税前)(元)",
        "配股方案(每10股配股股数)",
        "配股价格(元)",
        "基准股本(股)",
        "除权日",
        "缴款起始日",
        "缴款终止日",
        "配股上市日",
        "募集资金合计(元)",
    }
    empty_labels = {"暂时没有数据！", "暂无数据", "暂无分红数据", "暂无配股数据"}
    table_states = {}
    for table, width, kind in (
        ("sharebonus_1", 9, "distribution"),
        ("sharebonus_2", 11, "rights_issue"),
    ):
        business_rows, empty_marker = 0, False
        for index, row in enumerate(tables[table]):
            locator = f"{table}/row/{index}"
            if row and all(cell in known_headers for cell in row):
                continue
            if len(row) == 1 and row[0] in empty_labels:
                empty_marker = True
                continue
            if len(row) != width:
                unparsed.append(
                    dict(
                        locator=locator, native_fields=row, reason="unrecognized_business_row_shape"
                    )
                )
                continue
            business_rows += 1
            issues = []

            def read(name, value, parser):
                try:
                    return parser(value)
                except (ValueError, ArithmeticError, TypeError):
                    issues.append(
                        dict(field=name, reason="invalid_source_value", native_value=value)
                    )
                    return None

            def date_field(name, index):
                return read(name, row[index], source_day)

            def amount_field(name, index, divisor=1):
                return read(name, row[index], lambda value: number(value, divisor))

            if kind == "distribution":
                fields = dict(
                    announcement_date=date_field("announcement_date", 0),
                    bonus_share_ratio=amount_field("bonus_share_ratio", 1, 10),
                    capitalization_share_ratio=amount_field("capitalization_share_ratio", 2, 10),
                    cash_per_share=amount_field("cash_per_share", 3, 10),
                    ex_date=date_field("ex_date", 5),
                    record_date=date_field("record_date", 6),
                    new_shares_tradable_date=date_field("new_shares_tradable_date", 7),
                    payment_date=None,
                    share_credit_date=None,
                    price_adjustment_date=None,
                )
                status = row[4]
                if status not in {
                    "实施",
                    "预案",
                    "不分配",
                    "股东大会预案",
                    "股东大会通过",
                    "取消",
                    "撤销",
                }:
                    issues.append(
                        dict(
                            field="implementation_status",
                            reason="unknown_implementation_status",
                            native_value=status,
                        )
                    )
                basis = "gross_pre_tax_per_old_eligible_share"
            else:
                fields = dict(
                    announcement_date=date_field("announcement_date", 0),
                    rights_ratio=amount_field("rights_ratio", 1, 10),
                    rights_price=amount_field("rights_price", 2),
                    base_shares=amount_field("base_shares", 3),
                    ex_date=date_field("ex_date", 4),
                    record_date=date_field("record_date", 5),
                    subscription_start=date_field("subscription_start", 6),
                    subscription_end=date_field("subscription_end", 7),
                    new_shares_tradable_date=date_field("new_shares_tradable_date", 8),
                )
                status, basis = "source_ledger", "subscription_outflow_per_new_share_not_dividend"
            item = dict(
                security=security,
                type=kind,
                fields=fields,
                native_fields=row,
                locator=locator,
                source_status=status,
                currency="CNY",
                cash_basis=basis,
            )
            if issues:
                item["source_issues"] = issues
            target = (
                nonimplemented
                if kind == "distribution"
                and status in {"预案", "不分配", "股东大会预案", "股东大会通过", "取消", "撤销"}
                else actions
            )
            target.append(item)
        table_states[table] = dict(
            business_rows=business_rows,
            state="business_rows_observed"
            if business_rows
            else "provider_no_rows_reported"
            if empty_marker
            else "blank_table",
            explicit_empty_marker=empty_marker,
            absence_proved=False,
        )
    return dict(
        actions=actions,
        shares=[],
        nonimplemented=nonimplemented,
        unparsed_records=unparsed,
        table_states=table_states,
        source_status="partial_provider_tables" if unparsed else "parsed_provider_tables",
        absence_proved=False,
    )


def parse_sina_structure(raw, security):
    tables = sina_tables(raw, security)
    tables = {k: v for k, v in tables.items() if k.startswith("StockStructureNewTable")}
    if not tables:
        raise ValueError("Missing share-structure tables")
    records = []
    for table, rows in tables.items():
        values = {}
        for row in rows:
            label = re.sub(r"[·\s]", "", row[0]).replace("(历史记录)", "")
            if label in {
                "变动日期",
                "公告日期",
                "变动原因",
                "总股本",
                "流通A股",
                "流通B股",
                "流通H股",
                "优先股",
                "限售A股",
            }:
                if label in values:
                    raise ValueError("Duplicate share column label")
                values[label] = row[1:]
        required = {"变动日期", "公告日期", "变动原因", "总股本", "流通A股"}
        if not required <= values.keys() or len({len(v) for v in values.values()}) != 1:
            raise ValueError("Missing or ragged share history")
        for i, day in enumerate(values["变动日期"]):
            fields, precision = {}, {}
            for label, field in [
                ("总股本", "total_shares"),
                ("流通A股", "float_a_shares"),
                ("流通B股", "float_b_shares"),
                ("流通H股", "float_h_shares"),
                ("优先股", "preferred_shares"),
                ("限售A股", "restricted_a_shares"),
            ]:
                cell = values.get(label, [None] * len(values["变动日期"]))[i]
                if cell in (None, "--", "", "-"):
                    fields[field], precision[field] = None, None
                else:
                    match = re.fullmatch(r"([\d,.]+) 万股", cell)
                    if not match:
                        raise ValueError("Share unit is not explicitly 万股")
                    amount = match[1].replace(",", "")
                    fields[field] = number(amount, Decimal("0.0001"))
                    decimals = len(amount.split(".")[1]) if "." in amount else 0
                    precision[field] = format(Decimal(10000) / (10**decimals), "f")
            reason = values["变动原因"][i]
            records.append(
                dict(
                    security=security,
                    date=source_day(day),
                    announcement_date=source_day(values["公告日期"][i]),
                    date_semantics="report_snapshot_date"
                    if reason == "定期报告"
                    else "provider_change_date",
                    reason=reason,
                    fields=fields,
                    source_quantum_shares=precision,
                    native_fields={k: v[i] for k, v in values.items()},
                    locator=f"{table}/column/{i + 1}",
                )
            )
    return dict(
        actions=[],
        shares=records,
        nonimplemented=[],
        source_status="parsed_provider_tables",
        absence_proved=False,
    )


def parse_sina_action_detail(raw, security):
    """Explicit stock-credit label only; the combined label is not a cash pay date."""
    tables = sina_tables(raw, security)
    if "sharebonusdetail" not in tables:
        raise ValueError("Missing action detail table")
    values = {}
    for row in tables["sharebonusdetail"]:
        if len(row) != 2 or row[0] in values:
            raise ValueError("Changed action detail shape")
        values[row[0]] = row[1]
    required = {
        "转增比例（10转增）",
        "送股比例（10送）",
        "登记日",
        "除息日",
        "上市日",
        "红利/配股起始日（送、转股到账日）",
    }
    if not required <= values.keys():
        raise ValueError("Changed action detail labels")
    bonus = number(values["送股比例（10送）"], 10)
    capitalization = number(values["转增比例（10转增）"], 10)
    stock_right = any(v is not None and Decimal(v) > 0 for v in (bonus, capitalization))
    row = dict(
        security=security,
        type="distribution",
        source_status="source_detail_label",
        fields=dict(
            announcement_date=None,
            record_date=source_day(values["登记日"]),
            ex_date=source_day(values["除息日"]),
            bonus_share_ratio=bonus,
            capitalization_share_ratio=capitalization,
            share_credit_date=source_day(values["红利/配股起始日（送、转股到账日）"])
            if stock_right
            else None,
            new_shares_tradable_date=source_day(values["上市日"]),
        ),
        native_fields=values,
        locator="sharebonusdetail",
        currency="CNY",
        cash_basis="gross_pre_tax_per_old_eligible_share",
    )
    return dict(
        actions=[row],
        shares=[],
        nonimplemented=[],
        source_status="parsed_provider_tables",
        absence_proved=False,
    )


def parse_sina_float(raw, security):
    code = ("sh" if security.endswith("XSHG") else "sz") + security[:6]
    match = re.fullmatch(
        r"(?:/\*.*?\*/\s*)?var KKE_ShareAmount_" + code + r"=\((\[.*\]|null)\)\s*;?\s*",
        raw.decode(),
        re.S,
    )
    if not match:
        raise ValueError("Unexpected historical float envelope")
    rows = json.loads(match[1], parse_float=str)
    if rows is None or rows == []:
        return dict(
            actions=[],
            shares=[],
            nonimplemented=[],
            source_status="provider_no_data",
            absence_proved=False,
        )
    records = []
    for i, r in enumerate(rows):
        records.append(
            dict(
                security=security,
                date=source_day(r["date"]),
                announcement_date=None,
                date_semantics="provider_float_change_date",
                reason=None,
                fields={"float_a_shares": number(r["amount"], Decimal("0.0001"))},
                source_quantum_shares={
                    "float_a_shares": format(
                        Decimal(10) ** (Decimal(str(r["amount"])).as_tuple().exponent + 4), "f"
                    )
                },
                native_fields=r,
                locator=f"data/{i}",
            )
        )
    return dict(
        actions=[],
        shares=records,
        nonimplemented=[],
        source_status="parsed_provider_series",
        absence_proved=False,
    )


def parse_eastmoney(raw, security):
    payload = json.loads(raw, parse_float=str)
    result = payload.get("result")
    if payload.get("success") is not True or not isinstance(result, dict):
        return dict(
            actions=[],
            shares=[],
            nonimplemented=[],
            source_status="provider_no_data_or_error",
            absence_proved=False,
        )
    rows = result["data"]
    if result["pages"] != 1 or result["count"] != len(rows):
        raise ValueError("Incomplete Eastmoney pagination")
    actions, other = [], []
    for i, r in enumerate(rows):
        expected = security[:6] + (".SH" if security.endswith("XSHG") else ".SZ")
        if r["SECURITY_CODE"] != security[:6] or r["SECUCODE"] != expected:
            raise ValueError("Wrong dividend identity")
        item = dict(
            security=security,
            type="distribution",
            source_status=r["ASSIGN_PROGRESS"],
            fields=dict(
                announcement_date=source_day(r["NOTICE_DATE"]),
                plan_announcement_date=source_day(r["PLAN_NOTICE_DATE"]),
                record_date=source_day(r["EQUITY_RECORD_DATE"]),
                ex_date=source_day(r["EX_DIVIDEND_DATE"]),
                cash_per_share=number(r["PRETAX_BONUS_RMB"], 10),
                bonus_share_ratio=number(r["BONUS_RATIO"], 10),
                capitalization_share_ratio=number(r["IT_RATIO"], 10),
                payment_date=None,
                share_credit_date=None,
                new_shares_tradable_date=None,
            ),
            native_fields=r,
            locator=f"result/data/{i}",
            currency="CNY",
            cash_basis="gross_pre_tax_per_old_eligible_share",
        )
        (actions if r["ASSIGN_PROGRESS"] == "实施分配" else other).append(item)
    return dict(
        actions=actions,
        shares=[],
        nonimplemented=other,
        source_status="parsed_provider_tables",
        absence_proved=False,
    )


PARSERS = {
    "sina_actions": parse_sina_actions,
    "sina_structure": parse_sina_structure,
    "sina_float": parse_sina_float,
    "eastmoney_dividends": parse_eastmoney,
    "sina_action_detail": parse_sina_action_detail,
}
