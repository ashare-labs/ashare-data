"""Freeze existing independent evidence; no network and no JQ input."""

import argparse
import hashlib
import json
from pathlib import Path


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def freeze(independent, carry, action, output):
    output.mkdir(parents=True, exist_ok=False)

    def copy(path):
        raw = path.read_bytes()
        (output / path.name).write_bytes(raw)
        return {"path": path.name, "sha256": sha(raw)}

    base = json.loads((independent / "binding.json").read_text())
    source = base["sources"]["sina-pingan"]
    decoded = copy(independent / source["path"])
    if decoded["sha256"] != source["sha256"]:
        raise ValueError("original independent decoded pin changed")
    raw = copy(independent / "acquisition/sina-pingan-daily.js")
    decoder = copy(independent / "acquisition/sina-klc-decoder.js")
    if raw["sha256"] != source["raw_sha256"] or decoder["sha256"] != source["decoder_sha256"]:
        raise ValueError("original independent lineage changed")
    fact = copy(action / "action-fact.json")
    ab = json.loads((action / "binding.json").read_text())
    document = ab["documents"][0]
    doc = copy(action / document["file"])
    if fact["sha256"] != ab["fact_sha256"] or doc["sha256"] != document["sha256"]:
        raise ValueError("original issuer evidence changed")
    f = json.loads((output / fact["path"]).read_text())
    calendar = copy(carry / "acquisition/sina-calendar-decoded.json")
    b = {
        "schema": "ad.premarket-reference-binding.v1",
        "policy": "sina_native_prevclose_known_cash_research.v1",
        "role": "premarket_reference_reconstruction",
        "security": f["security"],
        "ex_date": f["ex_date"],
        "previous_session": f["record_date"],
        "model_available_at": f["ex_date"] + "T09:00:00+08:00",
        "historical_pit": False,
        "strategy_visible": False,
        "matching_eligible": False,
        "source_price_unit": "0.01",
        "cash_fact": fact,
        "calendar": calendar,
        "primary_document": {
            **doc,
            "url": document["url"],
            "document_date": f["announcement_date"],
        },
        "source": {
            **decoded,
            "raw": raw,
            "decoder": decoder,
            **{
                k: source[k]
                for k in ("kind", "security", "adjustment", "url", "captured_start", "captured_end")
            },
            "source_publication_time": None,
            "historical_received_at": None,
        },
        "original_bindings": {
            "independent": sha((independent / "binding.json").read_bytes()),
            "issuer": sha((action / "binding.json").read_bytes()),
        },
        "assumption": "The separately encoded source prevclose applies at ex-session 09:00 for bounded research valuation; actual historical availability is unproven.",
    }
    target = output / "binding.json"
    target.write_text(json.dumps(b, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"binding": str(target), "sha256": sha(target.read_bytes())}))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("independent", "carry", "action", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    freeze(**vars(p.parse_args()))
