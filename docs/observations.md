# 采集当前观察与导入已有 JQ 响应

## 当前观察

观察模块提供显式的一次采集、核验和读取；不会自行调度、启动服务、写账户或发订单。

| 入口模块 | 用途 |
| --- | --- |
| `ashare_data.observations.daily` | 日线与日历的原始响应、完成时刻、固定 packet 和比较 |
| `ashare_data.observations.phases` | 准备/开盘阶段的证券身份、停牌/ST、会话参考价、限价与行动检查 |
| `ashare_data.observations.intraday` | 有限盘中观察、源时间及实际接收时间 |
| `ashare_data.observations.close_confirmation` | 收盘观察的证据核对 |
| `ashare_data.observations.intraday_refresh` | 刷新时钟与显式查询窗口 |
| `ashare_data.observations.response_replay` | 固定响应重放与当前实现的重新解释 |

先运行相应 `python -m <模块> --help` 查看完整参数。示例：

```sh
python -m ashare_data.observations.phases --root data/observations admission --day YYYY-MM-DD --conditional
python -m ashare_data.observations.phases --root data/observations opening --day YYYY-MM-DD --conditional
```

命令中的日期、输出目录必须来自本次计划。当前有限条件策略仍只支持代码声明的证券/窗口。依赖发行人行动基线的阶段，通过 `ASHARE_PILOT_EVIDENCE` 显式指定仓库外合法证据；缺失时阻断，没有内置真实公告样例或自动寻找本机目录。

准备阶段的参考价/限价是会话事实，实时停牌状态可未知；已知停牌始终否决。开盘阶段需重新取得当前日、实际阶段内的行情与时间证据，核对正开盘价、源停牌标志及准备值一致性。旧日/未来时间、身份错配、缺源、限价公式冲突和新行动反证均阻断。读取重算判断，不能只信报告内的 ready 字段。

日标签、供应商时间、HTTP 开始/完成时间、历史 available_at 是不同字段。事后收到的数据不证明过去已可见，当前源股票池不证明历史挂牌，观测价不证明成交。所有 packet 保留 `execution_permission=false`。AQ 必须独立管理账户、时钟、请求身份、实际调用链与 native 风控。

代码移动和版本改变产生新的代码指纹。旧冻结 packet、账户及其解释继续由原固定运行环境读取；新观察/重放输出写入新目录，记录新代码身份，不能覆盖旧包后宣称继承旧验收。

## 稀疏行情观察

仅供有界本地研究，不继承其他产品的执行准入。公开 API 是可调用的软件接口；真实行情仍是用户本地私有文件，不在仓库或 wheel 中。调用者提供显式路径与原件摘要。

### 生成与固定重开

```python
from ashare_data import JQObservationInput, Store

store = Store.init("data/new-store")
dataset_id = store.import_jq_observations([
    JQObservationInput(
        case_id="B09", security="000001.XSHE", session_date="2024-03-04",
        events_path="data/observations/events.jsonl", events_sha256="完整SHA256",
        script_path="data/observations/jq.py", script_sha256="完整SHA256",
    ),
], source_kind="local_jq_backtest_observation")

view = Store("data/new-store").jq_observations(dataset_id)
manifest = view.describe()
result = view.read("000001.XSHE", kind="current",
                   as_of="2024-03-04T10:00:00+08:00", role="strategy")
print(result.to_dict())
```

输入绑定 1–10 份本地事件文件，每份至多 8 MiB / 1000 事件；单次事件最多 32 行。支持单证券、最多 31 个自然日的有界分钟回测及前 7 日以内已出现的历史标签，绝不补全历史。JSON 禁止重复键与非有限数，数字最多 12 位小数，不做舍入。价格和金额为精确十进制字符串，暂停/ST 为布尔；用 `Decimal(row["values"]["close"])` 消费价格。`money` 仅重命名为 `amount`，单位元；volume 为股，价格元/股。单位依据是源 API 字段惯例与固定源码请求，不是独立市场单位认证。

单日输入省略 `session_end_date`，仍产生完全兼容的 v1 产品身份。多日输入显式填写末日，例如 `session_end_date="2024-03-05"`，必须与固定源 identity 的 end_date 一致，产生 `ad.jq-sparse-observations.v2`。日期范围只绑定源运行边界，不证明范围内每天开市，也不补充无观察的日线。

原文件和脚本分别核对完整摘要，identity 只用来校验证券、日期和 raw/minute 设置。只导入 `current_data`、`market_bars`、`daily_market`、`call_auction_data` 白名单；每种行情结构严格校验，嵌入订单、成交等额外字段会被拒绝。订单/账户/决策事件仅检查顶层序号与运行身份，不投影、存储或用作数据值。`reference.json`、平台日志和成交标准答案不被导入器读取，脚本不执行。

