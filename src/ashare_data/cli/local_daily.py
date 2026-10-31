"""CLI parser and command dispatch for LocalDailyStore."""

from __future__ import annotations

import argparse
import json
from datetime import timedelta
from pathlib import Path


def add_parser(sub):
    p = sub.add_parser(
        "local-daily",
        help="批量本地日线维护、断点续取和覆盖报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "query-early 只读逐来源研究观察，不是完整可回测行情或全市场历史 PIT。\n"
            "来源行按 (symbol,date,source) 计数；唯一证券日按 (symbol,date) 去重，仍不认证成交日。\n"
            "当前数量与缺口请读取指定本地库的本次报告。上海代理不是深圳早年/PT 完整日历；"
            "缺口不等于已证实漏采。\n"
            "未认证 BaoStock 停牌/成交声明不消除缺口；validation_unknown 仅在 --diagnostics 展示。\n"
            "早期价格/股数/手数单位、证券身份阶段和复权依据仍有限制；"
            "strict_usable=false，不自动缩放、复权或生成总收益。\n"
            "范围、缺口归因和使用限制见 docs/local-daily.md#早期历史。"
        ),
    )
    p.add_argument(
        "action",
        choices=[
            "update",
            "resume",
            "report",
            "status",
            "query",
            "query-alias",
            "query-early",
            "universe",
            "factors",
            "import-bao",
            "supplement",
        ],
    )
    p.add_argument(
        "--root", type=Path, help="Explicit library; otherwise deployment or legacy user directory"
    )
    p.add_argument("--deployment", help="Shared deployment manifest")
    p.add_argument("--profile", default="product")
    p.add_argument(
        "--start",
        default=None,
        help="起始日；query-early 默认 1990-12-01，其他操作默认 2010-01-01",
    )
    p.add_argument("--end", default=None, help="结束日；query-early 默认 2009-12-31")
    p.add_argument("--workers", type=int, default=3)
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--timeout", type=float, default=30)
    p.add_argument("--retry-failed", action="store_true")
    p.add_argument("--repair-gaps", action="store_true")
    p.add_argument("--refresh-metadata", action="store_true")
    p.add_argument("--symbol", action="append")
    p.add_argument("--capture", type=Path, help="Explicit immutable capture for query-alias")
    p.add_argument(
        "--diagnostics",
        action="store_true",
        help="query-early: include unadmitted BaoStock and anomalous HTTP rows with validation status",
    )
    p.add_argument(
        "--mode",
        choices=["single", "compatible"],
        default="single",
        help="query: opt in to audited per-day/field completion",
    )
    p.add_argument(
        "--alias-capture",
        action="append",
        type=Path,
        default=[],
        help="query --mode compatible: explicit verified alias archive (repeatable)",
    )
    p.add_argument(
        "--include-sme-history",
        action="store_true",
        help="历史名单中包括原中小板时期；默认按当时主板归属筛选",
    )
    p.add_argument("--limit-jobs", type=int, help="受控验证可用；正常全量无需指定")
    p.add_argument(
        "--source",
        default="auto",
        choices=["auto", "eastmoney", "sina", "baostock", "sohu", "tencent"],
    )


