"""Accept current incremental data on top of the CMES historical store.

Receiving HTTP data, passing an age screen or exhausting source pages does not
prove the source has the latest official publication. Current snapshots may be
retained at their real observation time without inventing an effective date.
Historical backfill is separate from current incremental capability.
"""


def incremental_requirements(item):
    """Describe the common target without claiming an untested source works."""
    return {"excel_row": item["excel_row"],
        "goal": "CMES历史底库 + 免费源当期增量，采到后持续留存",
        "historical_api_required": False, "full_history_required": False,
        "snapshot_or_short_window_allowed": True,
        "full_market_required_for_bounded_delivery": False,
        "current_data_checks": ["真实可采", "本范围所需字段", "证券或指标身份",
            "已明确数值与单位", "源原时点与真实接收时点", "落盘、查询与重复采集去重"],
        "unknown_fields": "原样保留并标注，不猜换算，不阻断其他已明确字段",
        "historical_bridge": "历史衔接空档单列补数，不否定当期增量能力",
        "definition_rule": "以CMES原条目定义为准；先核tick是否指快照，不预设交易所逐笔要求"}


def acceptance_work(item):
    """Select the smallest useful contract by function, never broad row ranges.

    Expansion and deployment remain separate from whether a verified bounded
    collection can be used. Unknown source fields may be retained with labels.
    """
    functions = {c["function"] for c in item.get("calls", [])}
    row = item["excel_row"]
    if functions == {"stock_zh_a_daily"}:
        group = "复权因子"
        action = "按已验证证券读取全部返回因子，检查前/后复权方向、基准哨兵及修订；扩证券目录单列"
        check = "证券、因子日期和无量纲数值不混用；重跑及按证券查询通过"
    elif functions & {"tool_trade_date_hist_sina", "sina_trade_calendar"}:
        group = "交易日历"
        action = "复用旧日历解码器，核对返回交易日期唯一、递增及实际覆盖终点；交易所未来安排另行补证"
        check = "来源日历可读取和更新，保留当前已覆盖年份；不从缺失日期猜停牌"
    elif functions & {"sina_raw_daily", "tencent_raw_daily"}:
        group = "指定证券日线窗口"
        action = "按明确股票和日期窗口更新不复权日线，按股票及交易日读取；量额保留源原值和单位说明"
        check = "证券和交易日期明确，OHLC有效，缺失字段不猜补，滚动窗口重叠去重并保留修订"
    elif functions & {"sina_native_minutes", "stock_zh_a_hist_min_em", "fund_etf_hist_min_em", "index_zh_a_hist_min_em", "bond_zh_hs_cov_min"}:
        group = "指定证券分钟窗口"
        action = "先复用已有分钟入口，对明确证券和周期取滚动窗口，保留原标签、量额及复权口径；逐源定位失败"
        check = "代码与市场明确，OHLC有效、原单位不猜换算，重复键和查询读回通过；未全市场不阻塞本范围"
    elif functions & {"stock_balance_sheet_by_report_em", "stock_profit_sheet_by_report_em", "stock_cash_flow_sheet_by_report_em", "stock_financial_analysis_indicator_em"}:
        group = "财报与指标"
        action = "复用旧财报采集/查询，对指定公司按报告期更新；公告日单列，已明确科目先可用，未知单位字段原样标注"
        check = "发行人、报表类型、报告期及修订不混淆；原值和必要单位可追溯，不要求所有行业字段适用"
    elif row == 28:
        group = "可转债发行"
        action = "保留普通可转债和30条特殊历史身份，按原债券类型分别映射；先交付已明确的发行记录"
        check = "发行代码、证券类型和发行日期明确，未归类记录不删除或冒充普通可转债"
    elif functions & {"stock_profile_cninfo", "stock_new_ipo_cninfo", "bond_zh_cov_info", "fund_etf_scale_sse", "fund_etf_only_szse"}:
        group = "证券资料"
        action = "对明确公司或源当前名单读取资料，按源字段保留名称、代码和实际日期；未给统计日或单位的字段单独标注"
        check = "公司/证券身份与资料字段对应；成立、上市、统计和接收日期不互相替代"
    elif functions & {"index_stock_cons_csindex", "index_component_sw", "sw_index_third_cons", "stock_board_concept_cons_em", "stock_board_industry_cons_em", "tdx_members"}:
        group = "成分名单"
        action = "复用同一指数或板块的当前名单入口，保留成分代码和实际采集日期；不能用另一分类替代"
        check = "名单在明确指数/板块范围可更新、重跑和查询；当前快照不冒充历史入选日"
    elif functions & {"sw_index_first_info", "sw_index_second_info", "sw_index_third_info", "tdx_blocks", "stock_board_concept_name_ths", "stock_board_industry_name_ths", "stock_zh_a_st_em"}:
        group = "证券及行业目录"
        action = "读取源当前目录，核对代码、分类层级和名称，保留快照及变化；无法证明的历史属性单列"
        check = "当前返回目录身份唯一并可读回；不以缺少历史目录判当前名单不可用"
    elif functions & {"futures_zh_minute_sina"}:
        group = "期货原生分钟"
        action = "保留明确合约/连续序列的原生时间标签、OHLC和数量；未证实夜盘交易日、单位或换月规则不猜填"
        check = "原生记录可读取并持续留存；需交易日或标准单位的用途另标限制，不阻塞原字段查询"
    elif functions & {"futures_main_sina", "futures_resample"}:
        group = "期货日周月"
        action = "更新已核品种的日周月窗口，保留连续系列来源和未结束周期；其他品种逐批扩展"
        check = "已覆盖品种可重跑和查询；供应商换月规则与CMES等价未证实则明确标注"
    elif any("fund_flow" in f for f in functions):
        group = "资金流"
        action = "只复用相同供应商资金流口径；保留个股/行业、统计周期和大单分组，不用成交额替代资金净流入"
        check = "已明确指标可查询更新，来源分组阈值或单位未知的列单独标注；传输失败不推断不支持"
    elif any(f.startswith("stock_info_global") for f in functions) or functions & {"news_cctv", "stock_zh_a_disclosure_report_cninfo"}:
        group = "新闻与公告"
        action = "读取明确日期/证券或当前滚动列表，保留标题、链接和原发布时间；同链接或公告ID重跑去重"
        check = "范围内原文条目和时间可追溯；列表入口不以尚未下载全部正文判不可用"
    elif functions & {"security_events_em", "stock_gdfx_top_10_em", "stock_gdfx_free_top_10_em", "stock_lhb_jgmmtj_em", "stock_lhb_stock_detail_em"}:
        group = "证券事件与股东"
        action = "按本条事件类型、证券和源日期保留记录；股东报告期与公告日分开，榜单原因和席位保留"
        check = "明确范围内事件可更新和查询；比例、股数和金额不混用，同日不同理由不误去重"
    elif functions & {"stock_zt_pool_em", "stock_zt_pool_dtgc_em", "stock_zt_pool_zbgc_em", "ths_hot", "stock_bid_ask_em"}:
        group = "榜单与实时快照"
        action = "复核源回显日期与请求日期，保存实际取得的榜单/盘口快照；不以今天响应冒充历史日期"
        check = "本范围身份和快照时点明确、数值可读回；未取得的历史快照单列"
    elif functions & {"stock_board_concept_index_ths", "stock_board_industry_index_ths"}:
        group = "同花顺指数日线窗口"
        action = "以已确认原生指数代码直接读取公开年度及当日行情文件，按实际交易日持续保存；目录独立核查"
        check = "指数代码、OHLC和日期有效，量额保持原值并说明尺度未转换；增量重叠去重及查询通过"
    elif any("hist" in f or "raw_daily" in f or "_daily" in f for f in functions) and item["category"] in {"股票数据", "涨停打板", "成分股板块"}:
        group = "行情历史窗口"
        action = "复用同标的旧日线/指数入口，明确未复权或复权基准；周月注明原生或聚合来源"
        check = "本范围行情可续取、去重与读回；价格、量额和交易日期不混用，不以未全历史阻塞"
    else:
        group = "来源原生指标窗口"
        action = f"读取“{item['item']}”实际返回的统计期和字段；已明确列先可用，缺列、未知单位和源停更列分别保留"
        check = "原条目范围内有效字段可增量留存、重跑及查询；按原发布频率判断更新，未知列不猜补"
    return {"excel_row": row, "group": group, "next_action": action, "pass_condition": check,
            "expansion_separate": True, "deployment_separate": True,
            "goal": incremental_requirements(item)["goal"]}


