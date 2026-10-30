"""Refresh the separate 19-item increment from existing verified source snapshots."""

import argparse
import json
from pathlib import Path
from ashare_data.providers.supplemental_updates import refresh

if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-index", type=Path)
    p.add_argument("--root", type=Path)
    p.add_argument("--profile", type=Path)
    p.add_argument("--execute", action="store_true")
    p.add_argument("--as-of")
    p.add_argument("--max-jobs", type=int)
    a = p.parse_args()
    if a.profile:
        from ashare_data.providers.supplemental_batch import run
        result = run(a.profile, execute=a.execute, as_of=a.as_of, max_jobs=a.max_jobs)
    else:
        if not a.source_index or not a.root:
            p.error("provide --profile or both --source-index and --root")
        result = refresh(a.source_index, a.root)
    print(json.dumps(result, ensure_ascii=False))