def command(args):
    from ashare_data.storage import local_daily as _storage

    if getattr(args, "start", None) is None:
        args.start = "1990-12-01" if args.action == "query-early" else "2010-01-01"
    mode = getattr(args, "mode", "single")
    aliases = getattr(args, "alias_capture", ())
    if args.action != "query" and (mode != "single" or aliases):
        raise ValueError("--mode / --alias-capture are query options")
    if getattr(args, "diagnostics", False) and args.action != "query-early":
        raise ValueError("--diagnostics is a query-early option")
    readonly = args.action in (
        "status",
        "query",
        "query-alias",
        "query-early",
        "universe",
        "factors",
    )
    root = getattr(args, "root", None)
    if root is None:
        from ashare_data.product.configuration import deployment_path, load_profile

        selected = deployment_path(getattr(args, "deployment", None))
        if selected is None:
            root = _storage.DEFAULT_ROOT
        else:
            _, data_root, document, profile = load_profile(
                selected, profile=getattr(args, "profile", "product")
            )
            try:
                asset = document["assets"][profile["daily_asset"]]
                if asset["kind"] != "local_daily":
                    raise ValueError("Selected daily asset must have kind local_daily")
                target = Path(asset["path"]).expanduser()
                root = target if target.is_absolute() else data_root / target
            except (KeyError, TypeError) as exc:
                raise ValueError("Selected deployment has no valid daily asset") from exc
    store = _storage.LocalDailyStore(root, readonly=readonly)
    try:
        if args.action == "status":
            return store.summary()
        if args.action == "universe":
            return store.universe(
                args.end or store.meta("end"), main_board_only=not args.include_sme_history
            )
        if args.action == "factors":
            if not args.symbol or len(args.symbol) != 1:
                raise ValueError("factors needs one --symbol")
            return store.factor_snapshots(args.symbol[0])
        if args.action == "query":
            if not args.symbol or len(args.symbol) != 1:
                raise ValueError("query needs one --symbol")
            frame = store.get_price(
                args.symbol[0],
                args.start,
                args.end,
                args.source,
                mode=mode,
                alias_captures=aliases,
            )
            if mode == "compatible":
                return dict(rows=frame.to_dict("records"), metadata=frame.attrs)
            return frame.to_dict("records")
        if args.action == "query-alias":
            if not args.symbol or len(args.symbol) != 1 or not args.capture:
                raise ValueError("query-alias needs one --symbol and an explicit --capture")
            if args.source not in ("auto", "baostock"):
                raise ValueError("query-alias supports only the explicit BaoStock capture")
            return store.get_alias_price(
                args.symbol[0], args.capture, args.start, args.end
            ).to_dict("records")
        if args.action == "query-early":
            if not args.symbol or len(args.symbol) != 1:
                raise ValueError("query-early needs one --symbol")
            return store.get_early_observations(
                args.symbol[0],
                args.start,
                args.end or "2009-12-31",
                None if args.source == "auto" else args.source,
                diagnostics=getattr(args, "diagnostics", False),
            )
        with store.writer():
            if args.action == "import-bao":
                from ashare_data.providers.baostock_collection import import_captures

                result = import_captures(store)
                store.report()
                return result
            if args.action == "report":
                return store.report()
            if args.workers < 1 or args.interval < 0 or args.timeout <= 0:
                raise ValueError("workers/interval/timeout invalid")
            if args.action == "supplement":
                if args.source not in {"sohu", "tencent"}:
                    raise ValueError("supplement requires --source sohu or tencent")
                if not args.symbol:
                    store.set_meta(
                        "supplement_sources",
                        sorted(set(store.meta("supplement_sources", [])) | {args.source}),
                    )
            http = _storage.PublicHTTP(store.root, interval=args.interval, timeout=args.timeout)
            http.history.refresh = args.refresh_metadata
            end = args.end or (
                store.meta("end") if args.action == "resume" else _storage.target_end()
            )
            if not end:
                end = _storage.target_end()
            _storage.parse_day(args.start)
            _storage.parse_day(end)
            if args.start > end:
                raise ValueError("start after end")
            horizon_advanced = end > (store.meta("end") or "")
            http.history.refresh = args.refresh_metadata or horizon_advanced
            if args.action == "update" and (
                args.refresh_metadata
                or horizon_advanced
                or not store.meta("universe_observed_at")
                or store.meta("universe_observed_at")[:10] != _storage.now()[:10]
            ):
                records, receipts = _storage.exchange_universe(http)
                store.save_universe(records, receipts, args.start, end)
            latest_calendar = store.db.execute("SELECT max(date) FROM calendar").fetchone()[0]
            if not latest_calendar or latest_calendar < end or args.refresh_metadata:
                days, receipts = _storage.calendar(http, end)
                store.save_calendar(days, receipts)
            http.history.refresh = False
            if args.action == "update" and end > (store.meta("end") or ""):
                recent = (_storage.parse_day(end) - timedelta(days=7)).isoformat()
                symbols = [_storage._security(s) for s in args.symbol] if args.symbol else []
                selection = (
                    " AND symbol IN (" + ",".join("?" for _ in symbols) + ")" if symbols else ""
                )
                store.db.execute(
                    "UPDATE jobs SET state='pending' WHERE state IN ('failed','empty_unknown') AND end>=? AND "
                    + _storage.HTTP_JOB_PREDICATE
                    + selection,
                    (recent, *_storage.HTTP_JOB_SOURCES, *symbols),
                )
                store.db.commit()
            if args.action in ("update", "supplement"):
                from ashare_data.reference.code_history import refresh_code_history

                # Resolve retrospective identifiers before creating any jobs:
                # current provider codes may be backfilled before their lifetime.
                store.set_meta("start", min(args.start, store.meta("start") or args.start))
                store.set_meta("end", max(end, store.meta("end") or end))
                refresh_code_history(store, http)
            store.plan(
                args.start,
                end,
                args.repair_gaps,
                args.retry_failed,
                [_storage._security(s) for s in args.symbol] if args.symbol else None,
            )
            if args.action == "supplement":
                # A targeted source repair does not silently subscribe the entire
                # market to that additional provider on the next daily update.
                store.plan_source(
                    args.source,
                    args.start,
                    end,
                    symbols=[_storage._security(s) for s in args.symbol] if args.symbol else None,
                )
                store.db.commit()
            print(json.dumps(dict(planned=store.summary()), ensure_ascii=False), flush=True)
            store.run(
                http,
                workers=args.workers,
                limit=args.limit_jobs,
                only_source=args.source if args.action == "supplement" else None,
                symbols=[_storage._security(s) for s in args.symbol] if args.symbol else None,
            )
            return store.report()
    finally:
        store.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    add_parser(sub)
    args = parser.parse_args(argv)
    print(json.dumps(command(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
