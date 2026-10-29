"""Export original CMES rows with observed evidence and remaining coverage gaps."""
import argparse
import csv
import json
from pathlib import Path

from ashare_data.storage.free_updates import report


def blocker(run):
    status=run["status"]
    message=run.get("evidence",{}).get("error","")
    if status in {"received", "empty_source_window"}:
        return None
    if status=="interrupted":
        return "任务中断待续，非数据失败"
    if status=="stale":
        return "来源日期较旧，按原发布频率核实"
    if status in {"unverified_date","release_verification_pending"}:
        return "日期格式或发布周期待核实"
    if "ModuleNotFoundError" in message:
        return "可修复：可选依赖缺失"
    if "requires explicit review" in message:
        return "来源地址待核实"
    if any(x in message for x in ("HTTP 401","HTTP 403","HTTP 429","captcha")):
        return "接口拒绝、验证或限流；未证明需要账户权限"
    if "cooling down" in message:
        return "前序请求失败后冷却，未新增网络尝试"
    if status=="timeout" or any(x in message for x in ("SSLError","ProxyError","Timeout","ConnectionError","URLError")):
        return "临时网络或证书校验失败"
    if "budget" in message or "exceeds" in message:
        return "可修复：请求需分页或缩小窗口"
    if "empty response" in message:
        return "本次样本无数据，需核实交易或发布日期"
    return "可修复或待核实：参数、响应格式或字段校验"


UNIVERSES={
    "stock":"沪深京股票身份目录（上市/退市与市场映射）",
    "etf":"沪深 ETF 目录及跨市场份额/行情覆盖",
    "bond":"可转债发行、存续及退市目录",
    "index":"指数目录与指数编制口径",
    "csindex":"中证指数目录与各指数当前成分",
    "ths_concept":"同花顺概念目录",
    "ths_industry":"同花顺行业目录",
    "em_concept":"东方财富概念目录",
    "em_industry":"东方财富行业目录",
    "sw_first":"申万一级行业目录",
    "sw_second":"申万二级行业目录",
    "sw_third":"申万三级行业目录",
    "tdx_block":"通达信板块目录（不同查询类型）",
    "future_main":"期货全部品种连续合约及换月定义",
    "future_contract":"期货全部有效月份合约、到期及夜盘交易日",
    "sge_symbol":"上金所品种目录",
    "city_first":"公布的新房价城市范围",
}


def export(root):
    evidence=report(root)
    for item in evidence["items"]:
        item["collection_goal"] = dict(item["collection_goal"],
            full_coverage_accepted=item["coverage_verified"], daily_deployed=item["daily_deployment"])
        needed={v[1:] for call in item["calls"] for v in call["parameters"].values()
                if isinstance(v,str) and v.startswith("$")}
        if needed & {"stock_prefixed","stock_upper","stock_suffix","stock_market"}:
            needed.add("stock")
        if "bond_prefixed" in needed:
            needed.add("bond")
        universes=[v for k,v in UNIVERSES.items() if k in needed]
        runs=item["sample_runs"]
        failures=sorted({b for r in runs if (b:=blocker(r))})
        lifecycle = item.get("source_lifecycle", {})
        if lifecycle.get("status") == "discontinued":
            failures = ["源指标停发，无法每日续更"]
        if item.get("source_selection",{}).get("semantic_gap"):
            failures=["原条目口径尚未覆盖："+item["source_selection"]["semantic_gap"]]
        gaps=[]
        if item.get("coverage_verified"):
            gaps.append("已验来源可得全部统计期、字段与分页；尚未观察下一真实发布周期")
        elif universes:
            gaps.append("代表样本尚未覆盖全目录；目录清单需版本化并按代码/日期分片续采")
        else:
            gaps.append("核对全部分页、维度、停更列和首个正式启用日覆盖")
        if item["mode"] in {"bars","futures_bars"}:
            gaps.append("验收全部周期、可得历史窗口、复权/主连定义、量额单位、交易日及夜盘边界")
        if item["mode"]=="financial":
            gaps.append("跨行业/市场、公告日与报告期、修订和公告可见时点覆盖")
        if item["mode"]=="snapshot":
            gaps.append("仅保存实际取得时点的快照；历史成分/历史快照不可由当前结果推回")
        if not item["implementation_verified"]:
            gaps.append("尚未完成当前版本全部字段口径、业务键、失败恢复及重跑验收")
        for r in runs:
            coverage=r["evidence"].get("coverage",{})
            if coverage.get("complete_history") is False and ("next_offset" in coverage or "next_page" in coverage):
                cursor = coverage.get("next_offset", coverage.get("next_page"))
                gaps.append("本次为历史分片，下一偏移/页："+str(cursor))
            if coverage.get("history_limit"):
                gaps.append(coverage["history_limit"])
            if coverage.get("complete_universe") is False:
                gaps.append("本次为目录分片，下一页："+str(coverage.get("next_page")))
            gaps.extend(r["evidence"].get("semantics",{}).get("gaps",[]))
            gaps.extend(r["evidence"].get("semantics",{}).get("limitations",[]))
        if item.get("source_selection",{}).get("semantic_gap"):
            gaps.append(item["source_selection"]["semantic_gap"])
        gaps.append("正式调度未部署；待与现有单 writer 协调发布")
        contexts=[]
        for run in runs:
            request=Path(root)/"runs"/(run["run_id"]+"-request.json")
            if request.exists():
                job=json.loads(request.read_text())["job"]
                contexts.append({"function":job["function"],"parameters":job["parameters"]})
        item.update(blocker_categories=failures,full_coverage_required=universes or ["来源实际提供的完整序列/维度"],
                    sample_contexts=contexts,remaining_acceptance=gaps,
                    live_collected_at=sorted({r["finished_at"] for r in runs if r["returned"]}),
                    latest_source_dates=sorted({str(r["evidence"].get("assessment",{}).get("latest_nonfuture_date")) for r in runs if r["evidence"].get("assessment",{}).get("latest_nonfuture_date")}))
    evidence["blocker_counts"]={k:sum(k in i["blocker_categories"] for i in evidence["items"])
        for k in sorted({k for i in evidence["items"] for k in i["blocker_categories"]})}
    return evidence


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root",type=Path,required=True)
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--csv",type=Path,required=True)
    args=p.parse_args()
    evidence=export(args.root)
    args.output.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+"\n")
    fields=["excel_row","category","item","candidate_status","candidate_source","note","source_url",
            "status","implementation_verified","coverage_verified","coverage_status","coverage_evidence",
            "daily_deployment","live_collected_at","latest_source_dates",
            "sample_contexts","full_coverage_required","remaining_acceptance","blocker_categories",
            "source_selection","source_lifecycle","exclude_superseded_functions","collection_goal"]
    with args.csv.open("w",encoding="utf-8-sig",newline="") as stream:
        writer=csv.DictWriter(stream,fieldnames=fields,extrasaction="ignore")
        writer.writeheader()
        for item in evidence["items"]:
            writer.writerow({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(list,dict)) else v for k,v in item.items()})
    print(json.dumps({k:v for k,v in evidence.items() if k!="items"},ensure_ascii=False))


if __name__=="__main__":
    main()
