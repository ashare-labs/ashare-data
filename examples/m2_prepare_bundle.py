"""Maintainer-only offline packaging of the already reviewed finite M2 materials.

Does not acquire data. Product import accepts only the manifest pinned in its policy.
"""

import argparse
import hashlib
import json
from pathlib import Path


def prepare(design, facts, review, out):
    assert not out.exists(), "Refuse to overwrite any existing directory"
    out.mkdir(parents=True)
    entries = {}

    def put(name, data):
        path = out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        entries[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}

    def copy(name, path):
        put(name, path.read_bytes())

    def jsonbytes(v):
        return json.dumps(
            v, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()

    copy("old/daily.json", design / "inputs/old-baostock/daily-sh.600000-2019-12-30.json")
    copy("old/calendar.json", design / "inputs/old-baostock/calendar-2019-12-30.json")
    identities = {}
    for kind in ("daily", "calendar"):
        report = json.loads((design / f"samples/{kind}-capture.json").read_text())
        copy(f"new/{kind}/report.json", design / f"samples/{kind}-capture.json")
        identities[kind] = {
            "capture_id": report["capture_id"],
            "request": report["descriptor"]["request"],
        }
        for a in report["descriptor"]["artifacts"]:
            src = design / "samples/baostock-store/baostock-objects" / a["sha256"]
            assert hashlib.sha256(src.read_bytes()).hexdigest() == a["sha256"]
            copy(f"new/{kind}/" + a["name"], src)
        copy(
            f"new/{kind}/source-manifest.json",
            design / "samples/baostock-store/baostock-manifests" / (report["capture_id"] + ".json"),
        )
    for p in (design / "samples/preclose-st-probe").rglob("*"):
        if p.is_file():
            copy(
                "new/supplement/" + p.relative_to(design / "samples/preclose-st-probe").as_posix(),
                p,
            )
    identities["supplement"] = {
        "receipt_id": json.loads((out / "new/supplement/capture/receipt.json").read_text())[
            "receipt_id"
        ],
        "request": json.loads((out / "new/supplement/request-config.json").read_text())["request"],
    }
    required = {
        "manifest.json",
        "facts.json",
        "events.json",
        "evidence/announcement-index.json",
        "evidence/coverage-ledger.json",
        "evidence/review-completed.json",
    }
    fact = json.loads((facts / "facts.json").read_text())
    events = json.loads((facts / "events.json").read_text())
    for item in [*fact["facts"], *events]:
        for s in item.get("sources", []):
            if not Path(s["path"]).is_absolute():
                required.add(s["path"])
                assert hashlib.sha256((facts / s["path"]).read_bytes()).hexdigest() == s["sha256"]
    for name in sorted(required):
        copy("official/" + name, facts / name)
    for name in (
        "报告_M2数据设计与扩窗样本独立审阅.md",
        "revision_matrix.csv",
        "review_receipt.json",
        "evidence/independent_checks.json",
    ):
        copy("review/" + name, review / name)
    for p in (review / "evidence/reused_official").iterdir():
        if p.is_file():
            copy("review/reused_official/" + p.name, p)
    identities["original_official_manifest_sha256"] = entries["official/manifest.json"]["sha256"]
    put("source-identities.json", jsonbytes(identities))
    scopes = {
        w["candidate_window_id"]: {
            "price_basis": {"start": w["warmup"], "end": w["end"]},
            "registration": {"start": w["start"], "end": w["settlement_successor"]},
            "listing": {"start": w["warmup"], "end": w["settlement_successor"]},
        }
        for w in json.loads((design / "samples/sample-facts.json").read_text())["windows"]
    }
    annex = {
        "schema": "m2.expansion-annex.v1",
        "security": "600000.XSHG",
        "window_domains": scopes,
        "original_review_end": "2020-01-07",
        "expanded_review_end": "2020-01-20",
        "original_official_manifest_sha256": identities["original_official_manifest_sha256"],
        "official_and_review_objects": {
            k: v["sha256"] for k, v in entries.items() if k.startswith(("official/", "review/"))
        },
        "jan2_documents": "issuer 2020-001/002: branch construction, audit, governance; no relevant ordinary-share capital implementation identified",
        "jan20_rule": {
            "document_sha256": entries["review/reused_official/sse-amend-20200107.html"]["sha256"],
            "effective_date": "2020-01-20",
            "clause": "3.1.5",
            "change": "commodity futures ETF same-day resale added",
            "ordinary_share_T_plus_1_unchanged_by_this_amendment": True,
            "w2_jan20_role": "settlement_successor listing only; no bar/order/event",
        },
        "known_events_reclassified": {
            e["id"]: (
                "different_security_class"
                if e["entitled_security"] != "600000.XSHG"
                else "outside_modeled_horizon"
            )
            for e in events
        },
        "model_adoption": "owner proposes finite conditional model; explicit new per-window four-assumption acknowledgement required on every use",
        "verified_absent": False,
        "historical_pit_verified": False,
        "absolute_delisting_date": None,
        "limitations": [
            "metadata is not complete economic registry",
            "issuer/SSE binary equality not asserted",
            "2019 annual does not prove 2020 absence",
            "Q1 rounded capital does not exclude reversing actions",
            "known registration in domain blocks even if payment outside; unknown critical event date blocks",
            "terminal holding not certified after finite valuation/registration horizon",
        ],
    }
    put("expansion-annex.json", jsonbytes(annex))
    manifest = {"schema": "m2.inputs.v1", "security": "600000.XSHG", "files": entries}
    blob = jsonbytes(manifest)
    (out / "manifest.json").write_bytes(blob)
    print(
        json.dumps(
            {
                "bundle": str(out.resolve()),
                "manifest_sha256": hashlib.sha256(blob).hexdigest(),
                "annex_sha256": entries["expansion-annex.json"]["sha256"],
                "files": len(entries),
                "bytes": sum(v["bytes"] for v in entries.values()),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    for n in ("design", "facts", "review", "out"):
        p.add_argument("--" + n, type=Path, required=True)
    a = p.parse_args()
    prepare(a.design, a.facts, a.review, a.out)
