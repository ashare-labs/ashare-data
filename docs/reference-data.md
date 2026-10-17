# 日状态、前收、日历与上市事实

本页按所需参考字段选择公共接口。每份结果来自明确固定的 capture 或证据包；“源值已知”不等于历史资格、最终版、可交易或执行许可。采集依赖及原始回执见 [提供者](providers.md)。

## 停牌与-st

`BaoStockSource.fetch(store,kind="daily_status",security=...,start_date=...,end_date=...)` 显式采集一证券、≤31自然日，仅请求 date/code/tradestatus/isST/adjustflag，日线原价标记3，不重采 OHLCV。`source.get_status(...,require_known=False)` 合并采集和读取；固定离线读取为 `Store.baostock(capture_id).get_status(require_known=False)`。

DailyStatusResult.rows 是 frozen DailyStatusRow 元组。suspended/is_st 分别为 DailyStatusFlag，保留 source_field/raw_value/value/state。按 [BaoStock 原生定义](https://www.baostock.com/mainContent?file=stockKData.md)，tradestatus=0/1→停牌 True/False，isST=1/0→True/False；不从量价、缺行或当前名称推断。

| state | 含义 |
|---|---|
| source_observed | 来源支持的0/1；合成性仍看 evidence_kind/quality |
| field_not_requested | 旧 capture 没请求该列 |
| row_missing_unknown | 请求自然日无行，不推断休市/退市/停牌 |
| source_empty | 该字段原串为空，原串保留 |
| unsupported_enum | 非支持枚举，value=None |

缺列/错列宽由协议校验拒绝，不填0。require_known=True 任一天任字段未知报 DAILY_STATUS_UNKNOWN；成功仍不表示交易准入。请求起止包含端点、输出自然日网格；基本资料/minute不能冒充日状态，新 status capture也不能get_price。旧daily可投影tradestatus，未请求isST明确未知。

每行保留 capture/原记录/响应 hash、source_fields 副本、raw_json、同页末字节 response_received_at。策略 daily-source-status-2 核对时钟与 wall_clock_events，一致性错误 DAILY_STATUS_CLOCK_INVALID；记录的时钟失败保持 None。它不是历史发布时间或已认证响应完成；压缩外层未知时 received 仍拒绝，verified 报 PIT_UNAVAILABLE。historical_available_at/eligible=None，intraday_halts_verified/execution_permission=False。

CLI：baostock-fetch daily_status、baostock-query statuses --capture ID [--require-known]；示例 `examples/daily_status.py`。

## 源前收

`fetch(kind="daily_preclose",...)` 请求 date/code/preclose/adjustflag，日线标记3、单证券≤31自然日。采集读取 `source.get_preclose(...)`，离线 `view.get_preclose(require_known=False)`；SourcePrecloseResult.rows 为 frozen SourcePrecloseRow，preclose 为保留原串的 Decimal 值，不使用浮点/舍入。

state 区分 source_observed、source_zero、field_not_requested、row_missing_unknown、source_empty、invalid_numeric。零是已知 Decimal(0)，不是缺失；负数、指数、NaN/Infinity、空白等不接受，支持≤64字符的非负十进制定点串。require_known=True 只拒绝未知（SOURCE_PRECLOSE_UNKNOWN），不保证值适合定限价。

price_basis=`source_reference_adjustflag_3`，单位 CNY/share。来源明确在除权除息时可调整 preclose，所以它**不是前一日实际 raw close**。不计算限价、复权因子或事件完整性，也不把字段与前行close对齐/回填。保留 request/source_adjustflag、definition_version、来源字段/证据及完整 hash。

投影 source-preclose-1、capture baostock-source-preclose-1/receipt-4.1 与旧 capture 分开；未请求该字段返回 field_not_requested，不自动补采。时钟绑定错误 SOURCE_PRECLOSE_CLOCK_INVALID；historical_available_at/reference_price_eligible=None，PIT/events_complete/execution_permission=False。CLI baostock-fetch daily_preclose、baostock-query preclose；示例 `examples/preclose.py`。

## 源日历

`fetch(kind="calendar",start_date=...,end_date=...)` 无证券参数、≤31自然日。`Store.baostock(capture_id).get_calendar(require_known=False)` 返回完整请求网格的 SourceCalendarResult；原 get_trade_days DataFrame/过滤不变。

SourceCalendarFlag 保存 is_trading_day 原串：1/0→True/False，成功空响应每一天 row_missing_unknown，不填 False。缺自然日/错枚举等非空畸形响应按 SOURCE_SCHEMA_ERROR 拒绝；require_known=True 对未知报 SOURCE_CALENDAR_UNKNOWN。

`get_calendar_links(trading_dates=[...])` 接受1～31个唯一 date/规范日期串，按输入顺序返回。每个锚点必须是同一 capture 内已知开市日；previous_open/next_open 必须严格早/晚，所跨全部自然日有据关闭。越出捕获范围、没有邻日、未知链或闭市锚点分别报 OUT_OF_SCOPE、BOUNDARY_UNKNOWN、UNKNOWN、NOT_OPEN（SOURCE_CALENDAR_ 前缀）；不拼 capture、不凭周末推算或退回当天。

links.evidence_rows 保留完整自然日链；日期/timezone错误、同页接收与事件不一致拒绝。exchange_scope=provider_calendar_unspecified_exchange，没有认证各交易所个股资格、session、历史可见或最终性。批量任一失败整体拒绝。CLI baostock-query calendar [--require-known]、calendar-links --dates；后者不接受 require-known。示例 `examples/source_calendar.py`。

## 上市事实

`Store.import_listing_evidence(directory)`→`listing_fact(snapshot_id)` 只接纳以下固定审阅包，不是通用公告解析器。每次读取核验全部原件，latest/损坏/未发布拒绝。

| 输入包 SHA256 | 字段 |
|---|---|
| `08eb0578e608ed92d2ae8df9c2b7f72092016bdd63d13eaab26162da53d9f9e5` | 旧年精度 initial_listing_year，原 owner_version/snapshot 不变 |
| `f046562fc6db577ce0c201872655c0e91c7984bbb981c27bf75d4ece17685099` | initial_listing_date=2018-06-11，派生 year=2018 |

两包仅300750.XSHE，消费日期仅2026-09-28/29/30；查询日期不是事件日，也不证明这三日持续上市/可交易。`view.get(security,on_date)` 默认年份；field='initial_listing_date' 返回 FactRecord，旧包报 LISTING_FACT_UNAVAILABLE；年份返回 ListingYearFact，新包明确 derived_from_initial_listing_date。descriptor/lineage/evidence/validate 只证明对应年/日事实，require_eligible 始终 LISTING_ELIGIBILITY_UNKNOWN。

事件日期、文件发布日期、实际收件时间分别保留。新包文件元数据为2018-08-24 17:12:17但来源未标时区，保持 None；必需证据接收完成为2026-10-10T04:43:49.030165+00:00，不倒填为2018历史可见。默认 posthoc 不接隐式 knowledge_at；received 要求 aware knowledge_at≥接收完成，verified 始终 PIT_UNAVAILABLE。返回副本与原 PDF/元数据定位可追溯，时间仍不证明当时系统采用该事实。

listing_fact_snapshots/recover_listing_facts 显式列出/恢复，新旧版本共存；CLI 与最小例见 `examples/listing_fact.py`。

## 固定日历

`BoundedCalendarView(binding_path,expected_binding_sha256=...)` 读取一个已覆盖月份内、指定深市证券和限定窗口的 ad.bounded-calendar-binding.v1。`read_carry(window,security,role=...)` / `read_calendar(window,security,date,role=...)` 只接 engine_calendar_retrospective_research。

主源为深交所按月完整 jybz 自然日标志，可用独立新浪开市日交叉核对；重复、缺日、未知枚举、冲突拒绝，不从工作日或期望回调制造日历。开/闭日都返回严格前后最近开市日，外端点可超读取窗但须在同月原文内，缺证据不返回当前日。每次重验绑定/原件/hash；synthetic_fixture 单独标记。historical_pit/strategy_visible=False，不说明账户、价格、行动或结算资格。

## 盘前参考

`PremarketReferenceView.read(security,as_of=...,role="premarket_reference_reconstruction")` 只读取固定新浪 KLC 单独编码 prevclose。输入 ad.premarket-reference-binding.v1 和预期 SHA，政策 sina_native_prevclose_known_cash_research.v1。每次重验原文、decoder、公告PDF、事实与日历，要求指定相邻登记/除息日、此前公告和无送转现金事件。

源字段须按0.01元表示，与原始前收减毛分红差不足一个源价格单位；这是冲突检查，不用于生成价格或指定舍入。缺字段拒绝，无公式回退。输出仅参考价、raw前收、毛分红及日期，不读取当日OHLCV/后续累计因子。model_available_at 固定为绑定除息日09:00；不是任意 as_of，historical_available_at=None、PIT/策略/撮合/当前报价权限=False。

需要逐证券、日期、用途检查这些字段是否齐全时使用 [研究输入预检](execution-inputs.md#输入预检)；历史市场状态和股票池使用 [历史市场](historical-market.md)，不要互相代替。
