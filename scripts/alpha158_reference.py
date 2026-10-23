"""Generate a pinned Qlib Alpha158 oracle, or compare a candidate using only stdlib."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import csv
from datetime import date, timedelta
import gzip
import hashlib
from importlib import metadata
import io
import json
import math
from pathlib import Path
import platform
import struct
import subprocess
import sys
import tempfile
from unittest.mock import patch

QLIB_VERSION = "0.9.7"
QLIB_COMMIT = "da920b7f954f48ab1bb64117c976710de198373e"
QLIB_URL = "https://github.com/microsoft/qlib"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUNDLE = ROOT / "tests/fixtures/alpha158-reference"
ENVIRONMENT = ROOT / "scripts/alpha158-reference-environment"
INPUT_COLUMNS = ["instrument", "datetime", "open", "high", "low", "close", "volume", "vwap"]
PAYLOAD_FILES = (
    "calendar.txt", "instruments.json", "input.csv", "features.json", "expected.csv.gz",
    "environment.json", "qlib-license.txt",
)
SOURCE_FILES = (
    "qlib/contrib/data/loader.py", "qlib/contrib/data/handler.py", "qlib/data/ops.py",
    "qlib/data/base.py", "qlib/data/data.py", "qlib/data/storage/file_storage.py",
    "qlib/data/_libs/rolling.pyx", "qlib/data/_libs/expanding.pyx",
)
DEFAULT_RTOL = 2e-6
DEFAULT_ATOL = 2e-7


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def float_token(value):
    value = float(value)
    if math.isnan(value):
        return "nan"
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    return repr(value)


def float32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


def csv_bytes(header, rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def synthetic_inputs():
    """Small original synthetic data; weekday labels are not an exchange calendar."""
    calendar = []
    current = date(2024, 1, 2)
    while len(calendar) < 80:
        if current.weekday() < 5:
            calendar.append(current.isoformat())
        current += timedelta(days=1)
    descriptions = {
        "SYN_NORMAL": "Nonconstant positive prices and volume; deterministic arithmetic.",
        "SYN_MISSING": "Missing rows at 20/21; isolated missing close, volume, high/low and vwap.",
        "SYN_ZERO_VOLUME": "Zero-volume runs and recovery; vwap missing on zero-volume rows.",
        "SYN_CONSTANT": "All prices 10 and volume 1000; zero spread and exact ties.",
        "SYN_ZERO_PRICE": "Deliberately invalid market prices: close/open/all prices zero.",
        "SYN_TIES": "Repeated prices/volume to expose rank and extremum tie conventions.",
        "SYN_NEAR_CONSTANT": "Price changes below Qlib's 2e-5 degeneracy tolerance.",
        "SYN_ALL_MISSING": "Every input field NaN; comparisons can still yield false/zero.",
        "SYN_SHORT": "Three sessions, starting at calendar index 77; no earlier history.",
    }
    instruments = []
    rows = []
    for name, description in sorted(descriptions.items()):
        start = 77 if name == "SYN_SHORT" else 0
        instruments.append({
            "instrument": name, "start_index": start, "end_index": 79,
            "start_date": calendar[start], "end_date": calendar[-1], "description": description,
        })
        for index in range(start, 80):
            if name == "SYN_MISSING" and index in (20, 21):
                continue
            i = index - start
            close = 20 + (i % 11 - 5) / 8 + (i // 11) / 16 + ((i * 7) % 13 - 6) / 32
            opening = close + ((i * 3) % 5 - 2) / 16
            high = max(opening, close) + 1 / 4 + (i % 3) / 32
            low = min(opening, close) - 1 / 4 - (i % 4) / 64
            volume = 1000 + ((i * 11) % 17) * 37 + i * 13
            values = dict(open=opening, high=high, low=low, close=close, volume=volume,
                          vwap=(opening + high + low + 2 * close) / 5)
            if name == "SYN_CONSTANT":
                values = dict(open=10, high=10, low=10, close=10, volume=1000, vwap=10)
            elif name == "SYN_ALL_MISSING":
                values = dict.fromkeys(INPUT_COLUMNS[2:], math.nan)
            elif name == "SYN_TIES":
                price = 10 + (i % 5) // 2
                values = dict(open=price, high=price + 1, low=price - 1, close=price,
                              volume=100 * (1 + i % 4), vwap=price)
            elif name == "SYN_NEAR_CONSTANT":
                price = 1 + (i % 3) * 1e-6
                values = dict(open=price, high=price, low=price, close=price,
                              volume=1000 + i % 2, vwap=price)
            elif name == "SYN_MISSING":
                for field, indices in {
                    "close": (0, 7, 55), "volume": (12, 61), "vwap": (0, 33, 60),
                    "high": (38,), "low": (38,),
                }.items():
                    if i in indices:
                        values[field] = math.nan
            elif name == "SYN_ZERO_VOLUME" and (
                i in (0, 1, 5, 6) or 20 <= i <= 24 or 59 <= i <= 64
            ):
                values["volume"], values["vwap"] = 0, math.nan
            elif name == "SYN_ZERO_PRICE":
                if i in (10, 62):
                    values["close"] = 0
                if i == 40:
                    values["open"] = 0
                if i == 63:
                    for field in ("open", "high", "low", "close", "vwap"):
                        values[field] = 0
            rows.append({"instrument": name, "datetime": calendar[index],
                         **{key: float32(value) for key, value in values.items()}})
    return calendar, instruments, rows


def input_bytes(rows):
    return csv_bytes(INPUT_COLUMNS, [
        [row["instrument"], row["datetime"], *[float_token(row[k]) for k in INPUT_COLUMNS[2:]]]
        for row in rows
    ])


def read_matrix(path):
    """Strict names and keys, with explicit IEEE NaN/infinity handling."""
    opener = gzip.open if Path(path).suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", newline="") as stream:
        reader = csv.reader(stream, strict=True)
        header = next(reader, [])
        if header[:2] != ["instrument", "datetime"] or len(header) < 3:
            raise ValueError("CSV must start with instrument,datetime and feature columns")
        if len(set(header)) != len(header) or any(not name for name in header):
            raise ValueError("Duplicate or empty column name")
        rows = {}
        for line, record in enumerate(reader, 2):
            if len(record) != len(header):
                raise ValueError(f"Line {line}: expected {len(header)} columns, got {len(record)}")
            key = tuple(record[:2])
            if not key[0] or date.fromisoformat(key[1]).isoformat() != key[1]:
                raise ValueError(f"Line {line}: invalid instrument/date key")
            if key in rows:
                raise ValueError(f"Line {line}: duplicate key {key}")
            try:
                rows[key] = [float(value) if value.strip() else math.nan for value in record[2:]]
            except ValueError as exc:
                raise ValueError(f"Line {line}: invalid numeric value") from exc
            for token, value in zip(record[2:], rows[key]):
                if math.isinf(value) and token.strip().lower() not in {
                    "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"
                }:
                    raise ValueError(f"Line {line}: finite literal overflows; use explicit inf")
        if not rows:
            raise ValueError("CSV contains no data rows")
    return header, rows


def verify_bundle(bundle):
    bundle = Path(bundle)
    manifest = json.loads((bundle / "manifest.json").read_text())
    if manifest.get("schema_version") != 1 or manifest.get("qlib_commit") != QLIB_COMMIT:
        raise ValueError("Unsupported bundle schema or Qlib commit")
    if set(manifest.get("files", {})) != set(PAYLOAD_FILES):
        raise ValueError("Unexpected bundle payload list")
    for filename in PAYLOAD_FILES:
        if sha256(bundle / filename) != manifest["files"][filename]:
            raise ValueError(f"Bundle checksum mismatch: {filename}")
    snapshot = json.loads((bundle / "features.json").read_text())
    features = snapshot["features"]
    if len(features) != 158 or [v["index"] for v in features] != list(range(158)):
        raise ValueError("Expected ordered 158-feature snapshot")
    names = [v["name"] for v in features]
    if len(set(names)) != 158 or any(v["lookahead"] != 0 for v in features):
        raise ValueError("Invalid feature names or future dependency")
    header, rows = read_matrix(bundle / "expected.csv.gz")
    if header != ["instrument", "datetime", *names]:
        raise ValueError("Oracle column order differs from feature snapshot")
    calendar = (bundle / "calendar.txt").read_text().splitlines()
    instruments = json.loads((bundle / "instruments.json").read_text())
    expected_keys = {
        (item["instrument"], calendar[i]) for item in instruments
        for i in range(item["start_index"], item["end_index"] + 1)
    }
    if set(rows) != expected_keys or len(rows) != manifest["output_rows"]:
        raise ValueError("Oracle row keys differ from calendar/instrument contract")
    return manifest, header, rows


def compare_matrices(expected, candidate, *, rtol=DEFAULT_RTOL, atol=DEFAULT_ATOL):
    if not all(math.isfinite(v) and v >= 0 for v in (rtol, atol)):
        raise ValueError("Tolerances must be finite and nonnegative")
    expected_header, expected_rows = expected
    candidate_header, candidate_rows = candidate
    if not expected_rows or len(expected_header) < 3:
        raise ValueError("Reference matrix must not be empty")
    for header, rows in (expected, candidate):
        if (len(set(header)) != len(header)
                or any(len(row) != len(header) - 2 for row in rows.values())):
            raise ValueError("Duplicate columns or inconsistent matrix width")
    if candidate_header != expected_header:
        raise ValueError("Candidate column names/order must exactly match the oracle")
    missing = sorted(set(expected_rows) - set(candidate_rows))
    extra = sorted(set(candidate_rows) - set(expected_rows))
    mismatch_counts = Counter()
    examples = []
    compared = 0
    max_abs_error = 0.0
    for key in sorted(set(expected_rows) & set(candidate_rows)):
        for name, reference, actual in zip(expected_header[2:], expected_rows[key],
                                           candidate_rows[key]):
            compared += 1
            kind = None
            if math.isnan(reference) or math.isnan(actual):
                if not (math.isnan(reference) and math.isnan(actual)):
                    kind = "nan_mask"
            elif math.isinf(reference) or math.isinf(actual):
                if reference != actual:
                    kind = "infinity_value_or_sign"
            else:
                error = abs(actual - reference)
                max_abs_error = max(max_abs_error, error)
                if error > atol + rtol * abs(reference):
                    kind = "finite_value"
            if kind:
                mismatch_counts[kind] += 1
                if len(examples) < 20:
                    examples.append({"instrument": key[0], "datetime": key[1], "feature": name,
                                     "expected": float_token(reference),
                                     "actual": float_token(actual), "kind": kind})
    return {
        "passed": not (missing or extra or mismatch_counts),
        "expected_rows": len(expected_rows), "candidate_rows": len(candidate_rows),
        "features": len(expected_header) - 2, "compared_cells": compared,
        "missing_row_count": len(missing), "extra_row_count": len(extra),
        "missing_row_examples": missing[:20], "extra_row_examples": extra[:20],
        "mismatch_counts": dict(mismatch_counts), "examples": examples,
        "max_absolute_error": (max_abs_error if math.isfinite(max_abs_error) else "inf"),
        "rtol": rtol, "atol": atol,
    }


@contextmanager
def network_disabled():
    """The oracle has only local synthetic data; reject network access at runtime."""
    message = "Network access is disabled in the synthetic Alpha158 oracle"
    with patch("socket.socket.connect", side_effect=RuntimeError(message)), \
         patch("socket.socket.connect_ex", side_effect=RuntimeError(message)), \
         patch("socket.socket.sendto", side_effect=RuntimeError(message)), \
         patch("socket.getaddrinfo", side_effect=RuntimeError(message)):
        yield


def verify_qlib_source(source):
    import qlib
    import tomllib

    lock = tomllib.loads((ENVIRONMENT / "uv.lock").read_text())
    for package in ("numpy", "pandas", "scipy"):
        versions = {p["version"] for p in lock["package"] if p["name"] == package}
        if versions != {metadata.version(package)}:
            raise ValueError(f"Installed {package} differs from the locked numeric stack")
    source = Path(source).resolve()
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"],
                                     text=True).strip()
    if commit != QLIB_COMMIT or metadata.version("pyqlib") != QLIB_VERSION:
        raise ValueError("Qlib checkout/package must match the pinned version and commit")
    direct_url = json.loads(metadata.distribution("pyqlib").read_text("direct_url.json") or "{}")
    if (direct_url.get("vcs_info", {}).get("commit_id") != QLIB_COMMIT
            or direct_url.get("url") not in {QLIB_URL, QLIB_URL + ".git"}):
        raise ValueError("Installed Qlib lacks the required Git commit provenance")
    installed = Path(qlib.__file__).resolve().parent.parent
    hashes = {}
    for relative in SOURCE_FILES:
        original = subprocess.check_output(
            ["git", "-C", str(source), "show", f"{QLIB_COMMIT}:{relative}"])
        hashes[relative] = hashlib.sha256(original).hexdigest()
        if sha256(source / relative) != hashes[relative]:
            raise ValueError(f"Qlib checkout file differs from pinned commit: {relative}")
        if sha256(installed / relative) != hashes[relative]:
            raise ValueError(f"Installed/source Qlib file differs: {relative}")
    license_bytes = subprocess.check_output(
        ["git", "-C", str(source), "show", f"{QLIB_COMMIT}:LICENSE"])
    if (source / "LICENSE").read_bytes() != license_bytes:
        raise ValueError("Qlib license differs from pinned commit")
    return hashes


def write_provider(root, calendar, instruments, rows):
    root.mkdir()
    (root / "calendars").mkdir()
    (root / "instruments").mkdir()
    (root / "features").mkdir()
    (root / "calendars/day.txt").write_text("\n".join(calendar) + "\n")
    (root / "instruments/all.txt").write_text("".join(
        f"{item['instrument']}\t{item['start_date']}\t{item['end_date']}\n"
        for item in instruments
    ))
    lookup = {(row["instrument"], row["datetime"]): row for row in rows}
    for item in instruments:
        directory = root / "features" / item["instrument"].lower()
        directory.mkdir()
        start, end = item["start_index"], item["end_index"]
        for field in INPUT_COLUMNS[2:]:
            values = [float(start)] + [
                lookup.get((item["instrument"], calendar[i]), {}).get(field, math.nan)
                for i in range(start, end + 1)
            ]
            (directory / f"{field}.day.bin").write_bytes(struct.pack(f"<{len(values)}f", *values))


def initialize_qlib(provider, scratch):
    import qlib

    qlib.init(
        provider_uri=str(provider), region="cn", kernels=1, joblib_backend="threading",
        expression_cache=None, dataset_cache=None, logging_level=40, auto_mount=False,
        exp_manager={"class": "MLflowExpManager", "module_path": "qlib.workflow.expm",
                     "kwargs": {"uri": (scratch / "mlruns").as_uri(),
                                "default_exp_name": "alpha158-synthetic-reference"}},
    )


def evaluate(instruments, expressions, names, start, end):
    import pandas as pd
    from qlib.data import D

    ranges = {item["instrument"]: [(pd.Timestamp(item["start_date"]),
                                    pd.Timestamp(item["end_date"]))] for item in instruments}
    result = D.features(ranges, expressions, start_time=start, end_time=end,
                        freq="day", disk_cache=0)
    result = result.loc[:, expressions].copy()
    result.columns = names
    return result.sort_index()


def frame_matrix(frame):
    return (["instrument", "datetime", *list(frame.columns)], {
        (name, timestamp.strftime("%Y-%m-%d")): [float(v) for v in values]
        for (name, timestamp), values in zip(frame.index, frame.to_numpy())
    })


def assert_equivalent(expected, actual, label):
    report = compare_matrices(frame_matrix(expected), frame_matrix(actual))
    if not report["passed"]:
        raise ValueError(f"Oracle probe failed: {label}: {json.dumps(report)}")


def semantic_probes(frame, calendar):
    def value(instrument, index, feature):
        return float(frame.loc[(instrument, calendar[index]), feature])

    probes = {
        "partial_window_ma60_is_available": value("SYN_NORMAL", 0, "MA60") == 1,
        "roc60_requires_60_previous_sessions": (
            math.isnan(value("SYN_NORMAL", 59, "ROC60"))
            and math.isfinite(value("SYN_NORMAL", 60, "ROC60"))
        ),
        "sample_std_first_observation_is_nan": math.isnan(value("SYN_NORMAL", 0, "STD5")),
        "constant_rank_uses_average_ties": math.isclose(
            value("SYN_CONSTANT", 79, "RANK5"), 0.6, abs_tol=DEFAULT_ATOL),
        "constant_extremum_uses_first_one_based_position": math.isclose(
            value("SYN_CONSTANT", 79, "IMAX5"), 0.2, abs_tol=DEFAULT_ATOL),
        "constant_rsquare_is_nan": math.isnan(value("SYN_CONSTANT", 79, "RSQR5")),
        "near_constant_rsquare_is_nan": math.isnan(value("SYN_NEAR_CONSTANT", 79, "RSQR5")),
        "zero_volume_keeps_epsilon_scaled_large_value": value("SYN_ZERO_VOLUME", 20, "VMA5") > 1e12,
        "zero_close_keeps_positive_infinity": value("SYN_ZERO_PRICE", 10, "OPEN0") == math.inf,
        "missing_vwap_is_not_filled": math.isnan(value("SYN_MISSING", 33, "VWAP0")),
        "missing_session_stays_on_calendar": ("SYN_MISSING", calendar[20]) in frame_matrix(frame)[1],
        "nan_comparison_can_yield_zero_count": value("SYN_ALL_MISSING", 79, "CNTP5") == 0,
        "short_history_is_not_globally_dropped": value("SYN_SHORT", 77, "MA60") == 1,
        "short_history_roc60_is_nan": math.isnan(value("SYN_SHORT", 79, "ROC60")),
    }
    failed = [name for name, passed in probes.items() if not passed]
    if failed:
        raise ValueError(f"Unexpected official semantics: {failed}")
    return probes


def generate(output, qlib_source):
    output = Path(output)
    if output.exists():
        raise FileExistsError("Output must be a new directory; existing bundles are immutable")
    with network_disabled():
        import qlib
        from qlib.contrib.data.loader import Alpha158DL
        from qlib.data.data import ExpressionD

        source_hashes = verify_qlib_source(qlib_source)
        calendar, instruments, rows = synthetic_inputs()
        expressions, names = Alpha158DL.get_feature_config()
        if len(expressions) != 158 or len(set(names)) != 158:
            raise ValueError("Official default is not the expected 158 unique features")
        with tempfile.TemporaryDirectory(prefix="alpha158-oracle-") as temporary:
            scratch = Path(temporary)
            provider = scratch / "provider"
            write_provider(provider, calendar, instruments, rows)
            initialize_qlib(provider, scratch)
            features = []
            for index, (name, expression) in enumerate(zip(names, expressions)):
                left, right = ExpressionD.get_expression_instance(expression).get_extended_window_size()
                features.append({"index": index, "name": name, "expression": expression,
                                 "lookback": int(left), "lookahead": int(right)})
            if max(v["lookback"] for v in features) != 60 or any(v["lookahead"] for v in features):
                raise ValueError("Unexpected official default history/future dependency")
            full = evaluate(instruments, expressions, names, calendar[0], calendar[-1])
            checks = semantic_probes(full, calendar)
            sliced = evaluate(instruments, expressions, names, calendar[65], calendar[-1])
            expected_slice = full[full.index.get_level_values("datetime") >= calendar[65]]
            assert_equivalent(expected_slice, sliced, "query retrieves previous 60 sessions")
            checks["query_slice_retrieves_warmup"] = True
            mutated = []
            for row in rows:
                changed = row.copy()
                if row["datetime"] > calendar[65]:
                    for field in INPUT_COLUMNS[2:]:
                        changed[field] = float32(row[field] * 17 + 123)
                mutated.append(changed)
            future_provider = scratch / "future-mutated"
            write_provider(future_provider, calendar, instruments, mutated)
            initialize_qlib(future_provider, scratch)
            future = evaluate(instruments, expressions, names, calendar[0], calendar[-1])
            expected_prefix = full[full.index.get_level_values("datetime") <= calendar[65]]
            actual_prefix = future[future.index.get_level_values("datetime") <= calendar[65]]
            assert_equivalent(expected_prefix, actual_prefix, "future mutation")
            checks["future_mutation_does_not_change_past"] = True
        output.mkdir(parents=True)
        (output / "calendar.txt").write_text("\n".join(calendar) + "\n")
        write_json(output / "instruments.json", instruments)
        (output / "input.csv").write_bytes(input_bytes(rows))
        write_json(output / "features.json", {
            "qlib_version": QLIB_VERSION, "qlib_commit": QLIB_COMMIT,
            "source": f"{QLIB_URL}/blob/{QLIB_COMMIT}/qlib/contrib/data/loader.py",
            "call": "Alpha158DL.get_feature_config()",
            "default_config": {"kbar": {}, "price": {"windows": [0],
                               "feature": ["OPEN", "HIGH", "LOW", "VWAP"]}, "rolling": {}},
            "features": features,
        })
        header, values = frame_matrix(full)
        data = csv_bytes(header, [[*key, *[float_token(v) for v in row]]
                                  for key, row in sorted(values.items())])
        with (output / "expected.csv.gz").open("wb") as stream:
            with gzip.GzipFile(filename="", fileobj=stream, mode="wb", mtime=0) as compressed:
                compressed.write(data)
        packages = []
        for distribution in metadata.distributions():
            info = distribution.metadata
            license_name = info.get("License-Expression") or info.get("License") or ""
            classifiers = [v for v in info.get_all("Classifier", []) if v.startswith("License ::")]
            packages.append({"name": info["Name"], "version": distribution.version,
                             "license_metadata": license_name.strip().splitlines()[0][:200]
                             if license_name.strip() else None, "license_classifiers": classifiers})
        write_json(output / "environment.json", {
            "python": platform.python_version(), "system": platform.system(),
            "machine": platform.machine(), "qlib": qlib.__version__,
            "uv_lock_sha256": sha256(ENVIRONMENT / "uv.lock"),
            "pyproject_sha256": sha256(ENVIRONMENT / "pyproject.toml"),
            "packages": sorted(packages, key=lambda v: v["name"].lower()),
            "license_note": "Package metadata is attribution guidance, not a substitute for each license.",
        })
        (output / "qlib-license.txt").write_bytes((Path(qlib_source) / "LICENSE").read_bytes())
        categories = Counter("nan" if math.isnan(v) else "positive_inf" if v == math.inf
                             else "negative_inf" if v == -math.inf else "finite"
                             for row in values.values() for v in row)
        write_json(output / "manifest.json", {
            "schema_version": 1, "reference_id": "qlib-alpha158-v0.9.7-synthetic-v1",
            "synthetic_only": True, "qlib_version": QLIB_VERSION, "qlib_commit": QLIB_COMMIT,
            "qlib_source": QLIB_URL, "qlib_license": "MIT",
            "source_file_sha256": source_hashes, "generator_sha256": sha256(__file__),
            "input_rows": len(rows), "output_rows": len(full), "feature_count": 158,
            "output_value_counts": dict(categories), "checks": checks,
            "contract": {
                "input_columns": INPUT_COLUMNS, "price_unit": "synthetic currency/share",
                "volume_unit": "shares", "vwap": "explicit same-unit input; never inferred",
                "calendar": "80 synthetic weekday sessions; not the Chinese exchange calendar",
                "session_labels": "Asia/Shanghai session dates, timezone-naive ISO date strings",
                "binary_storage": "little-endian float32; missing calendar rows become NaN",
                "output_dtype": "official LocalExpressionProvider float32",
                "processors": [], "labels": [], "max_previous_sessions": 60,
                "rolling_windows": [5, 10, 20, 30, 60], "missing_policy": "no fill or row drop",
                "comparison": "abs(actual-expected) <= atol + rtol * abs(expected); masks exact",
                "default_rtol": DEFAULT_RTOL, "default_atol": DEFAULT_ATOL,
            },
            "files": {filename: sha256(output / filename) for filename in PAYLOAD_FILES},
        })
    verify_bundle(output)
    return {"passed": True, "output": str(output), "input_rows": len(rows),
            "output_rows": len(full), "features": 158, "checks": checks}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    generate_parser = commands.add_parser("generate", help="Run the pinned official Qlib oracle")
    generate_parser.add_argument("--output", required=True, type=Path)
    generate_parser.add_argument("--qlib-source", required=True, type=Path)
    verify_parser = commands.add_parser("verify", help="Verify existing fixture integrity, offline")
    verify_parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    compare_parser = commands.add_parser("compare", help="Compare candidate CSV/CSV.GZ, offline")
    compare_parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    compare_parser.add_argument("--candidate", type=Path, required=True)
    compare_parser.add_argument("--rtol", type=float, default=DEFAULT_RTOL)
    compare_parser.add_argument("--atol", type=float, default=DEFAULT_ATOL)
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            result = generate(args.output, args.qlib_source)
        else:
            manifest, header, rows = verify_bundle(args.bundle)
            if args.command == "verify":
                result = {"passed": True, "qlib_commit": manifest["qlib_commit"],
                          "rows": len(rows), "features": len(header) - 2}
            else:
                result = compare_matrices((header, rows), read_matrix(args.candidate),
                                          rtol=args.rtol, atol=args.atol)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0 if result["passed"] else 1
    except (ValueError, OSError, csv.Error, KeyError, ImportError) as exc:
        print(json.dumps({"passed": False, "error": str(exc)}, indent=2))
        return 2


if __name__ == "__main__":
    sys.exit(main())
