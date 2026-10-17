# 历史市场状态、股票池与分钟证据

## 历史状态与股票池

`HistoricalMarketView` 是 `DailyExecutionView` 的离线证据伴随接口。它读取调用者固定
SHA256 的便携包，不联网、不写账本、不复权、不隐式合并旧数据包。支持
`.XSHG`、`.XSHE`，本接口另支持北交所 `.XBSE`；不改变旧接口的代码范围。

```python
from ashare_data import HistoricalMarketView

v = HistoricalMarketView("historical-package/binding.json",
                         expected_binding_sha256="调用者固定的64位SHA256")
row = v.state("300750.XSHE", "2020-08-24")
population = v.universe("2014-06-04", exclude_st=True, exclude_suspended=True)
master = v.instruments(["600087.XSHG"], on_date="2014-06-05")
events = v.security_events(["600518.XSHG"], start="2019-05-17", end="2019-05-23")
actions = v.corporate_actions(["000651.XSHE", "300750.XSHE"],
                             start="2022-01-01", end="2023-12-31")
```

`descriptor()`、`lineage()`、`coverage()` 返回范围、固定文件及字段缺口。返回值可由调用者
修改而不改变已加载视图；加载后不重新读取磁盘。外部固定哈希验证数据所有者版本，
不是交易所真实性签名。结构化公告摘录仍需按绑定原文和页码独立审核。

### 事实与时间

状态字段沿用 `{value, status, source_refs, historical_available_at, reason}` 形态，额外保留
原始值。`source_observed` 为取得提供方的历史源记录，`document_extracted` 为原文摘录，
`derived` 为明确的模型结果；`unavailable` 必须为 null；`not_applicable` 也为 null，
仅允许 `unlimited` 规则下的 `upper_limit/lower_limit` 使用。停牌、ST、参考价、主数据日期
不能以 N/A 代替未知；加载及消费入口分别校验。自然日网格缺行不推断休市、停牌或退市。
`require_fields` 检验已知性及源依赖一致性，不授予执行准入。

`visibility="posthoc"` 为默认事后读取，不接受知识时钟；`received` 要求 `knowledge_at`
不早于整包全部采用回执的接收完成时间；`verified` 一律报 `PIT_UNAVAILABLE`。
公告日期、目录时间标签、事件生效日期和本次采集时间分别保存，均不冒充历史首次可见。
数据不含当时的完整修订链，不使用今日证券名称、ST名单或当前存续状态回填历史。

源日状态不是盘中停复牌区间；`intraday_halts_verified=False`。所有价格为十进制字符串，
单位人民币元/股，`reference_price` 是档案 `preclose` 原价/除权日参考基准；不以昨收替换
除息参考价。没有生成前/后复权价或宣称与 JQ 复权等价。

### 限价和分钟消费者

`source_claimed_limits` 原样保存档案的 `pre_close/up_limit/down_limit`、来源及与规则模型
的比较。该档案作者披露过按 ST 状态补算及按行情高点修订的过程，因此它不是逐日交易所
直接下发的限价证明。本批在科创板 IPO 和创业板改革后样本中发现了错误的 10% 价带。
这些声明的 `execution_usable` 恒为 false；不得因字段有数值就直接采用。

公开 `upper_limit/lower_limit` 是限定板块与历史日期、源前收和 ST 依赖得到的模型值。
未知依赖保持未知；主板风险警示5%、普通主板10%、创业板/科创板20%、北交所30%
分别声明，不能把所有 ST 都套5%。IPO 无日幅度限制用 `not_applicable`，不伪造0或无穷。
科创板前5日通过固定上市日期、源交易日历和历史规则联结，不按周一至周五猜测。
创业板 2020-08-24 边界的证据组合推断、规则版本及例外未认证范围写在 curation 中。

`dependency_conflicts` 比较同日状态、同日历史股票池及有界公告事件，逐项保留双方原值与
来源。存在矛盾时上下限保持 `unavailable/null`，`require_fields` 拒绝且 `order_state`
报告 `STATE_DEPENDENCY_CONFLICT`。源限价原值继续保留并标为不可执行。
构建器只在明确 `st_policy="daily_status"` 时按一般主板 ST 依赖计算；`fixed_event`
以 `event_id/start/end` 绑定公告规则，不会因错误日源把 5% 静默改为 10%。未标明政策的
旧策展规则保持其声明比例。公告只有起始日且没有有界规则关联时，不把状态无限延长。

```python
check = v.order_state("300750.XSHE", as_of="2020-08-24T09:31:00+08:00", side="BUY",
                      accept_daily_replay=True, accept_limit_model=True)
if not check["ready"]:
    raise RuntimeError(check["issues"])
# 仅当前证券、当前日期、当前方向的日状态输入；保留整个check作结果来源。
upper = check["state"]["upper_limit"]
```

两个模型须显式接受；研究回放时钟为上海时间09:00。未知不能被接受模型豁免；停牌、
上市前或已记载摘牌日阻断。`ready=True` 的范围仅为 `one_order_daily_state_only`，
**未检查现金/股权到账、行动覆盖、盘中停复牌、排队、流动性或账户条件**，不是整个
分钟回测或模拟账户的准入。严格历史 PIT 与实时执行直接拒绝。此接口不向策略暴露未来
行情，但 `state/universe/events` 为离线引擎数据访问，调用者须实施自己的策略时钟隔离。

与旧日频包结合时，按证券/日期和字段来源显式协调；本包缺值不能覆盖旧包已取得的事实。
不得用新模型值悄悄改写旧的不可变 binding，也不得把此单笔日状态检查等同于
`DailyExecutionView` 的整窗/行动准入。

