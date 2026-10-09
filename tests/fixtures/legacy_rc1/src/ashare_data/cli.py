"""Explicit import / validate / publish / query operations. JSON in and out."""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

from .model import DataError, require
from .storage import Store


def json_value(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(type(value).__name__)


def read_json(path):
    path = Path(path)
    require(path.is_file() and path.stat().st_size <= 64 * 1024 * 1024,
            "BOUNDED_IMPORT", "只允许最大 64 MiB 的本地 JSON 文件")
    return json.loads(path.read_text(encoding="utf-8"))


def parser():
    p = argparse.ArgumentParser(prog="ashare-data", description="A数达：显式离线快照数据内核")
    p.add_argument("--store", required=True, help="本地数据目录")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("init", "capabilities", "snapshots", "recover"):
        sub.add_parser(name)
    imp = sub.add_parser("import", help="从本地 JSON 导入；不采集数据")
    imp.add_argument("bundle")
    v = sub.add_parser("validate")
    v.add_argument("--batch", action="append", required=True)
    v.add_argument("--requirements", required=True, help="覆盖要求 JSON 列表")
    v.add_argument("--policy", choices=["observed", "synthetic", "research"], default="observed")
    pub = sub.add_parser("publish")
    pub.add_argument("--report", required=True)
    q = sub.add_parser("query")
    q.add_argument("--snapshot", required=True)
    q.add_argument("dataset", choices=["bars", "coverage", "instruments", "calendar", "sessions", "lineage", "quality", "corporate-actions", "adjustment-factors"])
    q.add_argument("--symbol", action="append")
    q.add_argument("--start")
    q.add_argument("--end")
    q.add_argument("--as-of")
    q.add_argument("--quality", choices=["observed", "synthetic", "research"], default="observed")
    q.add_argument("--allow-partial", action="store_true", help="仅 bars：明确返回缺口，仍不允许来源冲突")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            store = Store.init(args.store)
            result = {"store": str(store.root), "initialized": True}
        else:
            store = Store(args.store)
            if args.command in {"capabilities", "snapshots", "recover"}:
                result = getattr(store, args.command)()
            elif args.command == "import":
                result = {"batch_id": store.import_bundle(read_json(args.bundle))}
            elif args.command == "validate":
                result = store.validate(args.batch, requirements=read_json(args.requirements), policy=args.policy)
            elif args.command == "publish":
                result = {"snapshot_id": store.publish(args.report)}
            else:
                with store.snapshot(args.snapshot) as view:
                    ds = args.dataset
                    supplied = {k for k, val in {"symbol": args.symbol, "start": args.start, "end": args.end,
                                                "as_of": args.as_of, "allow_partial": args.allow_partial}.items() if val}
                    allowed = {"bars": {"symbol", "start", "end", "as_of", "allow_partial"},
                               "coverage": {"symbol", "start", "end", "as_of"},
                               "calendar": {"start", "end"}, "sessions": {"start"},
                               "instruments": {"as_of"}}
                    require(supplied <= allowed.get(ds, set()), "INVALID_REQUEST", "当前 dataset 不支持该参数")
                    require(ds in {"bars", "coverage"} or args.quality == "observed", "INVALID_REQUEST", "当前 dataset 不接受 quality")
                    if ds in {"bars", "coverage"}:
                        kw = {"symbols": args.symbol, "start": args.start, "end": args.end,
                              "as_of": args.as_of, "quality": args.quality}
                        if ds == "bars":
                            kw["strict"] = not args.allow_partial
                        result = getattr(view, ds)(**kw)
                    elif ds == "calendar":
                        result = view.calendar(args.start, args.end)
                    elif ds == "sessions":
                        result = view.sessions(args.start)
                    elif ds == "instruments":
                        result = view.instruments(as_of=args.as_of)
                    else:
                        result = getattr(view, ds.replace("-", "_"))()
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=json_value))
        return 0 if args.command != "validate" or result["passed"] else 2
    except DataError as exc:
        print(json.dumps({"error": exc.as_dict()}, ensure_ascii=False, default=json_value), file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({"error": {"code": "INPUT_OR_IO_ERROR", "message": str(exc)}}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