def user_progress(item):
    """A proven explicit scope is useful without whole-market/deployment gates."""
    decision = item.get("scope_decision", {})
    if decision.get("status") == "cancelled":
        return {"user_status": "用户取消", "user_reason": decision["reason"]}
    scoped = item.get("scoped_availability", {})
    if (scoped.get("verified") and scoped.get("scope") and scoped.get("entry")
            and scoped.get("evidence") and scoped.get("readback_verified")
            and scoped.get("rerun_verified")):
        return {"user_status": "可运行", "user_reason": scoped["scope"]}
    if decision.get("status") == "continue":
        return {"user_status": "继续实施", "user_reason": decision["reason"]}
    diagnosis = item.get("failure_diagnosis", {})
    if diagnosis.get("kind") in {"collector_transport", "cooldown_skipped", "implementation", "not_tested"}:
        return {"user_status": "待验证", "user_reason": diagnosis["reason"]}
    issue = item.get("source_issue_summary")
    if issue or item.get("status") == "受阻" or (item.get("source_lifecycle") or {}).get("status") == "discontinued":
        return {"user_status": "来源问题", "user_reason": issue or "来源访问、数据或定义有阻塞，详见逐项原因"}
    if (item.get("daily_staging_implementation_ready") and item.get("implementation_verified")
            and item.get("coverage_verified") and not item.get("acceptance_dimensions", {}).get("semantic_gaps")):
        return {"user_status": "可运行", "user_reason": "已有验证并固定的采集入口，可在表内范围手动运行；定时启用和正式发布另列"}
    return {"user_status": "待验证", "user_reason": "仍需补齐表内范围、日期/单位或运行验证；已有样本不代表全部完成"}


