"""Project frozen action tables through the existing corporate reconciliation gate.

A completed parsed provider ledger is source-scoped retrospective evidence, not
proof of all action types or historical announcement visibility. Its imported
query range, capture, parse counts and quarantine counts must support the window.
"""

from decimal import Decimal
import json

from ashare_data.corporate_actions.history import CorporateHistoryView, DATE_FIELDS, VALUE_FIELDS
from ashare_data.corporate_actions.identity import associate_actions
from ashare_data.corporate_actions.coverage import LEGACY_ROLES, declared_tables
from ashare_data.storage.market_release import MarketRelease

_CMES_EXCLUDED_STAGES = frozenset({
    "预案",
    "股东大会通过",
    "预披露",
    "股东大会未通过",
    "停止实施",
    "未通过",
})


class ReleasedCorporateView(CorporateHistoryView):
    """Internal adapter over already hash-verified, release-bound normalized tables."""

    def __init__(self, reader, securities, start, end):
        self.binding_sha256 = reader.release.sha256
        self._scope = {sid: dict(security=sid, start=start, end=end) for sid in securities}
        self._data = dict(sources={}, source_coverage={}, shares=[], security_boundaries=[])
        cmes_release = (
            isinstance(getattr(reader, "release", None), MarketRelease)
            and bool(reader.release.manifest.get("cmes"))
        )
        coverage = (
            []
            if cmes_release
            else reader.query(
                "meta.financial_coverage",
                "security IN (" + ",".join("?" for _ in securities) + ")",
                securities,
            )
        )
        for row in coverage:
            native = json.loads(row["coverage_json"])
            declaration = native.get("corporate_coverage")
            if row["source"] not in LEGACY_ROLES and declaration is None:
                continue  # Financial statements do not attest corporate action coverage.
            with reader.release.metadata() as db:
                batch = db.execute("SELECT body FROM data_batches WHERE batch_key=?", [row["batch_key"]]).fetchone()
            manifest = json.loads(batch[0]).get("manifest", {}) if batch else {}
            captures = reader.query("meta.financial_captures", "batch_key=? AND task_id=?",
                                    [row["batch_key"], row["task_id"]])
            in_scope = (manifest.get("target_start", "9999") <= start
                        and end <= manifest.get("target_end", "0000"))
            complete = (row["source_state"] == "complete" and row["quarantined_rows"] == 0
                        and row["mapped_rows"] == row["input_rows"]
                        and native.get("source_status") == "parsed_provider_tables"
                        and not native.get("counts", {}).get("unparsed_records")
                        and in_scope)
            if declaration is not None:
                tables = declared_tables(declaration)
                complete = complete and tables is not None and (
                    declaration["start"] <= start <= end <= declaration["end"]
                ) and all(t["row_count"] <= row["input_rows"] for t in tables.values())
            elif row["source"] == "sina_actions":
                tables = native.get("table_states") or {}
                complete = complete and all(
                    (tables.get(name, {}).get("state") == "business_rows_observed"
                     or (tables.get(name, {}).get("state") == "provider_no_rows_reported"
                         and tables[name].get("explicit_empty_marker") is True))
                    for name in ("sharebonus_1", "sharebonus_2")
                )
            source = dict(
                security=row["security"], kind=row["source"],
                status="parsed_provider_tables" if complete else "outside_requested_window"
                if not in_scope else "partial_provider_tables",
                unparsed_records=native.get("counts", {}).get("unparsed_records", 0),
                native_action_rows=native.get("counts", {}).get("actions", 0),
                nonimplemented_rows=native.get("counts", {}).get("nonimplemented", 0),
                source_coverage_id=row["coverage_id"], batch_key=row["batch_key"],
                query_start=manifest.get("target_start"), query_end=manifest.get("target_end"),
                captures=captures, parsed_coverage=native, quarantined_rows=row["quarantined_rows"],
                absence_proved=False, full_history_proven=False,
                **({"corporate_coverage": declaration, "coverage_observed_at": max(
                    (c["observed_at"] for c in captures if c["source"] == row["source"]
                     and c["source_state"] == "complete" and c["observed_at"]), default=None,
                )} if declaration is not None else {}),
            )
            self._data["source_coverage"][row["coverage_id"]] = source
        observations = []
        cmes_counts = {sid: {"actions": 0, "nonimplemented": 0} for sid in securities}
        for row in reader.selected("corporate_action.events", securities):
            # Plans/cancellations/non-distributions remain in the immutable formal table;
            # they must not become economic entitlements in a retrospective account.
            if row["implemented"] is False:
                if row["source"] == "cmes" and row["security"] in cmes_counts:
                    cmes_counts[row["security"]]["nonimplemented"] += 1
                continue
            if (row["source"] == "cmes" and row["implemented"] is None
                    and row.get("source_status") in _CMES_EXCLUDED_STAGES):
                # CMES preserves explicit pre-implementation stages as null here.
                # Unknown stages still need the implementation-status gate below.
                if row["security"] in cmes_counts:
                    cmes_counts[row["security"]]["nonimplemented"] += 1
                continue
            if row["source"] == "cmes" and row["security"] in cmes_counts:
                cmes_counts[row["security"]]["actions"] += 1
            native = json.loads(row["source_fields_json"])
            observation = _observation(row, native)
            if row["implemented"] is not True:
                observation.setdefault("source_issues", []).append(
                    dict(field="implementation_status", reason="source_implementation_unknown")
                )
            observation["source_reference"] = reader.ref(row, "corporate_action.events")
            self._data["sources"][observation["source_id"]] = dict(
                kind=row["source"], security=row["security"]
            )
            observations.append(observation)
        self._actions = associate_actions(observations, self._data["sources"])
        if cmes_release and any(
            x.get("kind") == "financial_table" and x.get("table") == "corporate_action.events"
            for x in reader.release.manifest.get("evidence", [])
        ):
            for item in _cmes_window_coverage(
                reader.release, securities, start, end, observations, cmes_counts
            ):
                self._data["source_coverage"][item["source_coverage_id"]] = item

    def coverage(self, securities, *, start, end):
        result = super().coverage(securities, start=start, end=end)
        result["contract"] = "released-retrospective-corporate.v1"
        # Share counts are not a prerequisite for cash/stock entitlement accounting.
        # The captured structure ledger is checked above; this adapter does not expose
        # a daily share register or invent point-in-time float shares.
        for row in result["rows"]:
            row["share_observations_not_requested"] = True
        return result


