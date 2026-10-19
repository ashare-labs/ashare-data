"""Run pytest with disposable test storage and no inherited live deployment."""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-temp", action="store_true", help="Retain this run for debugging")
    args, tests = parser.parse_known_args()
    if any(arg == "--basetemp" or arg.startswith("--basetemp=") for arg in tests):
        parser.error("The runner owns --basetemp; use --keep-temp to retain a debug run")
    root = Path(tempfile.mkdtemp(prefix="ashare-tests-"))
    environment = dict(os.environ, XDG_CONFIG_HOME=str(root / "config"),
                       TMPDIR=str(root), TMP=str(root), TEMP=str(root), PYTHONDONTWRITEBYTECODE="1")
    for key in ("ASHARE_DEPLOYMENT", "ASHARE_DATA_ROOT", "ASHARE_QUANT_DB"):
        environment.pop(key, None)
    try:
        return subprocess.call([sys.executable, "-B", "-m", "pytest",
                                "--basetemp=" + str(root / "pytest"),
                                "-o", "cache_dir=" + str(root / "cache"), *tests],
                               cwd=Path(__file__).resolve().parents[1], env=environment)
    finally:
        if args.keep_temp:
            print("Retained explicit debug directory: " + str(root), file=sys.stderr)
        else:
            shutil.rmtree(root)


if __name__ == "__main__":
    raise SystemExit(main())
