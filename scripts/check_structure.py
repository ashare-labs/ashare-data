"""Check source layout, internal imports, package resources and local documentation links."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import tomllib
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
CHAPTERS = ["项目简介", "安装指南", "快速开始", "使用指南", "限制与说明"]
LEGACY_NAME = re.compile(r"(?:^|[_-])(?:f[12]|d1|br1|m2|round\d+|review\d+)(?:[_.-]|$)")
PYTHON_NAME = re.compile(r"[a-z_][a-z0-9_]*\.py")
KEBAB_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\.(?:md|csv|json|sha256|sh|cjs|js)")
DOMAINS = {
    "core",
    "market",
    "reference",
    "corporate_actions",
    "financials",
    "factors",
    "storage",
    "providers",
    "observations",
    "research",
    "product",
    "cli",
}


def project_files(root):
    result = [p for p in root.iterdir() if p.is_file() and p.suffix in {".md", ".toml"}]
    for directory in ("src", "tests", "scripts", "examples", "docs"):
        result.extend(
            p for p in (root / directory).rglob("*") if p.is_file() and "__pycache__" not in p.parts
        )
    return sorted(result)


def headings(text):
    # Match GitHub's common heading form; duplicate anchors are checked by base name.
    output = set()
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        if not fenced and re.match(r"#{1,6} ", line):
            title = re.sub(r"^#+ ", "", line).strip().lower()
            title = re.sub(r"[^\w\s-]", "", title, flags=re.UNICODE)
            output.add(title.replace(" ", "-"))
    return output


def check(root):
    errors = []
    guides = list((root / "docs").rglob("*.md"))
    if not guides:
        errors.append("docs directory must contain markdown guides")
    if list((root / "docs").rglob("*.csv")):
        errors.append("generate coverage tables with the public API instead of storing docs CSVs")
    paths = project_files(root)
    package = root / "src/ashare_data"
    root_modules = {p.name for p in package.glob("*.py")}
    if root_modules != {"__init__.py"}:
        errors.append("package root must contain only __init__.py and functional packages")
    if {p.name for p in package.iterdir() if p.is_dir() and p.name != "__pycache__"} != DOMAINS:
        errors.append("functional package directories differ from documented domains")
    readme = (root / "README.md").read_text()
    if re.findall(r"^## (.+)$", readme, re.M) != CHAPTERS:
        errors.append("README must use the five agreed main chapters")
    metadata = tomllib.loads((root / "pyproject.toml").read_text())
    if metadata["project"]["version"] not in (package / "__init__.py").read_text():
        errors.append("package version differs from pyproject.toml")
    for path in paths:
        relative = path.relative_to(root)
        if LEGACY_NAME.search(path.name):
            errors.append(f"{relative}: name describes an old task or experiment")
        if path.suffix == ".py":
            if not PYTHON_NAME.fullmatch(path.name):
                errors.append(f"{relative}: Python filename must use snake_case")
            try:
                tree = ast.parse(path.read_text(), filename=str(relative))
            except SyntaxError as exc:
                errors.append(f"{relative}: {exc}")
                continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.keyword)
                    and node.arg in {"help", "description", "epilog"}
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    for link in re.findall(
                        r"[A-Za-z0-9_./-]+\.md(?:#[\w-]+)?", node.value.value
                    ):
                        name, _, anchor = link.partition("#")
                        target = root / name
                        if "/" not in name and not target.is_file():
                            target = root / "docs" / name
                        if not target.is_file():
                            errors.append(
                                f"{relative}:{node.lineno}: missing CLI help document {link}"
                            )
                        elif anchor and anchor not in headings(target.read_text()):
                            errors.append(
                                f"{relative}:{node.lineno}: missing CLI help heading {link}"
                            )
                modules = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module]
                    if isinstance(node, ast.ImportFrom) and not node.level
                    else []
                )
                for module in modules:
                    if module and (module == "ashare_data" or module.startswith("ashare_data.")):
                        target = root / "src" / module.replace(".", "/")
                        if (
                            not target.with_suffix(".py").is_file()
                            and not (target / "__init__.py").is_file()
                        ):
                            errors.append(
                                f"{relative}:{node.lineno}: missing internal module {module}"
                            )
        if relative.parts[0] in {"docs", "scripts"} and path.suffix in {
            ".md",
            ".csv",
            ".json",
            ".sha256",
            ".sh",
            ".cjs",
            ".js",
        }:
            if not KEBAB_NAME.fullmatch(path.name):
                errors.append(f"{relative}: non-Python filename must use kebab-case")
        if relative.parts[0] == "docs" and path.suffix in {".json", ".sha256"}:
            errors.append(f"{relative}: generated execution reports do not belong in docs")
        if path.suffix != ".md":
            continue
        text = path.read_text()
        for raw in re.findall(r"\[[^\]\n]+\]\(([^)]+)\)", text):
            link = raw.split(' "', 1)[0].strip("<>")
            if re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*:", link):
                continue
            name, _, anchor = unquote(link).partition("#")
            target = (path.parent / name).resolve() if name else path
            if not target.is_file():
                errors.append(f"{relative}: missing link target {link}")
            elif anchor and target.suffix == ".md" and anchor not in headings(target.read_text()):
                errors.append(f"{relative}: missing heading {link}")
        for name in re.findall(
            r"(?:scripts|examples|tests|src|docs)/[A-Za-z0-9_./-]+\.(?:py|cjs|sh|md|json|csv|txt)",
            text,
        ):
            if not (root / name).is_file():
                errors.append(f"{relative}: referenced project file does not exist: {name}")
    for resource, module, constant in [
        ("conditional-profile.json", "conditional.py", "PROFILE_SHA256"),
        ("conditional-listing-policy.json", "conditional_listing.py", "POLICY_SHA256"),
    ]:
        expected = hashlib.sha256((package / "research" / resource).read_bytes()).hexdigest()
        text = (package / "research" / module).read_text()
        if f'{constant} = "{expected}"' not in text:
            errors.append(f"research/{resource}: resource pin is stale")
    return sorted(set(errors)), len(paths)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    errors, count = check(args.root.resolve())
    print(json.dumps({"checked_files": count, "errors": errors}, indent=2, ensure_ascii=False))
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
