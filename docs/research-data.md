# 保存研究数据与读取独立归档

## 固定研究快照

公开接口与固定版本读取契约如下。

| 入口 | 精确形态及返回 |
|---|---|
| 显式新浪拉取并封存 | `store.fetch_price(security, start_date=None, end_date=None, frequency="daily", count=None, timeout=15)` → dataset_id；一只或最多10只显式股票，沿用现有Sina Client，每股一个有界请求，不分页/换源/补数 |
| 保存已有新浪观测 | `client.save_research(store)` → dataset_id；无网络；固定本Client已有观测集合，原文与观测日志进入独立对象引用，不要求A宽读缓存 |
| 导入本地Bao日线 | `store.import_research(paths, *, format="baostock_daily", calendar_path=None)` → dataset_id；只导入success、daily、flag3响应，校验请求/行证券与日期范围；全部保持未知历史接收，不采用新SDK采集器 |
| 公开离线重开 | `store.research(dataset_id)` → ResearchView；校验manifest及所有对象hash/兼容版本，缺失/篡改拒绝，不下载 |
| 描述符 | `view.descriptor()` → dict；identity_kind、dataset_id、manifest_sha256、source、objects、scope、guarantees、reopen、schema/parser/policy版本；原Client query_snapshot_id单列 |
| 多时钟 | `view.at(as_of, *, visibility="verified")` → 新ResearchView；同一dataset_id锁住观测集合，默认verified明确拒绝，可显式assumed/received；旧回填没有接收时间则received拒绝 |
| 聚宽风格行情 | `view.get_price(security, start_date=None, end_date=None, frequency="daily", fields=None, count=None, fq=None, *, require_complete=False, require_fresh=False, require_final=False, require_tradable=False)` → ResearchResult，含`.data` DataFrame、`.report`及`.to_dict()`；默认OHLCV，money/amount同义但不能同传；源标签闭区间；count/start互斥 |
| 覆盖/证据 | `view.coverage(security,start_date,end_date,frequency="daily")` → 报告；`view.quality()`、`view.lineage()` → 数据集质量/谱系；不自动补数据 |
| 目录/恢复 | `store.research_snapshots()`、`store.recover_research()` → 本地目录/恢复报告；不会把研究版本列为旧正式Store published snapshot |

ResearchResult默认价格/金额Decimal、股数整数，Naive索引仍明确Asia/Shanghai源标签。to_dict的rows包含源标签和代码，Decimal在CLI输出字符串。report保留原始source_rows、逐行hash和观测来源、未知字段、研究假设、覆盖与全部未支持保证；数据集可读不等于执行许可。

Sina重开复用dev4观测筛选/冲突/闭合规则；多时钟只换查询条件，不重开浮动cache。默认查询只读已封存原文；原形成中记录不随墙钟成熟。新捕获/导入产生新ID；旧ID不变。没有新源自动回退。Bao导入仅个人研究的旧回填，available_at/response_completed_at为null，不从文件时间或任务时间推回历史知识。

严格verified报PIT_UNAVAILABLE；require_final报BAR_NOT_FINAL。Bao require_complete/fresh/tradable在当前证据不足时分别明确拒绝；可同时返回源日历下六日格一致的source_grid_complete，不能将其提升为供应商/交易真值。Sina带有已封存CoverageContract时沿用该声明契约保证并保留synthetic/observed证据等级，否则拒绝。未实现公司行动、历史股票池、限价、RQ交易投影或订阅流。

CLI新增：`research-fetch`、`research-import`、`research-describe`、`research-price`、`research-snapshots`、`research-recover`，均使用显式`--store`；只有research-fetch联网。确切参数由--help和实现README同时给出。

### 可运行调用

```python
from ashare_data import Store

store = Store.init("./research-store")
# 显式网络操作，每股一个有界窗口；不是任意历史分页。
sina_id = store.fetch_price("600000.XSHG", frequency="daily", count=2)

# 只读既有 Bao 日线 JSON；不运行 Bao SDK，不信任其中任务时间。
local_id = store.import_research(["daily-sh.600000.json", "daily-sz.000001.json"],
                                 calendar_path="calendar.json")
# 另一个进程只需 store 路径和完整 dataset_id，不访问 Client 私有缓存。
view = Store("./research-store").research(local_id)
descriptor = view.descriptor()
result = view.get_price(["600000.XSHG", "000001.XSHE"],
                       start_date="2019-12-30", end_date="2020-01-07",
                       fields=["open", "close", "high", "low", "volume", "money"])
print(result.data)
print(result.report["provenance"][0]["coverage_report"])
# 旧回填的显式研究假设：日线仅在源日期结束后可见。
historical = view.at("2020-01-04T00:00:00+08:00", visibility="assumed")
```

