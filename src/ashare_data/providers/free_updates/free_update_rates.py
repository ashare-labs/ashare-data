"""Jin10 Shibor history with all eight native tenors and explicit rate units."""
import json
from ashare_data.providers.free_updates import source_date

TENORS = ("O/N", "1W", "2W", "1M", "3M", "6M", "9M", "1Y")
HEADERS = [{"name": "定价", "unit": ""}, {"name": "涨跌幅", "unit": "点"}]
UNITS = {f"{tenor}-{kind}": unit for tenor in TENORS for kind, unit in (("定价", "percent per annum"), ("涨跌幅", "basis_points"))}


def reserve_requirement(rows, as_of, numeric):
    """Separate source announcement and effective dates, including staged cuts."""
    from ashare_data.providers.free_updates.free_update_dates import reviewed_date
    units={**dict.fromkeys(['INTEREST_RATE_BB','INTEREST_RATE_BA','INTEREST_RATE_SB','INTEREST_RATE_SA'],'percent'),
           **dict.fromkeys(['CHANGE_RATE_B','CHANGE_RATE_S'],'percentage_points'),
           **dict.fromkeys(['NEXT_SH_RATE','NEXT_SZ_RATE'],'source_native_change_scale_unverified')}
    keys,announced,effective,issues=[],[],[],[]
    for row in rows:
        announcement=reviewed_date(row.get('PUBLISH_DATE'))
        activation=reviewed_date(row.get('TRADE_DATE'))
        report=reviewed_date(row.get('REPORT_DATE'))
        if not announcement or announcement>as_of or report!=announcement or not activation:
            raise ValueError('reserve requirement announcement/effective date identity invalid')
        keys.append((announcement,activation))
        announced.append(announcement)
        effective.append(activation)
        for field in units:
            if field not in row:
                raise ValueError('reserve requirement field missing: '+field)
            value=numeric(row[field])
            if field.startswith('INTEREST_RATE_') and value is not None and not 0<=value<=100:
                raise ValueError('reserve requirement rate outside percent range')
        for group,before,after in [('B','INTEREST_RATE_BB','INTEREST_RATE_BA'),('S','INTEREST_RATE_SB','INTEREST_RATE_SA')]:
            values=[numeric(row[k]) for k in [before,after,'CHANGE_RATE_'+group]]
            if all(v is not None for v in values) and abs(values[1]-values[0]-values[2])>numeric('0.000001'):
                issues.append({'announcement':announcement,'effective':activation,'group':group,
                    'reason':'source adjustment and displayed before/after rates differ; native values and policy remark retained'})
    if not rows or len(keys)!=len(set(keys)):
        raise ValueError('reserve requirement empty or duplicate announcement/effective key')
    return {'contract_reviewed':True,'business_key':['REPORT_DATE','TRADE_DATE'],'unique_keys':len(keys),
        'source_rows':len(rows),'units':units,'frequency':'announced_policy_change_and_revision',
        'latest_period':max(effective),'latest_announcement':max(announced),'quality_issues':issues,
        'gaps':[] if any(numeric(r['INTEREST_RATE_BA']) is not None or numeric(r['INTEREST_RATE_SA']) is not None for r in rows) else ['没有有效准备金率'],
        'date_roles':{'REPORT_DATE':'source announcement date','PUBLISH_DATE':'source announcement date label',
                      'TRADE_DATE':'announced effective date; may be future; not a completed policy execution'},
        'limitations':['同一次公告分次生效分别留存，不按公告日丢弃第二次调整',
            '大型/中小机构为来源概括；定向政策和适用对象按REMARK保留，不当作每家机构实际准备金率',
            '公告日是源提供的日期，精确公告时刻未提供；后续是否有新公告须按当前源更新核对',
            '附带股指次日变化保留原值与未知尺度，不当作因果效果或可预见收益']}


