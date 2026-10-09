"""The M2 CLI calls the same public codecs and views as Python; no acquisition."""

from .m2_payloads import economic_payload, query_payload
from .m2_types import M2AD08Call, M2ReadContext, canonical_bytes, canonical_hash, keys
from .model import DataError
from .storage import Store


def dispatch(root, action, request):
    if action == "canonical":
        keys(request, ("kind", "payload"))
        codecs = {
            "result": economic_payload,
            "query": query_payload,
            "context": lambda p: M2ReadContext.from_dict(p).to_dict(),
            "call": lambda p: M2AD08Call.from_dict(p).to_dict(),
        }
        if request["kind"] not in codecs:
            raise DataError("M2_SCHEMA", "未知规范编码类型")
        body = codecs[request["kind"]](request["payload"])
        return {
            "payload": body,
            "canonical_utf8": canonical_bytes(body).decode(),
            "sha256": canonical_hash(body),
        }
    store = Store(root)
    if action == "import-sources":
        keys(request, ("kind", "documents", "classification", "claims"))
        return {"component_id": store.import_m2_sources(**request)}
    if action == "import-component":
        keys(request, ("directory",))
        return {"component_id": store.import_m2_component(request["directory"])}
    if action == "component":
        keys(request, ("component_id",))
        return store.m2_component(request["component_id"]).to_dict()
    if action in ("validate", "compose"):
        fields = (
            "price_dataset_id",
            "facts_component_id",
            "calendar_component_id",
            "state_component_id",
            "window_ids",
            "mode",
        )
        keys(request, fields)
        return getattr(store, action + "_m2")(**request).to_dict()
    if action == "report":
        keys(request, ("report_id",))
        return store.m2_report(request["report_id"]).to_dict()
    if action == "export":
        keys(request, ("dataset_id", "directory"))
        return store.export_m2(**request).to_dict()
    if action == "import":
        keys(request, ("directory",))
        return {"dataset_id": store.import_m2(request["directory"])}
    if action in ("snapshots", "recover"):
        keys(request, ())
        return store.m2_snapshots() if action == "snapshots" else store.recover_m2()
    if action == "profiles":
        keys(request, ("dataset_id",))
        return [x.to_dict() for x in store.m2_profiles(request["dataset_id"])]
    if action != "query":
        raise DataError("M2_SCHEMA", "未知M2操作")
    base = ("dataset_id", "profile_sha256", "mode", "operation")
    op = request.get("operation")
    if op in ("descriptor", "profile", "windows", "quality", "lineage", "capabilities"):
        keys(request, base)
    elif op == "envelope":
        keys(request, (*base, "window_id", "assumption_ack", "consumer_binding"))
    else:
        keys(
            request,
            (*base, "window_id", "assumption_ack", "consumer_binding", "context", "parameters"),
        )
    view = store.m2(
        request["dataset_id"], profile_sha256=request["profile_sha256"], mode=request["mode"]
    )
    if op in ("descriptor", "profile", "quality", "lineage", "capabilities"):
        return getattr(view, op)().to_dict()
    if op == "windows":
        return [w.to_dict() for w in view.windows()]
    use = view.use(
        request["window_id"],
        assumption_ack=request["assumption_ack"],
        consumer_binding=request["consumer_binding"],
    )
    if op == "envelope":
        return use.envelope().to_dict()
    operations = {
        "decision_prev_close": ("call",),
        "execution_bar": (),
        "valuation_bar": (),
        "trading_state": (),
        "listing": ("target_date", "role"),
        "successor": ("candidate_date", "n"),
        "modeled_actions": (),
        "verify": ("read",),
        "revalidate": ("read",),
    }
    if op not in operations:
        raise DataError("M2_SCHEMA", "未知M2读操作")
    keys(request["parameters"], operations[op])
    context = M2ReadContext.from_dict(request["context"])
    if op in ("verify", "revalidate"):
        return getattr(use, op)(request["parameters"]["read"], context=context).to_dict()
    return getattr(use, op)(context, **request["parameters"]).to_dict()
