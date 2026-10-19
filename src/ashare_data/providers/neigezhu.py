"""Bounded, read-only native labels from an explicitly pinned local HF asset.

The manifest certifies downloaded bytes, not exchange truth. No resampling,
opening-label folding, adjustment, repair, fallback or execution admission.
"""

import csv
import hashlib
import io
import json
import math
from datetime import timedelta
from pathlib import Path
import re

import duckdb
import numpy as np
import pandas as pd

from ashare_data.core.model import require, timestamp
from ashare_data.market.neigezhu_quality import diagnostics
from ashare_data.research.datasets import ResearchResult
from ashare_data.research.minute_policy import native_denials


class NativeMinuteResult(ResearchResult):
    """Keep IEEE values in the frame; encode exceptional scalars losslessly in JSON."""

    def to_dict(self):
        result = super().to_dict()
        for row in result["rows"]:
            for field in ("open", "high", "low", "close", "volume", "amount"):
                if field in row["null_fields"]:
                    row[field] = None
                elif isinstance(row[field], float) and not math.isfinite(row[field]):
                    row[field] = (
                        "NaN"
                        if math.isnan(row[field])
                        else ("+Infinity" if row[field] > 0 else "-Infinity")
                    )
        return result


def _fingerprint(path):
    s = path.stat()
    return (s.st_size, s.st_mtime_ns, s.st_ino)


