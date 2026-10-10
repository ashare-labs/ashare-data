# 本地 JQ 稀疏行情观察：0.9.1.dev2 本地研究能力

仅供有界本地研究。此版本以已发布 v0.9.0 为基线，迁入原 0.8.8.dev1 的六题数据接口；既有 M2、研究 preflight 与发布标签保持不变，不继承其执行准入。本轮没有联网采集、JQ 重跑、上传、推送或发版。公开 API 是可调用的软件接口；真实行情仍是用户本地私有文件，不在仓库或 wheel 中。

## 生成与固定重开

```python
from ashare_data import JQObservationInput, Store

store = Store.init("/private/new-store")
dataset_id = store.import_jq_observations([
    JQObservationInput(
        case_id="B09", security="000001.XSHE", session_date="2024-03-04",
        events_path="/private/B09/events.jsonl", events_sha256="完整SHA256",
        script_path="/private/B09/jq.py", script_sha256="完整SHA256",
    ),
], source_kind="local_jq_backtest_observation")

view = Store("/private/new-store").jq_observations(dataset_id)
manifest = view.describe()
result = view.read("000001.XSHE", kind="current",
                   as_of="2024-03-04T10:00:00+08:00", role="strategy")
print(result.to_dict())
```

输入绑定 1–10 份本地事件文件，每份至多 8 MiB / 1000 事件；单次事件最多 32 行。支持单证券、最多 31 个自然日的有界分钟回测及前 7 日以内已出现的历史标签，绝不补全历史。JSON 禁止重复键与非有限数，数字最多 12 位小数，不做舍入。价格和金额为精确十进制字符串，暂停/ST 为布尔；用 `Decimal(row["values"]["close"])` 消费价格。`money` 仅重命名为 `amount`，单位元；volume 为股，价格元/股。单位依据是源 API 字段惯例与固定源码请求，不是独立市场单位认证。

单日输入省略 `session_end_date`，仍产生完全兼容的 v1 产品身份。多日输入显式填写末日，例如 `session_end_date="2024-03-05"`，必须与固定源 identity 的 end_date 一致，产生 `ad.jq-sparse-observations.v2`。日期范围只绑定源运行边界，不证明范围内每天开市，也不补充无观察的日线。

原文件和脚本分别核对完整摘要，identity 只用来校验证券、日期和 raw/minute 设置。只导入 `current_data`、`market_bars`、`daily_market`、`call_auction_data` 白名单；每种行情结构严格校验，嵌入订单、成交等额外字段会被拒绝。订单/账户/决策事件仅检查顶层序号与运行身份，不投影、存储或用作数据值。`reference.json`、平台日志和成交标准答案不被导入器读取，脚本不执行。

相同证券/种类/源标签的相同行情合并，保留全部来源指针和最早观察时钟；任何值或状态冲突整体拒绝，不采用最后一次值。ID 是固定规范 JSON 的 SHA-256，包括原始行情事件及全部来源哈希；输入排列、工作目录和重新生成时间不改变身份。查询每次校验完整摘要，返回可独立修改的副本。可把整个 store 复制到别处离线重开，源文件无需继续存在。

## 查询角色与可见性

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

## 状态、质量与初始化

源 `paused`、`is_st`、上下限都保留；并不升级为官方规则或历史交易资格。分钟本身没有 paused 时，只引用同一回测/同一观察时钟的 current-data 暂停证据，且不向前一交易日传播。停牌且零量的源填充值标 `quality=source_filled_suspended`、`executable=false`；任意零量行也不可执行。其他行 `executable=null` 表示未认证，不能读成交易许可。

`view.bootstrap(security, session_date)` 返回来源会话和实际观察时钟，并单列 `conditional_model`：CS、代码后缀交易所、CNY、100 股整手、0.01 tick、唯一日期及 09:00/15:30/23:59:59 生命周期回调，均为显式本地研究模型假设。没有用假日期填上市/退市事实，完整交易日历、前后开市邻日、公司行动、官方规则、历史资格保持 `null`。它足够让消费方选择有界单日初始化，不提供全市场证券池或历史资格认证。

`local_research_use=conditional_sparse_native_execution` 允许消费方在这些限制内读取以运行自己的原生研究引擎。AD 不执行交易、不输出目标成交数量，也不声明 AQ 已运行；`live_execution_permission=false`。未知生命周期若被消费方要求为已验证事实，必须明确阻断，不能默认补齐。

## 六题复现与缺口

```sh
python examples/jq_observations.py \
  --request /private/aq/docs/execution-six/data-request.json \
  --store /private/new-store --output /private/delivery
```

示例仅使用请求里的 case_id、证券、单日日期、源路径及 events/jq.py 摘要，其他设置和目标结果不进入数据产品。输出 `delivery.json` 含 dataset_id、manifest、各会话 bootstrap、请求摘要与本机 store 路径。wheel/commit 身份由交付记录单独固定。测试使用显式 `source_kind=synthetic_fixture` 的协议样例，真实数据在包外验证。

本次请求允许稀疏观测和有界初始化假设。既有真实材料的主要不足是：09:00 没有行情；09:30 没有分钟 bar；11 个主证券和 5 个停牌证券分钟标签不构成完整会话；前日尾分钟首次在次日 09:20 观察，不能回标成前日实时可见；竞价没有排队/逐笔撮合账簿；完整日历/上市事实未采集。无需为这些未请求的完整性证明扩采或制造行情。需要超出上述范围时，按证券、标签、字段和用途另报缺项。

错误均为公开 `DataError`：`JQ_INPUT`、`JQ_SOURCE_HASH`、`JQ_SCHEMA`、`JQ_SCOPE`、`JQ_CONFLICT`、`JQ_VISIBILITY`、`JQ_ROLE`、`JQ_RANGE`、`JQ_INTEGRITY`，以及既有标识符/日期/时间错误。校验失败不产生数据产品；固定产品损坏后不回退重采。
