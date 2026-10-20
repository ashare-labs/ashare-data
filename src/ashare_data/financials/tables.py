"""Shared financial/reference table contract and lossless source projections.

No network, mutable collector state, provider priority or guessed identity here.
Content identities exclude capture clocks and parser identities. Referenceable
links retain capture locations separately from report-relative field locations.
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib

from ashare_data.core.model import require
from ashare_data.storage.market_release import canonical

VERSION = "ad.financial-tables.v1"
CONTEXT_VERSION = "ad.financial-context.v1"


def identity(kind, *parts):
    return kind + ":" + hashlib.sha256(canonical(parts)).hexdigest()


def text(value):
    return canonical(value).decode()


def columns(spec):
    return dict(part.split(":", 1) for part in spec.split())


TABLES = {
    "financial.report_versions": columns("""
        report_version_id:VARCHAR issuer_id:VARCHAR source:VARCHAR statement:VARCHAR
        period_end:VARCHAR source_revision_id:VARCHAR projection_identity:VARCHAR
        version_kind:VARCHAR source_metadata_json:VARCHAR
    """),
    "financial.extractions": columns("""
        extraction_version_id:VARCHAR report_version_id:VARCHAR parser_sha256:VARCHAR
        mapping_sha256:VARCHAR schema_version:VARCHAR
    """),
    "financial.disclosure_events": columns("""
        event_id:VARCHAR issuer_id:VARCHAR source:VARCHAR native_event_id:VARCHAR
        title:VARCHAR published_date:VARCHAR published_at:VARCHAR precision:VARCHAR
        event_kind:VARCHAR historical_available_at:VARCHAR visibility_basis:VARCHAR
    """),
    "financial.observations": columns("""
        observation_id:VARCHAR report_version_id:VARCHAR extraction_version_id:VARCHAR
        context_id:VARCHAR context_version:VARCHAR issuer_id:VARCHAR
        statement:VARCHAR period_start:VARCHAR period_end:VARCHAR basis:VARCHAR scope:VARCHAR
        currency:VARCHAR column_role:VARCHAR dimensions_json:VARCHAR raw_context_json:VARCHAR
        source_field:VARCHAR source_title:VARCHAR source_ordinal:BIGINT source_locator:VARCHAR
        canonical_field:VARCHAR raw_value_text:VARCHAR raw_value_type:VARCHAR
        value_decimal_text:VARCHAR source_unit:VARCHAR canonical_unit:VARCHAR
        scale_decimal_text:VARCHAR unit_evidence:VARCHAR canonical_value_decimal_text:VARCHAR
        value_state:VARCHAR unit_state:VARCHAR quality_state:VARCHAR quality_reasons_json:VARCHAR
    """),
    "financial.links": columns("""
        link_id:VARCHAR from_type:VARCHAR from_id:VARCHAR to_type:VARCHAR to_id:VARCHAR
        relation:VARCHAR issuer_id:VARCHAR period_end:VARCHAR field:VARCHAR
        available_at:VARCHAR evidence_asset_id:VARCHAR source_locator:VARCHAR scope_state:VARCHAR
    """),
    "financial.visibility": columns("""
        visibility_id:VARCHAR observation_id:VARCHAR event_id:VARCHAR issuer_relation_id:VARCHAR
        policy:VARCHAR available_from:VARCHAR evidence_asset_id:VARCHAR evidence_json:VARCHAR
    """),
    "meta.financial_captures": columns("""
        capture_id:VARCHAR batch_key:VARCHAR task_id:VARCHAR source:VARCHAR
        raw_asset_id:VARCHAR normalized_asset_id:VARCHAR response_sha256:VARCHAR
        receipt_sha256:VARCHAR observed_at:VARCHAR source_state:VARCHAR source_rows:BIGINT
    """),
    "meta.financial_coverage": columns("""
        coverage_id:VARCHAR batch_key:VARCHAR task_id:VARCHAR source:VARCHAR security:VARCHAR
        source_state:VARCHAR input_rows:BIGINT mapped_rows:BIGINT quarantined_rows:BIGINT
        coverage_json:VARCHAR error:VARCHAR
    """),
    "quality.financial_quarantine": columns("""
        quarantine_id:VARCHAR capture_id:VARCHAR source_schema:VARCHAR
        source_locator:VARCHAR reason:VARCHAR normalized_record_json:VARCHAR
    """),
    "reference.exchange_calendar": columns("""
        observation_id:VARCHAR source:VARCHAR source_revision_id:VARCHAR exchange:VARCHAR
        date:VARCHAR is_open:BOOLEAN previous_session:VARCHAR
    """),
    "reference.price_limits": columns("""
        observation_id:VARCHAR instrument_id:BIGINT security:VARCHAR source:VARCHAR
        source_revision_id:VARCHAR trade_date:VARCHAR up_limit:VARCHAR down_limit:VARCHAR
        previous_close:VARCHAR unit:VARCHAR value_states_json:VARCHAR
    """),
    "reference.share_observations": columns("""
        observation_id:VARCHAR instrument_id:BIGINT security:VARCHAR source:VARCHAR
        source_revision_id:VARCHAR effective_date:VARCHAR announcement_date:VARCHAR
        field:VARCHAR value_decimal_text:VARCHAR unit:VARCHAR quantum_shares:VARCHAR
        raw_value_text:VARCHAR value_state:VARCHAR quality_reasons_json:VARCHAR
        carry_forward_authorized:BOOLEAN
    """),
    "reference.factor_observations": columns("""
        observation_id:VARCHAR instrument_id:BIGINT security:VARCHAR source:VARCHAR
        source_revision_id:VARCHAR trade_date:VARCHAR factor_value:VARCHAR basis:VARCHAR
        carry_forward_authorized:BOOLEAN
    """),
    "reference.security_status": columns("""
        observation_id:VARCHAR instrument_id:BIGINT security:VARCHAR source:VARCHAR
        source_revision_id:VARCHAR trade_date:VARCHAR is_st:VARCHAR is_paused:VARCHAR
        value_states_json:VARCHAR status_role:VARCHAR
    """),
    "corporate_action.events": columns("""
        event_id:VARCHAR instrument_id:BIGINT security:VARCHAR source:VARCHAR
        source_revision_id:VARCHAR event_type:VARCHAR source_status:VARCHAR implemented:BOOLEAN
        announcement_date:VARCHAR implementation_announcement_date:VARCHAR report_period:VARCHAR
        record_date:VARCHAR ex_date:VARCHAR payment_date:VARCHAR shares_tradable_date:VARCHAR
        base_date:VARCHAR base_shares:VARCHAR gross_cash:VARCHAR net_cash:VARCHAR
        bonus_ratio:VARCHAR capitalization_ratio:VARCHAR tax_basis:VARCHAR
        source_fields_json:VARCHAR quality_reasons_json:VARCHAR
    """),
}
KEYS = {table: next(iter(fields)) for table, fields in TABLES.items()}
ENDPOINTS = {
    "report": ("financial.report_versions", "report_version_id"),
    "extraction": ("financial.extractions", "extraction_version_id"),
    "observation": ("financial.observations", "observation_id"),
    "disclosure": ("financial.disclosure_events", "event_id"),
    "capture": ("meta.financial_captures", "capture_id"),
    "calendar": ("reference.exchange_calendar", "observation_id"),
    "limit": ("reference.price_limits", "observation_id"),
    "share": ("reference.share_observations", "observation_id"),
    "factor": ("reference.factor_observations", "observation_id"),
    "status": ("reference.security_status", "observation_id"),
    "action": ("corporate_action.events", "event_id"),
}
AMOUNTS = {"CNY", "shares", "percent", "ratio", "CNY/share_source_documentation"}


def decimal_text(value):
    if value is None or value == "" or value == "--":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return format(result, "f") if result.is_finite() else None


def raw_type(value, present=True):
    if not present:
        return "absent"
    if value is None:
        return "null"
    return "string" if isinstance(value, str) else type(value).__name__


def context(row, issuer, unit, column_role=None):
    dimensions = row.get("dimensions", {})
    require(isinstance(dimensions, dict), "FIN_CONTEXT", "Dimensions require typed unique keys")
    for key, value in dimensions.items():
        require(isinstance(key, str) and isinstance(value, dict)
                and set(value) == {"type", "value"}
                and value["type"] in {"string", "number", "boolean", "unknown", "not_applicable"},
                "FIN_CONTEXT", "Untyped dimension")
    value = dict(issuer_id=issuer, statement=row.get("statement"),
                 period_start=row.get("period_start"), period_end=row.get("period_end"),
                 basis=row.get("basis"), scope=row.get("scope"), currency=row.get("currency"),
                 column_role=column_role or row.get("column_role", "source_unspecified"),
                 unit=unit, dimensions=dimensions, version=CONTEXT_VERSION)
    return identity("context", value), value


class Projection:
    """One bounded source task. Registries are supplied by the release builder."""

    def __init__(self, *, instruments, register_issuer, mapping_sha256):
        self.instruments = instruments
        self.register_issuer = register_issuer
        self.mapping_sha256 = mapping_sha256
        self.rows = defaultdict(list)

    def add(self, table, **row):
        require(set(row) <= set(TABLES[table]), "FIN_SCHEMA", "Unknown target column")
        for key, value in row.items():
            expected = TABLES[table][key]
            require(value is None or (isinstance(value, str) if expected == "VARCHAR" else
                                     type(value) is bool if expected == "BOOLEAN" else type(value) is int),
                    "FIN_SCHEMA", "Unexpected value type for " + table + "." + key)
        row = {key: row.get(key) for key in TABLES[table]}
        self.rows[table].append(row)
        return row[KEYS[table]]

    def link(self, left_type, left, right_type, right, relation, **extra):
        value = dict(from_type=left_type, from_id=left, to_type=right_type, to_id=right,
                     relation=relation, **extra)
        self.add("financial.links", link_id=identity("link", value), **value)

    def quarantine(self, row, capture, reason):
        self.add("quality.financial_quarantine",
                 quarantine_id=identity("quarantine", capture, row, reason),
                 capture_id=capture, source_schema=row.get("schema"),
                 source_locator=row.get("source_locator"), reason=reason,
                 normalized_record_json=text(row))

    def report(self, row, task, capture, parser):
        source = row["source"]
        issuer = self.register_issuer(source, row["security"], row.get("period_end"), task)
        fields = row["fields"]
        projection = row.get("content_projection") or (
            sorted(x["source_field"] or "" for x in fields) if isinstance(fields, list) else sorted(fields))
        rid = identity("report", source, issuer, row["statement"], row["period_end"],
                       row["source_revision_id"], projection)
        self.add("financial.report_versions", report_version_id=rid, issuer_id=issuer,
                 source=source, statement=row["statement"], period_end=row["period_end"],
                 source_revision_id=row["source_revision_id"], projection_identity=identity("projection", projection),
                 version_kind=row.get("version_kind"), source_metadata_json=text(row.get("source_metadata", {
                     key: row.get(key) for key in ("report_type", "source_update_flag", "company_type", "end_type")
                 })))
        eid = identity("extraction", rid, parser, self.mapping_sha256, VERSION)
        self.add("financial.extractions", extraction_version_id=eid, report_version_id=rid,
                 parser_sha256=parser, mapping_sha256=self.mapping_sha256, schema_version=VERSION)
        self.link("report", rid, "capture", capture, "captured_in",
                  issuer_id=issuer, period_end=row["period_end"], source_locator=row.get("source_locator"))
        self.link("extraction", eid, "report", rid, "extracts", issuer_id=issuer)
        for role in ("announcement_date", "actual_announcement_date"):
            announcement = row.get(role)
            if not announcement:
                continue
            event = identity("disclosure", source, issuer, rid, role, announcement)
            self.add("financial.disclosure_events", event_id=event, issuer_id=issuer, source=source,
                     published_date=announcement, precision="date", event_kind="provider_date_claim:" + role,
                     visibility_basis="provider_date_only_not_historical_availability")
            self.link("report", rid, "disclosure", event, "date_claim", issuer_id=issuer,
                      period_end=row["period_end"])
        if isinstance(fields, list):
            mapped = {v["source_ordinal"]: (name, v) for name, v in row.get("metrics", {}).items()}
            items = [(x.get("source_field"), x, mapped.get(x["ordinal"])) for x in fields]
        else:
            items = [(name, dict(value, ordinal=i, source_title=name), (value.get("canonical_field"), value))
                     for i, (name, value) in enumerate(sorted(fields.items()))]
        for name, item, metric in items:
            field_row = dict(row, **item.get("context", {}))
            metric_name, metric_value = metric or (None, {})
            unit = metric_value.get("unit")
            verified_unit = unit if unit in AMOUNTS else None
            literal = row.get("_field_literals", {}).get(str(item["ordinal"]) if isinstance(fields, list) else name)
            raw = literal["value"] if literal else item.get("raw_value", item.get("value"))
            value = decimal_text(item.get("value"))
            state = item.get("state", "unknown")
            reasons = list(row.get("source_issues", []))
            if value is None:
                reasons.append("missing_or_unparsed_value")
            if verified_unit is None:
                reasons.append("unit_unverified")
            if row.get("scope") != "consolidated":
                reasons.append("scope_unverified")
            ctx_id, ctx = context(field_row, issuer, verified_unit)
            locator = "/fields/" + str(item["ordinal"])
            oid = identity("observation", rid, eid, ctx_id, name, locator, item["ordinal"])
            self.add("financial.observations", observation_id=oid, report_version_id=rid,
                     extraction_version_id=eid, context_id=ctx_id, context_version=CONTEXT_VERSION,
                     issuer_id=issuer, statement=field_row["statement"], period_start=field_row.get("period_start"),
                     period_end=field_row["period_end"], basis=field_row.get("basis"), scope=field_row.get("scope"),
                     currency=field_row.get("currency"), column_role=ctx["column_role"],
                     dimensions_json=text(ctx["dimensions"]), raw_context_json=text(ctx),
                     source_field=name, source_title=item.get("source_title"),
                     source_ordinal=item["ordinal"], source_locator=locator,
                     canonical_field=metric_name, raw_value_text=raw if isinstance(raw, str) else text(raw),
                     raw_value_type=literal["kind"] if literal else raw_type(raw), value_decimal_text=value,
                     source_unit=unit or row.get("unit_basis"), canonical_unit=verified_unit,
                     scale_decimal_text="1" if verified_unit else None,
                     unit_evidence="existing_normalizer_explicit_field_unit" if verified_unit else None,
                     canonical_value_decimal_text=value if verified_unit else None,
                     value_state=state, unit_state="explicit" if verified_unit else "unverified",
                     quality_state="usable_current_source" if not reasons and metric_name else "retained",
                     quality_reasons_json=text(reasons))
        return rid

    def reference(self, row, task, capture):
        schema, source = row["schema"], row.get("source", task["kind"])
        security = row.get("security")
        instrument = self.instruments.get(security) if security else None
        require(security is None or instrument is not None,
                "FIN_INSTRUMENT", "Source security absent from frozen market registry")
        native = row.get("observation", {})
        version = row.get("source_revision_id") or identity("source-row", {
            k: v for k, v in row.items() if k not in {"observed_at", "response_sha256", "task_id", "source_locator"}
        })
        common = dict(instrument_id=instrument, security=security, source=source, source_revision_id=version)
        locator = row.get("source_locator") or native.get("locator")
        if schema in {"ad.exchange-calendar-observation.v1", "ad.tushare-calendar-observation.v1"}:
            oid = identity("calendar", source, row["exchange"], row["date"], version)
            previous = row.get("pretrade_date")
            if previous and len(previous) == 8:
                previous = date.fromisoformat(previous).isoformat()
            self.add("reference.exchange_calendar", observation_id=oid, source=source,
                     source_revision_id=version, exchange={"SZSE": "XSHE", "SSE": "XSHG"}.get(row["exchange"], row["exchange"]),
                     date=row["date"], is_open=row["is_open"], previous_session=previous)
            endpoint = "calendar"
        elif schema in {"ad.tushare-limit-observation.v1", "ad.daily-reference-observation.v1"}:
            fields = row["fields"]
            oid = identity("limit", source, security, row["trade_date"], version)
            ts = schema == "ad.tushare-limit-observation.v1"
            self.add("reference.price_limits", observation_id=oid, **common, trade_date=row["trade_date"],
                     up_limit=decimal_text(fields["up_limit" if ts else "high_limit"]["value"]),
                     down_limit=decimal_text(fields["down_limit" if ts else "low_limit"]["value"]),
                     previous_close=fields.get("pre_close", {}).get("value"), unit="CNY/share",
                     value_states_json=text({k: v["state"] for k, v in fields.items()}))
            endpoint = "limit"
            if not ts:
                status_id = identity("status", source, security, row["trade_date"], version)
                self.add("reference.security_status", observation_id=status_id, **common,
                         trade_date=row["trade_date"], is_st=decimal_text(fields["is_st"]["value"]),
                         is_paused=decimal_text(fields["is_paused"]["value"]),
                         value_states_json=text({k: v["state"] for k, v in fields.items()}),
                         status_role=row["status_role"])
                self.link("status", status_id, "capture", capture, "captured_in", source_locator=locator)
        elif schema == "ad.tushare-factor-observation.v1":
            oid = identity("factor", source, security, version)
            self.add("reference.factor_observations", observation_id=oid, **common,
                     trade_date=row["trade_date"], factor_value=row["fields"]["adj_factor"]["value"],
                     basis=row["basis"], carry_forward_authorized=False)
            endpoint = "factor"
        elif schema == "ad.corporate-source-observation.v1" and row["category"] == "shares":
            from ashare_data.financials.expansion_access import share_overlap
            fields = native.get("fields", {})
            for field, value in fields.items():
                if not field.endswith("_shares"):
                    continue
                flags = list(row.get("quality_flags", []))
                if field.startswith("float_") and share_overlap(row):
                    state = "share_class_overlap_unresolved"
                elif value is None:
                    state = "missing_or_quarantined"
                else:
                    state = "source_observed"
                oid = identity("share", source, security, version, field)
                self.add("reference.share_observations", observation_id=oid, **common,
                         effective_date=native.get("date"), announcement_date=native.get("announcement_date"),
                         field=field, value_decimal_text=decimal_text(value), unit="shares",
                         quantum_shares=native.get("source_quantum_shares", {}).get(field),
                         raw_value_text=text(native.get("native_fields")), value_state=state,
                         quality_reasons_json=text(flags + native.get("field_gaps", [])),
                         carry_forward_authorized=False)
                self.link("share", oid, "capture", capture, "captured_in", source_locator=locator)
            # Negative cells and all other unparsed metadata remain inspectable.
            if native.get("field_gaps"):
                self.quarantine(row, capture, "source_share_field_gaps")
            return True
        elif schema == "ad.tushare-dividend-version.v1" or (
            schema == "ad.corporate-source-observation.v1" and row["category"] in {"actions", "nonimplemented"}
        ):
            ts = schema == "ad.tushare-dividend-version.v1"
            fields = {k: v.get("value") for k, v in row["fields"].items()} if ts else native.get("fields", {})
            oid = identity("action", source, security, version)
            self.add("corporate_action.events", event_id=oid, **common,
                     event_type="distribution" if ts else native.get("type"),
                     source_status=row.get("source_status") if ts else native.get("source_status"),
                     implemented=row.get("implemented") if ts else row["category"] == "actions",
                     announcement_date=fields.get("ann_date" if ts else "announcement_date"),
                     implementation_announcement_date=fields.get("imp_ann_date"),
                     report_period=fields.get("end_date"), record_date=fields.get("record_date"),
                     ex_date=fields.get("ex_date"), payment_date=fields.get("pay_date" if ts else "payment_date"),
                     shares_tradable_date=fields.get("div_listdate" if ts else "new_shares_tradable_date"),
                     base_date=fields.get("base_date"), base_shares=fields.get("base_share"),
                     gross_cash=fields.get("cash_div_tax" if ts else "cash_per_share"),
                     net_cash=fields.get("cash_div") if ts else None,
                     bonus_ratio=fields.get("stk_bo_rate" if ts else "bonus_share_ratio"),
                     capitalization_ratio=fields.get("stk_co_rate" if ts else "capitalization_share_ratio"),
                     tax_basis="gross_and_after_tax_source_claims" if ts else native.get("cash_basis"),
                     source_fields_json=text(row["fields"] if ts else native),
                     quality_reasons_json=text(row.get("quality_flags", [])))
            endpoint = "action"
        else:
            self.quarantine(row, capture, "schema_or_category_requires_mapping")
            return False
        self.link(endpoint, oid, "capture", capture, "captured_in", source_locator=locator)
        return True

    def project(self, row, task, capture, parser):
        if row["schema"] in {"ad.financial-source-report.v1", "ad.tushare-financial-version.v1"}:
            self.report(row, task, capture, parser)
            return True
        return self.reference(row, task, capture)
