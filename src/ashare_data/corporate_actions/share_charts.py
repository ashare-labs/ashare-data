"""Read inert Sina chart attributes; never run Flash or infer an omitted slice."""

from collections import defaultdict
from decimal import Decimal
import hashlib
import re
import xml.etree.ElementTree as ET

from ashare_data.corporate_actions.sources import SourceTables, sina_tables, source_day


def amount(cell):
    match = re.fullmatch(r"(-?[\d,.]+) 万股", cell or "")
    if not match:
        raise ValueError("Chart unit reference is not explicitly 万股")
    text = match[1].replace(",", "")
    return Decimal(text) * 10000, Decimal(10000).scaleb(-len(text.split(".")[1]) if "." in text else 0)


class ChartTables(SourceTables):
    def __init__(self):
        super().__init__()
        self.graphs = defaultdict(list)
        self.links = defaultdict(list)

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        if (not self.active or self.depth != 1 or not self.row
                or re.sub(r"[·\s]", "", self.row[0]) != "股本结构图"):
            return
        attrs = {k.lower(): v for k, v in attrs}
        key = (self.active, len(self.row))
        value = (attrs.get("value") if tag == "param" and attrs.get("name", "").lower() == "flashvars"
                 else attrs.get("flashvars") if tag == "embed" else None)
        if value is not None:
            self.graphs[key].append((tag, value))
        if tag == "a":
            self.links[key].append(attrs.get("href"))


def structure_charts(raw, security):
    # The ordinary parser validates the requested security identity before charts
    # are considered. Attribute strings are data only; no resource is fetched.
    sina_tables(raw, security)
    parser = ChartTables()
    parser.feed(raw.decode("gb18030"))
    return parser


def chart_observation(parser, security, table, column, day):
    key = (table, column)
    graphs = parser.graphs[key]
    if ([tag for tag, _ in graphs] != ["param", "embed"] or graphs[0][1] != graphs[1][1]
            or not graphs[0][1].startswith("&dataXML=")):
        raise ValueError("Chart representations missing, ambiguous or different")
    xml = graphs[0][1][9:]
    if "<!" in xml or len(xml) > 20000:
        raise ValueError("Unsupported chart XML")
    try:
        node = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError("Malformed chart XML") from exc
    if node.tag != "graph" or not len(node):
        raise ValueError("Missing chart slices")
    values, quantum, native = {}, {}, {}
    for child in node:
        label, text = child.get("name"), child.get("value", "")
        if (child.tag != "set" or len(child) or not label or label in values
                or not re.fullmatch(r"\d+(?:\.\d{1,12})?", text)):
            raise ValueError("Unrecognized, duplicate or negative chart slice")
        values[label] = Decimal(text) * 10000
        quantum[label] = Decimal(10000).scaleb(-len(text.split(".")[1]) if "." in text else 0)
        native[label] = text
    if "流通A股" not in values:
        raise ValueError("Chart does not explicitly report float A shares")
    rows = parser.tables[table]
    fields = {}
    for row in rows:
        label = re.sub(r"[·\s]", "", row[0]).replace("(历史记录)", "")
        if len(row) > column:
            if label in fields:
                raise ValueError("Duplicate chart context row")
            fields[label] = row[column]
    if source_day(fields["变动日期"]) != day:
        raise ValueError("Chart and requested source date disagree")
    link = f"/corp/go.php/vCI_StockStructurePicture/stockid/{security[:6]}/alertdate/{day.replace('-', '')}.phtml"
    if parser.links[key] != [link]:
        raise ValueError("Chart date/security anchor is missing or different")
    checks = []
    for label, value in values.items():
        if label == "流通A股":
            continue
        reference, q = amount(fields.get(label))
        if reference < 0 or abs(value - reference) > (q + quantum[label]) / 2:
            raise ValueError("Chart class differs from its explicitly unit-labelled table cell")
        checks.append(dict(label=label, table_value=fields[label], chart_value=native[label]))
    if not checks:
        raise ValueError("Chart has no independent unit-labelled class reference")
    total, tq = amount(fields.get("总股本"))
    if total < 0 or abs(sum(values.values()) - total) > (tq + sum(quantum.values())) / 2:
        raise ValueError("Chart classes do not partition the reported total")
    return dict(value=format(values["流通A股"], "f"), quantum=format(quantum["流通A股"], "f"),
        source_value=native["流通A股"], unit="shares", chart_source_unit="万股",
        source_locator=f"{table}/column/{column}/chart/流通A股",
        chart_sha256=hashlib.sha256(xml.encode()).hexdigest(), date_anchor=link,
        unit_evidence=checks, class_values={k: format(v, "f") for k, v in values.items()},
        table_total=fields["总股本"], table_float=fields["流通A股"],
        interpretation="same source column; explicit chart labels; table class units and total checked")
