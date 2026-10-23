"""Compare AD with pinned official Qlib on an explicitly frozen panel, offline.

Use the separate scripts/alpha158-reference-environment; install the candidate
AD wheel there with --no-deps plus its pinned duckdb dependency. No collection,
training, live store mutation or arbitrary expression input is supported.
"""

import argparse
import hashlib
import json
from pathlib import Path
import tempfile

import pandas as pd

import alpha158_reference as reference
from ashare_data.factors.qlib_alpha158 import Alpha158, coverage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panel", type=Path, required=True)
    parser.add_argument("--qlib-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = args.panel.read_bytes()
    panel = json.loads(raw)
    if panel.get("schema") not in {"alpha191.panel.v1", "qlib_alpha158.panel.v1"}:
        raise ValueError("Use an explicit frozen price/volume panel")
    args.output.mkdir(parents=True, exist_ok=False)
    axes = dict(index=pd.to_datetime(panel["sessions"]), columns=panel["securities"])
    frames = {k: pd.DataFrame(v, **axes, dtype=float) for k, v in panel["fields"].items()}
    membership = pd.DataFrame(panel["universe"], **axes, dtype=bool)
    ranges = panel.get("instrument_ranges") or {
        s: [panel["sessions"][0], panel["sessions"][-1]] for s in panel["securities"]
    }
    kwargs = dict(
        universe=membership,
        adjustment=panel["adjustment"],
        units=panel["units"],
        volume_basis=panel["volume_basis"],
        vwap_basis=panel["vwap_basis"],
        instrument_ranges=ranges,
        input_id=hashlib.sha256(raw).hexdigest(),
    )
    with reference.network_disabled(), tempfile.TemporaryDirectory(prefix="alpha158-real-") as tmp:
        hashes = reference.verify_qlib_source(args.qlib_source)
        from qlib.contrib.data.loader import Alpha158DL

        expressions, names = Alpha158DL.get_feature_config()
        assert names == list(coverage().index)
        assert expressions == list(coverage().expression)
        instruments = [
            dict(
                instrument=s,
                start_date=b[0],
                end_date=b[1],
                start_index=panel["sessions"].index(b[0]),
                end_index=panel["sessions"].index(b[1]),
            )
            for s, b in ranges.items()
        ]
        rows = [
            dict(
                instrument=s,
                datetime=d,
                **{
                    f: reference.float32(frames[f].loc[pd.Timestamp(d), s])
                    if f in frames
                    else float("nan")
                    for f in reference.INPUT_COLUMNS[2:]
                },
            )
            for d in panel["sessions"]
            for s in panel["securities"]
        ]
        scratch = Path(tmp)
        provider = scratch / "provider"
        reference.write_provider(provider, panel["sessions"], instruments, rows)
        reference.initialize_qlib(provider, scratch)
        official = reference.evaluate(
            instruments, expressions, names, panel["sessions"][0], panel["sessions"][-1]
        )
        candidate = Alpha158(frames, **kwargs).compute().values
        candidate = candidate.reorder_levels(["security", "date"]).sort_index()
        candidate.index.names = ["instrument", "datetime"]
        candidate = candidate.loc[official.index]
        # Official D.features applies instrument ranges, not research membership.
        # Compare all numerical values before an independently tested output mask.
        if not membership.to_numpy().all():
            raise ValueError("Comparison panels require full membership; test masks separately")
        official.to_csv(args.output / "official.csv.gz", float_format="%.17g")
        candidate.to_csv(args.output / "candidate.csv.gz", float_format="%.17g")
        report = reference.compare_matrices(
            reference.frame_matrix(official), reference.frame_matrix(candidate)
        )
        report["zero_tolerance"] = reference.compare_matrices(
            reference.frame_matrix(official), reference.frame_matrix(candidate), rtol=0, atol=0
        )
        report.update(
            input_sha256=hashlib.sha256(raw).hexdigest(),
            qlib_commit=reference.QLIB_COMMIT,
            source_file_sha256=hashes,
            input_contract={
                k: panel[k] for k in ("adjustment", "units", "volume_basis", "vwap_basis")
            },
            candidate_version=Alpha158(frames, **kwargs).metadata["engine_version"],
            network_used=False,
            provider_equivalence=False,
        )
    (args.output / "comparison.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
