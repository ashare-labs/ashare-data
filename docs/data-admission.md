# 数据准入契约 v1

0.3.0.dev3是本地修复候选，未发布。A数达负责采集、缺口检测及补齐能力、单位/量额、复权/状态资产、可见性模型与保证检查。上层只传策略逻辑时钟和所需保证，消费结果或明确错误，不再另建清洗、填价或猜测停牌逻辑。

## 接口

| 接口 | 行为及边界 |
|---|---|
| `Client.get_price(...)` | 保留 DataFrame、字段、代码、单位和 `end_date` 标签过滤；新增每股 freshness、coverage_report、visibility |
| `Client(coverage_contract=CoverageContract(data))` | 装载由数据层维护的版本化来源标签、日历、session和证券状态事实；没有自动适用的真实新浪契约 |
| `client.coverage(security,start_date,end_date,frequency=...)` | 检查头部、内部、尾部、整日、异常标签和未知事实。缺契约无网络即返回unknown，有契约时读取一个有界源窗口 |
| `get_price(...,require_complete=True)` | 检查**契约声明的标签槽完整性**；未知报COVERAGE_UNKNOWN，有缺口报COVERAGE_INCOMPLETE。不等价于全部真实成交完整 |
| `get_price(...,require_fresh=True)` | 源水位、所选窗口水位和当前证券/时段均须满足；旧窗口、休市、午休、停牌、未知事实分别明确拒绝 |
| `client.trading_status(security,as_of=...)` | 纯本地事实决策，返回state/tradable/证据；固定上下文自动用固定时钟，不可覆盖 |
| `get_price(...,require_tradable=True)` | 仅交易状态门禁，不等同数据新鲜/完整；不满足时在读行情前拒绝 |
| `get_price(...,require_final=True)` | 当前源没有最终发布/修订水位，报BAR_NOT_FINAL；15:05不是最终值证明 |
| `client.at(as_of,visibility=...,require_complete=False,require_fresh=False,require_final=False,require_tradable=False)` | 固定本地观测版本并返回离线上下文，查询不联网；缺缓存报CACHE_REQUIRED/CACHE_MISS。亦支持get_price的as_of/visibility关键字 |
| `client.acquire_price(security,start_date,end_date,frequency=...)` | 显式刷新一个近期源窗口，返回行、证据和requirements_met。无任意历史分页、不循环换源、不造行；固定上下文/only模式禁止调用 |
| `client.reconcile_day(security,trade_date)` | 对单股做1m、5m和当前quote诊断，输出原文hash及精确Decimal差异；当前quote不能当其他日期参考 |
| `reconcile_turnover(rows,reference,...)` | 股/元明确、同日期、整股，精确量额对账；缺amount不按零或close×volume替代。相等但范围/覆盖未知仍不能验收 |

原strict仍只检查已返回标签之间的间隔，避免悄悄改变既有窄契约。原始价、公司行动与因子保持分离；复权、ST/限价、生命周期、完整历史股票池依然没有合格资产，原拒绝接口保持。这些缺口仍由A数达补齐，不转给上层造数据。

## 可见性等级

| visibility | 保证与限制 |
|---|---|
| assumed（必须显式选择） | 按声明模型过滤未结束记录，历史回填可作非空研究查询。有可见契约用声明bar_end；缺映射时分钟假设源标签为结束时间，日线保守等待日期结束。不证明公布时间、延时或未修订历史；available_at=None、point_in_time_verified=False |
| received | 从本机观测日志选择策略时钟前收到的响应版本，仍执行闭合模型。available_at仅表示此版本本机收到时间，不代表供应商历史首次公布或最终值；point_in_time_verified=False |
| verified（有as_of时默认） | 请求证据级历史公布及版本保证；新浪缺此证据，报VISIBILITY_UNKNOWN。不会因为做过时间过滤就改标PIT |

无as_of的直接取数仍是unrestricted_research，日线继续使用15:05软件阈值。每股completion_model说明此阈值，closure_verified=False；即使声明日线范围延续至15:30，15:06的旧研究入口也可能返回当日标签，不能称其已按声明bar_end闭合。`end_date='2026-09-30 14:55'`原来可选当日00:00标签的日线，这一标签语义不变。增加独立时钟并显式选assumed后，当天完整日线被排除，但仍可返回前一符合模型的交易日，并非全部历史都空返回。

