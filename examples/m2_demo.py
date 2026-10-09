"""All finite M2 read roles and actual sealed samples through the public codec, offline.

This does not import RQ, place orders, or certify native calls. Consumer identities
are explicitly demo declarations, not a backend execution session.
"""

import argparse
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pandas as pd

from ashare_data import M2ConsumerBinding, Store
from ashare_data.m2_codec import consume_previous, history_array, native_ad08_call
from ashare_data.m2_payloads import economic_payload, query_contract
from ashare_data.m2_types import canonical_bytes, canonical_hash


def replay(bundle, root):
    store = Store.init(root)
    dataset = store.import_m2(bundle)
    results = []
    pins = []
    envelopes = []
    plans = []
    count = 0
    golden = None
    for registered in store.m2_profiles(dataset):
        profile = registered.to_dict()
        view = store.m2(
            dataset, profile_sha256=profile["profile_sha256"], mode="conditional_research"
        )
        pins.append(view.descriptor().to_dict())
        for plan_doc in view.windows():
            plan = plan_doc.to_dict()
            wid = plan["geometry"]["window_id"]
            plans.append(plan)
            binding = M2ConsumerBinding(
                view.descriptor().sha256,
                wid,
                plan["window_plan_sha256"],
                canonical_hash({"demo_data_binding": wid}),
                "offline-codec-" + wid,
                canonical_hash({"demo_request": wid}),
                "codec-replay-no-strategy",
                canonical_hash({"demo_seven_ack_declaration": wid}),
            )
            use = view.use(
                wid,
                assumption_ack=[x.to_dict() for x in view.assumptions(wid)],
                consumer_binding=binding,
            )
            envelopes.append(use.envelope().to_dict())
            links = {x["current"]: x for x in plan["geometry"]["calendar_links"]}
            for role in plan["read_roles"]:
                day = role["current_date"]
                previous = links[day]["previous_open"]
                phase = role["phase"]
                for op in role["operations"]:
                    clock = (
                        "08:00:00"
                        if phase == "INITIALIZE"
                        else "09:00:00"
                        if phase == "DECIDE"
                        else "15:00:00"
                    )
                    c = use.context(
                        current_date=day,
                        logical_at=day + "T" + clock + "+08:00",
                        phase=phase,
                        query_end=previous + "T23:59:59.999999+08:00"
                        if phase in ("INITIALIZE", "DECIDE")
                        else day + "T15:00:00+08:00",
                        visibility="assumed",
                        consumer="strategy" if op == "decision_prev_close" else "engine",
                        event_cursor=0,
                        restore_generation=0,
                        epoch=0,
                    )
                    if op == "decision_prev_close":
                        call = native_ad08_call(
                            security="600000.XSHG",
                            trade_date=day,
                            dt=pd.Timestamp(previous),
                            adjust_orig=datetime.fromisoformat(day),
                            frequency="1d",
                            fields="close",
                            bar_count=1,
                            include_now=False,
                            skip_suspended=False,
                            adjust_type="pre",
                            time_policy="rq641_shanghai_midnight",
                        )
                        read = use.decision_prev_close(c, call=call)
                        native_array = history_array(read, use=use, context=c)
                        consumed = consume_previous(native_array[0], read=read, use=use, context=c)
                        results.append(
                            {
                                "window": wid,
                                "trade_date": day,
                                "previous_open": previous,
                                "value": consumed.value,
                                "owner_read": read.to_dict(),
                                "codec_observation": consumed.codec_observation.to_dict(),
                            }
                        )
                    elif op == "listing":
                        read = use.listing(c, target_date=role["target_date"], role=role["role"])
                    elif op == "successor":
                        read = use.successor(c, candidate_date=role["target_date"], n=1)
                    else:
                        read = getattr(use, op)(c)
                    count += 1
                    if wid == "w1" and day == "2020-01-03" and op == "trading_state":
                        current = replace(c, restore_generation=1, epoch=1)
                        renewed = read.with_authorization(use.revalidate(read, context=current))
                        assert (
                            renewed.evidence == read.evidence
                            and renewed.result == read.result
                            and renewed.sources == read.sources
                        )
                        assert renewed.authorization != read.authorization
                        use.verify(renewed, context=current)
                        golden = {
                            "nested_listing_original": read.to_dict(),
                            "new_generation": renewed.to_dict(),
                            "stable_evidence_sha256": read.evidence.to_dict()["evidence_sha256"],
                            "window_plan": plan,
                            "owner_pin": view.descriptor().to_dict(),
                        }
    prev = results[0]["owner_read"]["result"]
    decimal_vectors = [economic_payload({**prev, "value": v}) for v in ("12.47", "12.4700")]
    assert canonical_hash(decimal_vectors[0]) == canonical_hash(decimal_vectors[1])
    return {
        "status": "OFFLINE_OWNER_AND_CODEC_REPLAY_PASS_NOT_NATIVE_ADMISSION",
        "dataset_id": dataset,
        "owner_pins": pins,
        "window_plans": plans,
        "envelopes": envelopes,
        "query_contract": query_contract(),
        "reads_exercised": count,
        "previous_close_consumptions": len(results),
        "window_execution_days": {
            p["geometry"]["window_id"]: len(p["geometry"]["calendar_links"]) for p in plans
        },
        "previous_reads": results,
        "golden_vector": golden,
        "decimal_equivalence_vectors": decimal_vectors,
        "native_runs": 0,
        "native_AD08_call_observed": False,
        "network_requests": 0,
        "backend_execution_authorized": False,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument("--store", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    result = replay(a.bundle, a.store)
    a.output.write_bytes(canonical_bytes(result) + b"\n")
    print(
        canonical_bytes(
            {
                k: result[k]
                for k in (
                    "status",
                    "dataset_id",
                    "reads_exercised",
                    "previous_close_consumptions",
                    "window_execution_days",
                    "native_runs",
                )
            }
        ).decode()
    )
