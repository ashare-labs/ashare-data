"""Verify a non-editable wheel in an isolated environment using locked hashes."""

from __future__ import annotations

import argparse
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import re
import tomllib
from urllib.parse import unquote, urlsplit
import zipfile


def bind_local_wheels(requirements, wheelhouse, lock_path):
    """Pin a compatible cached artifact to its exact, already locked hash.

    Other platforms' hashes need not accompany a concrete file URL. Hash checks
    remain mandatory; filename, size, ZIP metadata and both exported/lock hashes
    must all agree. The wheelhouse is opt-in and never executes downloaded code.
    """
    from packaging.tags import sys_tags
    from packaging.utils import canonicalize_name, parse_wheel_filename
    from packaging.version import Version
    lock = tomllib.loads(Path(lock_path).read_text())
    packages = {(canonicalize_name(p["name"]), Version(p["version"])): p for p in lock["package"]}
    compatible = set(sys_tags())
    candidates = {}
    for path in sorted(Path(wheelhouse).glob("*.whl")):
        name, version, _, tags = parse_wheel_filename(path.name)
        if tags & compatible:
            key = (name, version)
            if key in candidates:
                raise ValueError("multiple compatible local wheels; select one explicit platform artifact")
            candidates[key] = path.resolve()
    lines = Path(requirements).read_text().splitlines(keepends=True)
    output, bindings = [], []
    i = 0
    while i < len(lines):
        block = [lines[i]]
        i += 1
        while block[-1].rstrip().endswith("\\") and i < len(lines):
            block.append(lines[i])
            i += 1
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\s;\\]+)(\s*;[^\\]+)?\s*(?:\\)?", block[0].strip())
        key = (canonicalize_name(match[1]), Version(match[2])) if match else None
        if key not in candidates:
            output.extend(block)
            continue
        path = candidates[key]
        package = packages.get(key)
        artifacts = [w for w in package.get("wheels", []) if unquote(urlsplit(w["url"]).path).rsplit("/", 1)[-1] == path.name] if package else []
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        exported = set(re.findall(r"--hash=sha256:([0-9a-f]{64})", "".join(block)))
        if len(artifacts) != 1 or artifacts[0]["hash"] != "sha256:"+sha or artifacts[0]["size"] != len(raw) or sha not in exported:
            raise ValueError("local wheel must match exported and locked artifact hash/size: "+path.name)
        with zipfile.ZipFile(path) as wheel:
            if wheel.testzip() is not None:
                raise ValueError("local wheel ZIP CRC failed")
            metadata = [n for n in wheel.namelist() if n.endswith(".dist-info/METADATA")]
            if len(metadata) != 1:
                raise ValueError("local wheel must have one package metadata file")
            meta = BytesParser().parsebytes(wheel.read(metadata[0]))
            if canonicalize_name(meta["Name"]) != key[0] or Version(meta["Version"]) != key[1]:
                raise ValueError("local wheel metadata differs from locked name/version")
        marker = (match[3] or "").strip()
        output.append(f"{match[1]} @ {path.as_uri()}" + (" "+marker if marker else "") + " \\\n")
        output.append(f"    --hash=sha256:{sha}\n")
        bindings.append({"name": key[0], "version": str(key[1]), "file": path.name, "sha256": sha,
                         "size": len(raw), "hash_policy": "exact artifact matches original lock and export; --require-hashes retained"})
    Path(requirements).write_text("".join(output))
    return bindings

IDENTITY = """
import json
from importlib.resources import files
import ashare_data
from ashare_data.research.portable import code_identity
from ashare_data.research.portable_source import source_identity
from ashare_data.research.conditional import _profile_bytes
from ashare_data.research.conditional_listing import _policy
from ashare_data.providers.free_updates import implementation_identity as free_update_identity
for name in ('baostock-sdk.json', 'sina-decode.js', 'sina-decoder-notice.txt'):
    assert files('ashare_data.providers').joinpath(name).read_bytes()
for name in ('sources.json', 'NOTICE', 'LICENSE.dolphindb'):
    assert files('ashare_data.factors.alpha191').joinpath(name).read_bytes()
for name in ('features.json', 'qlib-license.txt'):
    assert files('ashare_data.factors.qlib_alpha158').joinpath(name).read_bytes()
assert _profile_bytes() and _policy()
print(json.dumps({'version': ashare_data.__version__, 'runtime': code_identity(), 'parser': source_identity(),
                  'free_updates': free_update_identity()}))
"""

CATALOG_IMPORT = """
import json
import ashare_data.factors.catalog as factor_catalog
print(json.dumps({
    'module': factor_catalog.__name__,
    'libraries': list(factor_catalog.libraries().index),
    'joinquant_fields': len(factor_catalog.catalog('joinquant')),
}))
"""


