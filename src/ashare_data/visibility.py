"""Clock filtering is a declared model, not proof of historical publication."""
from datetime import datetime, time

from .contracts import label_time
from .model import TZ, require, timestamp

LEVELS = {"assumed", "received", "verified"}


def select_visible(rows, meta, *, as_of, level, daily, contract=None):
    require(level in LEVELS, "INVALID_VISIBILITY", "visibility 支持 assumed/received/verified")
    require(level != "verified", "VISIBILITY_UNKNOWN", "当前公开源缺少逐版本历史公布、闭合语义和修订证据，不能承诺 PIT",
            {"requested_as_of": as_of.isoformat(), "point_in_time_verified": False})
    if level == "received":
        require(timestamp(meta["observed_at"]) <= as_of, "VISIBILITY_UNKNOWN",
                "此缓存版本是在策略时钟之后收到，不能作为当时已知数据",
                {"observed_at": meta["observed_at"], "as_of": as_of.isoformat(), "raw_hash": meta["sha256"]})
    accepted, excluded, assumptions = [], [], set()
    for row in rows:
        bound = contract.bounds(row["day"], as_of=as_of) if contract else None
        if bound:
            threshold = timestamp(bound["bar_end"])
            assumptions.add("使用覆盖契约声明的区间；此声明本身不是供应商公布或修订证明")
        elif daily:
            # Unknown daily scope can include after-hours trading. Do not assert
            # that 15:05 is full-day finality; the fallback waits until date end.
            threshold = datetime.combine(label_time(row["day"]).date(), time.max, TZ)
            assumptions.add("日线统计范围未知，模型保守到北京时间该日期结束才允许；不代表日线最终版")
        else:
            threshold = label_time(row["day"])
            assumptions.add("假设分钟源标签为结束时刻；区间/公布延时未实测，不平移源标签")
        if threshold <= as_of:
            accepted.append(row)
        else:
            excluded.append({"source_label": row["day"], "modeled_end": threshold.isoformat(), "reason": "not_closed_under_model"})
    report = {"level": level, "as_of": as_of.isoformat(), "point_in_time_verified": False,
              "historical_vendor_publication_verified": False, "revision_history": "unknown",
              "source_version_observed_at": meta["observed_at"], "raw_hash": meta["sha256"],
              "available_at": meta["observed_at"] if level == "received" else None,
              "visibility_basis": "local_response_received" if level == "received" else "explicit_clock_model",
              "finality": "CLOSED_PROVISIONAL", "assumptions": sorted(assumptions), "excluded": excluded}
    return accepted, report
