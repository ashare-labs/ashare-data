"""Produce the requested local sparse dataset through AD public APIs; never collect.

The request supplies only source scope and hashes. Orders, result values and desired
fill quantities in it are not consumed. Output is private local data, outside code.
"""

import argparse
import hashlib
import json
from pathlib import Path

from ashare_data import JQObservationInput, Store, __version__


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    request_bytes = args.request.read_bytes()
    request = json.loads(request_bytes)
    sources = []
    for case in request["cases"]:
        root = Path(case["source_directory"])
        settings = case["settings"]
        if settings["start_date"] != settings["end_date"]:
            raise ValueError("only bounded single-day source runs supported")
        sources.append(JQObservationInput(
            case_id=case["case_id"], security=settings["symbol"],
            session_date=settings["start_date"], events_path=str(root / "events.jsonl"),
            events_sha256=case["reference_hashes"]["events.jsonl"],
            script_path=str(root / "jq.py"), script_sha256=case["reference_hashes"]["jq.py"],
        ))
    store = Store.init(args.store)
    dataset_id = store.import_jq_observations(sources, source_kind="local_jq_backtest_observation")
    view = store.jq_observations(dataset_id)
    args.output.mkdir(parents=True, exist_ok=True)
    report = {
        "version": __version__, "dataset_id": dataset_id, "store_path": str(store.root),
        "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
        "manifest": view.describe(),
        "bootstrap": [view.bootstrap(security, session) for security, session in
                      sorted({(s.security, s.session_date) for s in sources})],
    }
    target = args.output / "delivery.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"dataset_id": dataset_id, "delivery": str(target.resolve()),
                      "record_count": report["manifest"]["record_count"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
