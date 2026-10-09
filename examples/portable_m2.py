"""Public preparation -> validation -> immutable product export; no bundled market data."""

import argparse
import copy
import json
from pathlib import Path

from ashare_data import DataError, Store


def recipe(path):
    body = json.loads(path.read_text())
    result = copy.deepcopy(body)
    for doc in result["documents"]:
        for source in doc["artifacts"].values():
            original = Path(source["path"]).expanduser()
            source["path"] = str(original if original.is_absolute() else path.parent / original)
    return result


def prepare(store, market_recipe, facts_recipe, windows, output):
    market = store.import_m2_sources(**recipe(market_recipe))
    facts = store.import_m2_sources(**recipe(facts_recipe))
    request = dict(
        price_dataset_id=market,
        facts_component_id=facts,
        window_ids=windows,
        mode="conditional_research",
    )
    report = store.validate_m2(**request).to_dict()
    if report["status"] == "BLOCKED":
        return {"market_component_id": market, "facts_component_id": facts, "report": report}
    product = store.compose_m2(**request).to_dict()
    exported = store.export_m2(product["dataset_id"], output).to_dict()
    fresh = Store.init(output.parent / (output.name + "-reopened-store"))
    assert fresh.import_m2(output) == product["dataset_id"]
    return {
        "market_component_id": market,
        "facts_component_id": facts,
        "product": product,
        "export": exported,
        "relocated_import_equal": True,
        "native_runs": 0,
        "data_redistribution_permission": "not_granted_by_software_license",
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--store", type=Path, required=True)
    p.add_argument("--market-recipe", type=Path, required=True)
    p.add_argument("--facts-recipe", type=Path, required=True)
    p.add_argument("--window", choices=("m2a", "w1", "w2"), action="append", required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    try:
        result = prepare(
            Store.init(args.store), args.market_recipe, args.facts_recipe, args.window, args.output
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        raise SystemExit(2 if result.get("report", {}).get("status") == "BLOCKED" else 0)
    except DataError as error:
        print(json.dumps({"error": error.as_dict()}, ensure_ascii=False))
        raise SystemExit(2)
