"""Offline, explicitly pinned corporate-action and share-history queries."""

from ashare_data.corporate_actions.history import CorporateHistoryView, DATE_FIELDS, SHARE_FIELDS
from ashare_data.core.model import require


def add_parser(sub):
    p = sub.add_parser(
        "corporate-history", help="固定公司行动/历史股本证据包；只读，含覆盖与AQ导出"
    )
    p.add_argument(
        "operation",
        choices=("descriptor", "lineage", "events", "shares", "shares-on", "coverage", "export-aq"),
    )
    p.add_argument("--binding", required=True)
    p.add_argument("--sha256", required=True)
    p.add_argument("--security", action="append")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--date")
    p.add_argument("--date-role", choices=sorted(DATE_FIELDS | {"any", "economic"}), default="any")
    p.add_argument("--source", choices=("sina_structure", "sina_float"), default="sina_structure")
    p.add_argument("--include-prior", action="store_true")
    p.add_argument("--carry", choices=("none", "last_observation"), default="none")
    p.add_argument(
        "--field", choices=sorted(SHARE_FIELDS), help="Select one source-observed share field"
    )
    p.add_argument(
        "--visibility",
        choices=("posthoc", "announced_next_day", "received", "verified"),
        default="posthoc",
    )
    p.add_argument("--as-of")
    p.add_argument("--tax-model", choices=("gross_cash_no_tax", "external_tax_ledger"))
    p.add_argument("--accept-source-scoped-coverage", action="store_true")


def command(args):
    view = CorporateHistoryView(args.binding, expected_binding_sha256=args.sha256)
    if args.operation in {"descriptor", "lineage"}:
        return getattr(view, args.operation)()
    require(args.security, "CORPORATE_SCOPE", "At least one --security is required")
    clock = dict(visibility=args.visibility, as_of=args.as_of)
    if args.operation == "shares-on":
        require(
            len(args.security) == 1 and args.date,
            "CORPORATE_SCOPE",
            "One security and --date are required",
        )
        return view.shares_on(
            args.security[0],
            args.date,
            source=args.source,
            carry=args.carry,
            field=args.field,
            **clock,
        )
    require(args.start and args.end, "CORPORATE_SCOPE", "--start and --end are required")
    window = dict(start=args.start, end=args.end)
    if args.operation == "events":
        return view.events(args.security, date_role=args.date_role, **window, **clock)
    if args.operation == "shares":
        require(len(args.security) == 1, "CORPORATE_SCOPE", "Select one share-history security")
        return view.share_history(
            args.security[0],
            source=args.source,
            include_prior=args.include_prior,
            **window,
            **clock,
        )
    require(
        args.visibility == "posthoc" and args.as_of is None,
        "CORPORATE_VISIBILITY",
        "Coverage and engine export describe the posthoc package",
    )
    if args.operation == "coverage":
        return view.coverage(args.security, **window)
    require(args.tax_model, "CORPORATE_TAX", "--tax-model is required")
    return view.export_aq(
        args.security,
        tax_model=args.tax_model,
        accept_source_scoped_coverage=args.accept_source_scoped_coverage,
        **window,
    )
