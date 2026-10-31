# 准备日频研究与执行输入

先选择固定日频包及其用途，再检查补充状态和缺口。研究时钟、模拟输入资格与真实执行权限分别判断；原件由调用者另行绑定。

## 日频研究

`DailyResearchView` 接受一个本地 `binding.json` 和外部固定的完整 SHA256。
打开时核验清单内每个原文、回执、解码结果和数据文件，随后只读内存中的固定版本。
返回对象均为独立副本；改文件或更新上游不会改变已打开的视图，重新打开会重新校验。
安装包仅含代码，不附第三方行情或私人证据。

```python
from ashare_data import DailyResearchView

view = DailyResearchView("/本地数据包/binding.json", expected_binding_sha256="交接的完整64位哈希")
print(view.descriptor())
print(view.quality())
engine_input = view.read_window("w2020", role="daily_strategy_retrospective_research")
history = view.history("w2020", "000001.XSHE",
                       as_of="2020-02-03T09:00:00+08:00", count=120)
opening = view.opening("w2020", "000001.XSHE", as_of="2020-02-03T09:30:00+08:00")
```

`examples/daily_research.py binding.json --sha256 <固定哈希>` 演示三个窗口的重开、
120 日预热、开盘价、收盘估值和默认未知状态；消费者仅通过公开接口读取。

| 入口 | 边界 |
|---|---|
| `descriptor()` | 单位、固定股票与窗口、假设、限制、生产者和版本 |
| `quality()` | 逐证券/窗口覆盖、停牌承接、原价/量额独立点核验、日历交叉核对 |
| `lineage()` | 原始文件哈希、URL、请求参数、实际抓取时段、复用来源及失败回执 |
| `get_trade_days(window_id, start_date=..., end_date=...)` | 固定实际源日期，包含预热和输出末日的后继交易日；越界拒绝 |
| `read_window(window_id, role=...)` | 引擎侧完整输入导出，`strategy_visible=False`，须与策略查询对象分开 |
| `history(window_id, security, as_of=..., count=..., fields=...)` | 只提供模拟时钟下已收盘的日线；缺行/预热不足拒绝，不静默跳过 |
| `opening(...)` / `closing(...)` | 当日研究开盘价最早09:30、收盘价最早15:00；分别只返回对应价格 |
| `state(..., accept_model=False)` | 默认盘前停牌和限价均未知；显式 opt-in 才返回 `status=modeled` 的研究模型 |
| `corporate_actions(..., as_of=...)` | 按公告日期的次日开始可见；保留支付日期与未知日内时刻，不推断提前到账 |

时间采用 Asia/Shanghai。上述09:00、09:30、15:00都是明确的事后研究时钟，
**不是历史接收、交易所发布或可执行成交时钟**。当前抓取时间只在来源回执中。
API 的 `historical_pit`、`execution_permission` 始终为 false。

原始 OHLC 单位为元/股、volume 为整数股、money 为元，均保留源精度。
只有独立停复牌证据、源行缺失和相邻真实收盘都存在时，才可标记
`documented_halt_carry`：OHLC承接前收、量额为显式无交易投影的零、`traded=False`。
它不成为真实行情或撮合价。未知缺日不允许这样处理。

`history(adjustment="pre", basis_date=...)` 用源累计因子 `F(d)/F(basis)`；
`post` 用 `F(d)/F(最早源归一日)`。基准日不能超过模拟时钟最后已完成交易日，
未来因子事件在计算前排除。只调整价格，股数和金额不变。它不等于 JQ 因子/量调整规则，
也不证明全公司行动链。策略 manifest 若选择 raw，应保持 `adjustment=None`。

盘前状态模型有单独名称及假设 ID。历史 ST/限价未知时不会默认填成普通股事实。
调用方可以显式研究“普通主板、非ST、10%涨跌幅、分位四舍五入”的条件，
但它不是源限价。源独立 prevclose 标记优先于上一原始收盘；不从当日 OHLC 反推。
现金到账时刻与递延持有期税务由消费者显式建模。