缺日线统计范围时，假设上下文保守到该日23:59:59.999999，而非用15:05宣称全日已经结束。阈值是模型，统一按北京时间，不证明最终版。分钟结束标签尚未实测，假设档不会伪造区间或公布时间。finality=CLOSED_PROVISIONAL不等于FINALIZED。

```python
from ashare_data import Client
collector = Client(cache=".data/research")
receipt = collector.acquire_price("600000.XSHG", "2026-09-30", "2026-10-08", frequency="1m")
# requirements_met可为False；取数成功不等于完整真实资产验收通过。
research = collector.at("2026-09-30T14:55:00+08:00", visibility="assumed")
bars = research.get_price("600000.XSHG", frequency="1m", count=3)
assert not bars.attrs["point_in_time_verified"]
```

上下文有query_snapshot_id，创建时固定请求索引与不可变观测日志；随后刷新不会改变该上下文。received先按observed_at <= as_of过滤，assumed可使用创建时已有的历史回填但不声称历史公布时间。每份响应先独立执行抓取时刻和策略时钟的闭合/可见性过滤，再对同一endpoint、证券、频率的实际标签取最新合格观测。精确URL和大窗口都没有优先权。相交、嵌套或不相交窗口均可提供各自实有记录；较新小窗口不会被旧大窗口遮蔽，较旧窗口仍可提供较新窗口未含的历史标签。窗口缺行不构成删除或修订撤回证明，不造bar、不换源、不联网。已有请求索引只是当前指针，旧观测与原文对象保持不变。

同一时刻同一标签出现不同值，没有可证明先后的版本；请求选中该标签时报OBSERVATION_CONFLICT。严格较新的合格观测可以解除该标签的冲突。一个查询最多读取128份同流观测、合计100000条原始行；固定上下文最多10000个索引/日志文件，超限报BOUNDED_QUERY。只有没有任何received合格窗口时才保留晚到版本用于解释VISIBILITY_UNKNOWN，不返回该版本作为历史行情。

逐行来源见provenance中的row_observations（source_label、sha256、observed_at、url、observation_id和completion_cutoff），所贡献的原响应摘要见response_observations。仅一份响应贡献结果时保留单一sha256/url；多响应组合时单一sha256/url/observation_id/request_started_at/completion_cutoff均为None，completion_basis为per_observation。汇总observed_at仅为最新贡献观测时刻，不能替代逐行溯源；visibility.raw_hashes列出贡献原文，raw_hash为None。这是固定本地观测的组合视图，不是供应商原子快照或PIT/最终版本认证。每份旧响应的未闭合记录仍不会随时间自动成熟。

上下文的当前证券名称及年度网页日历接口缺历史证明，会明确拒绝，防止从另一个入口读取未来事实。原Store固定snapshot保留as_of、quality及bar_end门禁；导入者的声明也不等于供应商历史真值认证。

## 覆盖事实 schema

CoverageContract顶层字段：schema_version=1或2、id、provider、frequency（daily/1m/5m）、trading_scope、evidence_kind（synthetic/observed）、evidence、label_semantics（verified/unverified）、calendar、statuses。

每个calendar日明确date、is_open、evidence、aware available_at、sessions。开市须有session，休市不得有session。每个session有id、aware start/end；mapping=end_labels按声明周期生成应有标签，mapping=explicit逐个声明slots中的label/start/end且须无缝覆盖该段。日线用声明时段的起止范围，标签仍为日期。不会默认每天240根，不从238根推断缺14:58/14:59。

v1的statuses仍表示有证据的整日证券状态，含security、date、state（trading/suspended/unknown）、evidence、available_at。v2改为security、effective_start、effective_end、state、evidence、available_at；生效区间为左闭右开，可跨日但最多366天，必须带时区，不得重叠或混用date。修订须建立新契约。缺记录、区间空洞或晚于as_of的事实为unknown；盘中停复牌不再铺成全天事实。

一分钟/五分钟槽若跨越状态切换且未明确映射为完整区间，报PARTIAL_BAR_STATUS，不能整根删除或造半根；已证全槽停牌才去除期望。日线可跨已知停牌，但其所有声明活跃session的状态须完整已知。源最新水位从最近闭合槽向前检查，不要求与最近已知复牌时段无关的旧历史事实齐备；全区间coverage仍须逐槽证明。

