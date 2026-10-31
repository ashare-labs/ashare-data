"""Checkpoint the vendor's visible product directory without executing its JS.

Vendor navigation groups are not exchange identities (INE is grouped in SHFE).
A discovered continuous symbol is not a deliverable futures contract. Missing
products and unit reviews remain explicit; a navigation list is no claim that
every exchange-listed product is available from this vendor.
"""
import ast
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlencode

from ashare_data.providers.free_updates import encoded
from ashare_data.storage.immutable import immutable_write

INDEX_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/view/js/qihuohangqing.js"
LIST_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQFuturesData"
GROUPS = {"czce", "dce", "shfe", "cffex", "gfex"}


def nodes(body):
    if len(body) > 128000:
        raise ValueError("futures navigation exceeds reviewed size")
    text = body.decode("gb18030")
    match = re.match(r"\s*ARRFUTURESNODES\s*=\s*(\{.*?\});", text, re.S)
    if not match:
        raise ValueError("futures navigation assignment absent")
    # Only the literal assignment is parsed; all script following it is ignored.
    literal = re.sub(r"(?m)^\s*//[^\n]*$", "", match[1])
    literal = re.sub(r"\b(czce|dce|shfe|cffex|gfex)\s*:", r"'\1':", literal)
    value = ast.literal_eval(literal)
    if not isinstance(value, dict) or set(value) != GROUPS:
        raise ValueError("futures navigation groups changed")
    output = []
    for group, entries in value.items():
        if not isinstance(entries, list) or not entries or not isinstance(entries[0], str):
            raise ValueError("futures navigation group schema changed")
        for entry in entries[1:]:
            if not isinstance(entry, list) or not 3 <= len(entry) <= 5 or not all(isinstance(v, (str, int)) for v in entry):
                raise ValueError("futures product entry schema changed")
            name, node = entry[:2]
            if not name or not re.fullmatch(r"[a-z0-9_]+_qh", node):
                raise ValueError("invalid futures product identity")
            output.append({"vendor_group": group, "vendor_group_name": entries[0],
                           "product_name": name, "node": node})
    if not 1 <= len(output) <= 300 or len({r["node"] for r in output}) != len(output):
        raise ValueError("futures product count/duplicates invalid")
    return output


def listing_url(node):
    if not re.fullmatch(r"[a-z0-9_]+_qh", node):
        raise ValueError("invalid vendor node")
    return LIST_URL+"?"+urlencode({"page": "1", "num": "100", "sort": "position", "asc": "0", "node": node, "base": "futures"})


def continuous_candidates(body):
    rows = json.loads(body)
    if not isinstance(rows, list) or len(rows) >= 100:
        raise ValueError("vendor contract list invalid or at page limit; pagination needed")
    if any(not isinstance(r, dict) or not isinstance(r.get("symbol"), str) for r in rows):
        raise ValueError("vendor contract identities invalid")
    symbols = [r["symbol"] for r in rows]
    if len(set(symbols)) != len(symbols):
        raise ValueError("vendor contract identities duplicated/invalid")
    candidates = []
    for row in rows:
        if re.fullmatch(r"[A-Z]{1,3}0", row.get("symbol", "")):
            if "连续" not in row.get("name", ""):
                raise ValueError("zero-suffix instrument not labelled continuous")
            candidates.append({"symbol": row["symbol"], "name": row["name"],
                               "vendor_exchange": row.get("exchange"), "source_native": row})
    if len(candidates) > 1:
        raise ValueError("ambiguous continuous instrument in vendor product node")
    return candidates


def verify_receipts(root, receipts):
    for receipt in receipts:
        body = gzip.decompress((root/"raw"/(receipt["sha256"]+".gz")).read_bytes())
        if hashlib.sha256(body).hexdigest() != receipt["sha256"]:
            raise ValueError("futures discovery receipt hash mismatch")


