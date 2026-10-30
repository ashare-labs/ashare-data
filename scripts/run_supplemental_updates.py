"""Refresh the separate 19-item increment from existing verified source snapshots."""

import argparse
import json
from pathlib import Path
from ashare_data.providers.supplemental_updates import refresh

if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-index", type=Path, required=True)
    p.add_argument("--root", type=Path, required=True)
    a = p.parse_args()
    result = refresh(a.source_index, a.root)
    print(json.dumps(result, ensure_ascii=False))
