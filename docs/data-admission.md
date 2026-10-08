# 数据准入契约 v1

0.3.0.dev1是本地修复候选，未发布。A数达负责采集、缺口检测及补齐能力、单位/量额、复权/状态资产、可见性模型与保证检查。上层只传策略逻辑时钟和所需保证，消费结果或明确错误，不再另建清洗、填价或猜测停牌逻辑。

## 接口

| 接口 | 行为及边界 |
|---|---|
| `Client.get_price(...)` | 保留 DataFrame、字段、代码、单位和 `end_date` 标签过滤；新增每股 freshness、coverage_report、visibility |
| `Client(coverage_contract=CoverageContract(data))` | 装载由数据层维护的版本化来源标签、日历、session和证券状态事实；没有自动适用的真实新浪契约 |
| `client.coverage(security,start_date,end_date,frequency=...)` | 检查头部、内部、尾部、整日、异常标签和未知事实。缺契约无网络即返回unknown，有契约时读取一个有界源窗口 |
| `get_price(...,require_complete=True)` | 检查**契约声明的标签槽完整性**；未知报COVERAGE_UNKNOWN，有缺口报COVERAGE_INCOMPLETE。不等价于全部真实成交完整 |
| `get_price(...,require_fresh=True)` | 根据日历/session/状态及逻辑时钟核对最新闭合标签；报STALE_SOURCE或FRESHNESS_UNKNOWN |
| `get_price(...,require_final=True)` | 当前源没有最终发布/修订水位，报BAR_NOT_FINAL；15:05不是最终值证明 |
| `client.at(as_of,visibility=...,require_complete=False,require_fresh=False,require_final=False)` | 固定本地观测版本并返回离线上下文，查询不联网；缺缓存报CACHE_REQUIRED/CACHE_MISS。亦支持get_price的as_of/visibility关键字 |
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

无as_of的直接取数仍是unrestricted_research。`end_date='2026-09-30 14:55'`原来可选当日00:00标签的日线，这一标签语义不变。增加独立时钟并显式选assumed后，当天完整日线被排除，但仍可返回前一符合模型的交易日，并非全部历史都空返回。

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

上下文有query_snapshot_id，创建后刷新不改变已固定版本。received可从不可变观测日志选择旧版本；旧缓存没有的历史不会被伪造。原文对象不能覆盖，每次观测存hash及supersedes_observation，当前请求索引只是一条指针。上下文可复用已固定的同一endpoint/证券/频率不同datalen窗口，标cache_window_reused，仍核对实际窗口及条数；不联网或换源。

上下文的当前证券名称及年度网页日历接口缺历史证明，会明确拒绝，防止从另一个入口读取未来事实。原Store固定snapshot保留as_of、quality及bar_end门禁；导入者的声明也不等于供应商历史真值认证。

## 覆盖事实 schema

CoverageContract v1顶层字段：schema_version=1、id、provider、frequency（daily/1m/5m）、trading_scope、evidence_kind（synthetic/observed）、evidence、label_semantics（verified/unverified）、calendar、statuses。

每个calendar日明确date、is_open、evidence、aware available_at、sessions。开市须有session，休市不得有session。每个session有id、aware start/end；mapping=end_labels按声明周期生成应有标签，mapping=explicit逐个声明slots中的label/start/end且须无缝覆盖该段。日线用声明时段的起止范围，标签仍为日期。不会默认每天240根，不从238根推断缺14:58/14:59。

statuses是有证据的整日证券状态，含security、date、state（trading/suspended/unknown）、evidence、available_at。缺记录或晚于as_of即unknown；停牌有证据才移除对应应有槽。本版尚不接收盘中停复牌序列，这类日应标unknown，不能以盘中某时“可交易”代替全天证明。

构造器检查格式、重叠、映射缺口、重复标签和重复事实；复制输入，contract_id为内容hash。它验证声明的一致性，**不认证引用证据的真实性**。合成契约覆盖通过仍标synthetic，不能据此对真实源声称完整。complete/grid_complete只说明声明标签槽覆盖，trade_totals_verified保持False，量额完整另需验收。

没有内置真实新浪规则库：标签到区间、竞价/盘后统计范围、历史证券状态欠证据。缺证据就unknown，不凭当前名称、日历开市或缺bar推断正常交易。真实契约采集、验证及版本维护仍是A数达职责。

## 新鲜度、量额及错误

freshness分开记录last_source_label、last_returned_label、两者相对逻辑时钟的秒数、response_observed_at和http_observation_age_seconds。日线标签年龄从00:00计算，明确不是发布年龄。新HTTP返回8天前源日不会再与“数据新鲜”混为一谈；缺事实时status=unknown，有证据才比对最新闭合槽。fresh只说明达到标签水位，不保证零延时或最终版。

DataFrame仍使用原float列保持兼容；原十进制文本保留在source_rows，对账直接用Decimal，不对浮点列求和。live及offline共用正数、有限、小于1e17和OHLC关系门禁；离线原6位精度要求未放宽。非法行情不缓存。

同源分钟与日累计仍差52300股及约507179元，不得通过放宽舍入容差、扣除含义未证尾字段或造两根分钟改成通过。CLI的live-coverage/acquire-price/reconcile-day在所请求验收未满足时退出2；可保留原始研究记录，但不能当合格资产。获取任意历史、状态/复权与完整成交验收的剩余阻塞见[data-attribution.md](data-attribution.md)。

CLI新增live-coverage、acquire-price、reconcile-day；price新增--as-of、--visibility、--coverage-contract、--require-complete、--require-fresh、--require-final。策略只传时钟与保证，A数达实施检查；显式采集与固定观测查询分离。
