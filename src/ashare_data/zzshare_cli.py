"""CLI for explicit opt-in research, separate from default price source selection."""

from .model import require
from .storage import Store
from .zzshare import ZzshareSource, _enabled


def add_parsers(sub):
    sub.add_parser("zzshare-capabilities", help="查看默认关闭的zzshare研究源能力，无网络")
    for name in (
        "zzshare-import",
        "zzshare-fetch",
        "zzshare-query",
        "zzshare-snapshots",
        "zzshare-recover",
    ):
        parser = sub.add_parser(name, help="显式zzshare研究操作；不授予PIT/执行许可")
        parser.add_argument(
            "--enable-research", action="store_true", help="显式启用默认关闭的研究源"
        )
        if name == "zzshare-import":
            parser.add_argument("directory")
        elif name == "zzshare-fetch":
            parser.add_argument("security")
            parser.add_argument("--start", required=True)
            parser.add_argument("--end", required=True)
            parser.add_argument("--timeout", type=float, default=15)
        elif name == "zzshare-query":
            parser.add_argument(
                "dataset",
                choices=["daily", "price", "descriptor", "coverage", "quality", "lineage"],
            )
            parser.add_argument("--capture", required=True)
            parser.add_argument(
                "--allow-partial",
                action="store_true",
                help="只限daily/price：保留缺日/未知字段诊断",
            )


def dispatch(args):
    if args.command == "zzshare-capabilities":
        return ZzshareSource.capabilities()
    _enabled(args.enable_research)
    require(args.store is not None, "STORE_REQUIRED", "研究操作需指定--store")
    store = Store(args.store)
    if args.command == "zzshare-import":
        return {"capture_id": store.import_zzshare_capture(args.directory, enable_research=True)}
    if args.command == "zzshare-fetch":
        return {
            "capture_id": ZzshareSource(enabled=True, timeout=args.timeout).fetch(
                store, security=args.security, start_date=args.start, end_date=args.end
            )
        }
    if args.command == "zzshare-snapshots":
        return store.zzshare_snapshots(enable_research=True)
    if args.command == "zzshare-recover":
        return store.recover_zzshare(enable_research=True)
    view = store.zzshare(args.capture, enable_research=True)
    if args.dataset in ("daily", "price"):
        return view.get_daily(strict=not args.allow_partial).to_dict()
    require(not args.allow_partial, "INVALID_ARGUMENT", "--allow-partial仅用于daily/price")
    return getattr(view, args.dataset)()
