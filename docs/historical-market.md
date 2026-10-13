# 有界历史状态、证券池及公司行动

`HistoricalMarketView` 是 `DailyExecutionView` 的离线证据伴随接口。它读取调用者固定
SHA256 的便携包，不联网、不写账本、不复权、不隐式合并旧数据包。支持
`.XSHG`、`.XSHE`，本接口另支持北交所 `.XBSE`；不改变旧接口的代码范围。

冻结提交 `720ff0a4c9ed0c73870b76240a3692e8d484d838` 的完整历史与分钟诊断链已完成
独立有界验收，F1～F4 及五组原生空报文问题均已闭合；[本地整合记录](historical-minute-integration.json)
绑定来源、独审与组合验证。旧 232 证券日／696 字段缺口和严格 PIT 未证实状态保持不变。

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

## 事实与时间

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

## 限价和分钟消费者

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

独审修复包的格力 `payment_date=2022-04-08` 仅适用于中登深圳代派现金；公司自行派发
持有人不共享该日期，`company_self_payment_date=None` 明确保留未知。每旧合资格股毛现金
1 元及公告除息扣减 0.936124 元保持原证据值。修复配方
`scripts/repair_historical_review_batch.py SOURCE_ROOT NEW_STAGING NEW_DESTINATION`
仅改已冻结批次的 curation 并生成新 binding；原包及全部原始回执不变。

## 历史证券池和主数据

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

## 分红送转

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

## 离线构建和本批范围

```sh
PYTHONPATH=src python scripts/build_historical_market.py SOURCE_ROOT NEW_PACKAGE_ROOT
PYTHONPATH=src python scripts/verify_historical_market.py NEW_PACKAGE_ROOT/binding.json --sha256 SHA
```

构建器验证原始HTTP回执、时钟、固定档案提交、精确SQL、证券日期、重复、分页及计数。
没有重试/换源/联网。已有目标目录直接拒绝覆盖；输出源包包含 scope、curation、原文和
回执，可作为下一次同字节重建的 SOURCE_ROOT。目录标签和PDF落款冲突保留原值。

本批为10证券、9有界面板（保留初选北交所日期并追加经原文确认的实际IPO窗口）、
412个自然日网格状态、5个历史池日期、2项分红送转和4项证券事件。
历史池取齐2,505/3,607/3,882/3,885条源记录，共13,879条；2024日期为源零行。
32个证券日取得日状态，17个源限价声明与适用模型冲突。原2024沪股232证券日、
696个ST/上下限字段缺口仍未解决，不以新包替换原失败样本；北交所日状态/前收仍不足。
具体数字属于本次固定证据包，不能推广至其他包或全市场。真实数据不进入源码仓或wheel。
