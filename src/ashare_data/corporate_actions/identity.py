"""Associate source observations without using a mutable date as entitlement ID.

Explicit reviewed identities/revision links take precedence. Legacy same-date
source association is retained, but unresolved competing associations block use.
No relationship is guessed from a shared record date or free-form prose.
"""

from collections import defaultdict
from copy import deepcopy
import hashlib
import json

from ashare_data.core.model import require


def associate_actions(rows, sources):
    rows = deepcopy(rows)
    by_id = {r["observation_id"]: i for i, r in enumerate(rows)}
    by_source = defaultdict(list)
    for i, row in enumerate(rows):
        by_source[row["source_id"]].append(i)
    parents = list(range(len(rows)))
    links = defaultdict(set)
    phantom = defaultdict(set)
    issues = []

    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    def union(a, b):
        a, b = root(a), root(b)
        if a != b:
            parents[max(a, b)] = min(a, b)

    def issue(reason, members, **details):
        issues.append(
            dict(
                reason=reason,
                observation_ids=sorted(rows[i]["observation_id"] for i in members),
                **details,
            )
        )

    def declared(row):
        value = row.get("event_identity")
        require(
            value is None or isinstance(value, str) and 0 < len(value) <= 200,
            "CORPORATE_SCHEMA",
            "Invalid declared event identity",
        )
        return value

    explicit = defaultdict(list)
    for i, row in enumerate(rows):
        if declared(row):
            explicit[(row["security"], row["type"], declared(row))].append(i)
    for group in explicit.values():
        for i in group[1:]:
            union(group[0], i)

    for i, row in enumerate(rows):
        refs = row.get("supersedes", [])
        require(
            isinstance(refs, list),
            "CORPORATE_SCHEMA",
            "supersedes must be a list of source references",
        )
        native = row.get("native_fields", {})
        # Backward-compatible structured document curation keys, never text matching.
        if not refs and isinstance(native, dict):
            old = native.get("corrects") or native.get("supersedes_source_id")
            refs = [old] if old else []
        if (
            not refs
            and set(row.get("resolves", {})) & {"ex_date", "effective_date", "record_date"}
            and not declared(row)
        ):
            issue("unlinked_date_correction", [i])
        for ref in refs:
            if sources[row["source_id"]]["kind"] != "issuer_pdf" or not row.get("citations"):
                issue("uncited_event_revision_link", [i])
                continue
            require(isinstance(ref, (str, dict)), "CORPORATE_SCHEMA", "Invalid revision reference")
            if isinstance(ref, str) and ref in by_id:
                targets = [by_id[ref]]
                source_id = rows[targets[0]]["source_id"]
            else:
                source_id = ref if isinstance(ref, str) else ref.get("source_id")
                targets = by_source.get(source_id, [])
                if isinstance(ref, dict) and ref.get("locator") is not None:
                    targets = [j for j in targets if rows[j]["locator"] == ref["locator"]]
            source = sources.get(source_id)
            if (
                not source
                or source["kind"] != "issuer_pdf"
                or source.get("security") != row["security"]
            ):
                issue("unresolved_event_revision_target", [i], target=ref)
                continue
            if not targets:
                # Raw predecessor can be retained without an admissible normalized schedule.
                phantom[i].add(source_id)
                continue
            if len(targets) != 1:
                issue("ambiguous_event_revision_target", [i], target=ref)
                continue
            j = targets[0]
            old = rows[j]
            if (
                row["type"] != old["type"]
                or declared(row)
                and declared(old)
                and declared(row) != declared(old)
            ):
                issue("incompatible_event_revision_identity", [i, j])
                continue
            a, b = row["fields"].get("announcement_date"), old["fields"].get("announcement_date")
            if a is not None and b is not None and a < b:
                issue("reversed_event_revision_order", [i, j])
                continue
            links[i].add(j)
            union(i, j)

    # Preserve cross-source association for ordinary uncorrected records. Explicit
    # distinct identities are never collapsed just because their dates coincide.
    dated = defaultdict(list)
    for i, row in enumerate(rows):
        date = row["fields"].get("ex_date") or row["fields"].get("effective_date")
        if date:
            dated[(row["security"], row["type"], date)].append(i)
    for group in dated.values():
        identities = {declared(rows[i]) for i in group if declared(rows[i])}
        if len(identities) > 1:
            if any(not declared(rows[i]) for i in group):
                issue("ambiguous_source_event_association", group)
            continue
        for i in group[1:]:
            union(group[0], i)

    groups = defaultdict(list)
    for i in range(len(rows)):
        groups[root(i)].append(i)

    # Similar but unlinked schedules are a diagnostic, never a guessed merger.
    collision = defaultdict(lambda: defaultdict(set))
    for i, row in enumerate(rows):
        native = row.get("native_fields", {})
        labels = {"record_date": row["fields"].get("record_date")}
        if isinstance(native, dict):
            labels["notice_identity"] = native.get("notice_identity")
        for kind, label in labels.items():
            if isinstance(label, str) and label:
                collision[(row["security"], row["type"], kind, label)][root(i)].add(i)
    for roots in collision.values():
        if len(roots) < 2:
            continue
        identities = [{declared(rows[i]) for i in groups[r] if declared(rows[i])} for r in roots]
        if all(len(ids) == 1 for ids in identities) and len(set().union(*identities)) == len(roots):
            continue
        members = [i for matching in roots.values() for i in matching]
        issue(
            "ambiguous_event_identity",
            members,
            visibility_groups=[
                sorted(rows[i]["observation_id"] for i in matching)
                for _, matching in sorted(roots.items())
            ],
        )

    for i, row in enumerate(rows):
        ancestors, todo = set(), list(links[i])
        while todo:
            j = todo.pop()
            if j in ancestors:
                continue
            ancestors.add(j)
            todo.extend(links[j])
        if i in ancestors:
            issue("cyclic_event_revision", sorted(ancestors))
        row["_supersedes_observations"] = sorted(rows[j]["observation_id"] for j in ancestors)

    for members in groups.values():
        identities = {declared(rows[i]) for i in members if declared(rows[i])}
        first = rows[members[0]]
        if identities:
            if len(identities) != 1:
                issue("incompatible_event_revision_identity", members)
            anchor, method = "declared:" + sorted(identities)[0], "declared_identity"
        else:
            predecessor_sources = set().union(*(phantom[i] for i in members))
            originals = [
                i
                for i in members
                if sources[rows[i]["source_id"]]["kind"] == "issuer_pdf"
                and not links[i]
                and not phantom[i]
            ]
            if predecessor_sources:
                anchor, method = "document:" + min(predecessor_sources), "cited_predecessor_source"
            elif originals:
                old = min(
                    (rows[i] for i in originals),
                    key=lambda r: (r["fields"].get("announcement_date") or "9999", r["source_id"]),
                )
                anchor, method = "document:" + old["source_id"], "document_source"
                if len({root(j) for j in by_source[old["source_id"]]}) > 1:
                    issue(
                        "ambiguous_multi_event_document", [j for j in by_source[old["source_id"]]]
                    )
                    anchor += ":" + old["locator"]
            else:
                old = min((rows[i] for i in members), key=lambda r: (r["source_id"], r["locator"]))
                anchor, method = (
                    "source:" + old["source_id"] + ":" + old["locator"],
                    "source_locator",
                )
        digest = hashlib.sha256(
            json.dumps([first["security"], first["type"], anchor], ensure_ascii=False).encode()
        ).hexdigest()
        for i in members:
            rows[i]["_event_key"] = ":".join((first["security"], first["type"], digest))
            rows[i]["_identity_method"] = method

    for members in groups.values():
        for i in members:
            rows[i]["_identity_issues"] = [
                r
                for r in issues
                if any(rows[j]["observation_id"] in r["observation_ids"] for j in members)
            ]
    return rows
