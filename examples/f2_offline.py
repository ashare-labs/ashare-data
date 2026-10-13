"""Read a captured six-factor panel with the installed public interface."""

import argparse
import json
from pathlib import Path

from ashare_data import FactorResearchView
from ashare_data.f2 import DEFINITION, FIELDS, MODE

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("evidence_root", type=Path)
parser.add_argument("--date", default="2024-05-06")
parser.add_argument("--security", action="append", default=None)
parser.add_argument("--expected-sha256", required=True)
args = parser.parse_args()
view = FactorResearchView.from_file(
    args.evidence_root / "derived/dataset.json",
    evidence_root=args.evidence_root,
    expected_sha256=args.expected_sha256,
)
result = view.query(
    args.security or ["600519.XSHG", "300124.XSHE"],
    list(FIELDS),
    dates=args.date,
    definition=DEFINITION,
    mode=MODE,
)
print(json.dumps(result, ensure_ascii=False, indent=2))
