"""Publish frozen research inputs; the mutable source library stays in place."""

from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
from pathlib import Path
import time

from ashare_data.core.model import DataError, canonical, digest, require
from ashare_data.product.daily import LIMITATIONS
from ashare_data.product import references as reference_api
from ashare_data.product.validation import validate_covered_input
from ashare_data.storage.immutable import Store, identifier, immutable_write, now

MAX_INPUT_BYTES = 64 * 1024 * 1024
SCHEMA = "ad-local-daily-product.v1"
PUBLICATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS local_daily_products (
 dataset_id TEXT PRIMARY KEY, input_sha256 TEXT NOT NULL, created_at TEXT NOT NULL);
"""
ASSUMPTIONS = [
    "Explicit price-only diagnostic: corporate-action cash/shares and price limits are excluded; "
    "returns are not total return or economic acceptance.",
    "Suspension state is retrospective: explicit provider tradestatus is retained; otherwise "
    "positive observed share volume proves trading occurred, not preopen tradability.",
    "Listing/code bounds use current recorded facts; historical names and PIT are not certified.",
]


def _history_digest(value):
    # AQ's ad-daily-portfolio.v1 codec uses JSON's default ensure_ascii policy.
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _producer():
    from ashare_data import __version__

    package = Path(__file__).resolve().parents[1]
    files = [
        *package.joinpath("product").glob("*.py"),
        package / "storage/local_daily.py",
        package / "storage/immutable.py",
        package / "market/client.py",
        package / "core/model.py",
    ]
    hashes = {
        str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(files)
    }
    return {
        "name": "ashare-data",
        "version": __version__,
        "owner_api": "ashare_data.product.LocalDailyProduct",
        "source_files": hashes,
        "source_sha256": digest(hashes),
    }


def _number(row, field, gaps):
    value = row.get(field)
    try:
        n = Decimal(value) if isinstance(value, str) else Decimal(str(value))
        if n.is_finite() and (n >= 0 if field == "volume" else n > 0):
            return n
    except (InvalidOperation, TypeError, ValueError):
        pass
    gaps.append(
        {
            "code": "DAILY_NUMBER_UNAVAILABLE",
            "security": row["security"],
            "date": row["date"],
            "field": field,
            "value": value,
            "detail": "A finite positive original value is required (volume may be zero)",
        }
    )
    return None


def _portfolio(bundle, securities, start, end, successor, *, ordinary=False):
    gaps = list(bundle["gaps"])
    rows = []
    known_receipts = {r["id"] for r in bundle["receipt"]["responses"] if r.get("sha256")}
    for original in bundle["rows"]:
        row = dict(original)
        required = ("close",) if row["date"] < start else ("open", "high", "low", "close", "volume")
        numbers = {field: _number(row, field, gaps) for field in required}
        if not row.get("row_hash") or row.get("response_id") not in known_receipts:
            gaps.append(
                {
                    "code": "SOURCE_RECEIPT_MISSING",
                    "security": row["security"],
                    "date": row["date"],
                    "field": "source_receipt",
                    "detail": "The original row hash and a source body SHA are required",
                }
            )
        if row["date"] >= start:
            op, hi, lo, cl = (numbers[f] for f in ("open", "high", "low", "close"))
            if (
                all(v is not None for v in (op, hi, lo, cl))
                and not lo <= min(op, cl) <= max(op, cl) <= hi
            ):
                gaps.append(
                    {
                        "code": "INCONSISTENT_OHLC",
                        "security": row["security"],
                        "date": row["date"],
                        "field": "daily",
                        "detail": "Original OHLC values are inconsistent",
                    }
                )
            volume = numbers["volume"]
            status = row.get("tradestatus")
            if ordinary:
                pass  # Pinned reference evidence supplies state; prices cannot infer it.
            elif status in ("0", "1"):
                row["suspended"] = status == "0"
                row["state_origin"] = "retrospective_provider_tradestatus"
                if status == "0" and volume is not None and volume > 0:
                    gaps.append(
                        {
                            "code": "SOURCE_STATE_CONFLICT",
                            "security": row["security"],
                            "date": row["date"],
                            "field": "suspended",
                            "detail": "Source declares suspension but reports positive volume",
                        }
                    )
            elif volume is not None and volume > 0:
                row["suspended"] = False
                row["state_origin"] = "retrospective_positive_observed_volume"
            else:
                row["suspended"] = None
                gaps.append(
                    {
                        "code": "SUSPENSION_UNKNOWN",
                        "security": row["security"],
                        "date": row["date"],
                        "field": "suspended",
                        "detail": "Zero/missing volume does not establish suspension state",
                    }
                )
            row["upper_limit"] = row["lower_limit"] = None
        rows.append(row)
    for record in [*bundle["calendar_evidence"], successor]:
        if record.get("receipt") not in known_receipts:
            gaps.append(
                {
                    "code": "CALENDAR_RECEIPT_MISSING",
                    "date": record["date"],
                    "field": "calendar",
                    "detail": "Calendar source receipt is unavailable",
                }
            )
    require(
        not gaps,
        "PRODUCT_DATA_GAPS",
        "The selected scope cannot form a complete input",
        {"gaps": gaps, "receipt": bundle["receipt"]},
    )
    calendar = [*bundle["calendar"], successor["date"]]
    index = {(r["security"], r["date"]): r for r in rows}
    history_hashes, reads = {}, [{"operation": "prices", "receipt": bundle["receipt"]}]
    for pos, date in enumerate(calendar):
        if not start <= date <= end:
            continue
        history = {
            sid: [index[sid, d]["close"] for d in calendar[pos - 120 : pos]] for sid in securities
        }
        history_hashes[date] = _history_digest(history)
        reads.append(
            {
                "operation": "history",
                "date": date,
                "count": 120,
                "first": calendar[pos - 120],
                "last": calendar[pos - 1],
                "response_sha256": history_hashes[date],
                "source_receipt_sha256": bundle["receipt"]["receipt_sha256"],
            }
        )
    reads.append(
        {"operation": "successor_session", "row": successor, "response_sha256": digest(successor)}
    )
    return {
        "schema": "ad-daily-portfolio.v1",
        "producer": _producer(),
        "real_market_data": True,
        "source_refs": [
            {
                "kind": "local_daily_read",
                **bundle["receipt"]["asset"],
                "receipt_sha256": bundle["receipt"]["receipt_sha256"],
                "rows_sha256": bundle["receipt"]["rows_sha256"],
            }
        ],
        "limitations": [*LIMITATIONS, "Price-only diagnostic; no total-return or covered mode."],
        "assumptions": list(ASSUMPTIONS),
        "price_basis": "raw",
        "volume_unit": "share",
        "securities": securities,
        "calendar": calendar,
        "daily": rows,
        "actions": [],
        "action_coverage": {"status": "unavailable", "scope": "Not part of this publication"},
        "additional_gaps": [],
        "listing_coverage": "scoped_research_model",
        "listing_evidence": bundle["listing_evidence"],
        "state_mode": "retrospective_model",
        "research_model": "price_only_no_actions",
        "price_limit_policy": "disabled_for_price_only_diagnostic",
        "economic_acceptance": "price_only_diagnostic_not_economic_acceptance",
        "strict_preopen_state_verified": False,
        "historical_pit_verified": False,
        "owner_history_sha256": history_hashes,
        "owner_api_reads": reads,
    }


def _prepare(product, securities, start, end, source, *, ordinary):
    with product._read() as library:
        calendar = [dict(r) for r in library.db.execute("SELECT * FROM calendar ORDER BY date")]
        days = [r["date"] for r in calendar]
        require(
            start in days and end in days,
            "PRODUCT_SESSION_UNAVAILABLE",
            "Publication endpoints must be actual stored calendar sessions",
            {"start": start, "end": end, "missing": [d for d in (start, end) if d not in days]},
        )
        first, last = days.index(start), days.index(end)
        require(
            first >= 120 and last + 1 < len(days),
            "PRODUCT_WARMUP_UNAVAILABLE",
            "120 earlier sessions and a successor calendar session are required",
            {"available_warmup": first, "successor_available": last + 1 < len(days)},
        )
        warmup, successor = days[first - 120], calendar[last + 1]
        selection = product._prices(library, securities, start, end, source)
        require(
            not selection["gaps"],
            "PRODUCT_DATA_GAPS",
            "The requested price window has missing observations",
            {"gaps": selection["gaps"], "receipt": selection["receipt"]},
        )
        selected_sources = {r["security"]: r["source"] for r in selection["coverage"]}
        # Freeze the same source the displayed price query selected. Missing warmup
        # never triggers an implicit replacement or a stitched source series.
        bundle = product._prices(
            library, securities, warmup, end, source, selected_sources=selected_sources
        )
        # Keep the successor's original receipt in the same transaction as all prices.
        bundle["receipt"] = product._receipt(
            library,
            "publish_input",
            bundle["receipt"]["request"],
            bundle["rows"],
            [*bundle["calendar_evidence"], successor],
            bundle["listing_evidence"],
        )
        document = _portfolio(bundle, securities, start, end, successor, ordinary=ordinary)
        document["owner_api_reads"].append(
            {"operation": "source_selection", "receipt": selection["receipt"]}
        )
    return document, bundle, selection, warmup, successor


def _assess(document, bundle, securities, start, end, source, references):
    scope = dict(securities=securities, start=start, end=end, source=source)
    try:
        evidence, schedule, gaps, reads = reference_api.assess(
            references, securities, start, end, bundle["rows"], document["calendar"]
        )
    except DataError as exc:
        evidence, schedule, gaps, reads = None, None, [reference_api.gap(exc)], []
    result = dict(
        schema="ad-local-daily-coverage.v1",
        scope=scope,
        ready=not gaps,
        gaps=gaps,
        assumptions=(
            evidence["market"]["accepted_assumptions"] if evidence and evidence["market"] else []
        ),
        limitations=list(reference_api.LIMITATIONS),
        reference_set=evidence["reference_set"] if evidence else None,
        source_bindings=evidence["source_bindings"] if evidence else {},
        selected_sources={r["security"]: r["source"] for r in bundle["coverage"]},
        price_receipt_sha256=bundle["receipt"]["receipt_sha256"],
        reference_evidence_sha256=digest(evidence) if evidence else None,
        corporate_coverage=schedule["coverage"] if schedule else None,
        owner_api_reads=reads,
    )
    result["coverage_sha256"] = digest(result)
    return result, evidence, schedule


def coverage(product, securities, start, end, source, references):
    """Full ordinary admission, without initializing any output Store or account."""
    try:
        document, bundle, _, _, _ = _prepare(product, securities, start, end, source, ordinary=True)
        return _assess(document, bundle, securities, start, end, source, references)[0]
    except DataError as exc:
        result = dict(
            schema="ad-local-daily-coverage.v1",
            scope=dict(securities=securities, start=start, end=end, source=source),
            ready=False,
            gaps=exc.details.get("gaps") or [reference_api.gap(exc, field="prices")],
            assumptions=[],
            limitations=list(reference_api.LIMITATIONS),
            reference_set=None,
            source_bindings={},
            owner_api_reads=[],
            price_issue=exc.as_dict(),
        )
        try:
            result["reference_set"], _, result["source_bindings"] = reference_api.normalize(
                references
            )
        except DataError:
            pass
        result["coverage_sha256"] = digest(result)
        return result


def _ordinary(document, evidence, schedule, report):
    for key in ("research_model", "price_limit_policy"):
        document.pop(key, None)
    document.update(
        reference_evidence=evidence,
        coverage_sha256=report["coverage_sha256"],
        action_coverage={
            "status": "separate_corporate_schedule",
            "scope": "AQ must attach the frozen corporate schedule before account use",
        },
        limitations=[x for x in LIMITATIONS if not x.startswith("Corporate actions,")]
        + list(reference_api.LIMITATIONS),
        assumptions=[
            "Source-scoped market-reference models explicitly accepted for retrospective research.",
            {"market_reference": evidence["market"]["accepted_assumptions"]},
        ],
        economic_acceptance="conditional_research_only",
    )
    states = {(r["security"], r["date"]): r for r in evidence["market"]["rows"]}
    for row in document["daily"]:
        state = states.get((row["security"], row["date"]))
        if state is None:
            continue
        for field in ("suspended", "reference_price", "upper_limit", "lower_limit"):
            row[field] = state[field]["value"]
        row["state_evidence"] = {"reference_sha256": digest(state)}
        row["state_origin"] = evidence["reference_set"]["kind"]
        row["limit_origin"] = (
            "Pinned public reference facts; derived values retain their rule model"
        )
    document["owner_api_reads"].extend(report["owner_api_reads"])
    document["source_refs"].append(
        {
            "kind": "product_reference_evidence",
            "source_bindings": evidence["source_bindings"],
            "response_sha256": digest(evidence),
        }
    )
    validate_covered_input(document, schedule)


@contextmanager
def _publication_lock(destination):
    # Serialize creation as well as publication; Store.init otherwise races on a new root.
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (destination.parent / ("." + destination.name + ".product.lock")).open("a+b") as stream:
        until = time.monotonic() + 30
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                require(
                    time.monotonic() < until,
                    "WRITER_BUSY",
                    "Product publication is busy; retry this exact request",
                )
                time.sleep(0.02)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def publish(product, store_root, securities, start, end, source, *, mode, references):
    require(
        mode in {"covered_research", "price_only_no_limits"},
        "PRODUCT_MODE",
        "Choose an explicit supported publication mode",
    )
    destination = Path(store_root).expanduser().resolve()
    require(
        not destination.is_relative_to(product.root),
        "PRODUCT_OUTPUT_OVERLAP",
        "The publication Store must be outside the read-only source library",
    )
    ordinary = mode == "covered_research"
    document, bundle, selection, warmup, successor = _prepare(
        product, securities, start, end, source, ordinary=ordinary
    )
    report, schedule = None, None
    if ordinary:
        report, evidence, schedule = _assess(
            document, bundle, securities, start, end, source, references
        )
        require(
            report["ready"],
            "PRODUCT_COVERAGE_GAPS",
            "The chosen ordinary scope has source gaps",
            report,
        )
        _ordinary(document, evidence, schedule, report)
    raw = canonical(document)
    require(
        len(raw) <= MAX_INPUT_BYTES,
        "PRODUCT_RESOURCE_LIMIT",
        "Publication exceeds 64 MiB; reduce securities or window",
    )
    input_hash = hashlib.sha256(raw).hexdigest()
    manifest = {
        "schema": SCHEMA,
        "producer": document["producer"],
        "securities": securities,
        "start": start,
        "end": end,
        "warmup_start": warmup,
        "warmup_sessions": 120,
        "next_session": successor["date"],
        "source_selection": source,
        "source_selection_scope": {"start": start, "end": end},
        "input_scope": {"start": warmup, "end": end},
        "source_selection_receipt": selection["receipt"],
        "selected_sources": {r["security"]: r["source"] for r in bundle["coverage"]},
        "portfolio_input_sha256": input_hash,
        "portfolio_input_path": "input.json",
        "portfolio_input_bytes": len(raw),
        "receipt": bundle["receipt"],
        "mode": mode,
        "reference_set": report["reference_set"] if report else None,
        "source_bindings": report["source_bindings"] if report else {},
        "coverage_sha256": report["coverage_sha256"] if report else None,
        "limitations": document["limitations"],
        "dependencies": {
            "execution": "input.json and this manifest; no live-library dependency",
            "original_evidence": "Source raw bodies remain in the preserved library",
        },
    }
    if ordinary:
        schedule_raw, coverage_raw = canonical(schedule), canonical(report)
        require(
            max(len(schedule_raw), len(coverage_raw)) <= MAX_INPUT_BYTES,
            "PRODUCT_RESOURCE_LIMIT",
            "Reference response exceeds 64 MiB",
        )
        manifest.update(
            corporate_schedule_path="corporate-schedule.json",
            corporate_schedule_sha256=hashlib.sha256(schedule_raw).hexdigest(),
            corporate_schedule_bytes=len(schedule_raw),
            coverage_path="coverage.json",
            coverage_file_sha256=hashlib.sha256(coverage_raw).hexdigest(),
            coverage_bytes=len(coverage_raw),
        )
    dataset_id = digest(manifest)
    # Nothing is initialized or written until the complete source scope passed validation.
    with _publication_lock(destination):
        store = Store.init(destination)
        with store._writer():
            root = store.root / "local_daily_products" / dataset_id
            root.mkdir(parents=True, exist_ok=True)
            immutable_write(root / "input.json", raw)
            if ordinary:
                immutable_write(root / "corporate-schedule.json", schedule_raw)
                immutable_write(root / "coverage.json", coverage_raw)
            immutable_write(root / "manifest.json", canonical(manifest))
            with store._db(write=True) as db:
                db.executescript(PUBLICATION_SCHEMA)
                db.execute(
                    "INSERT OR IGNORE INTO local_daily_products VALUES (?,?,?)",
                    (dataset_id, input_hash, now()),
                )
    view = LocalDailyProductView(store, dataset_id)
    return {
        "dataset_id": dataset_id,
        "portfolio_input_path": str(view.portfolio_input_path),
        "portfolio_input_sha256": input_hash,
        "corporate_schedule_path": str(root / "corporate-schedule.json") if ordinary else None,
        "corporate_schedule_sha256": manifest.get("corporate_schedule_sha256"),
        "mode": mode,
        "reference_set": manifest["reference_set"],
        "source_bindings": manifest["source_bindings"],
        "coverage_sha256": manifest["coverage_sha256"],
        "manifest": view.describe(),
    }


class LocalDailyProductView:
    """Public, hash-checked reads from a Store after the source library is unavailable."""

    def __init__(self, store, dataset_id):
        self.store, self.dataset_id = store, identifier(dataset_id)
        self._load()

    @property
    def portfolio_input_path(self):
        self._load()
        return self.store.root / "local_daily_products" / self.dataset_id / "input.json"

    def _load(self):
        with self.store._db() as db:
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='local_daily_products'"
            ).fetchone()
            record = (
                db.execute(
                    "SELECT * FROM local_daily_products WHERE dataset_id=?", (self.dataset_id,)
                ).fetchone()
                if table
                else None
            )
        require(
            record is not None, "PRODUCT_NOT_PUBLISHED", "Product is not registered in this Store"
        )
        root = self.store.root / "local_daily_products" / self.dataset_id
        try:
            require(
                not root.is_symlink()
                and not root.parent.is_symlink()
                and root.resolve().is_relative_to(self.store.root),
                "PRODUCT_INTEGRITY",
                "Release directory escapes the selected Store",
            )
            manifest = json.loads(self._bytes(root, "manifest.json"))
            require(
                manifest.get("portfolio_input_path", "input.json") == "input.json",
                "PRODUCT_INTEGRITY",
                "Input path must be inside this release",
            )
            raw = self._bytes(root, "input.json")
            require(
                digest(manifest) == self.dataset_id
                and manifest["schema"] == SCHEMA
                and hashlib.sha256(raw).hexdigest()
                == record["input_sha256"]
                == manifest["portfolio_input_sha256"]
                and len(raw) == manifest["portfolio_input_bytes"],
                "PRODUCT_INTEGRITY",
                "Published manifest or input hash differs",
            )
            document = json.loads(raw)
            require(
                document["schema"] == "ad-daily-portfolio.v1"
                and document["securities"] == manifest["securities"]
                and document["calendar"][0] == manifest["warmup_start"]
                and document["calendar"][120] == manifest["start"]
                and document["calendar"][-2:] == [manifest["end"], manifest["next_session"]],
                "PRODUCT_INTEGRITY",
                "Published input schema or scope differs",
            )
            schedule, report = None, None
            require(
                manifest["mode"] in {"covered_research", "price_only_no_limits"},
                "PRODUCT_INTEGRITY",
                "Unsupported publication mode",
            )
            if manifest["mode"] == "covered_research":
                for field, filename in (
                    ("corporate_schedule", "corporate-schedule.json"),
                    ("coverage", "coverage.json"),
                ):
                    require(
                        manifest[field + "_path"] == filename,
                        "PRODUCT_INTEGRITY",
                        "Reference file must be inside this release",
                    )
                    body = self._bytes(root, filename)
                    hash_key = "coverage_file_sha256" if field == "coverage" else field + "_sha256"
                    require(
                        len(body) == manifest[field + "_bytes"]
                        and hashlib.sha256(body).hexdigest() == manifest[hash_key],
                        "PRODUCT_INTEGRITY",
                        "Frozen reference file differs",
                    )
                    if field == "coverage":
                        report = json.loads(body)
                    else:
                        schedule = json.loads(body)
                expected_scope = {k: manifest[k] for k in ("securities", "start", "end")}
                require(
                    report["schema"] == "ad-local-daily-coverage.v1"
                    and report["ready"] is True
                    and report["gaps"] == []
                    and report["scope"]
                    == {**expected_scope, "source": manifest["source_selection"]}
                    and document["reference_evidence"]["scope"] == expected_scope
                    and digest({k: v for k, v in report.items() if k != "coverage_sha256"})
                    == report["coverage_sha256"]
                    == manifest["coverage_sha256"]
                    == document["coverage_sha256"],
                    "PRODUCT_INTEGRITY",
                    "Coverage scope or semantic identity differs",
                )
                for key in ("reference_set", "source_bindings"):
                    require(
                        manifest[key] == report[key] == document["reference_evidence"][key],
                        "PRODUCT_INTEGRITY",
                        "Reference selection or source binding differs",
                    )
                require(
                    report["reference_evidence_sha256"] == digest(document["reference_evidence"])
                    and report["corporate_coverage"] == schedule["coverage"]
                    and report["selected_sources"] == manifest["selected_sources"]
                    and report["price_receipt_sha256"] == manifest["receipt"]["receipt_sha256"],
                    "PRODUCT_INTEGRITY",
                    "Frozen coverage evidence differs",
                )
                validate_covered_input(document, schedule)
            else:
                require(
                    document.get("research_model") == "price_only_no_actions"
                    and not document.get("corporate_schedule")
                    and not document.get("reference_evidence"),
                    "PRODUCT_INTEGRITY",
                    "Diagnostic mode conflicts with ordinary evidence",
                )
            return manifest, document, schedule, report
        except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            raise DataError(
                "PRODUCT_INTEGRITY", "Published product is missing or malformed"
            ) from exc

    @staticmethod
    def _bytes(root, name):
        path = root / name
        require(
            not path.is_symlink()
            and path.resolve().is_relative_to(root)
            and path.stat().st_size <= MAX_INPUT_BYTES,
            "PRODUCT_INTEGRITY",
            "Published file escapes its release or exceeds its budget",
        )
        return path.read_bytes()

    def describe(self):
        return self._load()[0]

    def read(self):
        return self._load()[1]

    def read_corporate_schedule(self):
        return self._load()[2]

    def read_coverage(self):
        return self._load()[3]
