"""Discover a bounded slice of the public vendor product directory; resume safely."""
import argparse
import json
from pathlib import Path
import sqlite3
import time

from ashare_data.providers.free_update_futures_universe import discover


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--max-nodes", type=int, default=24)
    parser.add_argument("--cooldown-root", type=Path, action="append", default=[])
    args = parser.parse_args()
    blocked = set()
    for root in args.cooldown_root:
        with sqlite3.connect(f"file:{root/'observations.sqlite'}?mode=ro", uri=True) as db:
            blocked.update(r[0] for r in db.execute("SELECT host FROM cooldowns WHERE until_epoch>?", (time.time(),)))
    result = discover(args.root, max_nodes=args.max_nodes, blocked_hosts=blocked)
    print(json.dumps({k: result[k] for k in ["source_navigation_count", "resolved_node_count", "attempted_this_run", "errors"]}, ensure_ascii=False))
    print(json.dumps({"continuous_symbols": len(result["members"]), "reviewed_contexts": len(result["contexts"]), "pending_nodes": len(result["pending_nodes"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