`get_price` 标量证券返回 time 索引；证券列表返回 time/code 列。price/money 是 Decimal；volume 是整数股。`to_dict()` 返回 Python 字典（时间/Decimal 保留类型）；CLI 用 ISO 时间和十进制定点字符串编码。`.data` 与 `.report` 可改，不会修改已封存版本；描述符和谱系返回副本。

无策略时钟的研究查询读封存的合格观测。新浪多观测取每标签最新合格接收版本；同刻异值报 `OBSERVATION_CONFLICT`。不同日线原文同标签异值且接收时间未知报 `VERSION_ORDER_UNKNOWN`，应分别导入成两个版本。`.at()` 改查询时钟与可见性，dataset_id 不变；`received` 表示本机当时已经收到，并不证明历史供应商 PIT。Bao assumed 用源日期日终作为保守研究模型，未证明交易所日线最终性。

`.report.provenance[].records[]` 保留证券、源标签、完整源行、行 hash、原文 hash、接收完成时间、available_at 和未知 bar_start/end；新浪另保留逐行观测 ID、冲突历史和贡献响应。`available_at` 仅新浪 received 模式可指本机接收时间，其余为 null。源原文的其他字段（例如 Bao tradestatus、isST、preclose）仅是源声明，不升级为准入证据。

#### 严格错误与限制

| 请求 | 明确结果 |
|---|---|
| `.at(t)` 默认 verified | `PIT_UNAVAILABLE` |
| Bao `.at(t, visibility="received")` | `RECEIPT_TIME_UNKNOWN` |
| `require_final=True` | `BAR_NOT_FINAL` |
| Bao `require_complete/fresh/tradable=True` | `COVERAGE_UNKNOWN` / `FRESHNESS_UNKNOWN` / `TRADING_STATUS_UNKNOWN` |
| `fq="pre"` / `"post"` | `UNSUPPORTED_ADJUSTMENT` |
| 新浪日线请求 money/amount | `UNSUPPORTED_FIELD`（来源能力未支持） |
| 支持字段在某行缺值（Bao金额或新浪分钟金额等） | `SOURCE_FIELD_MISSING`；不补零 |
| 数据不足 count / 空日期区间 | `COVERAGE_INCOMPLETE` |
| Hash 损坏 / 版本不兼容 | `INTEGRITY` / `RESEARCH_VERSION_UNSUPPORTED` |
| 未提交版本 / latest | `RESEARCH_NOT_PUBLISHED` / `INVALID_ID` |

Bao 日期区间可返回部分实际记录，缺口在逐证券 coverage_report 中明确列出；没有完整源日历时 expected/missing/source_grid_complete 均未知，不按工作日推断。源日历匹配仅 `source_grid_complete=True`，`complete=False` 保持；只要要求强完整性仍拒绝。Sina 无 CoverageContract 时 coverage 保持 unknown；已有契约会随 save_research 封存并沿用 dev4 判断，契约等级也保留，不能把 synthetic 声明当实测。

研究对象最多128个、每个2MiB、10只证券；Bao 合计最多10000行，每响应请求最多367自然日；每次 get_price 的 count 为1..1000。此版本保存原始 JSON 和内容寻址清单，SQLite 记录 prepared/published/aborted；已有严格 Parquet/DuckDB 快照路径不变。研究 ID 不会混进正式 `snapshots()`。这一步没有新增分析物化或日线聚合层。

发布按原文→prepared目录→原子清单→published 完成；中断后用 `recover_research()` 校验原文并完成 prepared，损坏者标 aborted。无目录引用的残留原文不影响读者，本版不提供垃圾清理。已发布文件不覆盖；兼容变更需提高 parser/policy 版本。校验能检测内容变化，不证明供应商真实性或提供加密签名。

#### CLI

```sh
ashare-data --store ./research-store init
ashare-data --store ./research-store research-fetch 600000.XSHG --frequency daily --count 2
ashare-data --store ./research-store research-import daily-sh.600000.json daily-sz.000001.json --calendar calendar.json
ashare-data --store ./research-store research-snapshots
ashare-data --store ./research-store research-describe --dataset FULL_SHA256
ashare-data --store ./research-store research-price 600000.XSHG 000001.XSHE --dataset FULL_SHA256 --start 2019-12-30 --end 2020-01-07 --fields close volume money
ashare-data --store ./research-store research-recover
```

