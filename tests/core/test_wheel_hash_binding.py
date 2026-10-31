"""Synthetic wheels only; the helper must never weaken locked-artifact checks."""
import hashlib
from pathlib import Path
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from verify_wheel import bind_local_wheels


def fixture(tmp_path, *, metadata_name="synthetic"):
    directory = tmp_path / "wheels"
    directory.mkdir()
    wheel = directory / "synthetic-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as z:
        z.writestr("synthetic-1.0.dist-info/METADATA", f"Name: {metadata_name}\nVersion: 1.0\n")
    raw = wheel.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    lock = tmp_path / "uv.lock"
    lock.write_text(f'[[package]]\nname="synthetic"\nversion="1.0"\nwheels=[{{url="https://files.pythonhosted.org/synthetic-1.0-py3-none-any.whl",hash="sha256:{sha}",size={len(raw)}}}]\n')
    req = tmp_path / "requirements.txt"
    unchanged = 'unrelated==2.0 \\\n    --hash=sha256:'+'1'*64+'\n'
    req.write_text('synthetic==1.0 ; python_version >= "3.12" \\\n    --hash=sha256:'+'0'*64+' \\\n    --hash=sha256:'+sha+'\n'+unchanged)
    return directory, wheel, sha, lock, req, unchanged


def test_exact_locked_artifact_keeps_marker_and_other_requirements(tmp_path):
    directory, _, sha, lock, req, unchanged = fixture(tmp_path)
    bindings = bind_local_wheels(req, directory, lock)
    assert bindings[0]["sha256"] == sha
    text = req.read_text()
    assert ' ; python_version >= "3.12" \\' in text
    assert '--hash=sha256:'+sha in text and '0'*64 not in text
    assert text.endswith(unchanged)


@pytest.mark.parametrize("problem", ["contents", "exported_hash", "size", "metadata"])
def test_corrupt_or_mismatched_local_artifact_is_rejected(tmp_path, problem):
    directory, wheel, sha, lock, req, _ = fixture(tmp_path, metadata_name="wrong" if problem == "metadata" else "synthetic")
    if problem == "contents":
        wheel.write_bytes(wheel.read_bytes()+b"changed")
    if problem == "exported_hash":
        req.write_text(req.read_text().replace(sha, "9"*64))
    if problem == "size":
        lock.write_text(lock.read_text().replace(f'size={wheel.stat().st_size}', 'size=1'))
    previous = req.read_bytes()
    with pytest.raises(ValueError):
        bind_local_wheels(req, directory, lock)
    assert req.read_bytes() == previous
