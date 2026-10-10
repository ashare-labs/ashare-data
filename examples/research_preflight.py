"""Read one explicit local research preflight request; no acquisition or execution."""
import argparse
import json
from pathlib import Path

from ashare_data import Store

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--store', required=True)
parser.add_argument('--request', required=True)
parser.add_argument('--require-complete', action='store_true')
args = parser.parse_args()
result = Store(args.store).preflight_research_request(
    json.loads(Path(args.request).read_text()), require_complete=args.require_complete)
print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
