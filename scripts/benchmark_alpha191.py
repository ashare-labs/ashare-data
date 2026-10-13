"""Bounded reproducible offline benchmark; synthetic data, not trading evidence."""

import argparse
import importlib.util
import json
from pathlib import Path
import platform
import resource
import sys
import time

import numpy as np
import pandas as pd

from ashare_data.alpha191 import Alpha191, VERSION, VERSIONS


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sessions", type=int, default=512)
    p.add_argument("--securities", type=int, default=128)
    p.add_argument("--output", type=Path)
    p.add_argument("--version", choices=VERSIONS, default=VERSION)
    p.add_argument("--alpha146-m", type=int)
    args = p.parse_args()
    if not 20 <= args.sessions <= 2000 or not 2 <= args.securities <= 512:
        p.error("bounded benchmark: 20..2000 sessions, 2..512 securities")
    spec = importlib.util.spec_from_file_location(
        "demo", Path(__file__).parents[1] / "examples" / "alpha191_offline.py"
    )
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    data, universe = demo.synthetic_panel(args.sessions, args.securities)
    rng = np.random.default_rng(192)
    benchmark = pd.DataFrame(
        rng.uniform(90, 110, (args.sessions, 2)), index=universe.index, columns=["open", "close"]
    )
    factors = pd.DataFrame(
        rng.normal(0, 0.01, (args.sessions, 3)), index=universe.index, columns=["mkt", "smb", "hml"]
    )
    start = time.perf_counter()
    result = Alpha191(
        data,
        universe=universe,
        adjustment="raw",
        benchmark=benchmark,
        factors=factors,
        alpha143_seed=1,
        rank_policy="available",
        version=args.version,
        alpha146_m=args.alpha146_m,
    ).compute()
    elapsed = time.perf_counter() - start
    record = {
        "schema": "alpha191.benchmark.v1",
        "version": result.metadata["version"],
        "engine_version": result.metadata["engine_version"],
        "synthetic": True,
        "sessions": args.sessions,
        "securities": args.securities,
        "factor_ids": 191,
        "elapsed_seconds": elapsed,
        "output_cells": int(result.values.size),
        "valid_cells": int(result.values.notna().sum().sum()),
        "peak_process_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        * (1 if sys.platform == "darwin" else 1024),
        "python": platform.python_version(),
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "platform": platform.platform(),
        "processor": platform.machine(),
        "rank_policy": "available",
        "ambiguous_formulas": int((result.report.availability == "AMBIGUOUS_FORMULA").sum()),
        "factors_with_valid_cells": int((result.report.valid_cells > 0).sum()),
        "missing_dependencies": result.metadata["missing_dependencies"],
        "alpha146_m": result.metadata.get("alpha146_m"),
        "input_sha256": result.metadata["input_sha256"],
    }
    rendered = json.dumps(record, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
