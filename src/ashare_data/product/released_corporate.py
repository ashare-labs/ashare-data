"""Project frozen action tables through the existing corporate reconciliation gate.

A completed parsed provider ledger is source-scoped retrospective evidence, not
proof of all action types or historical announcement visibility. Its imported
query range, capture, parse counts and quarantine counts must support the window.
"""

import json

from ashare_data.corporate_actions.history import CorporateHistoryView, DATE_FIELDS, VALUE_FIELDS
from ashare_data.corporate_actions.identity import associate_actions


class ReleasedCorporateView(CorporateHistoryView):
    """Internal adapter over already hash-verified, release-bound normalized tables."""

    def __init__(self, reader, securities, start, end):
        self.binding_sha256 = reader.release.sha256
        self._scope = {sid: dict(security=sid, start=start, end=end) for sid in securities}
        self._data = dict(sources={}, source_coverage={}, shares=[], security_boundaries=[])
        coverage = reader.query(
            "meta.financial_coverage",
            "security IN (" + ",".join("?" for _ in securities) + ") AND source IN ('sina_actions','sina_structure')",
            securities,
        )
        for row in coverage:
            native = json.loads(row["coverage_json"])
            with reader.release.metadata() as db:
                batch = db.execute("SELECT body FROM data_batches WHERE batch_key=?", [row["batch_key"]]).fetchone()
            manifest = json.loads(batch[0]).get("manifest", {}) if batch else {}
            captures = reader.query("meta.financial_captures", "batch_key=? AND task_id=?",
                                    [row["batch_key"], row["task_id"]])
            capture_after_window = any(c["source_state"] == "complete" and c["observed_at"]
                                       and c["observed_at"][:10] > end for c in captures)
            in_scope = (manifest.get("target_start", "9999") <= start
                        and end <= manifest.get("target_end", "0000"))
            complete = (row["source_state"] == "complete" and row["quarantined_rows"] == 0
                        and row["mapped_rows"] == row["input_rows"]
                        and native.get("source_status") == "parsed_provider_tables"
                        and not native.get("counts", {}).get("unparsed_records")
                        and in_scope and capture_after_window)
            if row["source"] == "sina_actions":
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
                if not in_scope or not capture_after_window else "partial_provider_tables",
                unparsed_records=native.get("counts", {}).get("unparsed_records", 0),
                native_action_rows=native.get("counts", {}).get("actions", 0),
                nonimplemented_rows=native.get("counts", {}).get("nonimplemented", 0),
                source_coverage_id=row["coverage_id"], batch_key=row["batch_key"],
                query_start=manifest.get("target_start"), query_end=manifest.get("target_end"),
                captures=captures, parsed_coverage=native, quarantined_rows=row["quarantined_rows"],
                absence_proved=False, full_history_proven=False,
            )
            self._data["source_coverage"][row["coverage_id"]] = source
        observations = []
        for row in reader.selected("corporate_action.events", securities):
            # Plans/cancellations/non-distributions remain in the immutable formal table;
            # they must not become economic entitlements in a retrospective account.
            if row["implemented"] is False:
                continue
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
    else:
        fields = {
            "announcement_date": row["implementation_announcement_date"],
            "plan_announcement_date": row["announcement_date"],
            "record_date": row["record_date"], "ex_date": row["ex_date"],
            "payment_date": row["payment_date"],
            "new_shares_tradable_date": row["shares_tradable_date"],
            "cash_per_share": row["gross_cash"], "bonus_share_ratio": row["bonus_ratio"],
            "capitalization_share_ratio": row["capitalization_ratio"],
        }
    return dict(
        observation_id=row["event_id"], security=row["security"], type=row["event_type"],
        source_id=row["source"] + ":" + row["security"], locator=row["event_id"],
        fields=fields, cash_basis="gross_pre_tax_per_old_eligible_share",
        source_status="source_observed", native_fields=native,
        source_issues=native.get("source_issues", []),
    )