相同证券/种类/源标签的相同行情合并，保留全部来源指针和最早观察时钟；任何值或状态冲突整体拒绝，不采用最后一次值。ID 是固定规范 JSON 的 SHA-256，包括原始行情事件及全部来源哈希；输入排列、工作目录和重新生成时间不改变身份。查询每次校验完整摘要，返回可独立修改的副本。可把整个 store 复制到别处离线重开，源文件无需继续存在。

### 查询角色与可见性

| role | kind | 用途和时钟 |
|---|---|---|
| `strategy` | current/minute/daily/auction | 只返回在 as_of 前实际观察到的源值 |
| `matching` | current/minute | 明确本地原生研究撮合读；同样遵守观察时钟 |
| `early_order` | current | 早盘状态和源限价观察；不构造撮合 bar |
| `opening_observation` | current/auction | 开盘报价/竞价观察；不是独立竞价撮合证明 |
| `final_valuation` | daily | 只在日线被观察后给出日终 OHLCV |
| `engine_capacity` | daily | 显式事后单笔容量模型，只给原始全日 volume；不含 OHLC/金额，不计算可成交数量 |
| `initialization` | daily | 仅 v2；显式指定一个早于 as_of 日期的 daily 午夜标签，只返回已经观察的 close 供前日估值 |

所有 as_of/start/end 必须含 UTC offset，转换到 Asia/Shanghai；as_of 必须属于数据已绑定的证券/源运行日期范围。`start/end` 是可选的源标签过滤，不代表连续区间覆盖。`current` 只匹配精确观察时点，绝不延续到下个未观察分钟；其他种类只返回实际存在的稀疏行。空结果为 `status=unavailable`，原因是 `NO_SOURCE_OBSERVATION` 或 `NOT_OBSERVED_AS_OF`，不是行情零值。

例如读取次日盘前持仓估值：`view.read("000001.XSHE", kind="daily", role="initialization", as_of="2024-03-05T09:00:00+08:00", start="2024-03-04T00:00:00+08:00", end="2024-03-04T00:00:00+08:00")`。返回只含 close，保留原日线标签和真实观察时钟，标记 `matching_eligible=false`、`strategy_visible=false`。这不是当日 pre_close 字段，不证明两个日期相邻开市，更不授权成交。

`source_label_at` 原样保留源标签；`observed_at/available_at` 是最早取得该条行情的 **JQ 历史回测模拟时钟**，不是 2016/2024 年真实发布或收包时间。`historical_pit=false` 始终保留，实际墙钟采集时刻未知。普通查询还过滤晚于 as_of 的证据指针。

例如 09:25 标签的竞价行若在 09:28 才观察，则 09:25 查询 unavailable、09:28 才返回；09:30 的 current-data 不会转成 09:30 分钟 bar。源 current-data 在盘前给出的 day_open 也只是源当时暴露的字段，不据此认证无前视。日线若在 15:30 观察，则 15:00 仍不可见。

导入器还拒绝 15:00 之前取得的 daily_market，避免把盘中未完成日线当作全日容量或最终估值；分钟标签必须整分钟、日线标签必须为源午夜日期标签。拒绝不符合的输入，不改写观察时间或补默认行。

容量角色是唯一允许盘中读取同会话日线全日量的例外，返回 `retrospective=true`、`strategy_visible=false` 及实际观察时钟；仅供明确的事后研究容量模型，不是策略信号、PIT 或真实市场容量。不得写回分钟 volume。角色是调用契约，不是进程内安全沙箱；AQ 应分离策略与引擎读取通道，不能将 owner 的 `describe/market_evidence` 管理接口暴露给策略。

### 状态、质量与初始化

源 `paused`、`is_st`、上下限都保留；并不升级为官方规则或历史交易资格。分钟本身没有 paused 时，只引用同一回测/同一观察时钟的 current-data 暂停证据，且不向前一交易日传播。停牌且零量的源填充值标 `quality=source_filled_suspended`、`executable=false`；任意零量行也不可执行。其他行 `executable=null` 表示未认证，不能读成交易许可。

`view.bootstrap(security, session_date)` 返回来源会话和实际观察时钟，并单列 `conditional_model`：CS、代码后缀交易所、CNY、100 股整手、0.01 tick、唯一日期及 09:00/15:30/23:59:59 生命周期回调，均为显式本地研究模型假设。没有用假日期填上市/退市事实，完整交易日历、前后开市邻日、公司行动、官方规则、历史资格保持 `null`。它足够让消费方选择有界单日初始化，不提供全市场证券池或历史资格认证。