def acceptance_dimensions(item):
    runs = item.get("sample_runs", [])
    semantics = [r.get("evidence", {}).get("semantics", {}) for r in runs]
    frequencies = sorted({s["frequency"] for s in semantics if s.get("frequency")})
    periods = sorted({s["latest_period"] for s in semantics if s.get("latest_period")})
    received = sorted({r["finished_at"] for r in runs if r.get("finished_at") and r.get("status") in {"received", "empty_source_window"} and r.get("evidence", {}).get("receipts")})
    gaps = sorted({g for s in semantics for g in s.get("gaps", [])})
    field_periods = [{"run_id": r["run_id"], "latest_nonnull_periods": s["field_latest_nonnull_periods"]}
                     for r, s in zip(runs, semantics) if s.get("field_latest_nonnull_periods")]
    known_gap = (item.get("source_selection") or {}).get("semantic_gap")
    if known_gap:
        gaps.append(known_gap)
    stopped = (item.get("source_lifecycle") or {}).get("status") == "discontinued"
    technical = bool(item.get("implementation_verified"))
    functions = {c["function"] for c in item.get("calls", [])}
    if stopped:
        state, label = "discontinued", "源指标已停发，无法每日续更"
        required = "保留原指标和停发证据，不以其他指标替代"
    elif gaps:
        state, label = "definition_or_field_gap", "指标或字段覆盖未完整，新鲜度未验"
        required = "先补原定义、必要字段和各字段最新期，再核对官方发布"
    elif functions & {"energy_oil_hist", "energy_oil_detail_latest"}:
        state = "official_adjustment_not_compared"
        label = "实现测试通过但新鲜度未验" if technical else "调价窗口新鲜度未验"
        required = "按官方调价事件核对最近源调整日；当前窗口完整不证明官方事件齐全"
    elif functions == {"stock_zh_a_daily"}:
        state, label = "adjustment_events_not_compared", "因子按除权除息等事件变更；最近调整事件待核"
        required = "核对前/后复权方向、锚定基准和源修订；不用财报发布日要求验收因子"
    elif item.get("mode") == "calendar":
        state, label = "published_horizon_not_compared", "交易日历最新官方安排未核全"
        required = "核对当前年份交易所安排及修订；未来开市日期不作新鲜度证据"
    elif item.get("mode") == "snapshot" or any("snapshot" in f for f in frequencies):
        state, label = "observed_current_snapshot", "当期快照按实际采集时点留存"
        required = "核对本范围身份与已明确字段；源未给生效日时保留空值，以真实接收时点记录观察，不要求历史查询"
    elif item.get("mode") in {"event", "financial", "policy_event"} or any(
            "corporate_action" in f or "publication" in f for f in frequencies):
        state, label = "latest_release_not_compared", "按事件或财报发布更新；最新发布待核"
        required = "按发行人/指标核对最新公告；报告期和上市日不替代发布时间"
    else:
        state, label = "latest_release_not_compared", "最新正式发布未核实"
        required = "按原发布频率和各字段最新非空期核对，不使用统一天数阈值判最新"
    return {
        "technical_sample_verified": technical,
        "source_window_verified": bool(item.get("coverage_verified")),
        "publication_frequency": frequencies or ["not_yet_reviewed"],
        "source_latest_periods": periods, "field_latest_nonnull_periods": field_periods,
        "last_live_received_at": received[-1] if received else None,
        "freshness_state": state, "freshness_label": label,
        "latest_official_publication_verified": False,
        "freshness_verified": False, "freshness_requires": required,
        "semantic_gaps": sorted(set(gaps)), "daily_data_ready": False,
        "policy": "technical tests, source window, publication freshness, schedule enablement and formal publication are separate; no broad age threshold is acceptance proof",
    }


