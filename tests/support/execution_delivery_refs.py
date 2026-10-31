"""Validate local delivery references independently of market-data admission.

These offline helpers belong to delivery tooling, not the runtime wheel. Paths
in delivery contracts are relative to their package root unless absolute;
source_refs in market evidence retain their own binding-defined base.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import unquote


EVENT_FIELDS = (
    "security",
    "ex_date",
    "type",
    "announcement_date",
    "record_date",
    "payment_date",
    "cash_per_share",
    "bonus_share_ratio",
    "capitalization_share_ratio",
    "ex_price_cash_deduction",
)


def check(condition, message):
    if not condition:
        raise ValueError(message)


def load(path):
    return json.loads(Path(path).read_text())


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def resolve_reference(reference, root):
    """Resolve an existing local file plus an optional RFC 6901 URI fragment."""
    check(isinstance(reference, str) and reference, "Empty reference")
    name, separator, fragment = reference.partition("#")
    check(not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", name), "Local reference required")
    path = Path(name)
    if not path.is_absolute():
        path = Path(root) / path
    check(path.is_file(), "Missing reference file: " + str(path))
    if not separator:
        return path, None
    pointer = unquote(fragment)
    check(pointer == "" or pointer.startswith("/"), "Invalid JSON pointer")
    target = load(path)
    for token in pointer[1:].split("/") if pointer else ():
        check(not re.search(r"~(?![01])", token), "Invalid JSON pointer escape")
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(target, list):
            check(re.fullmatch(r"0|[1-9][0-9]*", token) is not None, "Invalid array index")
            check(int(token) < len(target), "JSON pointer index out of range")
            target = target[int(token)]
        else:
            check(isinstance(target, dict) and token in target, "Missing JSON pointer member")
            target = target[token]
    return path, target


def verify_event(target, expected):
    check(isinstance(target, dict), "Event pointer must resolve to an object")
    for field in EVENT_FIELDS:
        check(field in expected and field in target, "Missing event field: " + field)
        check(target[field] == expected[field], "Event field mismatch: " + field)
    return {field: target[field] for field in EVENT_FIELDS}


def action_reference(path, expected):
    """Find the raw-array index by identity; a filtered API index is not reused."""
    path = Path(path).resolve()
    actions = load(path)["actions"]
    matches = [
        i
        for i, event in enumerate(actions)
        if event.get("security") == expected["security"]
        and event.get("ex_date") == expected["ex_date"]
    ]
    check(len(matches) == 1, "Expected one raw action for security/ex_date")
    verify_event(actions[matches[0]], expected)
    return str(path) + "#/actions/" + str(matches[0])


def verify_cash_contract(cash, root):
    expected = cash["source_event"]
    results = []
    for key in ("research_event", "execution_event"):
        reference = cash["paths"][key]
        _, target = resolve_reference(reference, root)
        results.append(
            dict(field=key, reference=reference, identity=verify_event(target, expected))
        )
    pdf, _ = resolve_reference(cash["paths"]["issuer_pdf"], root)
    check(digest(pdf) == cash["paths"]["issuer_pdf_sha256"], "Issuer PDF hash mismatch")
    receipt, _ = resolve_reference(cash["paths"]["receipt"], root)
    check(load(receipt)["sha256"] == digest(pdf), "Issuer receipt hash mismatch")
    check(
        cash["accounting"]["cash_entitlement_per_eligible_share"] == expected["cash_per_share"],
        "Cash accounting entitlement mismatch",
    )
    check(
        cash["accounting"]["ex_price_deduction"] == expected["ex_price_cash_deduction"],
        "Cash deduction mismatch",
    )
    return results


def verify_gap_contract(contract, root):
    path, _ = resolve_reference(contract["unresolved"]["details"], root)
    actual = load(path)
    # The dev12 and revised dev13 reports use different container field names.
    fields_count = actual.get("fields_count", actual.get("missing_fields"))
    cells = actual.get("cells", actual.get("missing_cells"))
    issues = actual.get("exact_issues", actual.get("issues"))
    check(isinstance(issues, list), "Missing exact gap issues")
    unique = {(x["security"], x["date"], x["field"]) for x in issues}
    check(len(unique) == len(issues) == fields_count, "Gap issue detail count mismatch")
    check(len({(s, d) for s, d, _ in unique}) == cells, "Gap issue cell count mismatch")
    check(fields_count == contract["unresolved"]["field_count"], "Gap field count mismatch")
    check(cells == contract["unresolved"]["security_date_cells"], "Gap cell count mismatch")
    check(
        sorted({s for s, _, _ in unique}) == sorted(contract["unresolved"]["securities"]),
        "Gap security scope mismatch",
    )
    check(
        sorted({f for _, _, f in unique}) == sorted(contract["unresolved"]["fields_each"]),
        "Gap field scope mismatch",
    )
    return dict(path=str(path), fields_count=fields_count, cells=cells)


def strings(value, trail=""):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from strings(child, trail + "/" + str(key))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield from strings(child, trail + "/" + str(i))
    elif isinstance(value, str):
        yield trail, value


def audit_delivery(root, research, execution):
    """Check all comparable contract paths and JSON-pointer strings in 3 packages.

    Raw receipts may record failed downloads, so those declared attempted paths
    are not advertised as delivery files and are not existence requirements.
    The bound source references are checked separately by the public binding API.
    """
    root, research, execution = map(Path, (root, research, execution))
    results = dict(
        paths=[], pointers=[], markdown_paths=[], cash_events=[], gaps=[], non_json_raw_captures=[]
    )
    # All path-looking strings in machine handoff/consumer documents have a
    # package-root base. Prose, API examples and field-name lists are excluded.
    prefix = re.compile(r"^(?:/|raw/|derived/|reports/|verification/|dist/|\.\./)")
    suffix = re.compile(r"\.(?:json|jsonl|md|py|whl|pdf|xml)$")
    for package in (research, execution, root):
        for name in ("handoff.json", "consumer-contract.json"):
            path = package / name
            if not path.is_file():
                continue
            for trail, value in strings(load(path)):
                if "://" in value or not (
                    prefix.match(value) or (not re.search(r"\s", value) and suffix.search(value))
                ):
                    continue
                # Worktree paths are explicitly directories, not artifact files.
                if trail in ("/worktree", "/previous_delivery/root"):
                    check(Path(value).is_dir(), "Missing worktree reference")
                else:
                    resolve_reference(value, package)
                results["paths"].append(dict(document=str(path), field=trail, reference=value))
        # Enumerate all JSON pointers, including pointers nested in reports.
        for path in sorted(package.rglob("*.json")):
            if "wheel-runtime" in path.parts or path.name == "reference-audit.json":
                continue
            try:
                document = load(path)
            except json.JSONDecodeError:
                check(
                    path.relative_to(package).parts[0] == "raw",
                    "Malformed delivery JSON: " + str(path),
                )
                results["non_json_raw_captures"].append(dict(path=str(path), sha256=digest(path)))
                continue
            for trail, value in strings(document):
                if "#/" in value:
                    _, target = resolve_reference(value, package)
                    item = dict(document=str(path), field=trail, reference=value)
                    if trail in ("/paths/research_event", "/paths/execution_event"):
                        item["identity"] = verify_event(target, load(path)["source_event"])
                    results["pointers"].append(item)
        doc = package / "HANDOFF.md"
        if doc.is_file():
            for ref in re.findall(r"\]\(([^)]+)\)", doc.read_text()):
                if ref.startswith(("http:", "https:", "#")):
                    continue
                ref = ref.strip("<>")
                path = Path(ref) if ref.startswith("/") else package / ref
                check(path.exists(), "Missing Markdown reference: " + str(path))
                results["markdown_paths"].append(dict(document=str(doc), reference=ref))
    cash = root / "reports/cash-event-contract.json"
    results["cash_events"] = verify_cash_contract(load(cash), root)
    for package in (execution, root):
        results["gaps"].append(
            verify_gap_contract(load(package / "consumer-contract.json"), package)
        )
    results.update(result="passed", source_facts_changed=False, runtime_gate_changed=False)
    return results


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root", type=Path)
    p.add_argument("--research", required=True, type=Path)
    p.add_argument("--execution", required=True, type=Path)
    p.add_argument("--output", type=Path)
    args = p.parse_args()
    result = audit_delivery(args.root, args.research, args.execution)
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(encoded)
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