`require_complete`的count=N按所需尾部范围取证：从min(end_date, as_of)对应的最近闭合槽向前逐槽检查，直到取得N个有据可交易槽。只有有据休市/全槽停牌可跳过；遇到所需范围内未知日历、状态空洞、未来才可见的事实或跨状态半根bar即unknown，不越过它们寻找更早记录。找到N槽后，更早且与这N槽无关的未知事实不再阻断。warm-up需要更多条时应请求更大的N，新增所需范围仍必须有证据；下限耗尽仍不足则拒绝。报告含requested_count和count_evidence_start（最早所需槽起点）。

显式start_date/end_date仍检查整个闭区间标签范围，不采用尾部缩限；start_date与count互斥。Client无固定上下文时也按本次逻辑时钟限制日历/状态available_at，未来事实不能给当前查询背书。count只决定所需范围，不能通过减少范围掩盖该范围内缺失的最新bar或内部缺口。

交易时段也使用左闭右开：开盘边界可交易，收盘边界不再可交易；边界bar可以已经闭合。这是本库明确的paper模型，不代表经纪商接单规则。合成时段、盘后范围或订单类型不能自动升级成真实市场规则。

构造器检查格式、重叠、映射缺口、重复标签和重复事实；复制输入，contract_id为内容hash。它验证声明的一致性，**不认证引用证据的真实性**。合成契约覆盖通过仍标synthetic，不能据此对真实源声称完整。complete/grid_complete只说明声明标签槽覆盖，trade_totals_verified保持False，量额完整另需验收。

没有内置真实新浪规则库：标签到区间、竞价/盘后统计范围、历史证券状态欠证据。缺证据就unknown，不凭当前名称、日历开市或缺bar推断正常交易。真实契约采集、验证及版本维护仍是A数达职责。

## 新鲜度、量额及错误

freshness分开记录last_source_label、last_returned_label、两者相对逻辑时钟的秒数、response_observed_at和http_observation_age_seconds。日线标签年龄从00:00计算，明确不是发布年龄。新HTTP返回8天前源日不会再与“数据新鲜”混为一谈；缺事实时明确unknown。有证据才比对最新闭合槽，仍不保证零延时或最终版。

从dev2开始分别返回：source_watermark_status（源已选可见记录水位）、selected_window_status（最终返回子集水位）、trading_status（当前事实决策），以及组合admissible。CoverageContract.freshness没有查询子集，故组合源水位与交易状态；Client再加入所选窗口。status保留stale优先诊断；水位已达但市场关闭/午休/停牌则分别为closed_market/session_break/suspended，admissible=False。只有未来标签时为unknown并给FUTURE_SOURCE_LABEL。

require_fresh在此开发候选中加强为上述组合准入，不再只是标签水位检查：STALE_SOURCE、STALE_SELECTED_WINDOW、MARKET_CLOSED、SESSION_BREAK、SUSPENDED或FRESHNESS_UNKNOWN。范围外新bar不会给返回的旧窗口背书。历史研究不带保证仍可返回；需要检验当时准入应使用自身as_of及明确visibility模型。require_tradable只检查状态，不能替代require_fresh、require_complete、require_final。

DataFrame仍使用原float列保持兼容；原十进制文本保留在source_rows，对账直接用Decimal，不对浮点列求和。live及offline共用正数、有限、小于1e17和OHLC关系门禁；离线原6位精度要求未放宽。公开价格还必须在float64正常正数范围内；小于sys.float_info.min的次正规/下溢输入明确拒绝，不舍入为零或补值，原十进制文本继续保留。非法行情不缓存。

同源分钟与日累计仍差52300股及约507179元，不得通过放宽舍入容差、扣除含义未证尾字段或造两根分钟改成通过。CLI的live-coverage/acquire-price/reconcile-day在所请求验收未满足时退出2；可保留原始研究记录，但不能当合格资产。获取任意历史、状态/复权与完整成交验收的剩余阻塞见[data-attribution.md](data-attribution.md)。

CLI提供trading-status、live-coverage、acquire-price、reconcile-day；price支持--as-of、--visibility、--coverage-contract、--require-complete、--require-fresh、--require-final、--require-tradable。trading-status在tradable时退出0，其他状态退出2，均不联网。策略只传时钟与保证，A数达实施检查；显式采集与固定观测查询分离。

覆盖assess检查请求的闭区间标签子集；范围外标签另列out_of_request_labels，不按请求内缺陷报错。已证休市/停牌的空集合仍可complete=True，但status分别为closed_market/suspended、tradable=False；该字段说明请求范围有无可交易槽，当前paper状态应读trading_status。C14/C15原冻结结果保留；独立裁决新增5项通过，见[第三轮修复与版本化裁决](review3-fixes.md)。
