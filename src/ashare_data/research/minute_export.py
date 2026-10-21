"""Bounded source-labelled minute export, without execution or PIT certification.

The caller supplies separately reviewed calendar/state/action evidence. This API
only projects a published Store snapshot; AQ owns execution-capability admission.
"""

from copy import deepcopy
from datetime import timedelta
from decimal import Context, localcontext
import json

from ashare_data.core.model import digest, require, timestamp
from ashare_data.research.minute_policy import row_use_policy

SCHEMA = "ad-minute-research.v1"


def export_minute_research(snapshot, metadata):
    """Return a JSON document preserving source fields and unknown source clocks.

    End labels are interpreted under the explicit ``source-labelled-closed-bars.v1``
    research model. Computed Store intervals remain in ``evidence.ad_row``; they
    are not promoted to observed exchange intervals. No fetching or file writing.
    """
    doc = deepcopy(metadata)
    require(
        snapshot.snapshot_id and type(doc.get("real_market_data")) is bool,
        "MINUTE_RESEARCH_SNAPSHOT_REQUIRED",
        "A published snapshot and explicit real/synthetic flag are required",
    )
    require(
        not (
            {"minute", "unavailable_minutes", "producer", "content_sha256", "schema"} & doc.keys()
        ),
        "MINUTE_RESEARCH_RESERVED_FIELDS",
        "The exporter owns schema, producer, minute rows and content identity",
    )
    ids, days = doc["securities"], doc["days"]
    # This is an explicit, separately admitted AQ research capability. The
    # exporter still grants no execution permission or source certification.
    long_model = doc.get("research", {}).get("profile") == "source-labelled-corporate-date-model.v1"
    require(
        0 < len(ids) <= 20
        and len(set(ids)) == len(ids)
        and 0 < len(days) <= (512 if long_model else 60)
        and days == sorted(set(days))
        and set(doc["sessions"]) == set(days),
        "MINUTE_RESEARCH_BOUNDED_SCOPE",
        "Choose unique securities within the declared bounded research capability",
    )
    lineage = snapshot.lineage()
    sources = {b["source"]["id"]: b["source"] for b in lineage["batches"]}
    require(
        len(sources) == len(lineage["batches"]),
        "MINUTE_RESEARCH_SOURCE_ID_CONFLICT",
        "Distinct batches must have distinct source identities",
    )
    annotations = doc.pop("bar_annotations", {})
    gap_policy = doc.get("gap_policy", "reject")
    require(
        gap_policy in {"reject", "explicit_unavailable"},
        "MINUTE_RESEARCH_GAP_POLICY",
        "Unknown gap policy",
    )
    consumed, rows, gaps = set(), [], []
    policy = "research" if doc["real_market_data"] else "synthetic"
    for day in days:
        sessions = doc["sessions"][day]
        require(
            0 < len(sessions) <= 4,
            "MINUTE_RESEARCH_BOUNDED_SCOPE",
            "At most four intervals per day",
        )
        for start, end in sessions:
            a, b = timestamp(start, minute=True), timestamp(end, minute=True)
            require(
                a.date().isoformat() == b.date().isoformat() == day
                and timedelta(0) < b - a <= timedelta(minutes=240),
                "MINUTE_RESEARCH_BOUNDED_SCOPE",
                "Invalid bounded session interval",
            )
            # Native engines may alter the process Decimal context. Source
            # projection must not silently inherit their lower precision.
            with localcontext(Context(prec=28)):
                read = snapshot.bars(
                    ids,
                    start,
                    end,
                    quality=policy,
                    strict=gap_policy == "reject",
                    as_of=None,
                    adjustment=None,
                    frequency="1m",
                )
            require(
                read["units"] == {"price": "CNY", "volume": "shares", "amount": "CNY"},
                "MINUTE_RESEARCH_UNITS",
                "Raw CNY prices/amount and share volume required",
            )
            coverage = read["coverage"]
            require(
                not any(
                    coverage[k]
                    for k in ("unknown_calendar_dates", "unexpected", "excluded", "duplicate_keys")
                ),
                "MINUTE_RESEARCH_UNRESOLVED_COVERAGE",
                "Only explicit missing quote slots may continue",
            )
            for missing in coverage["missing"]:
                label = timestamp(missing["bar_start"], minute=True) + timedelta(minutes=1)
                known = [
                    r
                    for r in coverage.get("unavailable_quotes", [])
                    if r["security"] == missing["symbol"] and timestamp(r["source_label"]) == label
                ]
                require(len(known) <= 1, "MINUTE_RESEARCH_GAP_BINDING", "Duplicate missing quote")
                reason = "no_quote_in_selected_snapshot"
                if known:
                    quote = known[0]
                    require(
                        quote.get("price_status") == "invalid"
                        and quote.get("missing_reason") == "confirmed_invalid_price"
                        and set(quote.get("missing_fields", []))
                        == {"open", "high", "low", "close", "volume", "amount"}
                        and all(quote.get(k) is None for k in quote["missing_fields"])
                        and quote.get("use_decision", {}).get("execution") == "deny"
                        and quote.get("use_decision", {}).get("fresh_valuation") == "deny"
                        and quote["source_contract_sha256"] == digest(sources[quote["source_id"]]),
                        "MINUTE_RESEARCH_GAP_BINDING",
                        "Missing quote lacks bound invalidity evidence",
                    )
                    reason = quote["missing_reason"]
                gaps.append(
                    {
                        "security": missing["symbol"],
                        "source_label": label.isoformat(),
                        "reason": reason,
                        "execution": "deny",
                        "fresh_valuation": "deny",
                        "evidence": {
                            "snapshot_id": snapshot.snapshot_id,
                            "coverage": coverage,
                            "coverage_sha256": digest(coverage),
                        },
                    }
                )
            for original in read["rows"]:
                source = sources[original["source_id"]]
                require(
                    source["label"] == "end" and original["available_at"] is None,
                    "MINUTE_RESEARCH_CLOCK_MODEL",
                    "This schema requires end labels with unknown historical availability",
                )
                require(
                    (source["kind"] != "synthetic") == doc["real_market_data"],
                    "MINUTE_RESEARCH_SOURCE_KIND",
                    "Source kind conflicts with real-data flag",
                )
                raw = json.loads(json.dumps(original, default=str, allow_nan=False))
                label = timestamp(raw["source_label"], minute=True)
                row = {
                    "security": raw["symbol"],
                    "source_label": raw["source_label"],
                    **{
                        k: str(raw[k]) for k in ("open", "high", "low", "close", "volume", "amount")
                    },
                    "quality": raw["quality"],
                    "use_policy": row_use_policy(raw, source),
                    "bar_start": None,
                    "bar_end": None,
                    "available_at": None,
                    "historical_available_at": None,
                    "replay_bar_start": (label - timedelta(minutes=1)).isoformat(),
                    "replay_bar_end": label.isoformat(),
                    "evidence": {
                        "snapshot_id": snapshot.snapshot_id,
                        "source_id": raw["source_id"],
                        "source_contract_sha256": digest(source),
                        "row_sha256": digest(raw),
                        "ad_row": raw,
                    },
                }
                key = row["security"] + "|" + row["source_label"]
                if key in annotations:
                    annotation = annotations[key]
                    require(
                        set(annotation) <= {"session_phase", "phase_evidence"},
                        "MINUTE_RESEARCH_ANNOTATION_FIELDS",
                        "Annotations cannot replace source facts",
                    )
                    row.update(annotation)
                    consumed.add(key)
                rows.append(row)
    require(
        consumed == set(annotations),
        "MINUTE_RESEARCH_ANNOTATION_OUT_OF_SCOPE",
        "Every annotation must identify an exported row",
    )
    from ashare_data import __version__

    doc.update(
        schema=SCHEMA,
        producer={
            "name": "ashare-data",
            "version": __version__,
            "snapshot_id": snapshot.snapshot_id,
        },
        minute=rows,
    )
    if gap_policy == "explicit_unavailable":
        doc["unavailable_minutes"] = gaps
    doc.setdefault("source_refs", {})["ad_lineage"] = lineage
    doc["content_sha256"] = digest(doc)
    return doc