只有 `research-fetch` 联网；其失败不会发布半个多证券数据集，也不会自动重试、换源或扩大采集。未提供 HTTP 服务、UI、策略引擎、聚宽全 API、历史股票池、证券属性/限价/公司行动执行投影。此次解决 A宽 AD-FACTS 持久公开重开、AD-D2 描述符和 AD-D3 原生日线读取；不宣称完整 A宽交易验收通过。数据清洗和准入在 A数达；A宽使用公开 ResearchView/ResearchResult 做薄映射。


#### 可变对象所有权

公开结果是调用者可修改的独立副本。`.report`、`.data`、`.data.attrs` 和每次 `.to_dict()` 均不共享可变对象与固定视图；DataFrame 的 object 单元格也逐项深复制。构造 `ResearchResult(data, report)` 时同样脱离传入对象。修改这些副本只改变当前副本，后续查询、已有/新建兄弟 view、重新打开的同 ID 数据均不改变。消费者自己的缓存可以保存并修改返回值，但不能因此改写 A数达内部版本。

`.at()` 只建立轻量查询上下文，仍共享未向外暴露的内部已验证记录；没有为每次调用重读磁盘或复制整个数据集。描述符、谱系、覆盖、质量及异常字典也隔离可变引用。旧正式 DataView 的 loaded/manifest 属性现在返回副本；构造器不保留传入元数据引用。此修复保持原有 manifest/parser/policy 版本和数据集 ID，因为解析、可见性与准入规则没有改变。