def validate_ledger(ledger):
    """Reject drift in the original Excel identities or contradictory totals."""
    from collections import Counter
    from ashare_data.providers.free_updates import catalog
    original = catalog()
    fields = ('sheet', 'excel_row', 'category', 'item', 'candidate_status', 'candidate_source', 'note', 'source_url')
    expected = {i['excel_row']: i for i in original['items']}
    items = ledger['items']
    if len(items) != 198 or {i['excel_row'] for i in items} != set(expected) or ledger['workbook_sha256'] != original['workbook_sha256']:
        raise ValueError('acceptance ledger does not match the frozen 198-row workbook')
    for item in items:
        if any(item.get(k) != expected[item['excel_row']].get(k) for k in fields):
            raise ValueError('original workbook fields changed at row '+str(item['excel_row']))
        if item['coverage_verified'] and not item['implementation_verified']:
            raise ValueError('source coverage cannot precede implementation verification')
        if item['implementation_verified'] and not all(item.get('evidence_binding', {}).get(k) for k in ['version','implementation_sha256','observation_root','core_proof']):
            raise ValueError('verified item lacks version-bound source evidence')
    counts = {'completed': sum(i['implementation_verified'] for i in items),
        'coverage_verified_count': sum(i['coverage_verified'] for i in items),
        'status_counts': dict(Counter(i['status'] for i in items)),
        'daily_deployed': sum(bool(i.get('daily_deployment')) for i in items),
        'formal_published': sum(bool(i.get('formal_published')) for i in items)}
    if any(ledger.get(k) != v for k, v in counts.items()):
        raise ValueError('acceptance headline disagrees with row-level evidence')
    return counts


