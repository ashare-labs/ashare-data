"""Seal captured free-source history and explicitly reviewed document facts.

Offline and deterministic. Input bytes are copied, hashed, and never modified.
The resulting package does not use or write the shared local daily database.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from ashare_data.corporate_actions.history import (
    CorporateHistoryView,
    SCHEMA,
    DATA_SCHEMA,
    intersects,
    unlocated,
    event_key,
)
from ashare_data.corporate_actions.identity import associate_actions
from ashare_data.corporate_actions.sources import PARSERS
from ashare_data.core.model import timestamp


def encode(value):
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def write_fixed(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError(f"Refusing to replace fixed output {path}")
    else:
        with path.open("xb") as stream:
            stream.write(raw)


def build(captures, curation_path, scope_path, output):
    captures, curation_path, scope_path, output = map(
        Path, (captures, curation_path, scope_path, output)
    )
    scope = json.loads(scope_path.read_text())
    scopes = {r["security"]: r for r in scope["securities"]}
    curation = json.loads(curation_path.read_text())
    if curation["schema"] != "ad.corporate-history-curation.v1":
        raise ValueError("Unsupported document curation")
    inventory, sources, coverage, failures = {}, {}, {}, []

    def bind(raw, suffix="bin"):
        name = "evidence/" + sha(raw) + "." + suffix
        write_fixed(output / name, raw)
        inventory[name] = dict(path=name, sha256=sha(raw), bytes=len(raw))
        return name

    bind(scope_path.read_bytes(), "json")
    bind(curation_path.read_bytes(), "json")
    all_actions, all_shares, other = [], [], []

    def observation(row, source_id):
        row = deepcopy(row)
        observed_at = max(
            (
                sources[s]["observed_at"]
                for s in [source_id] + [r["source_id"] for r in row.get("supporting_documents", [])]
            ),
            key=timestamp,
        )
        row.update(source_id=source_id, observed_at=observed_at, historical_available_at=None)
        row["observation_id"] = sha(encode({"source_sha256": sources[source_id]["sha256"], **row}))
        return row

    for path in sorted(captures.glob("*.receipt.json")):
        raw_receipt = path.read_bytes()
        r = json.loads(raw_receipt)
        receipt = bind(raw_receipt, "json")
        q = r["request"]
        if Path(r["body"]).name != r["body"]:
            raise ValueError("Capture body must be a local filename")
        if not r["ok"]:
            failed_body = captures / r["body"]
            raw = failed_body.read_bytes() if failed_body.exists() else b""
            if len(raw) != r["bytes"] or sha(raw) != r["sha256"]:
                raise ValueError("Failed capture hash/size mismatch")
            failures.append(
                dict(
                    request=q,
                    receipt=receipt,
                    body=bind(raw),
                    http_status=r["http_status"],
                    error=r["error"],
                )
            )
            continue
        source_id = q["id"]
        raw = (captures / r["body"]).read_bytes()
        if len(raw) != r["bytes"] or sha(raw) != r["sha256"]:
            raise ValueError("Capture hash/size mismatch")
        if source_id in sources:
            if sources[source_id]["sha256"] != sha(raw):
                raise ValueError("Multiple successful source versions require separate packages")
            continue
        sources[source_id] = dict(
            kind=q["kind"],
            security=q.get("security"),
            url=q["url"],
            body=bind(raw),
            receipt=receipt,
            sha256=sha(raw),
            observed_at=r["received_at"],
        )
        if q["kind"] not in PARSERS:
            if q["kind"] == "issuer_pdf" and not raw.startswith(b"%PDF-"):
                failures.append(dict(request=q, receipt=receipt, reason="HTTP 200 body is not PDF"))
            continue
        try:
            parsed = PARSERS[q["kind"]](raw, q["security"])
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            failures.append(dict(request=q, receipt=receipt, reason=str(exc)))
            coverage[source_id] = dict(
                security=q["security"],
                kind=q["kind"],
                status="parse_failed",
                absence_proved=False,
                native_action_rows=None,
                native_share_rows=None,
                nonimplemented_rows=None,
                unparsed_records=[],
                parse_error=str(exc),
            )
            continue
        coverage[source_id] = dict(
            security=q["security"],
            kind=q["kind"],
            status=parsed["source_status"],
            native_action_rows=len(parsed["actions"]),
            native_share_rows=len(parsed["shares"]),
            nonimplemented_rows=len(parsed["nonimplemented"]),
            absence_proved=False,
            unparsed_records=parsed.get("unparsed_records", []),
            table_states=parsed.get("table_states", {}),
        )
        all_actions += [observation(row, source_id) for row in parsed["actions"]]
        all_shares += [observation(row, source_id) for row in parsed["shares"]]
        other += [observation(row, source_id) for row in parsed["nonimplemented"]]
    for row in curation["actions"]:
        source_id = row["source_id"]
        if source_id not in sources or sources[source_id]["kind"] != "issuer_pdf":
            raise ValueError("Reviewed document lacks a successful original capture")
        text_path = curation_path.parent / row["document_text"]
        if not text_path.resolve().is_relative_to(curation_path.parent.resolve()):
            raise ValueError("Unsafe document text reference")
        item = {**row, "document_text": bind(text_path.read_bytes(), "txt")}
        item["supporting_documents"] = []
        for doc in row.get("supporting_documents", []):
            if doc["source_id"] not in sources:
                raise ValueError("Unbound supporting document")
            path = curation_path.parent / doc["document_text"]
            if not path.resolve().is_relative_to(curation_path.parent.resolve()):
                raise ValueError("Unsafe supporting text path")
            item["supporting_documents"].append(
                {**doc, "document_text": bind(path.read_bytes(), "txt")}
            )
        all_actions.append(observation(item, source_id))
    for rule in curation.get("event_classifications", []):
        if rule["source_id"] not in sources or sources[rule["source_id"]]["kind"] != "issuer_pdf":
            raise ValueError("Event classification requires a captured issuer document")
        for row in all_actions:
            if (
                row["security"] == rule["security"]
                and row["fields"].get("ex_date") == rule["ex_date"]
            ):
                row["native_type"] = row["type"]
                row["type"] = rule["type"]
                row["classification_evidence"] = rule
                row["observed_at"] = max(
                    row["observed_at"], sources[rule["source_id"]]["observed_at"], key=timestamp
                )
                row["observation_id"] = sha(
                    encode({k: v for k, v in row.items() if k != "observation_id"})
                )
    actions, shares, excluded = [], [], []
    associated = associate_actions(all_actions, sources)
    for security, s in scopes.items():
        keys = {
            event_key(a)
            for a in associated
            if a["security"] == security and (intersects(a, s["start"], s["end"]) or unlocated(a))
        }
        kept_ids = {a["observation_id"] for a in associated if event_key(a) in keys}
        # Keep the whole revision family, including a corrected predecessor just
        # outside the requested dates; unknown dates remain explicitly unlocated.
        actions += [a for a in all_actions if a["observation_id"] in kept_ids]
        for kind in ("sina_structure", "sina_float"):
            rows = [
                r
                for r in all_shares
                if r["security"] == security and sources[r["source_id"]]["kind"] == kind
            ]
            baseline = max((r["date"] for r in rows if r["date"] < s["start"]), default=None)
            shares += [
                r for r in rows if s["start"] <= r["date"] <= s["end"] or r["date"] == baseline
            ]
    boundaries = curation.get("security_boundaries", [])
    for boundary in boundaries:
        if boundary["source_id"] not in sources:
            raise ValueError("Unbound security identity boundary")
        for rows, field in ((shares, "date"),):
            removed = [
                r
                for r in rows
                if r["security"] == boundary["security"] and r[field] > boundary["valid_through"]
            ]
            excluded += [
                dict(observation=r, reason="past_security_identity_boundary") for r in removed
            ]
            rows[:] = [r for r in rows if r not in removed]
    data = dict(
        schema=DATA_SCHEMA,
        scope=scope,
        actions=sorted(actions, key=lambda r: r["observation_id"]),
        shares=sorted(shares, key=lambda r: r["observation_id"]),
        sources=sources,
        source_coverage=coverage,
        nonimplemented=[
            r
            for r in other
            if intersects(r, scopes[r["security"]]["start"], scopes[r["security"]]["end"])
        ],
        security_boundaries=boundaries,
        excluded_identity_records=excluded,
        acquisition_failures=failures,
        historical_pit=False,
        market_complete=False,
        limitations=[
            "Provider tables and selected issuer announcements are not a complete all-type action register",
            "Announcement labels and receipt times are separate; original release timestamps/revisions are unproven",
            "Historical share structure is rounded provider observations, not an exact daily CSDC register",
            "Same-date float series disagree; A/B/H source labels can overlap; conflicts are retained and shares_on blocks affected fields",
            "Share issuance, buyback, restricted-to-float changes do not imply holder bonus shares",
            "Cash is gross; deferred tax, investor eligibility, rounding and actual intraday credit are not supplied",
            "Rights subscriptions, reorganization and code changes require specific native account support",
            "No exchange-certified historical price limits are supplied",
            "Raw prices and source factors are not modified",
        ],
    )
    raw = encode(data)
    write_fixed(output / "data.json", raw)
    inventory["data.json"] = dict(path="data.json", sha256=sha(raw), bytes=len(raw))
    binding = dict(
        schema=SCHEMA,
        historical_pit=False,
        execution_permission=False,
        data="data.json",
        files=sorted(inventory.values(), key=lambda r: r["path"]),
    )
    binding_raw = encode(binding)
    write_fixed(output / "binding.json", binding_raw)
    view = CorporateHistoryView(output / "binding.json", expected_binding_sha256=sha(binding_raw))
    coverage_report = [
        view.coverage(s["security"], start=s["start"], end=s["end"])["rows"][0]
        for s in scopes.values()
    ]
    write_fixed(output / "coverage.json", encode(coverage_report))
    result = dict(
        binding=str((output / "binding.json").resolve()),
        sha256=sha(binding_raw),
        action_observations=len(actions),
        share_observations=len(shares),
        sources=len(sources),
        acquisition_failures=len(failures),
    )
    print(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("captures", type=Path)
    p.add_argument("curation", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--scope", type=Path, required=True)
    args = p.parse_args()
    build(args.captures, args.curation, args.scope, args.output)
