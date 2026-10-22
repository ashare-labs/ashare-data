"""Replay one explicit frozen factor/price/membership matrix, without acquiring data."""

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from ashare_data.factors.evaluation import evaluate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--sha256", required=True, help="Expected input file SHA256")
    parser.add_argument("--output", type=Path, required=True, help="New JSON report path")
    args = parser.parse_args()
    raw = args.input.read_bytes()
    if hashlib.sha256(raw).hexdigest() != args.sha256:
        parser.error("frozen input digest mismatch")
    data = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    if data.get("schema") != "ad.factor-evaluation-input.v1":
        parser.error("expected ad.factor-evaluation-input.v1")
    dates = pd.DatetimeIndex(data["dates"])
    frames = {}
    for key in ("values", "prices", "membership"):
        if key != "membership" and any(
            cell is not None and (type(cell) not in (int, float))
            for row in data[key] for cell in row
        ):
            parser.error(f"{key}: JSON numbers or null required")
        frames[key] = pd.DataFrame(data[key], index=dates, columns=data["securities"],
                                   dtype=None if key == "membership" else float)
    result = evaluate(frames["values"], frames["prices"], membership=frames["membership"],
                      config=data["config"])
    result["source"] = dict(input_file_sha256=args.sha256, provenance=data.get("provenance", {}))
    content = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    with args.output.open("x") as handle:
        handle.write(content)
    print(json.dumps(dict(schema=result["schema"], sha256=hashlib.sha256(content.encode()).hexdigest(),
                          summary=result["summary"]), ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
