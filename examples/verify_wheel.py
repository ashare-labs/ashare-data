"""Fresh wheel verification using official PyPI and hashes exported from uv.lock."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def run(wheel, *, offline=False, cache=None):
    project = Path(__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("UV_", "PIP_"))}
    env["UV_PYTHON_DOWNLOADS"] = "never"
    with tempfile.TemporaryDirectory(prefix="ashare-wheel-") as temporary:
        root = Path(temporary)
        env["UV_CACHE_DIR"] = str(cache or root / "empty-cache")
        executable = root / "venv" / "bin" / "python"
        requirements = root / "requirements.txt"
        def command(args, cwd=project):
            return subprocess.run(args, cwd=cwd, env=env, check=True, capture_output=True, text=True)
        command(["uv", "export", "--locked", "--no-dev", "--no-emit-project", "--no-config",
                 "--format", "requirements-txt", "--output-file", str(requirements)])
        command(["uv", "venv", "--no-config", "--python", sys.executable, str(root / "venv")])
        command(["uv", "pip", "install", "--no-config", *(["--offline"] if offline else []),
                 "--python", str(executable), "--require-hashes", "-r", str(requirements)])
        command(["uv", "pip", "install", "--no-config", "--no-deps", "--python", str(executable),
                 str(Path(wheel).resolve())])
        origin = command([str(executable), "-I", "-c", "import ashare_data; print(ashare_data.__file__)"], root).stdout.strip()
        assert origin.startswith(str(root / "venv"))
        golden = command([str(executable), "-I", str(project / "examples" / "golden.py"), "--store", str(root / "golden")], root)
        capabilities = command([str(root / "venv" / "bin" / "ashare-data"), "capabilities"], root)
        return {"wheel": Path(wheel).name, "isolated_import_confirmed": True, "editable_install": False,
                "dependency_install": "locked runtime hashes; official PyPI" if not offline else "explicit offline cache",
                "fresh_empty_cache": cache is None, "golden": json.loads(golden.stdout),
                "capabilities": json.loads(capabilities.stdout)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--cache", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.wheel, offline=args.offline, cache=args.cache), ensure_ascii=False, indent=2))