生成工具 `scripts/build_daily_research.py <目录> --source-commit <源基线完整提交>` 只读取已采集原文：
固定策略 manifest、`raw/`、`derived/`、经原文核对的 `facts.json`。
解码步骤、固定窗口及复验命令见 [开发工具说明](development.md#为什么保留-javascript)。原文和外部 decoder 不随 wheel 分发；新采集使用新目录/绑定，生成器拒绝覆盖不同内容。

包内 reports/coverage.json、binding.json 记录当前来源覆盖与摘要；保留固定样本的失败和限制，不以收益或完整性删选股票。

预热长度由研究包的 `warmup_sessions` 声明，离线构建脚本读取 `data_requirements.warmup_open_sessions_before_start`；不再强制每个策略准备 120 日。至少保留一个前交易日、输出窗口和一个后继日，日期及数量须与来源日历一致。查询时 `history(count=N)` 仍检查当时实际可见的 N 条，不足则明确拒绝。120 日仍是上方示例的模型需求。

## 执行字段

`DailyExecutionView` 复用 `DailyResearchView` 的行情、预热、日历和行动原文，
另行加载一个 SHA256 固定的执行补充包。不下载、不复制原行情，不含撮合或资金账户。
补充包绑定原研究 binding 的外部哈希；消费端必须分别固定两者的哈希。

```python
from ashare_data import DailyResearchView, DailyExecutionView

research = DailyResearchView(research_binding, expected_binding_sha256=research_sha)
v = DailyExecutionView(research, supplement_binding, expected_binding_sha256=supplement_sha)
```

`execution_permission=False` 指本产品不授予真实交易权限。
是否具备**声明模型下的模拟输入**，看 `preflight().ready` 与
`read_window().report.simulation_admitted`，不能用该布尔值拒绝全部事后研究。

| 用途 | 行为 |
|---|---|
| `signal_research` | 允许已有真实行情、预热和时钟查询；状态缺口保留为告警，完整输出所有固定证券和日期 |
| `retrospective_simulation` | 状态、限价、行动覆盖和适用时间必须满足检查，所需模型必须逐项显式接受 |
| `historical_pit` | 拒绝：本产品没有证明当时真实送达、修订历史或盘前可见性 |
| `live_execution` | 拒绝：回溯包不提供实时状态、权限或可成交性 |

每个状态字段均是 `{value, status, source_refs, historical_available_at, assumption_ids, reason}`。
`status` 只取 `source_observed`、`derived`、`unavailable`；最后一种必须 `value=None`。
归档字段的 `source_observed` 指读到了提供方历史标志，不是交易所原始下发证明。
名称区间推导、前收推导和普通限价公式为 `derived`。任何接受模型的选择均不能将未知 ST
默认为正常或将未知限价变为 0 / 10%。外部固定哈希是调用者对数据所有者版本的约束，
不是来源真实性的加密签名。

```python
accepted = (
    "retrospective_daily_status",  # 日终状态放在研究 09:00 回放
    "shared_market_calendar",     # 公共日历、官方休市通知、实际日期交叉支持
    "standard_mainboard_limits",  # 普通主板规则，不声称取得每日例外覆盖
    "scoped_action_absence",       # 限定来源的无行动，不要求全历史公告链
    "gross_cash_zero_withholding", # 消费端税务模型
    "dated_name_history_status",  # 仅使用有日期锚点和完整官方更名查询的区间推导
)
check = v.preflight(
    "w2022", purpose="retrospective_simulation",
    accepted_assumptions=accepted,
    cash_credit_policy="pay_date_after_close",
)
if not check["ready"]:
    # 每项都有 window / security / date / field / reason / source_refs。
    # 保留原样本，不删除失败证券、日期或窗口。
    raise RuntimeError(check["issues"])
engine = v.read_window(
    "w2022", purpose="retrospective_simulation",
    accepted_assumptions=accepted,
    cash_credit_policy="pay_date_after_close",
)
```

`read_window` 只给引擎，包含整窗未来记录，**不可直接充当策略上下文**。
它给出原始 `bars`、带字段来源的 `execution_states`、`actions`、日历、行动覆盖和准入报告。
剔除旧 `research_state`，防止消费者误将旧的未验证非 ST 模型覆盖新证据。
原价字段用十进制字符串；量以股、金额以元，不从收盘价与股数相乘制造成交额。
执行状态字段在引擎内可通过 `row[field]["value"]` 转成模拟器输入，但必须保存准入报告。

策略历史用 `v.history(...)`：沿用原包的完成日线时钟，09:00 只能见之前日线。
开盘、估值仍用 `research.opening(...09:30)`、`research.closing(...15:00)`。
`v.state(...as_of=...)` 仅在整窗已通过模拟准入且时刻达到 09:00 时返回当日回放状态，
不给当日 OHLCV，不声称源当时已送达。

现金时点分开保存：`ex_date` 是除息及应收模型日期，`payment_date` 是来源确认的支付日，
`cash_available_at` 是实际可用时刻。原文只有日期时，实际时刻保持空。
消费者必须选择 `pay_date_before_open`（09:00）或 `pay_date_after_close`（15:30）；
由此产生的字段单独命名为 `model_cash_available_at`。
`cash_state()` 返回 `not_ex`、`receivable` 或 `model_available`，不计算个人持股和税款。
金额按登记日收盘后实际持有的合资格股数在消费者账户计算。

股份数量明确为零时，可用日期是“不适用”，不伪造等于除权日。
非零送转股必须有分别给出的到账和可卖日期，当前契约只接受两者都等于除权日；
登记日和除权日必须是相邻交易日。支付超出输出窗、配股或其他未支持行动均精确拒绝。
日内税务、排队成交、停牌中途恢复和流动性均不由本数据接口证明。

`quality()` 返回当前输入包的来源等级与缺口，不能从旧样本数量推断本次覆盖。

离线构建：`scripts/build_daily_execution.py RESEARCH_BINDING SUPPLEMENT_ROOT --research-sha256 HASH`。
只读已捕获来源，验证查询身份、边界、重复、字段、前收、名称查询分页、日历差异及限价点；
现有固定输出字节不符时拒绝覆盖。真实数据不进入仓库或 wheel。
可选真实验收用 `ASHARE_DAILY_RESEARCH_PACKAGE` 和 `ASHARE_DAILY_EXECUTION_PACKAGE` 指向本地包。

现有研究接口保持兼容；读取示例见 `examples/daily_execution.py`。


### 按实际用途检查执行字段

`preflight_orders(window, requests, purpose=..., accepted_assumptions=..., cash_credit_policy=...)`
接收真实拟提交的 `[{"security": ..., "date": ..., "side": "BUY"|"SELL"}]`。
空列表明确表示没有订单；不是未知清单。只阻断该证券日的停牌、参考价和对应方向限价；
重建/未知限价同时检查 ST 依赖。另一方向及非订单观察日的未知保留为告警，
行动覆盖、现金/股份时点、日历和严格 PIT/实时限制仍执行，不删除任一固定证券或日期。

返回 `admission_scope="explicit_order_requests"`、`requests`、`required_fields` 及
`whole_window_ready`。`ready=True` 只对这份请求成立；不能用于后来新增的订单。
原 `preflight/read_window/state` 的整窗行为不变。引擎可用 `signal_research` 导出保留未知的
完整行情，随后每次下单前用 `order_state(...as_of=..., side=...)` 获得该笔订单的单独准入。
观测或历史订单清单的事后检查不能替代运行中对新订单的检查。

`order_limit_scenarios(...as_of=...09:30..., side=..., accept_ordinary_scenarios=True)`
比较普通主板研究模型：未知 ST 时分别计算 5% 与 10% 情景，已知时只计算其对应情景。
使用 AD 原始开盘价和已知参考价，不选择某种 ST、不更改事实值。报告每个情景的价带与
对应方向是否被限价阻断、结果是否相同；涨停价不买入、跌停价不卖出。仍显示
`simulation_admitted=False`、`scenario_only=True`。它只检验该限价环节的敏感性，
不证明例外限价、流动性、队列、经纪商权限或整个账户路径已获经济验收。


### 交付引用验证

`tests/support/execution_delivery_refs.py` 核对显式本地交付根目录中的机器交接、消费契约、链接和 JSON 指针，检查事件身份与缺口明细。指针必须以完整源数组为基准，不能使用窗口过滤后的列表下标。该工具只属于开发交付校验，不进入运行 wheel。

新交付使用新目录、新绑定及本次验证结果。

## 输入预检

Preflight把用户显式绑定的本地不可变版本转成有证据的研究值和缺口。没有采集、默认选源、换源、跨日历capture拼接、事实回填或执行授权。`research_values_complete=True`只表示下表要求的研究字段有来源值，停牌/ST为True或preclose为零也仍是已知来源值；不表示可以买卖、PIT成立或满足PORTABLE/INITIALIZE要求。

### Python和CLI契约

```python
from ashare_data import Store, ResearchInputRef, ResearchSecurityInputs

store = Store('/已有本地目录')
binding = ResearchSecurityInputs(
    '600000.XSHG',
    price=ResearchInputRef('baostock', '完整价格capture_id'),
    status=ResearchInputRef('baostock', '完整状态capture_id'),
    preclose=ResearchInputRef('baostock', '完整preclose_capture_id'),
)
result = store.preflight_research(
    inputs=[binding],
    calendar=ResearchInputRef('baostock', '完整日历capture_id'),
    trading_dates=['2026-09-29'],
    require_complete=False,
)
print(result.report_id, result.research_values_complete)
for row in result.rows:
    print(row.security, row.trade_date, row.role)
    for fact in row.facts:
        print(fact.field, fact.state, fact.value, fact.unit, fact.reason)
```

ID示例是占位符；实际必须为64位小写SHA256。`ResearchInputRef(kind, version_id, store_path=None)`的kind仅`research`、`baostock`；价格支持两种，其余引用仅支持baostock。可选store_path只定位显式本地store，省略使用调用Store；同一固定版本迁移目录后报告身份不变。不同数据源不能共享一个隐式兜底路径，每个绑定在请求和结果中清楚可见。

`ResearchSecurityInputs(security, price=None, status=None, preclose=None)`每个证券一份，未绑定项返回INPUT_NOT_BOUND。输入限1–10个唯一规范证券（六位ASCII代码.XSHG/.XSHE）；锚点为1–31个唯一date或精确YYYY-MM-DD，最早至最晚的自然日包络至多31天。拒绝datetime截断、重复、子类/其他类型和非bool严格标志。按锚点输入顺序、证券输入顺序、下表三用途顺序返回。

JSON请求格式与上面的构造一致：顶层必须且仅为 `inputs`、`calendar`、`trading_dates`。证券绑定字段为security及可选price/status/preclose；引用字段为kind/version_id及可选store_path。未知字段拒绝。

```shell
ashare-data --store /本地store research-preflight --request request.json
ashare-data --store /本地store research-preflight --request request.json --require-complete
```

Python JSON入口是 `Store.preflight_research_request(request, require_complete=False)`。成功输出JSON、退出0；错误使用既有DataError JSON、退出2。默认成功返回可以包含缺口，必须检查研究字段和执行边界；`--require-complete`只要求研究字段齐全，不要求或证明执行资格。`Store.capabilities()['research_preflight']`提供策略、边界和来源kind。

### 用途、日期和字段

策略版本 `research-input-preflight-1`。P/T/next都来自同一个固定源日历capture的 `get_calendar_links()`。锚点不是已知开市日、邻日超界或未知时，保留锚点日期及错误，前日和后继日期均为None；不猜任何一个邻日。邻接采用完整三元组，不能把部分成功包装成完整窗口。

| role（研究用途，非执行read_role） | 日期 | 要求的研究字段 |
| --- | --- | --- |
| prior_day_research | 源日历前一开市日P | 完整日历关系、calendar_open、close、suspended、is_st |
| anchor_day_research | 请求锚点T | 完整日历关系、calendar_open、open/high/low/close、volume、amount、source_preclose、suspended、is_st |
| successor_metadata | 源日历后一开市日next | 完整日历关系、calendar_open、suspended、is_st |

不产生后继行情字段。来源证据可以保留完整原始源行；这些研究用途不是隔离原始字段的权限沙箱。不存在INITIALIZE/DECIDE/结算读权限，也不构建新的owner window plan/profile。

每个用途另外显式列出未支持项：historical_eligibility、listing_status、official_price_limits、rule_version、corporate_action_coverage、adjusted_prev_close、historical_pit、finality、owner_execution_plan，状态均unsupported、值None。它表示本接口未提供这些事实，不代表其他版本从未有过该数据或现实事件不存在。上市年份不代替逐日资格；源preclose不代替实际P日close、调整前收或无事件证明。

### 类型、单位、质量和身份

返回frozen `ResearchPreflightResult`、`ResearchInputRow`、`ResearchInputFact`；rows/facts为元组。`request`、`sources`、`fact.evidence`、`fact.value`与`to_dict()`都返回独立副本。每行带security/anchor_date/trade_date/role/row_sha256；每个字段有state/value/unit/price_basis/source_kind/version_id/evidence/reason。

- 值状态：available为已有源值；unknown为缺绑定、缺日期、未请求或源空/非法枚举等；unsupported为本接口未实现的事实。未知不用0、False或“无事件”填充。
- 价格和源preclose是Decimal的精确十进制字符串，volume为整数股，amount为十进制元。原字段和原串保留在证据。前日close和OHLC是raw_unadjusted；源preclose保持source_reference_adjustflag_3，不舍入或计算替代。
- 每个已取得值带固定版本ID、原行/响应hash、收到时间、原字段和原public quality；preclose附源定义。calendar_relation带完整自然日链。response_received_at可能只是最后收到字节，quality里的完成/完整性未知仍保留；historical_available_at=None。
- report_id绑定规范请求、固定来源描述符、逐用途值/缺口与策略；row_sha256绑定完整用途行。路径、运行时刻、缓存age不进入身份。ID是结果内容摘要，**没有持久化发布新snapshot**，复现须固定wheel、请求与全部源版本。
- 所有结果network_used=False、historical_pit=False、execution_permission=False，execution_admission=BLOCKED_NO_OWNER_PLAN。sources保存现有公共descriptor/quality，不将source_claim_unverified、合成或协议完整性未知提升为市场真实性。

实现只消费Store/view公共接口：research.get_price/descriptor/quality、baostock.get_price/get_status/get_preclose/get_calendar/get_calendar_links。未直接读取私有事实文件或存储表，没有对AQ私有实现的依赖。

### 错误与严格模式

| 错误 | 行为 |
| --- | --- |
| PREFLIGHT_ARGUMENT | 输入类型/规范证券/日期边界/引用kind或ID/JSON字段不合法，查询前拒绝 |
| PREFLIGHT_BINDING_MISMATCH | 绑定证券、频率或价基与用途不符，整体拒绝 |
| PREFLIGHT_SOURCE_MISMATCH | 公共返回的价格行或证据不匹配请求，整体拒绝 |
| PREFLIGHT_INCOMPLETE | 严格模式下所需研究字段有缺口；details保留完整同一报告 |
| INTEGRITY、BAOSTOCK_NOT_PUBLISHED、RESEARCH_NOT_PUBLISHED、QUERY_KIND_MISMATCH、SOURCE_REQUEST_FAILED等 | 破坏、未发布、错种类、失败源或畸形源输入整体拒绝，不降为正常缺口 |

合法来源中的COVERAGE_INCOMPLETE、SOURCE_FIELD_MISSING、UNSUPPORTED_FIELD、CACHE_MISS转成对应字段unknown并保留错误；日历未知/闭市/越界/边界不足转成calendar_relation缺口。日状态与preclose沿用其精确未知原因，false和0不误判缺失。不会吞掉未知异常后返回成功。

路径错误契约：`store_path` 的空白、NUL 或本地不可编码字符串返回 `PREFLIGHT_ARGUMENT`；有效字符串在系统分辨或访问目录时发生的错误（包括超长路径）返回 `PREFLIGHT_SOURCE_IO_ERROR`。库抛出 `DataError`，CLI 返回相同 code、退出 2；两者都不降格为空数据或缺口。既有 `STORE_NOT_FOUND`、`INTEGRITY` 等数据错误原样传播。