固定版本 `get_price` 支持 `allow_partial=True` 和 `query_limits=QueryLimits(...)`。返回表只含可用原记录，`report.query_status`、`partial_result` 保留缺口或失败，语义与[行情 API](market-data.md#查询与缓存)相同；不修改版本或发起网络请求。

## 独立归档查询

### 发行人事实与归档版本

`read_action_evidence(product, security, role=role)` 读取经过原文核对的固定行动事实。
返回原始 PDF 哈希、引用页、文档日期标签与真实采集时钟。已解决的安排日期和只确认年度
事项的 `partial_history` 分开；未知除权日保留 `null`。这是局部事实补证，不能据此认证
完整公司行动链、替换提供商台账或自动构建因子。

`read_archive_daily(product, security, source=..., role=role)` 与
`read_archive_calendar(product, security, source=..., role=role)` 读取公开 Dolt 档案的
固定提交响应。前者范围最多12行，后者最多20行，均需要准确的请求日期清单与不可变
32位提交；每次调用核验响应的仓库、完整 SQL `AS OF`、行身份、覆盖及提交日志。
日线保留 `native_fields` 原始数值字符串，并以 Decimal 将手乘100转股、千元乘1000转元。
日历只属于其声明的交易所，ASCII十六进制30/31分别表示关市/开市，不推断其他交易所。

`archive_version.commit` 是数据库版本。其 `commit_time_raw` 保持原日志文本，未给时区
便不推测；`original_source_capture_time/source_publication_time/source_revision_id`
均未知。数据库提交不同不代表所查行情值发生修订。当前 HTTP 取证时间保留于 source，
不会作为历史源采集时间。历史 PIT、策略可见、撮合、官方日历及全日覆盖认证均不开放。

### 既有独立读取

`IndependentQueryView` 读取 AD 固定 binding 内的独立真实来源。它不联网，也不读取 JQ
答案。使用角色必须为 `independent_retrospective_query_research`；策略、撮合、初始化角色
均拒绝。每次读取重新校验 binding 和依赖证据 SHA256，路径限制在 binding 所在目录内。
包中没有私人行情；产品目录另行本地交付。

```python
from ashare_data import IndependentQueryView

view = IndependentQueryView("binding.json", expected_binding_sha256="交付的64位SHA256")
role = "independent_retrospective_query_research"
result = view.read_daily("B27-daily", "000001.XSHE", source="sina-pingan", role=role)
```

调用固定产品 ID，避免把局部覆盖扩大为任意日期承诺。证券和来源必须匹配 binding。
日线的 OHLC/volume/money 以十进制字符串返回；`avg` 是标明公式和 34 位十进制精度的
量额比，不是声称源原生 avg。正成交量只能推导“不是全日停牌”。没有证据的涨跌停、
pre_close、factor、open_interest、vwap 是 `unsupported`，与 `nan` 分开。

新浪 KLC 保留逐股量和源成交额解码。成交额含独立编码的残差/差分，不以收盘价乘量
代造金额；目标日期的源金额仅到整数元。东方财富金额保留小数分，量为整手乘100，
不能据此恢复逐股精度。两个来源必须分别选择，接口不拼接或静默合并。

`read_paused("B28-pause", security, skip_paused=..., fill_paused=..., role=role)`
要求独立公告停牌事实、原始行缺失和最近真实收盘价同时存在。skip 优先并返回空集；
fill=True 用最近真实收盘价填 OHLC、量额置零，全部标记 `synthetic_paused_fill` 和
`traded=False`；fill=False 的六个缺失行情字段标记 `nan`，停牌事实仍为 True。
这是声明的 AD 日线研究政策，不是造出可交易行情，也不认证全部 JQ 停牌语义。

`read_adjusted("B25-adjustment", security, basis_date=..., fq=None/"pre"/"post", role=role)`
采用新浪真实累计后复权因子：前复权价格乘 F(行情日)/F(基准日)，后复权乘
F(行情日)/F(固定归一日)。仅调整价格，量额保留原始值；无隐式价格舍入。产品保留截至
基准日的源因子序列与归一日，排除后续因子。该源累计因子不等于已独立重建每一项
公司行动，不宣称完整历史行动链或与 JQ 调整成交量、因子定义等价。

`read_opening("B12-opening", security, role=role)` 只读取 TDX 历史成交包中状态不为8的
09:25开盘成交记录，保留整手量、状态和原始字节。金额未独立观测，保持空值。
它不证明用户委托参与竞价或任何独立撮合行为。

`read_index_members("C04-members", "000300.XSHG", as_of_date=..., source=...,
role=role, allow_uncertified=True)` 读取固定的独立历史成分候选。区间来源按
opt-in 包含、opt-out 不包含选择；日期快照只接受其绑定日期。接口拒绝越界日期、
重复证券、缺失成员以及未明确接受候选质量的调用。成员数和归档一致不能认证完整
官方生效公告链，名称也不保证为历史名称。原始进出日期和源代码保留，只做代码格式
转换；不根据后来的合并事件自动替换历史证券。该接口仅供事后只读比较。

全部来源于当前真实采集时间，历史日期是行情有效日，不是过去到达时间。源发布时间
和修订号未知时保留为空；不得用于策略历史PIT或把全日量提前提供给策略。C03仅承诺
源返回行和窗内覆盖清单；缺日原因未知，不补行，不把首个返回日称作绝对最早日。


`read_daily(..., standardize=True)` 需要产品显式绑定
`raw_identity_previous_session_close.v1` 与固定日历。未复权 factor=1 表示原始价格
恒等变换，不是完整公司行动累计因子；pre_close 明确是上一日历交易日的原始收盘，
不是除权日交易所参考价。上一日没有真实行时保留 unsupported，不跨缺口取更早价格。
默认 False 保持旧输出；money/volume 精度和派生 avg 不变。

`read_corporate_actions(product, security, source=..., role=role)` 分别读取巨潮分红
与配股账目，按除权日限定窗口，保留公告、登记、付款、上市日期角色及原始空值。
不把源空值解释为零，不合并相互冲突的早期事件，不生成累计复权因子。

`read_halt_events(product, security, role=role)` 仅提取绑定证券、固定日期的东方财富
全日停牌事件：原文必须明确同日9:30至15:00停牌一天。页面只用 JSON 解码，
不执行脚本。缺失、重复、半日停牌和错误证券均拒绝。事件源的 NOTICE_DATE 是
有效日标签，非可见时钟；提供商事件记录并不自动等于已核验交易所公告链。
独立覆盖报告可将这些事件与无成交日交叉核对，不会改写或填充原日线。

## 录制行情

`Store.import_recorded_market([...], source_kind=...)` 接受 1–10 个 `JQObservationInput`，由 `Store.recorded_market(dataset_id)` 重开。输入原始事件和策略均需完整 SHA-256。接口处理当前价、分块 get_price 请求响应及实际 day_start 的前日信息，不存账户、订单、成交或标准答案；版本一旧接口保持兼容。

分块响应必须按 offset 完整、连续，字段严格匹配请求；跨块重名标签、缺块、冲突当前价、非有限数、夹带字段、源/产品篡改均拒绝。精确请求与原观察时钟绑定，不补查询或数据行。`current` 仅精确时点；`query` 由 AD 自身检查未来截止及未完成当日日线字段，10:00 的 open 可以读，high/low/close/volume/money 拒绝；不会把 JQ 源异常字符串作为 AD 的运行异常。

引擎与策略用途分离：`market(... role=matching)` 只给截至读数时已观察的原始分钟；`retrospective_capacity` 只给同日全日 volume；`retrospective_minute` 明确允许事后读取已固定的精确分钟用于有限重建。它们不证明历史 PIT。`engine_quote` 仅对存在精确分钟的时点，将该 close 与同日更早已观测限价合并，明确记录限价沿用假设与来源，不延续缺失的最新价。策略 current/query 不接触事后通道。角色是调用契约，不是进程安全沙箱。

独立日线、发行人报告继续通过既有 `IndependentQueryView` 读取。数据缺口由 AD 返回 unavailable；AQ 不维护第二份行情账本。软件包与公开源码不包含本地原始行情或回测账户记录。

## Native neigezhu minute research

`NeigezhuMinuteView` reads the `minute-hf-download` asset registered in an existing
`ashare-local/deployment.json`. It does not create a data home, install anything,
fetch data, change the deployment, copy a full library, or modify Parquet files.

The caller supplies an externally pinned `file_manifest.csv` from a completed
byte/integrity audit. Each queried source file is hashed on first use and after
any size/mtime/inode change. Calls select one security and the complete requested interval, without a fixed
day or row ceiling. Hashes certify source identity, not market correctness.

```python
from ashare_data import NeigezhuMinuteView

view = NeigezhuMinuteView(
    entry="/path/to/ashare-local/deployment.json",
    manifest="/path/to/review/file_manifest.csv",
    expected_manifest_sha256="<externally reviewed SHA256>",
    review="/path/to/review/quality_overlay.json",       # optional, paired with pin
    expected_review_sha256="<externally reviewed overlay SHA256>",
)
result = view.get_price(
    "600000.XSHG",
    mode="retrospective",
    start="2026-06-02T09:30:00+08:00",
    end="2026-06-02T15:00:00+08:00",
)
```

The query is inclusive on source labels. It retains 09:30, zero-volume rows,
invalid prices, native binary values, exact physical row numbers and source file
hashes. It neither folds the opening observation nor assumes a 240-bar day.
BJ securities use explicit source identities such as `BJ.920000`; they are not
mapped into an execution consumer's unsupported exchange identity.

`labels=[...]` optionally reports exact requested omissions. A range-only query
does not guess an expected trading calendar, suspension state, or complete
history. An empty result stays empty.

Asset paths follow the shared deployment convention: a relative `data_root`
is relative to the deployment file, and a relative asset path is relative to
that resolved root. Moving a small deployment fixture does not depend on cwd.

`coverage_basis` distinguishes requested-label omissions, publisher calendar /
listing-span expectations, and actual missing trading days (unknown). The
145,243 published-expected missing security-days are not verified lost trading
days: 68,658 have existing source-reported suspension, 76,585 lack an existing
daily reference; 62,161 of the latter are only publisher trading-day claims.
The 243 dates after known official delisting overlap those groups. Another 412
observed-span gaps lie outside the publisher's expected span. These are separate
contexts, not additive error counts or a certified historical universe.

Every row has separate quality information:

| Field | Meaning |
| --- | --- |
| `price_status` | `invalid` for structural/nonpositive failures, `conflict` for a pinned scoped reference conflict, otherwise `unverified` |
| `price_structurally_usable` | Basic OHLC checks only; this is not a permission to trade or value |
| `activity_status` | Positive, zero, invalid (negative/noninteger), or unknown volume; none proves liquidity or suspension |
| `diagnostics` | Fixed storage-envelope findings and separately named rounding hypotheses |
| `review_evidence` | Optional range restriction, bound to file hash and physical positions |
| `permission_status` | The reader grants no execution/valuation permission; an explicit bounded consumer profile can grant otherwise eligible uses |
| `use_decision` | `deny` versus `not_granted` independently for execution and fresh valuation; known price invalid/conflict denies both, hard quantity errors deny execution |
| `execution_admitted`, `new_valuation_admitted` | Legacy constant false means no reader grant, not a whole-library denial |

Nulls, NaN/Inf, negative quantity/amount and noninteger quantity are explicitly
diagnosed. Nonfinite prices cannot be structurally usable. The DataFrame retains
IEEE values; `to_dict()` encodes NaN/+Infinity/-Infinity as strings and original
nulls as JSON null, with `null_fields` retaining their distinction. Nothing is
replaced by a zero or dropped.

Price structure is checked independently of volume and amount. A finite
`close < low` still makes the price invalid when volume is NaN or amount is
null; both the price and activity diagnostics remain visible. Normal prices
with invalid activity keep their own structural status: activity defects deny
execution without inventing a price defect or granting fresh valuation.

OHLC/volume/amount, interval semantics, daily state, corporate actions, and
historical availability are separate dependencies. Float32 compatibility does
not prove the original encoding. Integer-yuan or cumulative-100 feasibility is
not a vendor-algorithm certificate or an enlarged acceptance tolerance.

A quality overlay only denies use on exact ranges; it cannot authorize a row,
repair values, remove securities, or liquidate holdings. A bad/missing quote
must leave account quantity intact and require explicit stale-mark/state
handling. Reverting the overlay changes review classification, not raw data.

`export_minute_research` derives `minute-row-use.v1` from the stored native
observation and binds it to the source contract and stored row hashes. Keep the
complete `native_observation` in the imported row; substituting another label,
source hash or six-field value is rejected. AQ independently checks this binding
(including legacy nested observations), and research opt-in cannot override a
deny. Conditional VWAP/extrema warnings never produce a hard deny.

The default export still rejects missing slots. For a reviewed partial snapshot,
explicit metadata `gap_policy="explicit_unavailable"` exports separate
`unavailable_minutes` backed by the snapshot coverage report. It does not create
OHLC rows, infer suspension, or claim a lost trade. Publish only the coverage
that actually passed Store validation. Unknown calendar, excluded quality rows
and source conflicts still reject export. AQ requires exactly the union of
observed rows and declared missing slots, forbids matching at denied/missing
slots, and preserves quantity with the last usable mark explicitly `stale`.
The normal quote after the gap resumes marking/matching. Account positions and
`get_current_data()[security].valuation` expose the mark status and source label.

Execution isolation does not filter historical indicator windows. The native
reader keeps invalid/conflict values together with their quality columns. AQ's
historical `get_price` returns the observed raw six fields without a historical
`use_policy` column; it may return a conflict price even while the current
account mark is stale at an earlier usable quote. Missing quotes add no rows,
and `count` counts observations, not necessarily consecutive trading minutes.

There is no automatic quality-filtered historical research API in this change.
An indicator or signal must explicitly use a reviewed input/window selection,
inspect native `price_status`, `diagnostics` and `use_decision`, and verify the
remaining label coverage before computing an aggregate. `labels=[...]` selects
only the caller's named native labels; it does not review or certify them.
Excluding a denied row must remain an explicit missing observation, not silently
compress time or fill a value. A non-denied or `unverified` row is not a grant.

For bounded AQ research, use an explicitly reviewed scope and the public AD
Store / `export_minute_research` APIs. Store's decimal contract can reject native
values with more than six decimal places; this reader never rounds them to make
an import pass. Preserve the native observation in source evidence. Keep source
interval/availability unknown, use the explicit retrospective replay model,
review the calendar/state/actions, and audit every fill against source open,
capacity, fees, T+1 and account conservation. A successful backtest is not a data
acceptance result. Do not export the whole library into that bounded interface.

The optional `cumulative_100_feasible` diagnostic is a mathematical existence
test for integer true volumes whose cumulative sum rounds half-up to reported
100-share cumulative volume. It requires the reported lattice and checks all
prefixes jointly, starting true and reported day cumulative volume at zero.
An empty integer-volume interval is explicitly infeasible under the hypothesis;
unsupported numeric bounds/lattices remain separately inapplicable. It never
reconstructs or overwrites true volume.

The extrema diagnostics additionally ask whether at least one share could have
traded at each distinct high/low. They use actual-trade/same-interval/exact-share
assumptions. A separate +/-99-share and one-yuan branch remains a counterfactual.
These warnings never change a row to a certain error or increase admission.

The envelope also requires price, volume and amount to describe the same trade
set, units and interval. Existing JQ raw data is itself a counterexample to
treating this as unconditional truth: 600000, 2026-06-04 10:04 has low=9.21,
volume=683000 and money=6289257, below low×volume=6290430 by 1173 CNY; HF has
the same volume/low and amount one yuan lower. This neither authenticates either
source nor identifies the mechanism. Repeated agreement or a cumulative-volume
fit cannot repair or admit the whole library. No warning threshold is widened.