def export_local_ledger(ledger_path, output, *, workbook=None):
    """Single local entry point; immutable historical ledgers are never rewritten.

    Publishing latest.json is the final local step, after all payloads are saved.
    No source requests, scheduling, formal data root or upload are involved.
    """
    import csv
    import hashlib
    import io
    import json
    from pathlib import Path
    from ashare_data.providers.free_updates import encoded
    ledger_path, output = Path(ledger_path).resolve(), Path(output).resolve()
    body = ledger_path.read_bytes()
    ledger = json.loads(body)
    counts = validate_ledger(ledger)
    if any((output/n).exists() for n in ['CURRENT', 'CURRENT.json', 'releases', 'observations.sqlite']):
        raise ValueError('acceptance output cannot be a data store')
    output.mkdir(parents=True, exist_ok=True)
    def save(name, content):
        temporary = output/(name+'.next')
        temporary.write_bytes(content)
        temporary.replace(output/name)
    fields = ['excel_row','category','item','candidate_status','candidate_source','note','source_url','user_status','user_reason',
        'status','implementation_verified','coverage_verified','daily_deployment','formal_published',
        'scope_clarification','acceptance_dimensions','remaining_acceptance','evidence_binding','coverage_evidence','scope_decision']
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    for item in ledger['items']:
        writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for k, v in item.items()})
    save('acceptance.json', body)
    save('CMES-198项验收.csv', stream.getvalue().encode('utf-8-sig'))
    from collections import Counter
    user_counts = dict(Counter(user_progress(i)['user_status'] for i in ledger['items']))
    description = ('CMES 198项验收：唯一最新入口\n\n'
        f"生成时间：{ledger['generated_at']}\n"
        f"可运行（已验证入口/范围）：{user_counts.get('可运行', 0)}；待验证：{user_counts.get('待验证', 0)}；来源问题：{user_counts.get('来源问题', 0)}。\n"
        + (f"用户取消后续开发：{user_counts['用户取消']}项，保留原行及历史证据，不再研究、实现或重试。\n" if user_counts.get('用户取消') else '')
        + (f"继续实施：{user_counts['继续实施']}项。\n" if user_counts.get('继续实施') else '')
        + f"技术核心样本：{counts['completed']}/198；来源窗口：{counts['coverage_verified_count']}/198。\n"
        f"日常启用：{counts['daily_deployed']}；正式发布：{counts['formal_published']}。\n\n"
        '目标是CMES历史底库加免费源当期增量；当前快照和短窗口可以持续留存，历史衔接空档单列补数。\n'
        '可运行指表内明确范围已有实际入口；日常启用和正式发布另列。\n'
        '技术样本、来源可见窗口、最新正式发布、日常启用和正式发布分别验收。\n'
        'CSV/JSON保留原表字段、逐行范围和阻塞。历史证据按原实现版本保留。\n'
        + (f"已启用{counts['daily_deployed']}项隔离日常采集；正式发布状态另列。\n" if counts['daily_deployed']
         else '隔离日常采集尚未启用；权限或部署条件见逐项状态。\n')
        + f'本次历史台账：{ledger_path}\n')
    save('README.md', description.encode())
    payloads = [output/n for n in ['acceptance.json','CMES-198项验收.csv','README.md']]
    if workbook is not None:
        workbook = Path(workbook).resolve()
        if not workbook.is_file():
            raise ValueError('reviewed workbook is not present')
        payloads.append(workbook)
    manifest = {'schema':'ad.local-acceptance-entry.v1', 'generated_at':ledger['generated_at'],
        'source_ledger':str(ledger_path), 'source_ledger_sha256':hashlib.sha256(body).hexdigest(),
        'scope_total':198, **counts, 'user_status_counts':user_counts, 'local_only':True,
        'files':[{'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in payloads]}
    save('latest.json', encoded(manifest))
    return manifest
