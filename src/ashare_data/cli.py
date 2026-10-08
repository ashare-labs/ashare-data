"""Explicit import / validate / publish / query operations. JSON in and out."""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from datetime import date, datetime
from pathlib import Path

from .model import DataError, require
from .storage import Store
from .live import Client, capabilities
from .contracts import CoverageContract


def json_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(type(value).__name__)


def read_json(path):
    path = Path(path)
    require(path.is_file() and path.stat().st_size <= 64 * 1024 * 1024,
            "BOUNDED_IMPORT", "只允许最大 64 MiB 的本地 JSON 文件")
    return json.loads(path.read_text(encoding="utf-8"))


def parser():
    p = argparse.ArgumentParser(prog="ashare-data", description="A数达：直接查询公开 A 股行情，支持可选缓存与高级离线快照")
    p.add_argument("--store", help="高级离线快照目录；直接行情查询无需此参数")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("init", "capabilities", "snapshots", "recover"):
        sub.add_parser(name)
    price = sub.add_parser("price", help="直接从公开源获取行情，无需初始化")
    price.add_argument("security", nargs="+")
    price.add_argument("--start")
    price.add_argument("--end")
    price.add_argument("--count", type=int)
    price.add_argument("--frequency", choices=["daily", "1d", "1m", "minute", "5m"], default="daily")
    price.add_argument("--fields", nargs="+")
    price.add_argument("--fq", choices=["none", "pre", "post"], default="none")
    price.add_argument("--strict", action="store_true")
    price.add_argument("--as-of", help="独立策略时钟；不改变 end 标签过滤语义；仅读固定缓存")
    price.add_argument("--visibility", choices=["assumed", "received", "verified"], default="verified")
    price.add_argument("--require-complete", action="store_true")
    price.add_argument("--require-fresh", action="store_true")
    price.add_argument("--require-final", action="store_true")
    price.add_argument("--coverage-contract", help="A数达本地证据契约 JSON；不从缺 bar 推测状态")
    coverage = sub.add_parser("live-coverage", help="检查声明日历/session/status 下的标签覆盖")
    coverage.add_argument("security")
    coverage.add_argument("--start", required=True)
    coverage.add_argument("--end", required=True)
    coverage.add_argument("--frequency", choices=["daily", "1m", "5m"], default="1m")
    coverage.add_argument("--coverage-contract")
    acquire = sub.add_parser("acquire-price", help="显式刷新一个有限源窗口；无历史分页或造数补齐")
    acquire.add_argument("security")
    acquire.add_argument("--start", required=True)
    acquire.add_argument("--end", required=True)
    acquire.add_argument("--frequency", choices=["daily", "1m", "5m"], default="1m")
    acquire.add_argument("--coverage-contract")
    reconcile = sub.add_parser("reconcile-day", help="单股单日分钟与当前日快照量额诊断")
    reconcile.add_argument("security")
    reconcile.add_argument("--date", required=True)
    info = sub.add_parser("security", help="查询显式证券的当前名称/代码")
    info.add_argument("security")
    days = sub.add_parser("trade-days", help="查询上交所当前发布年度交易日")
    days.add_argument("--start")
    days.add_argument("--end")
    days.add_argument("--count", type=int)
    for live in (price, info, days, coverage, acquire, reconcile):
        live.add_argument("--cache", help="可选缓存目录")
        live.add_argument("--cache-mode", choices=["prefer", "only", "refresh"], default="prefer")
        live.add_argument("--timeout", type=float, default=15)
    imp = sub.add_parser("import", help="高级功能：从本地 JSON 导入离线快照")
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
        if args.command in {"price", "security", "trade-days", "live-coverage", "acquire-price", "reconcile-day"}:
            contract_path = getattr(args, "coverage_contract", None)
            client = Client(cache=args.cache, cache_mode=args.cache_mode, timeout=args.timeout,
                            coverage_contract=CoverageContract(read_json(contract_path)) if contract_path else None)
            if args.command == "price":
                frame = client.get_price(args.security[0] if len(args.security) == 1 else args.security,
                                         start_date=args.start, end_date=args.end, count=args.count,
                                         frequency=args.frequency, fields=args.fields,
                                         fq=None if args.fq == "none" else args.fq, strict=args.strict,
                                         as_of=args.as_of, visibility=args.visibility, require_complete=args.require_complete,
                                         require_fresh=args.require_fresh, require_final=args.require_final)
                result = {"rows": (frame.reset_index() if frame.index.name == "time" else frame).to_dict("records"),
                          "metadata": frame.attrs}
            elif args.command == "live-coverage":
                result = client.coverage(args.security, args.start, args.end, frequency=args.frequency)
            elif args.command == "acquire-price":
                result = client.acquire_price(args.security, args.start, args.end, frequency=args.frequency)
            elif args.command == "reconcile-day":
                result = client.reconcile_day(args.security, args.date)
            elif args.command == "security":
                result = client.get_security_info(args.security)
            else:
                result = {"days": client.get_trade_days(args.start, args.end, args.count),
                          "source": "SSE current published annual schedule"}
        elif args.command == "capabilities" and args.store is None:
            result = capabilities()
        elif args.command == "init":
            require(args.store is not None, "STORE_REQUIRED", "高级快照操作须指定 --store")
            store = Store.init(args.store)
            result = {"store": str(store.root), "initialized": True}
        else:
            require(args.store is not None, "STORE_REQUIRED", "高级快照操作须指定 --store")
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
        if args.command in {"live-coverage", "acquire-price", "reconcile-day"}:
            return 0 if result.get({"live-coverage": "complete", "acquire-price": "requirements_met", "reconcile-day": "accepted"}[args.command]) else 2
        return 0 if args.command != "validate" or result["passed"] else 2
    except DataError as exc:
        print(json.dumps({"error": exc.as_dict()}, ensure_ascii=False, default=json_value), file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({"error": {"code": "INPUT_OR_IO_ERROR", "message": str(exc)}}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