def collect(capture, params):
    if params:
        raise ValueError("Shibor source exposes its current whole history without date parameters")
    response = json.loads(capture.get("https://cdn.jin10.com/data_center/reports/il_1.json"))
    if response.get("keys") != HEADERS or response.get("products") != list(TENORS) or not isinstance(response.get("values"), dict):
        raise ValueError("Shibor native header/tenor schema changed")
    rows = []
    for day, terms in sorted(response["values"].items()):
        if set(terms) != set(TENORS) or any(not isinstance(terms[t], list) or len(terms[t]) != 2 for t in TENORS):
            raise ValueError("Shibor missing native tenor/value pair; no fill")
        rows.append({"日期": day, **{f"{t}-{label}": terms[t][i] for t in TENORS for i, label in enumerate(("定价", "涨跌幅"))}})
    capture.dataset_metadata = {"source": "Jin10 il_1 Shibor", "source_count": len(rows), "pages_received": 1,
        "complete_returned_window": True, "complete_history": True, "complete_universe": True,
        "scope": "all eight Shibor tenors and dates currently in the public source JSON; not all history before that window",
        "source_headers": response["keys"], "source_tenors": response["products"], "units": UNITS,
        "unit_definition": "https://www.chinamoney.com.cn/chinese/syh/",
        "publication_definition": "https://www.chinamoney.com.cn/chinese/bkshibor/"}
    return rows


def validate(rows, job, as_of, numeric):
    if job['function']=='macro_china_reserve_requirement_ratio':
        return reserve_requirement(rows,as_of,numeric)
    if job["function"] != "macro_china_shibor_all":
        return None
    dates, missing = [], dict.fromkeys(UNITS, 0)
    for row in rows:
        day = source_date(row.get("日期"))
        if not day or not "1990-01-01" <= day <= as_of:
            raise ValueError("Shibor date invalid/future")
        dates.append(day)
        for field in UNITS:
            if field not in row:
                raise ValueError("Shibor tenor/measure field absent")
            if numeric(row[field]) is None:
                missing[field] += 1
    if not rows or len(dates) != len(set(dates)):
        raise ValueError("Shibor empty/duplicate trading dates")
    latest = rows[dates.index(max(dates))]
    # Older source histories can have missing dates or rounded corrections, so
    # only consecutive calendar days support a strict basis-point scale check.
    scale_checks, scale_disagreements = 0, []
    from datetime import date
    ordered = sorted(rows, key=lambda r: r["日期"])
    for previous, current in zip(ordered, ordered[1:]):
        if (date.fromisoformat(current["日期"])-date.fromisoformat(previous["日期"])).days != 1:
            continue
        for t in TENORS:
            a, b, change = [numeric(v) for v in (previous[t+"-定价"], current[t+"-定价"], current[t+"-涨跌幅"])]
            if all(v is not None for v in (a, b, change)):
                scale_checks += 1
                if abs((b-a)*100-change) > numeric("0.011"):
                    scale_disagreements.append({"date": current["日期"], "tenor": t, "source_change": str(change), "rate_implied_bp": str((b-a)*100)})
    return {"contract_reviewed": True, "business_key": ["日期"], "unique_keys": len(dates), "source_rows": len(rows),
        "units": UNITS, "frequency": "Shibor_publication_day", "latest_period": max(dates), "latest_announcement": None,
        "missing_field_observations": missing, "consecutive_day_scale_checks": scale_checks,
        "scale_disagreements": scale_disagreements,
        "gaps": [t+" latest rate missing" for t in TENORS if numeric(latest[t+"-定价"]) is None],
        "validated_fields": [t+"-定价" for t in TENORS],
        "source_change_fields": [t+"-涨跌幅" for t in TENORS],
        "change_field_validation": "source displayed changes retained; historical arithmetic disagreements are separately reported",
        "limitations": ["定价为年化百分率，涨跌为BP；不将BP当百分比涨跌幅；八期限与缺值原样保留",
            "历史涨跌值与相邻日定价差的异常单列，不以其否定已明确的八期限定价；不重算或覆盖原值",
            "交易日期不是本次上游首次发布时间；历史来源窗口不等价于Shibor全部历史"]}