`local_research_use=conditional_sparse_native_execution` 允许消费方在这些限制内读取以运行自己的原生研究引擎。AD 不执行交易、不输出目标成交数量，也不声明 AQ 已运行；`live_execution_permission=false`。未知生命周期若被消费方要求为已验证事实，必须明确阻断，不能默认补齐。

### 显式本地请求

请求参数包含 case_id、证券、单日日期、源路径及 events/jq.py 摘要，其他设置和目标结果不进入数据产品。输出 `delivery.json` 含 dataset_id、manifest、各会话 bootstrap、请求摘要与本机 store 路径。wheel/commit 身份由交付记录单独固定。测试使用显式 `source_kind=synthetic_fixture` 的协议样例，真实数据在包外验证。

稀疏观测不能推断完整会话、开盘行情或历史可见性。范围外的证券、标签、字段和用途分别返回缺项；读取成功不要求隐式扩采。

错误均为公开 `DataError`：`JQ_INPUT`、`JQ_SOURCE_HASH`、`JQ_SCHEMA`、`JQ_SCOPE`、`JQ_CONFLICT`、`JQ_VISIBILITY`、`JQ_ROLE`、`JQ_RANGE`、`JQ_INTEGRITY`，以及既有标识符/日期/时间错误。校验失败不产生数据产品；固定产品损坏后不回退重采。

## 完整请求观察

`Store.import_jq_queries` 导入摘要固定的本地事件文件，`Store.jq_queries` 只读重开。此接口覆盖已采集的有限查询，不连接 JQ，不实现完整 JQ SDK，也不承担 AQ 交易逻辑。行情和完整证券列表留在私有 store，不进入 Git 或安装包。

```python
from ashare_data import Store, JQObservationInput

store = Store.init("/private/query-store")
dataset_id = store.import_jq_queries([
    JQObservationInput(
        case_id="B25", security="000001.XSHE",
        session_date="2024-06-13", session_end_date="2024-06-14",
        events_path="/private/B25/events.jsonl", events_sha256="完整SHA256",
        script_path="/private/B25/jq.py", script_sha256="完整SHA256",
    ),
], source_kind="local_jq_backtest_observation")
view = store.jq_queries(dataset_id)
queries = view.describe()["queries"]  # owner 管理接口，不能暴露给策略
snapshot = view.read(queries[0]["query_id"], as_of="2024-06-14T15:30:00+08:00")
```

输入仍为 `JQObservationInput`，1–10 份，每份至多 8 MiB、1000 事件，运行范围最多 31 个自然日。查询结果最多 6000 行。原始文件与脚本都验 SHA-256；只读取明确的数据请求、数据响应、元数据、日历和采集时钟事件白名单。严格核对嵌套字段、请求响应关系、分块序号、总数与唯一键，拒绝缺块、冲突或夹带订单字段。脚本不执行；账户、订单、成交、题库标准答案不进入产品。

结果通过 `result.status` 区分 `observed_rows`、`observed_empty`、`observed_source_error`。价格、金额、因子保留源 JSON 的精确十进制字符串；查询产品允许至多 64 位精度，不将复权因子截到行情接口的 12 位。`money` 保留原字段名和元的源约定，volume 为股，价格为元/股。源 `dtypes` 存在则保留，未采集则为 null。NaN/Inf 只接受源的显式 `{"nonfinite":"nan"}` 等标签，不替换成零；空响应保持空列表，停牌填充由请求的 `fill_paused/skip_paused` 和源结果共同表达。所有查询结果 `matching_eligible=false`；行情撮合仍使用独立的稀疏行情接口。

同一历史标签的 raw/pre/post/factor 在不同观察时刻是不同快照，不去重成一套复权因子，也不重算价格。`adjustment_anchor` 是该快照的 JQ 观察日期，原始 `fq`、查询区间与所有字段完整保留，不认证为某个官方复权基准。

`view.query(api, parameters, as_of=..., observed_at=None)` 按**完整参数**匹配实际已观察的请求；返回该时钟下最新一致快照，或 unavailable。可用 `observed_at` 指定一个实际观察时钟。get_price 的 end_date、start_date 接受源格式日期/时间或带 offset 的时间；请求必须显式包含 security、fields、frequency、fq、end_date，并保留源请求里的 count、skip_paused、fill_paused 等字段。不会把参数不同的结果冒充同一请求。

get_price 的未来截止时钟与非法字段由 AD 自身代码分别抛出 `DataError("JQ_FUTURE_QUERY", ...)`、`DataError("JQ_QUERY_FIELD", ...)`，失败后可以继续正常查询。源异常只保留为 `source_error` 数据及 audit 证据，不抛出源异常文本来冒充 AD 行为。`read(query_id)` 是精确源快照读取，`query` 是带 AD 请求验证的入口。

