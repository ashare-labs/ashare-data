"""Read a pinned batch by menu row, security or source date, with bounded pages."""
import argparse
from datetime import date
import json
from pathlib import Path

from ashare_data.providers.free_updates.free_update_manual import read_snapshot
from ashare_data.providers.free_updates import digest


def query(result_file, row, *, symbol=None, source_date=None, offset=0, limit=100):
    path = Path(result_file).resolve()
    result = json.loads(path.read_text())
    root = Path(result["root"]).resolve()
    if (result.get("schema") != "ad.security-update-batch-result.v1"
            or path.parent != root / "batch-results"
            or path.name != result["as_of"] + "-" + digest(result) + ".json"):
        raise ValueError("choose the immutable result_path, not a mutable latest pointer")
    if row not in result["rows"]:
        raise ValueError("menu row is outside this pinned batch")
    if source_date is not None:
        date.fromisoformat(source_date)
    if (not isinstance(offset, int) or isinstance(offset, bool) or offset < 0
            or not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000):
        raise ValueError("offset>=0 and limit1..1000 required")
    items = [i for i in result["items"] if i["excel_row"] == row
             and (symbol is None or i["parameters"].get("symbol") == symbol)]
    if not items or any(i["status"] != "可读取" for i in items):
        raise ValueError("requested security/row has no complete captured scope")
    total, records, scopes = 0, [], []
    for item in sorted(items, key=lambda i: i["job_id"]):
        snapshot = Path(item["snapshot"]).resolve()
        if snapshot != root / "manual-snapshots" / item["snapshot_id"]:
            raise ValueError("snapshot escapes its captured batch")
        manifest, native = read_snapshot(snapshot)
        if (manifest["excel_row"] != row or manifest["job_id"] != item["job_id"]
                or manifest["records"] != item["records"]):
            raise ValueError("snapshot is not the recorded menu/security scope")
        scopes.append({"parameters": item["parameters"], "snapshot_id": item["snapshot_id"],
                       "observed_at": manifest["observed_at"], "semantics": manifest["semantics"]})
        for record in native:
            day = record.get("day") or record.get("date") or record.get("trade_date")
            if source_date is not None and str(day)[:10] != source_date:
                continue
            if offset <= total < offset + limit:
                records.append(record)
            total += 1
    if offset > total:
        raise ValueError("offset exceeds the selected pinned data")
    end = min(offset + limit, total)
    return {"result_path": str(path), "excel_row": row, "symbol": symbol, "source_date": source_date,
            "as_of": result["as_of"], "total": total, "offset": offset,
            "next_offset": end if end < total else None, "scopes": scopes, "records": records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", required=True)
    parser.add_argument("--row", type=int, required=True)
    parser.add_argument("--symbol")
    parser.add_argument("--date", dest="source_date")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    print(json.dumps(query(args.result, args.row, symbol=args.symbol, source_date=args.source_date,
                           offset=args.offset, limit=args.limit), ensure_ascii=False))


if __name__ == "__main__":
    main()
