"""Replay bounded synthetic faults through the builder using sealed real input.

The unchanged original package is the real source evidence. Mutated copies are
explicit counterexamples, never replacement historical observations. No network.
"""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

import ashare_data
from ashare_data import HistoricalMarketView

BASE_SHA = "6eb76cddb27e15fb43e6c782dd337f710f81044f35cdf075b9d8abd21fd5f7a8"
SYMBOL = "600518.XSHG"
DAY = "2019-05-21"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_bytes())


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")


def verify(package, output):
    package, output = Path(package).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    checks, cases = [], []

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    check("original_binding", sha(package / "binding.json") == BASE_SHA)
    inventory = read(package / "binding.json")["files"]
    for entry in inventory:
        check("original/" + entry["path"], sha(package / entry["path"]) == entry["sha256"])
    builder_path = Path(__file__).with_name("build_historical_market.py")
    spec = importlib.util.spec_from_file_location("universe_fault_builder", builder_path)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    with patch(
        "socket.socket",
        side_effect=AssertionError("Offline universe verification attempted network"),
    ):
        baseline = HistoricalMarketView(package / "binding.json", expected_binding_sha256=BASE_SHA)
        ordinary = baseline.universe(DAY, candidates=[SYMBOL], exclude_st=True)
        check(
            "known_ST_excluded",
            len(ordinary["excluded"]) == 1 and not ordinary["rows"] and not ordinary["unknown"],
        )
        original_received = baseline.descriptor()["received_at"]
        definitions = [
            (
                "pool_st_conflict",
                "pool",
                "is_st",
                "0",
                "is_st",
                "exclude_st",
                "filter_dependency_conflict",
            ),
            (
                "daily_st_conflict",
                "state",
                "is_st",
                "0",
                "is_st",
                "exclude_st",
                "filter_dependency_conflict",
            ),
            (
                "pool_suspension_conflict",
                "pool",
                "tradestatus",
                "0",
                "suspended",
                "exclude_suspended",
                "filter_dependency_conflict",
            ),
            (
                "daily_suspension_conflict",
                "state",
                "tradestatus",
                "0",
                "suspended",
                "exclude_suspended",
                "filter_dependency_conflict",
            ),
            ("pool_st_empty", "pool", "is_st", "", "is_st", "exclude_st", "filter_field_unknown"),
            (
                "pool_suspension_empty",
                "pool",
                "tradestatus",
                "",
                "suspended",
                "exclude_suspended",
                "filter_field_unknown",
            ),
            (
                "pool_st_na_token",
                "pool",
                "is_st",
                "not_applicable",
                "is_st",
                "exclude_st",
                "filter_field_unknown",
            ),
            (
                "pool_suspension_na_token",
                "pool",
                "tradestatus",
                "not_applicable",
                "suspended",
                "exclude_suspended",
                "filter_field_unknown",
            ),
        ]
        for name, family, field, value, fact_name, option, reason in definitions:
            with tempfile.TemporaryDirectory(prefix=".universe-fault-", dir=output) as temporary:
                root = Path(temporary)
                source = root / "source"
                shutil.copytree(package, source)
                relative = (
                    "raw/universe-2019-05-21.json"
                    if family == "pool"
                    else "raw/kangmei-st-status.json"
                )
                path = source / relative
                data = read(path)
                row = next(
                    r for r in data["rows"] if r["symbol"] == "SH600518" and r["tradedate"] == DAY
                )
                previous = row[field]
                row[field] = value
                write(path, data)
                receipt_path = source / (relative + ".receipt.json")
                receipt = read(receipt_path)
                receipt.update(bytes=path.stat().st_size, sha256=sha(path))
                write(receipt_path, receipt)
                # No source receipt clock or market availability is fabricated.
                info = builder.build(source, root / "built")
                view = HistoricalMarketView(
                    root / "built/binding.json", expected_binding_sha256=info["binding_sha256"]
                )
                check(
                    name + "/clock_preserved", view.descriptor()["received_at"] == original_received
                )
                for candidates in (None, [SYMBOL]):
                    result = view.universe(DAY, candidates=candidates, **{option: True})
                    check(
                        name + "/not_selected",
                        SYMBOL not in {r["security"] for r in result["rows"]},
                    )
                    check(
                        name + "/not_silently_excluded",
                        SYMBOL not in {r["security"] for r in result["excluded"]},
                    )
                    unknown = next(r for r in result["unknown"] if r["security"] == SYMBOL)
                    check(
                        name + "/reason",
                        unknown["reason"] == reason and fact_name in unknown["fields"],
                    )
                    if reason == "filter_dependency_conflict":
                        check(
                            name + "/opposite_claims",
                            all(
                                {c["value"] for c in conflict["claims"]} == {False, True}
                                for conflict in unknown["conflicts"]
                            ),
                        )
                        check(
                            name + "/bound_sources",
                            all(
                                c["source_refs"]
                                for conflict in unknown["conflicts"]
                                for c in conflict["claims"]
                            ),
                        )
                    else:
                        check(
                            name + "/unknown_not_filled",
                            unknown["source_row"][fact_name]["value"] is None,
                        )
                raw = view.universe(DAY, candidates=[SYMBOL])
                check(
                    name + "/raw_observation_retained",
                    len(raw["rows"]) == 1 and raw["filter_fields"] == [],
                )
                cases.append(
                    dict(
                        name=name,
                        synthetic_fault=True,
                        source_path=relative,
                        source_before_sha256=sha(package / relative),
                        synthetic_after_sha256=sha(path),
                        field=field,
                        before=previous,
                        injected=value,
                        unknown=unknown,
                        unfiltered_diagnostics=raw["dependency_conflicts"],
                        rebuilt_binding_sha256=info["binding_sha256"],
                        historical_available_at=None,
                    )
                )
        for entry in inventory:
            check("unchanged/" + entry["path"], sha(package / entry["path"]) == entry["sha256"])
    report = dict(
        schema="ad.universe-filter-faults.v1",
        passed=len(checks),
        failed=0,
        checks=checks,
        source_binding_sha256=BASE_SHA,
        module=ashare_data.__file__,
        builder_sha256=sha(builder_path),
        synthetic_fault_cases=cases,
        source_mutated=False,
        network_used=False,
        new_source_acquisition=False,
        historical_pit=False,
        real_backtest_executed=False,
    )
    write(output / "report.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.package, args.output)
    print(
        json.dumps(
            {key: result[key] for key in ("passed", "failed", "source_mutated", "network_used")}
        )
    )