def _sha(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


class NeigezhuMinuteView:
    """Read one security and <=60 days per call; no persistent data directory.

    ``entry`` is the caller's existing deployment.json. ``manifest`` is the
    independently sealed file_manifest.csv from a completed all-file audit.
    Its expected SHA256 must come from that review, not from an untrusted file.
    A source file is rehashed on first use and whenever its fingerprint changes.
    """

    def __init__(
        self,
        *,
        entry,
        manifest,
        expected_manifest_sha256,
        review=None,
        expected_review_sha256=None,
        source_root=None,
    ):
        self.entry = Path(entry).expanduser().resolve()
        doc = json.loads(self.entry.read_bytes())
        root = Path(doc.get("data_root", ".")).expanduser()
        root = (root if root.is_absolute() else self.entry.parent / root).resolve()
        target = Path(
            source_root if source_root is not None else doc["assets"]["minute-hf-download"]["path"]
        ).expanduser()
        self.root = (target if target.is_absolute() else root / target).resolve()
        self.manifest = Path(manifest).resolve()
        raw = self.manifest.read_bytes()
        require(
            hashlib.sha256(raw).hexdigest() == expected_manifest_sha256,
            "NEIGEZHU_MANIFEST_INTEGRITY",
            "Pinned audit manifest does not match",
        )
        self.manifest_sha256 = expected_manifest_sha256
        rows = list(csv.DictReader(io.StringIO(raw.decode())))
        self.files = {r["path"]: r for r in rows if r["path"].startswith("data/stock_1m/")}
        require(
            len(self.files) == sum(r["path"].startswith("data/stock_1m/") for r in rows),
            "NEIGEZHU_MANIFEST_INTEGRITY",
            "Duplicate source path",
        )
        self._verified = {}
        self.review_sha256 = None
        self.restrictions = {}
        require(
            (review is None) == (expected_review_sha256 is None),
            "NEIGEZHU_REVIEW_INTEGRITY",
            "Review and its external SHA256 pin must be supplied together",
        )
        if review is not None:
            raw = Path(review).read_bytes()
            require(
                hashlib.sha256(raw).hexdigest() == expected_review_sha256,
                "NEIGEZHU_REVIEW_INTEGRITY",
                "Pinned quality review does not match",
            )
            document = json.loads(raw)
            require(
                document["schema"] == "neigezhu-quality-overlay.v1"
                and document["manifest_sha256"] == self.manifest_sha256,
                "NEIGEZHU_REVIEW_INTEGRITY",
                "Review belongs to another manifest",
            )
            for rule in document["ranges"]:
                require(
                    rule["path"] in self.files
                    and rule["source_sha256"] == self.files[rule["path"]]["computed_hash"]
                    and type(rule["first_row"]) is int
                    and type(rule["last_row"]) is int
                    and 0 <= rule["first_row"] <= rule["last_row"]
                    and rule["price_status"] in {"invalid", "conflict"}
                    and rule["evidence"],
                    "NEIGEZHU_REVIEW_INTEGRITY",
                    "Invalid bound restriction",
                )
                self.restrictions.setdefault(rule["path"], []).append(rule)
            self.review_sha256 = expected_review_sha256

    def get_price(self, security, *, mode, start, end, labels=None):
        """Inclusive native-label query. Bad and zero-volume rows remain visible.

        SH/SZ use .XSHG/.XSHE. BJ uses the explicit source identity BJ.920xxx;
        it is not silently mapped into a consumer's unsupported exchange code.
        No missing labels are fabricated. A date range alone cannot establish
        expected trading minutes; explicit ``labels`` reports exact omissions.
        """
        require(
            mode == "retrospective",
            "NEIGEZHU_RESEARCH_MODE",
            "Explicit mode='retrospective' is required",
        )
        match = re.fullmatch(r"(\d{6})\.(XSHG|XSHE)", security)
        bj = re.fullmatch(r"BJ\.(\d{6})", security)
        require(
            match is not None or bj is not None,
            "INVALID_SECURITY",
            "Use an exact native security identity",
        )
        exchange, code = (
            ({"XSHG": "SH", "XSHE": "SZ"}[match[2]], match[1]) if match else ("BJ", bj[1])
        )
        a, b = timestamp(start, minute=True), timestamp(end, minute=True)
        require(
            a <= b and b - a <= timedelta(days=60), "NEIGEZHU_QUERY_BOUND", "Choose at most 60 days"
        )
        wanted = None
        if labels is not None:
            require(
                isinstance(labels, (list, tuple)) and 0 < len(labels) <= 15000,
                "NEIGEZHU_QUERY_BOUND",
                "Choose 1..15000 explicit labels",
            )
            wanted = {timestamp(t, minute=True) for t in labels}
            require(
                len(wanted) == len(labels) and all(a <= t <= b for t in wanted),
                "NEIGEZHU_QUERY_BOUND",
                "Labels must be unique and within bounds",
            )
        name = f"data/stock_1m/{exchange}/{code}.parquet"
        require(name in self.files, "NEIGEZHU_SOURCE_NOT_IN_MANIFEST", name)
        record, path = self.files[name], (self.root / name).resolve()
        require(
            path.is_relative_to(self.root) and path.is_file(),
            "NEIGEZHU_SOURCE_INTEGRITY",
            "Source path escapes root or is missing",
        )
        require(
            record["hash_matches"] == "True"
            and record["input_unchanged_after_scan"] == "True"
            and record["status"] in {"scanned", "scanned_with_errors"}
            and re.fullmatch(r"[0-9a-f]{64}", record["computed_hash"]),
            "NEIGEZHU_SOURCE_INTEGRITY",
            "Source lacks a completed immutable byte audit",
        )
        before = _fingerprint(path)
        require(
            before[0] == int(record["expected_bytes"]),
            "NEIGEZHU_SOURCE_INTEGRITY",
            "Source size changed",
        )
        if self._verified.get(name) != before:
            require(
                _sha(path) == record["computed_hash"],
                "NEIGEZHU_SOURCE_INTEGRITY",
                "Source content changed",
            )
            require(
                _fingerprint(path) == before,
                "NEIGEZHU_SOURCE_INTEGRITY",
                "Source changed while hashing",
            )
            self._verified[name] = before
        with duckdb.connect(
            ":memory:",
            config={
                "threads": 1,
                "memory_limit": "128MB",
                "autoinstall_known_extensions": "false",
                "autoload_known_extensions": "false",
            },
        ) as con:
            frame = con.execute(
                "SELECT timestamp,open,high,low,close,volume,turnover,file_row_number, "
                + ",".join(
                    f"{f} IS NULL AS null_{f}"
                    for f in ("open", "high", "low", "close", "volume", "turnover")
                )
                + " "
                "FROM read_parquet(?,file_row_number=true) WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp LIMIT 15001",
                [str(path), a.replace(tzinfo=None), b.replace(tzinfo=None)],
            ).fetchdf()
        require(
            _fingerprint(path) == before,
            "NEIGEZHU_SOURCE_INTEGRITY",
            "Source changed while reading",
        )
        require(len(frame) <= 15000, "NEIGEZHU_QUERY_BOUND", "Too many rows; narrow the interval")
        frame["source_label"] = (
            pd.to_datetime(frame.pop("timestamp"))
            .dt.tz_localize("Asia/Shanghai")
            .map(lambda x: x.isoformat())
        )
        if wanted is not None:
            frame = frame[frame.source_label.isin({t.isoformat() for t in wanted})].copy()
        frame.rename(columns={"turnover": "amount", "file_row_number": "source_row"}, inplace=True)
        frame["null_fields"] = [
            [
                "amount" if f == "turnover" else f
                for f in ("open", "high", "low", "close", "volume", "turnover")
                if row["null_" + f]
            ]
            for row in frame.to_dict("records")
        ]
        frame.drop(
            columns=["null_" + f for f in ("open", "high", "low", "close", "volume", "turnover")],
            inplace=True,
        )
        flags, _ = diagnostics(
            frame[["open", "high", "low", "close"]].to_numpy(dtype=float, na_value=np.nan),
            frame.volume.to_numpy(dtype=float, na_value=np.nan),
            frame.amount.to_numpy(dtype=float, na_value=np.nan),
        )
        names = [
            "nonfinite_price",
            "nonfinite_volume",
            "nonfinite_amount",
            "negative_volume",
            "negative_amount",
            "noninteger_volume",
            "nonpositive_price",
            "ohlc_envelope_error",
            "zero_volume_nonzero_amount",
            "positive_volume_zero_amount",
            "vwap_beyond_storage_envelope",
            "vwap_beyond_candidate_yuan_rounding",
            "outside_cumulative_100_local_candidate",
            "new_extrema_inside_vwap_envelope",
            "new_extrema_beyond_99_share_and_one_yuan_hypotheses",
        ]
        frame["diagnostics"] = [[k for k in names if flags[k][i]] for i in range(len(frame))]
        for i in frame.index:
            frame.at[i, "diagnostics"].extend("null_" + f for f in frame.at[i, "null_fields"])
        frame["price_structurally_usable"] = ~(
            flags["nonfinite_price"] | flags["nonpositive_price"] | flags["ohlc_envelope_error"]
        )
        # Passing structural checks is not market correctness certification.
        frame["price_status"] = np.where(frame.price_structurally_usable, "unverified", "invalid")
        frame["review_evidence"] = [[] for _ in range(len(frame))]
        for rule in self.restrictions.get(name, []):
            mask = frame.source_row.between(rule["first_row"], rule["last_row"])
            for i in frame.index[mask]:
                if rule["rule"] not in frame.at[i, "diagnostics"]:
                    frame.at[i, "diagnostics"].append(rule["rule"])
                frame.at[i, "review_evidence"].append(rule["evidence"])
            # Source conflicts do not erase a stronger structural invalidity.
            frame.loc[mask & (frame.price_status != "invalid"), "price_status"] = rule[
                "price_status"
            ]
        frame["activity_status"] = np.select(
            [
                flags["nonfinite_volume"],
                flags["negative_volume"] | flags["noninteger_volume"],
                frame.volume.to_numpy(dtype=float, na_value=np.nan) == 0,
            ],
            ["unknown_volume", "invalid_volume", "no_positive_volume_observed"],
            default="positive_volume_observed_unverified",
        )
        frame["use_decision"] = [native_denials(r) for r in frame.to_dict("records")]
        frame["permission_status"] = "not_granted_by_reader"
        frame["execution_admitted"] = False
        frame["new_valuation_admitted"] = False
        frame["security"] = security
        frame["source_file_sha256"] = record["computed_hash"]
        frame["source_path"] = name
        report = dict(
            schema="neigezhu-native-read.v2",
            rows=len(frame),
            source_root=str(self.root),
            source_path=name,
            source_sha256=record["computed_hash"],
            manifest_sha256=self.manifest_sha256,
            quality_review_sha256=self.review_sha256,
            entry=str(self.entry),
            entry_asset="minute-hf-download",
            range_inclusive=[a.isoformat(), b.isoformat()],
            missing_requested_labels=sorted(
                {t.isoformat() for t in wanted} - set(frame.source_label)
            )
            if wanted is not None
            else None,
            native_labels_preserved=True,
            opening_rows_folded=0,
            repaired_rows=0,
            unit_conversion=None,
            source_claimed_units={"price": "CNY", "volume": "shares", "amount": "CNY"},
            interval_verified=False,
            historical_pit=False,
            execution_admitted=False,
            permission_status="not_granted_by_reader; explicit bounded consumer profile may grant uses unless row denies them",
            exceptional_value_encoding="DataFrame keeps IEEE values; JSON uses NaN/+Infinity/-Infinity strings and original nulls remain null",
            coverage_basis={
                "requested_label_omissions": "Only explicitly requested labels; not verified missing trades",
                "publisher_expected_days": "Publisher calendar/listing span, separate from exchange and suspension evidence",
                "actual_missing_trading_days": None,
                "historical_investible_universe_verified": False,
            },
            coverage="Explicit-label omissions only; no fabricated calendar, suspension or identity state",
            limits=[
                "Every source price/volume remains unverified absent a bounded reference review",
                "Integer-yuan and cumulative-100 diagnostics are hypotheses, not acceptance tolerances",
                "VWAP/extrema envelopes require same trade set, exact units/volume/amount and actual extrema; reference providers can violate these assumptions too",
                "Invalid or absent quotes cannot close/delete positions; use separate account/state handling",
                "A passed row does not prove same-day extrema, interval semantics, actions or absence of halts",
            ],
        )
        return NativeMinuteResult(frame, report)
