"""Plan bounded free Sina stock-distribution detail requests from captured ledgers.

No network or shared database access. Pass the output to capture_action_history.
Only implemented bonus/capitalization events overlapping the frozen scope qualify.
"""

import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from urllib.parse import urlencode

from ashare_data.corporate_history import intersects
from ashare_data.corporate_history_sources import parse_sina_actions


def plan(captures, scope):
    captures = Path(captures)
    windows = {s["security"]: s for s in scope["securities"]}
    result = {}
    for path in sorted(captures.glob("*.receipt.json")):
        r = json.loads(path.read_text())
        q = r["request"]
        if not r["ok"] or q["kind"] != "sina_actions" or q["security"] not in windows:
            continue
        if Path(r["body"]).name != r["body"]:
            raise ValueError("Capture body must be local")
        body = (captures / r["body"]).read_bytes()
        if hashlib.sha256(body).hexdigest() != r["sha256"] or len(body) != r["bytes"]:
            raise ValueError("Capture digest mismatch")
        s = windows[q["security"]]
        for row in parse_sina_actions(body, q["security"])["actions"]:
            if row["type"] != "distribution" or not intersects(
                row, s["start"], s["end"], "economic"
            ):
                continue
            if not any(
                row["fields"].get(k) is not None and Decimal(row["fields"][k]) > 0
                for k in ("bonus_share_ratio", "capitalization_share_ratio")
            ):
                continue
            announced = row["fields"]["announcement_date"]
            if announced is None:
                continue
            code = row["security"][:6]
            ident = "sina_action_detail-" + code + "-" + announced.replace("-", "")
            result[ident] = dict(
                id=ident,
                kind="sina_action_detail",
                security=row["security"],
                url="https://vip.stock.finance.sina.com.cn/corp/view/vISSUE_ShareBonusDetail.php?"
                + urlencode(dict(stockid=code, type=1, end_date=announced)),
            )
    return [result[k] for k in sorted(result)]


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("captures", type=Path)
    p.add_argument("--scope", type=Path, default=Path("docs/action-history-scope.json"))
    args = p.parse_args()
    print(
        json.dumps(
            plan(args.captures, json.loads(args.scope.read_text())), ensure_ascii=False, indent=2
        )
    )
