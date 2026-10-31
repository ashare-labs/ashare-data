"""Freeze current ETF identities from hash-checked, complete exchange captures.

This is an offline manifest export, never a new capture or acceptance upgrade.
Listing history and a missing SZSE share-statistics date remain explicit gaps.
"""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import re

from ashare_data.storage.free_updates import connect


def export(root):
    root=Path(root).resolve()
    records, contexts, evidence, counts = [], [], [], {}
    sources={"fund_etf_scale_sse":"SH", "fund_etf_only_szse":"SZ"}
    with connect(root,readonly=True) as db:
        for function,market in sources.items():
            run=db.execute("SELECT * FROM runs WHERE status='received' AND json_extract(job_json,'$.function')=? ORDER BY finished_at DESC LIMIT 1",(function,)).fetchone()
            if run is None:
                raise ValueError("missing corrected successful exchange capture: "+function)
            result=json.loads((root/"runs"/(run["run_id"]+".json")).read_text())
            if not result.get("coverage",{}).get("complete_universe"):
                raise ValueError("exchange capture is not the complete returned identity universe")
            for receipt in result["receipts"]:
                body=gzip.decompress((root/"raw"/(receipt["sha256"]+".gz")).read_bytes())
                if hashlib.sha256(body).hexdigest() != receipt["sha256"]:
                    raise ValueError("exchange raw receipt hash mismatch")
            rows=result["records"]
            counts[market]=len(rows)
            for row in rows:
                code=row["基金代码"]
                if not re.fullmatch(r"\d{6}",code) or not row.get("基金简称"):
                    raise ValueError("invalid exchange ETF identity")
                if market == "SZ" and row.get("基金类别") != "ETF":
                    raise ValueError("non-ETF leaked into corrected exchange capture")
                records.append({"security":market+"."+code,"code":code,"market":market,
                    "name":row["基金简称"],"source_function":function,"run_id":run["run_id"],
                    "listing_date":row.get("上市日期"),"statistical_date":row.get("统计日期"),
                    "fund_type":row.get("基金类别",row.get("ETF类型"))})
                contexts.append({"etf":code,"etf_market":market})
            evidence.append({"function":function,"run_id":run["run_id"],"observed_at":run["finished_at"],
                "records":len(rows),"coverage":result["coverage"],"receipts":result["receipts"]})
    if len({r["security"] for r in records}) != len(records):
        raise ValueError("duplicate ETF exchange identities")
    return {"schema":"ad.free-update-universe.v1", "scope":"all ETF identities in the two captured current exchange lists",
        "observed_at":max(r["observed_at"] for r in evidence), "counts":counts,
        "contexts":contexts,"members":records,"source_receipts":evidence,
        "coverage_limitations":["只证明这两个原始回执时点的当前ETF目录，不证明退市ETF和历史身份全覆盖",
            "深交所份额统计日缺失，上市日期不可充当份额更新日；未提高份额条目的核心验收状态",
            "全目录计划不是已执行采集；每条行情与日期分片仍须单独验收"]}



def export_stock_directory(root):
    """Freeze the official directory, retaining aliases and unknown active status."""
    from collections import Counter
    from ashare_data.providers.free_updates.free_update_reference import stock_market
    from ashare_data.providers.free_updates.free_update_disclosures import DIRECTORY
    root = Path(root).resolve()
    with connect(root, readonly=True) as db:
        candidates = db.execute("SELECT * FROM runs WHERE status IN ('received','empty_source_window') AND json_extract(job_json,'$.function')='stock_zh_a_disclosure_report_cninfo' ORDER BY finished_at DESC").fetchall()
        for run in candidates:
            result = json.loads((root/"runs"/(run["run_id"]+".json")).read_text())
            receipts = [r for r in result["receipts"] if r["url"] == DIRECTORY]
            if receipts:
                break
        else:
            raise ValueError("missing official directory capture")
    if len(receipts) != 1:
        raise ValueError("ambiguous official directory receipt")
    receipt = receipts[0]
    body = gzip.decompress((root/"raw"/(receipt["sha256"]+".gz")).read_bytes())
    if hashlib.sha256(body).hexdigest() != receipt["sha256"]:
        raise ValueError("official directory raw hash mismatch")
    rows = json.loads(body)["stockList"]
    members, excluded = [], []
    for row in rows:
        if row.get("category") != "A股":
            excluded.append(row)
            continue
        if not row.get("orgId") or not row.get("zwjc"):
            raise ValueError("directory issuer identity/name missing")
        code = row["code"]
        members.append({"security": code+"."+stock_market(code), "code": code,
            "issuer_org_id": row["orgId"], "name": row["zwjc"], "source_category": row["category"],
            "active_listing_verified": False, "native": row})
    if not members or len({r["security"] for r in members}) != len(members):
        raise ValueError("directory empty or duplicate security identity")
    groups = {}
    for row in members:
        groups.setdefault(row["issuer_org_id"], []).append(row["security"])
    return {"schema": "ad.free-update-stock-directory.v1", "scope": "all A-share identities currently returned by CNInfo; contains historical identities and aliases, not an active-listed universe",
        "observed_at": receipt["received_at"], "source_receipt": receipt, "source_run_id": run["run_id"],
        "source_count": len(rows), "selected_count": len(members), "excluded_count": len(excluded),
        "excluded_categories": dict(Counter(r.get("category") for r in excluded)),
        "market_counts": dict(Counter(r["security"][-2:] for r in members)), "members": members,
        "contexts": [{"stock": r["code"]} for r in members],
        "multi_identity_issuers": {k:v for k,v in groups.items() if len(v)>1},
        "coverage_limitations": ["旧代码与迁移新代码均保留，不将相同机构ID误当重复证券后静默删除",
            "来源目录不包含可证明现役上市状态的字段；完整目录计划不等于现役全市场已采集",
            "逐证券任务须按限额和冷却断点运行，资料返回代码不一致时拒绝入库"]}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root",type=Path,required=True)
    p.add_argument("--kind", choices=["etf", "stock-directory"], default="etf")
    p.add_argument("--output",type=Path,required=True)
    args=p.parse_args()
    result=export(args.root) if args.kind == "etf" else export_stock_directory(args.root)
    data=(json.dumps(result,ensure_ascii=False,indent=2)+"\n").encode()
    if args.output.exists() and args.output.read_bytes() != data:
        raise ValueError("use a new versioned universe file; existing manifest differs")
    args.output.write_bytes(data)
    print(json.dumps({"contexts":len(result["contexts"]),"counts":result.get("counts", result.get("market_counts")),"sha256":hashlib.sha256(data).hexdigest()},ensure_ascii=False))


if __name__ == "__main__":
    main()
