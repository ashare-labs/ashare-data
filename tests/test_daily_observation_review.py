"""R2: raw source semantics survive rehashed derived projections (fixtures)."""

import copy
import json

import pytest

from ashare_data import DataError, daily_observation as observation
from ashare_data.model import canonical

from test_daily_observation import capture, upstream as source_fixture


@pytest.fixture
def upstream(monkeypatch):
    return source_fixture.__wrapped__(monkeypatch)


@pytest.mark.parametrize(
    "edit", ["clear", "omit_change", "invent", "omit_basis", "remove_metadata"]
)
def test_rehashed_revision_semantics_rejected(upstream, tmp_path, edit):
    first = capture(tmp_path)
    original = observation.read(tmp_path, first["capture_id"])
    upstream["rows"][-1]["close"] = "10.20"
    second = capture(tmp_path)
    assert second["status"] == "revision_blocked", second
    packet = copy.deepcopy(observation.read(tmp_path, second["capture_id"]))
    if edit == "clear":
        packet.update(revisions=[], signal_ready=True)
    elif edit == "omit_change":
        packet["revisions"][0]["changes"].pop()
    elif edit == "invent":
        packet["revisions"][0]["changes"][0]["before"]["close"] = "9.00"
    elif edit == "omit_basis":
        packet.update(comparison_captures=[], revisions=[], signal_ready=True)
    else:
        packet.pop("comparison_captures")
        packet.pop("comparison_sha256")
        packet.update(revisions=[], signal_ready=True)
    raw = canonical(packet)
    cap = observation.sha(raw)
    (tmp_path / "packets" / (cap + ".json")).write_bytes(raw)
    with pytest.raises(DataError):
        observation.read(tmp_path, cap)
    # Even rebinding the mutable attempt cannot bypass raw comparison semantics
    # or the immutable pre-fetch comparison manifest.
    path = tmp_path / "attempts" / (packet["attempt_id"] + ".json")
    attempt = json.loads(path.read_bytes())
    attempt["capture_id"] = cap
    if edit == "remove_metadata":
        attempt.pop("comparison_captures")
    path.write_bytes(canonical(attempt))
    with pytest.raises(DataError):
        observation.read(tmp_path, cap)
    assert observation.read(tmp_path, first["capture_id"]) == original
