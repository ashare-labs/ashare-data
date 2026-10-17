# 固定日频执行输入与用途门禁

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


## 按实际用途检查执行字段

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


## 交付引用验证

`scripts/execution_delivery_refs.py` 核对显式本地交付根目录中的机器交接、消费契约、链接和 JSON 指针，检查事件身份与缺口明细。指针必须以完整源数组为基准，不能使用窗口过滤后的列表下标。该工具只属于开发交付校验，不进入运行 wheel。

旧批次的修订/订单验收脚本已退出发布树。需要新交付时生成新目录、新绑定和本次验证结果，不覆盖已冻结材料。