def _observation(row, native):
    if isinstance(native.get("fields"), dict):
        fields = {k: v for k, v in native["fields"].items()
                  if k in DATE_FIELDS | VALUE_FIELDS | {"successor_security"}}
        if row.get("event_type") == "distribution":
            for vname in ("cash_per_share", "bonus_share_ratio", "capitalization_share_ratio"):
                if fields.get(vname) is None:
                    fields[vname] = "0"
    else:
        ex_date = row.get("ex_date")
        gross_cash = row.get("gross_cash")
        bonus_ratio = row.get("bonus_ratio")
        cap_ratio = row.get("capitalization_ratio")
        record_date = row.get("record_date")
        payment_date = row.get("payment_date")
        tradable_date = row.get("shares_tradable_date")
        credit_date = None
        announcement_date = row.get("implementation_announcement_date") or row.get("announcement_date")
        if row.get("event_type") == "distribution":
            if gross_cash is None and row.get("source") == "cmes":
                gross_cash = "0"
            if bonus_ratio is None and row.get("source") == "cmes":
                bonus_ratio = "0"
            if cap_ratio is None and row.get("source") == "cmes":
                cap_ratio = "0"
            if row.get("source") == "cmes" and ex_date is not None:
                payment_date = payment_date or ex_date
                announcement_date = announcement_date or ex_date
                if Decimal(str(bonus_ratio or "0")) > 0 or Decimal(str(cap_ratio or "0")) > 0:
                    tradable_date = tradable_date or ex_date
                    credit_date = tradable_date or ex_date
        fields = {
            "announcement_date": announcement_date,
            "plan_announcement_date": row.get("announcement_date") or announcement_date,
            "record_date": record_date,
            "ex_date": ex_date,
            "payment_date": payment_date,
            "new_shares_tradable_date": tradable_date,
            "cash_per_share": gross_cash,
            "bonus_share_ratio": bonus_ratio,
            "capitalization_share_ratio": cap_ratio,
        }
        if credit_date is not None:
            fields["share_credit_date"] = credit_date
    return dict(
        observation_id=row["event_id"], security=row["security"], type=row["event_type"],
        source_id=row["source"] + ":" + row["security"], locator=row["event_id"],
        fields=fields, cash_basis="gross_pre_tax_per_old_eligible_share",
        source_status="source_observed", native_fields=native,
        source_issues=native.get("source_issues", []),
    )


def _cmes_window_coverage(release, securities, start, end, observations, cmes_counts):
    with release.metadata() as db:
        cal = [r[0] for r in db.execute("SELECT date FROM calendar ORDER BY date")]
    cov_start = min(cal[0], start) if cal else start
    cov_end = max(cal[-1], end) if cal else end
    items = []
    for sid in securities:
        counts = cmes_counts.get(sid, {"actions": 0, "nonimplemented": 0})
        action_count = counts["actions"]
        declaration = {
            "schema": "ad.corporate-coverage.v1",
            "start": cov_start,
            "end": cov_end,
            "tables": {
                "distributions": {
                    "source_table": "corporate_action.events",
                    "row_count": action_count,
                    "state": "business_rows_observed" if action_count > 0 else "provider_no_rows_reported",
                    "explicit_empty_marker": True,
                },
                "rights_issues": {
                    "source_table": "corporate_action.events",
                    "row_count": 0,
                    "state": "provider_no_rows_reported",
                    "explicit_empty_marker": True,
                },
                "capital_changes": {
                    "source_table": "reference.cmes_daily_metrics",
                    "row_count": 0,
                    "state": "provider_no_rows_reported",
                    "explicit_empty_marker": True,
                },
            },
        }
        coverage_id = f"cmes-corporate:{release.sha256}:{sid}"
        items.append(
            dict(
                security=sid,
                kind="cmes",
                status="parsed_provider_tables",
                unparsed_records=0,
                native_action_rows=action_count,
                nonimplemented_rows=counts["nonimplemented"],
                source_coverage_id=coverage_id,
                batch_key="cmes-release:" + str(release.release_id),
                query_start=cov_start,
                query_end=cov_end,
                captures=[],
                parsed_coverage={
                    "source_status": "parsed_provider_tables",
                    "counts": counts,
                    "corporate_coverage": declaration,
                },
                quarantined_rows=0,
                absence_proved=False,
                full_history_proven=False,
                corporate_coverage=declaration,
                coverage_observed_at=cov_end + "T15:00:00+08:00",
            )
        )
    return items
