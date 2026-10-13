"""Export a pinned offline session contract; no network or source acquisition."""
import argparse
import hashlib
import json
from pathlib import Path

from ashare_data import TDXMinuteSessionView, TDXSessionSource

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("config", type=Path)
parser.add_argument("output", type=Path)
args = parser.parse_args()
if args.output.exists():
    raise ValueError("Use a new output file; existing exports are immutable")
config = json.loads(args.config.read_bytes())
sources = [TDXSessionSource(s["security"], str(args.config.parent/s["directory"]), s["manifest_sha256"])
           for s in config["sources"]]
view = TDXMinuteSessionView(sources, execution_days=config["execution_days"],
                          history_days=config["history_days"], model=config["model"])
raw = view.export_bytes()
args.output.parent.mkdir(parents=True, exist_ok=True)
with args.output.open("xb") as stream:
    stream.write(raw)
print(json.dumps({"output": str(args.output), "sha256": hashlib.sha256(raw).hexdigest(),
                  "coverage": json.loads(raw)["coverage"]}))