def discover(root, *, max_nodes=24, blocked_hosts=()):
    """Bounded one-request-per-node discovery, resumed from immutable receipts."""
    from ashare_data.providers.free_updates.free_update_worker import Capture
    from ashare_data.providers.free_updates.free_update_futures import PRODUCTS
    from ashare_data.providers.free_updates import implementation_identity
    root = Path(root).resolve()
    if not 1 <= max_nodes <= 100:
        raise ValueError("discovery limit must be 1..100 nodes")
    root.mkdir(parents=True, exist_ok=True)
    (root/"nodes").mkdir(exist_ok=True)
    capture = Capture(root/"raw", blocked_hosts, interval=1.2, request_limit=max_nodes+1)
    index_path = root/"index.json"
    if index_path.exists():
        index = json.loads(index_path.read_text())
        verify_receipts(root, index["receipts"])
        body = gzip.decompress((root/"raw"/(index["receipts"][0]["sha256"]+".gz")).read_bytes())
        if nodes(body) != index["products"]:
            raise ValueError("frozen futures navigation differs from its receipt")
    else:
        body = capture.get(INDEX_URL)
        index = {"products": nodes(body), "receipts": list(capture.receipts)}
        immutable_write(index_path, encoded(index))
    attempted, errors = 0, []
    for product in index["products"]:
        target = root/"nodes"/(product["node"]+".json")
        if target.exists():
            saved = json.loads(target.read_text())
            verify_receipts(root, saved["receipts"])
            if saved["product"] != product or len(saved["receipts"]) != 1 or saved["receipts"][0]["url"] != listing_url(product["node"]):
                raise ValueError("futures saved node does not match product or source URL")
            body = gzip.decompress((root/"raw"/(saved["receipts"][0]["sha256"]+".gz")).read_bytes())
            if continuous_candidates(body) != saved["candidates"] or len(json.loads(body)) != saved["returned_contract_count"]:
                raise ValueError("futures saved candidates differ from native receipt")
            continue
        if attempted >= max_nodes:
            break
        before = len(capture.receipts)
        attempted += 1
        try:
            body = capture.get(listing_url(product["node"]))
            candidates = continuous_candidates(body)
            immutable_write(target, encoded({"product": product, "candidates": candidates,
                "returned_contract_count": len(json.loads(body)), "receipts": capture.receipts[before:]}))
        except (ValueError, OSError, RuntimeError) as exc:
            errors.append({"node": product["node"], "error": type(exc).__name__+": "+str(exc),
                           "receipts": capture.receipts[before:]})
            break  # no host-hopping or unbounded retry after a source failure
    members, pending, no_continuous = [], [], []
    for product in index["products"]:
        target = root/"nodes"/(product["node"]+".json")
        if not target.exists():
            pending.append(product)
            continue
        saved = json.loads(target.read_text())
        if not saved["candidates"]:
            no_continuous.append(product)
        for candidate in saved["candidates"]:
            members.append(product | candidate | {"reviewed_contract": PRODUCTS.get(candidate["symbol"]),
                "received_at": saved["receipts"][-1]["received_at"]})
    if len({m["symbol"] for m in members}) != len(members):
        raise ValueError("one continuous symbol appears under multiple product nodes")
    output = {"schema": "ad.free-update-universe.v1", "observed_at": datetime.now(timezone.utc).isoformat(),
        "implementation_sha256": implementation_identity(), "source_navigation_count": len(index["products"]),
        "resolved_node_count": len(index["products"])-len(pending), "members": members,
        "contexts": [{"future_main": m["symbol"]} for m in members if m["reviewed_contract"]],
        "pending_nodes": pending, "nodes_without_continuous": no_continuous, "errors": errors,
        "attempted_this_run": attempted, "source_receipts": index["receipts"],
        "scope": "current vendor navigation nodes and their explicit continuous identities",
        "all_exchange_products_verified": False, "complete_delivery_contract_universe": False,
        "coverage_limitations": ["供应商导航分组不等于真实交易所，未审单位品种不进入采集队列",
            "导航之外新上市/停牌/退市品种须与交易所目录另行对账；源无主连的节点保留，不删除",
            "只解析数据字面量，未执行供应商JavaScript。目录可断点续传，版本变化需新建目录快照"]}
    temporary = root/"manifest.next.json"
    temporary.write_bytes(encoded(output))
    temporary.replace(root/"manifest.json")
    return output