现金支付日期需要区分代派和公司自派；现金权益金额与除息价格扣减分别保留，不根据某一事件推广到账模型。

### 历史证券池和主数据

`universe` 读取当日历史源行；不会先与今日/当前主数据求交，因此保留后来退市的证券。
公开接口的1,000行截断通过固定提交、代码续页及 COUNT 精确核对。`source_query_complete`
只说明这份档案查询取齐，`market_complete=False`，要求全市场完整性时明确拒绝。
源零行返回 `coverage_state="unavailable"`，不等于当日没有上市证券。

可传 `candidates`；未出现的证券返回 `unknown`，不推断当时不存在。`filter_fields` 列出本次
启用的 ST/停牌筛选字段；只针对这些字段作判断，顺序如下：

| 启用字段的证据 | 返回 |
| --- | --- |
| 同证券同日的池源、日状态、有界公告或规则存在相反布尔声明 | `unknown`，reason=`filter_dependency_conflict`，保留双方原值和引用 |
| 无冲突，但任一所需池源字段未知或不是合法已知布尔事实 | `unknown`，reason=`filter_field_unknown`，列出缺失字段和原池源行 |
| 所需字段均已知且一致，至少一个为 true | `excluded` |
| 所需字段均已知且一致，均为 false | `rows` |

多个过滤器启用时，冲突/未知优先于排除；不能用另一字段的真值掩盖证据不足。
缺少额外日状态不等于相反声明，明确的池源布尔值仍可用于本有界来源筛选；池源未知则不由
其他来源静默回填。无日状态或日事实为 null 时，仍比较池源与有日期边界的公告。
停牌区间、复牌日和 ST 事件沿用已绑定的原始日期范围，不扩展到其他日期或证券。

两个过滤器均关闭时返回原始池源记录（含未知值），并通过 `dependency_conflicts` 展示已知
字段矛盾；开启其中一个过滤器也不会因另一未启用字段而改变入选结果。
非法 `not_applicable`/`unknown` 字段状态在加载时拒绝，ST/停牌的 N/A 不能变成 false；
合法的无限幅 IPO 价格 N/A 与这两个布尔筛选无关。以上仍是事后日来源筛选，不是执行准入。

主数据上市日期仅针对该代码所在交易所，北交所上市日期不冒充公司最早的新三板挂牌日期。
日期字段不足时保留 null；空退市日期不证明持续上市。日期可给出 `not_yet_listed` 或
`delisted` 的来源报告状态，但其他日期仍为 `unknown`，持续上市及交易资格未认证。
摘牌安排原文、档案的退出日期和事后实际执行回执的区别都保留在证券事件证据中。

### 分红送转

`cash_per_share` 为每旧股合资格股东的税前毛现金；`bonus_per_share` 为送股，
`transfer_per_share` 为资本公积转增，币种 CNY。null 与明确0分开。
格力2022事件毛现金 **1元/股** 与除息参考扣减 **0.936124元/股** 分列；JQ
0.9361/0.74888 的观察值不回填这些字段。无通用税率或现金账本假设。

宁德时代2023事件为每旧股现金2.52元、转增0.8股。登记日、除权日、支付日期、股份到账
日期和无限售新股上市日分别保存。支付日期仅适用于中登代派，自派现金日期未知；新股
上市日仅适用于无限售股份，限售股不能据此变为可卖。碎股分配规则仍来自发行人原文，
本接口不把个人持仓简单乘比例再任意取整。

`cash_available_at/share_credit_at` 均为 null；实际账户实现回执未取得。
`require_cash_time` 或 `require_share_credit_time` 可严格拒绝相关日内时点不足。
返回 `complete=False/absence_proved=False`，空事件列表只是未取得匹配事件，不证明无行动。
新接口没有激活旧 snapshot 中仍为 UNSUPPORTED 的通用复权/行动存储入口。

### 离线构建

```sh
python scripts/build_historical_market.py SOURCE_ROOT NEW_PACKAGE_ROOT
```

构建器验证原始 HTTP 回执、时钟、明确档案提交、精确 SQL、证券日期、重复、分页及计数。不会联网、重试或换源；已有目标目录拒绝覆盖。新包包含 scope、curation、原文和回执，由调用者固定 binding 摘要后重开。

实际证券、日期、事件数量及未证实字段以本次包的 coverage/lineage 为准。不能将历史固定样本的行数当作当前全市场覆盖；真实数据不进入源码或 wheel。

## 分钟证据

`MinuteEvidenceView(binding, expected_binding_sha256=...)` 只读固定来源包，复用历史市场的哈希、回执、路径、状态和限价校验。`seal_minute_evidence` 封存明确输入；原始响应、发行人证据及交付包由调用方另行保存。

公开查询包括 `state`、`bars`、`calendars`、`label_coverage`、`corporate_actions` 和 `admission_report`。先看字段 status、source_quality、缺失原因及 ready_for_execution；来源行本身不表示可交给引擎。

`visibility="posthoc"` 明示事后研究；received 要求 knowledge_at 不早于实际采集完成；verified 对未获证实的历史可见性拒绝。源分钟标签不推断 interval_start/end；未知边界时 `require_intervals=True` 拒绝。源量额单位声明与独立认证分开，源 preclose 不自动成为公司行动后的限价参考价。

状态未知、单位未确认、历史股票池/行动覆盖不足和时间边界未证实会保留在门禁报告。`export_backtest()` 当前仍拒绝 `MINUTE_GATES_BLOCKED`，软件测试通过不关闭这些事实缺口。旧一次性采集/续包脚本与验收计数不属于当前分发；当前运行结果需对新输入重新核验。
