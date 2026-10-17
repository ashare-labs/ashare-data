"""Run the versioned API offline with synthetic inputs or a hash-pinned local panel.

Synthetic inputs demonstrate the API; they are not market evidence. The optional
JSON panel format is documented in docs/alpha191.md and never fetched by this script.
"""

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from ashare_data.factors.alpha191 import Alpha191, VERSIONS
from ashare_data.factors.catalog import catalog, libraries


def synthetic_panel(sessions=320, securities=8):
    rng = np.random.default_rng(191)
    index = pd.bdate_range("2023-01-02", periods=sessions)
    columns = [f"SYNTHETIC_{i:03d}" for i in range(securities)]
    close = 20 * np.exp(rng.normal(0, 0.05, (sessions, securities)))
    opening = close * np.exp(rng.normal(0, 0.01, close.shape))
    volume = rng.uniform(1e4, 1e6, close.shape)
    vwap = (opening + close) / 2
    arrays = {
        "open": opening,
        "close": close,
        "high": np.maximum(opening, close) * 1.02,
        "low": np.minimum(opening, close) * 0.98,
        "volume": volume,
        "vwap": vwap,
        "amount": volume * vwap,
    }
    frames = {k: pd.DataFrame(v, index=index, columns=columns) for k, v in arrays.items()}
    return frames, pd.DataFrame(True, index=index, columns=columns)


def load_panel(path, expected_sha256):
    raw = Path(path).read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256:
        raise ValueError(f"panel hash mismatch: {actual}")
    obj = json.loads(raw)
    if obj["schema"] != "alpha191.panel.v1":
        raise ValueError("unsupported panel schema")
    ix = pd.DatetimeIndex(obj["sessions"])
    securities = obj["securities"]
    data = {
        k: pd.DataFrame(v, index=ix, columns=securities, dtype=float)
        for k, v in obj["fields"].items()
    }
    universe = pd.DataFrame(obj["universe"], index=ix, columns=securities)
    options = {
        "adjustment": obj["adjustment"],
        "input_id": actual,
        "units": obj["units"],
        "rank_policy": obj.get("rank_policy", "strict"),
    }
    for key in ["benchmark", "factors"]:
        if key in obj:
            options[key] = pd.DataFrame(obj[key], index=ix, dtype=float)
    for key in ["version", "alpha143_seed", "alpha146_m"]:
        if key in obj:
            options[key] = obj[key]
    return data, universe, options


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panel", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--version", choices=VERSIONS)
    parser.add_argument("--alpha146-m", type=int)
    args = parser.parse_args()
    if args.panel is None:
        data, universe = synthetic_panel()
        options = {"adjustment": "raw", "input_id": "synthetic-seed-191"}
        print("SYNTHETIC demo: not market evidence")
    else:
        if not args.sha256:
            parser.error("--panel requires --sha256")
        data, universe, options = load_panel(args.panel, args.sha256)
    if args.version is not None:
        options["version"] = args.version
    if args.alpha146_m is not None:
        options["alpha146_m"] = args.alpha146_m
    result = Alpha191(data, universe=universe, **options).compute()
    print(libraries()[["namespace", "label", "entry_count", "api_field_count"]].to_string())
    print(
        catalog("alpha191", alpha191_version=result.metadata["version"])
        .availability.value_counts()
        .to_string()
    )
    print(result.values[["alpha012", "alpha081", "alpha191"]].tail())
    print(result.report[["availability", "valid_cells", "total_cells"]].head())
    print("Input SHA256:", result.metadata["input_sha256"])
    print("Missing dependencies:", result.metadata["missing_dependencies"])


if __name__ == "__main__":
    main()