def run(wheel, *, offline=False, cache=None, full=False, extras=(), test_paths=(), wheelhouse=None):
    project = Path(__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("UV_", "PIP_", "PYTHON"))}
    env.update(UV_PYTHON_DOWNLOADS="never", PYTHONDONTWRITEBYTECODE="1")
    with tempfile.TemporaryDirectory(prefix="ashare-wheel-") as temporary:
        root = Path(temporary)
        env["UV_CACHE_DIR"] = str(Path(cache).resolve() if cache else root / "empty-cache")
        executable = root / "venv/bin/python"
        requirements = root / "requirements.txt"

        def command(args, cwd=project):
            result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(result.stdout + result.stderr or f"exit {result.returncode}")
            return result.stdout

        command(
            [
                "uv",
                "export",
                "--locked",
                *([] if full or test_paths else ["--no-dev"]),
                "--no-emit-project",
                *[argument for extra in extras for argument in ("--extra", extra)],
                "--no-config",
                "--format",
                "requirements-txt",
                "--output-file",
                str(requirements),
            ]
        )
        bindings = bind_local_wheels(requirements, wheelhouse, project / "uv.lock") if wheelhouse else []
        command(["uv", "venv", "--no-config", "--python", sys.executable, str(root / "venv")])
        command(
            [
                "uv",
                "pip",
                "install",
                "--no-config",
                *(["--offline"] if offline else []),
                "--python",
                str(executable),
                "--require-hashes",
                "-r",
                str(requirements),
            ]
        )
        command(
            [
                "uv",
                "pip",
                "install",
                "--no-config",
                "--no-deps",
                "--python",
                str(executable),
                str(Path(wheel).resolve()),
            ]
        )
        origin = command(
            [str(executable), "-I", "-c", "import ashare_data; print(ashare_data.__file__)"], root
        ).strip()
        assert Path(origin).resolve().is_relative_to((root / "venv").resolve())
        installed = json.loads(command([str(executable), "-I", "-c", IDENTITY], root))
        source_code = "import sys; sys.path.insert(0, sys.argv[1]);\n" + IDENTITY
        source = json.loads(
            command([str(executable), "-I", "-c", source_code, str(project / "src")], root)
        )
        assert installed == source, "installed resource/code identity differs from source"
        golden = command(
            [
                str(executable),
                "-I",
                str(project / "examples/golden.py"),
                "--store",
                str(root / "golden"),
            ],
            root,
        )
        capabilities = command([str(root / "venv/bin/ashare-data"), "capabilities"], root)
        assert json.loads(capabilities)["version"] == installed["version"]
        catalog = json.loads(command([str(executable), "-I", "-c", CATALOG_IMPORT], root))
        assert catalog == {
            "module": "ashare_data.factors.catalog",
            "libraries": ["joinquant", "alpha191", "qlib_alpha158"],
            "joinquant_fields": 19,
        }
        amount_comparison = json.loads(
            command([str(executable), "-I", str(project / "examples/amount_comparison.py")], root)
        )
        assert amount_comparison["input_kind"] == "synthetic"
        assert amount_comparison["strict_arithmetic_compatible"] is False
        assert amount_comparison["direct_source_interval_compatible"] is False
        assert amount_comparison["composite_model_compatible"] is True
        assert amount_comparison["summary_all_compatible"] is True
        assert amount_comparison["summary_sample_count"] == 1
        assert amount_comparison["source_error_bound_certified"] is False
        assert amount_comparison["production_eligible"] is False
        result = {
            "wheel": Path(wheel).name,
            "extras": list(extras),
            "local_locked_artifacts": bindings,
            "isolated_import_confirmed": True,
            "editable_install": False,
            "identity": installed,
            "source_identity_matches": True,
            "dependency_install": "locked hashes; official PyPI"
            if not offline
            else "locked hashes; explicit offline cache",
            "fresh_empty_cache": cache is None,
            "golden": json.loads(golden),
            "capabilities": json.loads(capabilities),
            "catalog_import": catalog,
            "amount_comparison": amount_comparison,
        }
        if full or test_paths:
            validation = root / "validation"
            validation.mkdir()
            # The test tree has no src directory, editable installation or source path.
            for name in ("tests", "scripts", "examples"):
                shutil.copytree(
                    project / name, validation / name, ignore=shutil.ignore_patterns("__pycache__")
                )
            shutil.copyfile(project / "pyproject.toml", validation / "pyproject.toml")
            output = command(
                [
                    str(executable),
                    "-I",
                    "-c",
                    # Tests share fixtures through both bare modules and the
                    # tests namespace. Add only the copied validation tree;
                    # it contains no src or editable package installation.
                    "import sys; from pathlib import Path; sys.path.insert(0,str(Path.cwd())); "
                    "import pytest; raise SystemExit(pytest.main(sys.argv[1:]))",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "--basetemp",
                    str(root / "test-output"),
                    *(["tests"] if full else test_paths),
                ],
                validation,
            )
            result["installed_test_summary"] = output.strip().splitlines()[-1]
            result["tests_have_source_tree"] = False
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--wheelhouse", type=Path, help="Opt-in local platform wheels, verified against uv.lock and exported hashes")
    parser.add_argument("--extra", action="append", default=[], help="Locked optional dependency group")
    parser.add_argument("--test", action="append", default=[], help="Selected test path in isolated wheel tree")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Install locked dev dependencies and run all tests against the wheel",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            run(args.wheel, offline=args.offline, cache=args.cache, full=args.full, extras=args.extra,
                test_paths=args.test, wheelhouse=args.wheelhouse),
            ensure_ascii=False,
            indent=2,
        )
    )