`observed_at/available_at` 是 JQ 回测模拟时钟，读取不得早于观察时刻。C01–C05 若有 `capture_start/end.observed_wall_utc`，另存真实 `physical_capture_interval`，与模拟时钟严格区分；没有墙钟的源保持 null。`published_at`、revision_id 不因存在采集时钟而变为已知。所有产品 `historical_pit=false`；回读验证只证明源传递保真，不是独立市场事实或当时可得性认证。

C01 的完整性只指固定查询返回的 5108 行均齐全，不是对全市场证券域独立审计。每个元数据行另外标记未来 end_date 观测和 `end_date_is_historical_pit_fact=false`，保留 2200-01-01 等源值，禁止升级成当时已知退市事实。C03 2000 年起查询而首条为 2005 年，只说明实际返回范围，不能证明更早无数据。C04 指数成员按查询日期和观察日期保留；C05 是一次固定快照，不是持续更新或修订历史。

日历只提供源明确列出的 dates 和 `source_previous_date/previous_date_anchor`，没有列出的日期不自动判为休市。bootstrap 的交易日历/公司行动未知项不由此暗中改成已验证或无事件。

`describe()` 和 `source_evidence()` 属于 owner 管理/审核通道，包含整个固定产品的快照描述和源错误。AQ 应将它们与策略可见查询分开。市场值的公开接口不意味着把私有行情发布到互联网。

### 显式前日估值初始化

`view.read_initialization(query_id, security, valuation_date="2024-06-13", as_of="2024-06-14T09:00:00+08:00")`
仅从指定的 `get_price` 原始日线（`fq=None`）查询中选取精确午夜日期标签的 `close`。
必须在源观察时钟之后读取，日期必须早于 as_of 日期且相隔不超过 31 个自然日；
未观察、空响应或明确非有限值返回 unavailable，不改写为零。证券不一致、复权查询或分钟查询拒绝。

返回 `role=initialization`、`strategy_visible=false`、`matching_eligible=false`，
只含 close 并保留源标签、源证据及观察／实际采集时钟。接口不返回当日日线的其他字段，
不把前日 close 称为当日 pre_close 或当前报价，也不认证两个日期是相邻交易日。
日历事实仍须独立提供；这不改变源数据 `historical_pit=false` 的限制。

## 免费源当期增量

CMES 提供历史底库，免费接口采集当期数据并持续留存。各条目的标的、日期窗口、字段子集以验收表为准。第一批 198 行和第二批 19 行分别验收；不要求免费接口补齐 CMES 全历史。

### 已收到的东财成分页

完整板块采集仍要求所有分页完整、总数一致、证券身份唯一。连接在中途断开时，已收到的合法分页也可以独立保存和查询：

```sh
.venv/bin/python -m ashare_data.cli.free_updates manual-query \
  --snapshot /path/from/import-result --offset 0 --limit 100
```

这是离线导入已收到响应的入口，没有远端请求。收据必须含原请求 URL、`status=200`、`protocol=http`、实际带时区接收时间 `received_at`、原文 `bytes` 和 `sha256`。URL 必须与指定原生 BK 代码、页码及已验请求参数一致。

每页独立保存原文、收据、证券原生身份及实际采集时间，重复导入同一收据不新增。不同页和完整板块使用不同检查点，某页失败不清除另一页。查询 `coverage.complete_received_page` 仅表示该页完整；`complete_named_board=false` 明确未证明全板块同一时点覆盖。遗漏成员不能据此视为退选。原生市场路由码 0 不直接改写成深圳交易所。

### 第二批的每日新输入

`run_supplemental_updates.py --profile ... --execute` 的第二批采集按北京时间日期创建检查点。同日已成功输入复用，新日重新获取当期名单、题材、日线和去重后的成员行情。新名单先完成，再生成行情队列；失败的行情批次续跑，已成功证券不重抓。

第二批使用第一批的可转债资料、PE/PB、公司资料、分红配股、题材成分和美国国债数据。调度器要求这五组依赖在当前日完成，旧日成功状态不能满足新日依赖。数据来自对应目录下最新的已验证快照，不会永久停留在初始种子文件。源没有新发布时可以无变化，来源实际日期与接收时间分别保留。

`tests/test_supplemental_fresh_day.py` 以离线响应模拟周五到周一：19 项都读取新输入、新成员进入行情队列、新日行情失败不会被旧报价判成功、仅失败批次续采、同日重跑零新增。此测试不代表已观察真实跨交易日网络运行。

部署入口固定于已验证版本；修改工作树不改变已冻结的采集器。真实验收记录和运行版本存放在调用方的独立证据目录中，离线测试不会触发自然调度或写正式 CURRENT。
