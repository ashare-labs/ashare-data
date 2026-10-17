# 有限事实与显式条件研究

## 有限事实

`FiniteFacts` / `FiniteFactsView` 是特定证据清单和有限日期范围的严格只读模型，仍限制 600000.XSHG 及 2020-01-02～07；不是通用历史事实数据库。原件、价格和日历的来源 pin 是证据身份，不是新代码的验收结果。缺失的历史资格、相关事件完整性及复权等价不被推断补齐。

版本 0.10.0.dev1 使用功能名称和新的 owner/schema 身份。`Store.import_fact_component(directory)` 导入核对原件，`Store.compose_facts(price_dataset_id, component_id)` 生成本版本新 ID；旧数据目录与旧消费者继续使用原固定运行环境。不得覆盖旧包或变更原件再重算 pin 冒充原证据。

### Python契约

所有结果为 frozen dataclass/tuple；`to_dict()` 返回独立可序列化副本（Decimal为字符串、日期为ISO）。
原始字段保存在不可变JSON字符串及 `source_fields` 副本，来源含原路径、SHA、URL、定位、2026读取时刻，
历史available_at为空。查询默认**离线、事后研究**；本接口不提供策略时钟/PIT视图，不可直接作策略输入。

| 入口 | 返回/行为 |
|---|---|
| `Store.import_fact_component(directory)` | 显式校验并隔离封存固定官方包，返回facts_component_id；不执行包内脚本 |
| `Store.fact_component(component_id)` | `FiniteFacts`，`descriptor()`、`records()`、`evidence(path)`；证据只读字节 |
| `Store.compose_facts(price_dataset_id, facts_component_id)` | 固定两组件、review policy及backend plan的组合dataset_id；不提升原价格质量 |
| `Store.facts(dataset_id)` | 每次重开验证目录、manifest、全部对象及两组件关联，返回`FiniteFactsView` |
| `view.descriptor()/coverage()/lineage()/quality()` | typed范围、组件hash及质量；`complete=false`、PIT/finality未验证 |
| `view.instrument()` | 身份/上市日期、带推断标签的board candidate；historical_eligible=None |
| `view.rules()` | 普通A股竞价lot、tick、T+1及版本/窗口；正常限幅仅条件规则 |
| `view.calendar()` / `view.bars()` / `view.statuses()` | 有界typed源日历、四日原价OHLCV及源状态；不联网补数 |
| `view.source_prev_close(trade_date)` | 源preclose及原文证据；不名为调整后前收 |
| `view.known_events()/event_coverage()` | 五个已知事件与有限检索范围；不能把列表当完整事件集 |
| `view.limit_candidates()/prev_close_candidates()` | 显式conditional候选，`engine_value=None`、`eligible_for_engine=false` |
| `view.price_limits(trade_date)` / `view.corporate_actions()` | 本版抛出 `FACTS_FACT_UNAVAILABLE`；不返回伪造空事件或限价 |
| `view.adjusted_prev_close(**call)` | 先核对完整四种AD08调用tuple；未审tuple报`FACTS_UNREVIEWED_CALL`，已审仍报`FACTS_FACT_UNAVAILABLE` |
| `view.admission()/owner_receipt()` | 17项逐字段结论，11必需项中7支持4未知；回执兼容后端提议的envelope |
| `view.require_fact(key)` / `view.require_execution(plan_sha256=...)` | 严格字段/执行闸；执行始终BLOCKED，不存在force或allow_unknown |
| `Store.facts_snapshots()/recover_facts()` | 固定组件与组合目录；显式恢复prepared，缺失/损坏引用则aborted |

AD08完整参数：`security, trade_date, history_dt, adjust_orig, frequency='1d', field='close',
bar_count=1, include_now=False, skip_suspended=False, adjustment_requested='pre'`。
四个 `(trade_date, history_dt, adjust_orig)` 分别为 `(01-02,12-31,01-02)`、
`(01-03,01-02,01-03)`、`(01-06,01-03,01-06)`、`(01-07,01-06,01-07)`（2019-12-31除外均2020）。
不能把RQ的`get_prev_close(adjust_type='none')`当raw调用；固定RQ实现内部仍请求pre。

### CLI和存储

`--store DIR facts-import-facts PACKAGE`、`facts-compose --price ID --facts ID`、
`facts-query DATASET --dataset ID`、`facts-snapshots`、`facts-recover`。
`facts-query`选择descriptor/instrument/rules/bars/calendar/statuses/events/event-coverage/
limit-candidates/prev-close-candidates/coverage/lineage/quality/admission/owner-receipt。
admission返回诊断并exit 2；其他只读诊断成功exit 0。未知/缺对象/越界/不支持皆为稳定DataError JSON。

原件进入content-addressed对象；SQLite只记不可变manifest和prepared/published/aborted日志。
同一Store共用单写者锁，发布仅在全部引用验证后可见。中断不自动恢复，不覆盖旧对象。
manifest只含确定的内容和版本标识，不含本机导入路径或时间。证据包内原始路径仅作来源文本保留，
永不据此读取外部文件。导入只接受此审阅包的精确manifest hash；自行改facts并重算hash不构成owner采纳。

普通Python对象不是防恶意同进程执行的沙箱；源码/安装环境及存储可信边界须由调用端保护。
消费者应从`Store.facts(ID)`公开接口取得回执，不能信任调用者粘贴的JSON。
owner 读取接口的存在不证明消费者已完成执行验证。

## 条件投影

`ConditionalView` 在严格有限事实之上标记假设，不改变底层事实、历史可见性或执行权限。当前模型仍限 600000.XSHG 的固定 2020 年四日读取范围。A-EQ、A-NORMAL、A-AD08 必须显式接受；未知停牌/ST、已知相关权益事件、规则/价格矛盾均阻断。

策略资源 `research/conditional-profile.json` 的新哈希进入新组合身份。输入必须是本版本核验的 `FiniteFactsView`；其原件和价格仍由有限事实政策核对。新代码不复用旧组合 ID，不把旧 backend profile 的历史引用当作本次执行许可。

| 公共接口 | 行为 |
|---|---|
| `Store.compose_conditional(strict_dataset_id, *, mode, assumption_ids)` | mode必须显式为conditional_research，三个假设必须齐全且唯一；返回新的组合ID |
| `Store.conditional(ID, *, mode)` | 仅显式conditional_research可重开；验证全部嵌套清单和输入，再验证状态/事件停止条件 |
| `view.descriptor()/profile()/admission()/owner_receipt()` | typed身份、完整profile、投影权限/执行拒绝及依赖回执；无严格“全通过”回执 |
| `view.instrument()/rules()/bars()/calendar()/statuses()` | 原事实和规则；instrument返回历史资格假设标签，不改原instrument事实 |
| `view.modeled_events()` | `assumed_no_relevant_events`的模型空tuple，绑定A-EQ；完整经济事件集仍unknown |
| `view.price_limits(date)` | 带A-EQ/A-NORMAL依赖的typed模型上下限，使用原source preclose、Decimal四舍五入 |
| `view.adjusted_prev_close(**call)` | 仅四个精确tuple，返回带A-EQ/A-AD08标签和来源的有限值；其余拒绝 |
| `view.require_execution()` | 始终CONDITIONAL_REVIEW_REQUIRED；独审前没有放行开关 |
| `Store.conditional_snapshots()/recover_conditional()` | 独立发布日志/显式恢复；底层仍共用同一Store单写者锁 |

所有投影可`.to_dict()`；不是策略时钟视图，不可向预定策略暴露当天执行数据。策略意图隔离与原生
停牌/ST、手数、tick、T+1、限幅及原计划的日量参与限制由后端实施和独审；owner回执列明required_native_checks，
不会以“模型限值内”替代原生检查或强制成交。

CONDITIONAL的日期参数接受严格`datetime.date`或等价`YYYY-MM-DD`字符串，不接受datetime隐式截日。
这样A宽`AD08Call.model_dump()`的date可原样传递；JSON CLI使用ISO日期。证券、频率、字段、
bar_count、两个bool及adjustment_requested均精确比较，不推导日期或把none改成pre。
严格FACTS已冻结接口仍使用ISO字符串且缺事实时报错；codec可显式用`model_dump(mode="json")`。
required_native_checks是要求，不能直接复制为后端“检查已启用/已通过”的证据。

CLI：`conditional-compose --strict ID --mode conditional_research --assumption A-EQ --assumption A-NORMAL --assumption A-AD08`；
`conditional-query DATASET --dataset ID --mode conditional_research`；`conditional-snapshots`、`conditional-recover`。
price-limits需`--date`；adjusted-prev-close需`--call`完整tuple JSON。admission诊断exit 2（执行仍拒绝）。

**停止条件**：源码/日期/价基/hash或身份冲突；任何四日停牌、ST、未知状态；已知事件登记或
调整生效落在资格/价基区间；tuple越界；profile/假设缺失。新矛盾材料必须生成新审阅版本，
不能修改固定包后重算hash授权自己。此离线组件不监控未来新增公告，不能声称自动发现外部新事件。
实际订单未按计划成交或后端违反原生检查时，后端停止本profile并记录失败，不对齐目标现金强制成交。

原FACTS接口返回的events、candidate、verified_absent和complete不变；CONDITIONAL派生结果另命名、另存版本。
用户批准假设不等于独立实现审阅已通过；本库不运行账户或引擎。

## 条件上市

`ConditionalListingView` 对条件模型输出有限挂牌谓词和严格前进的后继日期，绝对退市日仍为 unknown。`ConditionalView.listing_contract()` 返回本版本策略哈希；`listing(contract_sha256=...)` 要求调用方显式锁定它。来源事实、允许日期与 consumer context 每次重验；不扩张为全市场生命周期。

### 公共接口

```python
conditional = Store(root).conditional(conditional_dataset_id, mode="conditional_research")
contract = conditional.listing_contract()  # 供冻结、独审
listing = conditional.listing(contract_sha256=PINNED_LISTING_CONTRACT_SHA256)
value = listing.status(
    security="600000.XSHG", current_date="2020-01-03", target_date="2020-01-03",
    role="current", context_sha256=FRESH_CALLER_CONTEXT_SHA256,
)
following = listing.successor(
    security="600000.XSHG", current_date="2020-01-03", candidate_date="2020-01-06",
    n=1, context_sha256=FRESH_CALLER_CONTEXT_SHA256,
)
```

| 方法 | 返回/行为 |
|---|---|
| conditional.listing_contract() | ConditionalListingContract，独立契约身份、读域、事件日、允许请求、完整策略及依赖 |
| conditional.listing(contract_sha256=...) | 显式固定身份后得到 ConditionalListingView；错误 pin 拒绝 |
| listing.descriptor() | 重新校验并读取契约 |
| listing.status(*, security, current_date, target_date, role, context_sha256) | 校验双日期、用途、调用方上下文摘要，返回 ConditionalListingProjection |
| listing.successor(*, security, current_date, candidate_date, context_sha256, n=1) | 验证实际引擎候选后继；必须等于 owner next_open 且严格晚于当前日，不修正、不补数 |
| listing.revalidate(projection, *, security, current_date, target_date, role, context_sha256) | 用最新请求重查并比对全部字段，返回新投影；拒绝缓存漂移和序列化 dict |
| listing.require_execution() | 始终 CONDITIONAL_LISTING_REVIEW_REQUIRED；无 force/allow_unknown |

只允许以下五组请求：

| role | current_date | target_date |
|---|---|---|
| warmup | 2020-01-02 | 2020-01-02 |
| current | 2020-01-03 | 2020-01-03 |
| current | 2020-01-06 | 2020-01-06 |
| settlement_successor | 2020-01-03 | 2020-01-06 |
| settlement_successor | 2020-01-06 | 2020-01-07 |

Jan-07 只有后继读角色，不是新增执行日。Jan-02 暖启动也不能推进成交事件。
12-31 仅属于原 AD08 价基依赖；周末、Jan-08、其他证券拒绝。日期接受精确 date 或规范 ISO
字符串；拒绝所有 datetime（含时区），不隐式截日。后端按其冻结模型时钟契约显式转换，
将原时刻/时区纳入上下文并检查跨日折算。

frozen 返回保留 A-NORMAL、derived_under_declared_assumptions、原证据等级及 manifest/profile/
策略/源日历引用；原绝对日期 unknown，**没有引擎 sentinel 字段**。to_dict() 为独立副本。
每次查询/缓存复核重开固定 CONDITIONAL，验证全部嵌套对象及原状态/权益停止条件。
新资格事实、已退市/未挂牌/特别状态或冲突应退出当前 profile 并另审。离线组件不会监控外部公告，
不能发现未交给它的新证据。原 instrument/price_limits/event_absence/adjusted_prev_close 不升级。

### 错误与后继边界

稳定 DataError：CONDITIONAL_LISTING_BINDING、POLICY_INTEGRITY、SECURITY、DATE、SCOPE、ROLE、CONTEXT、
CALENDAR、SUCCESSOR、CONFLICT、CACHE_MISMATCH、REVIEW_REQUIRED（全部带 CONDITIONAL_LISTING_ 前缀）。
原 CONDITIONAL/FACTS 完整性、源状态/事件错误原样传播。

RQ 末端 next(Jan-07)=Jan-07，本接口因 next_open=null/非严格后继拒绝；next(Jan-08)=Jan-07，
因当前日越界拒绝。不能只检查返回日落在四日范围内。n 仅接受精确 int 1，True/1.0/其他值拒绝。

### 消费者责任

context_sha256 是**调用方声明的摘要，不是 owner 验证的运行许可**。固定算法应至少纳入新 owner
契约/wheel、adapter/RQ/config/run 身份、实际 phase、完整模型时刻、event cursor 和恢复 generation。
数据方不控制这些运行状态；若后端重放旧摘要和旧日期，owner 无法知道实际引擎已经前进。
必须从受控运行入口获得新上下文，不能从缓存结果抄回。

实际事件推进、getter/谓词、最终缓存消费及恢复入口都检查当前日与目标日。零仓位、空订阅及
缺失/额外订阅路径也必须检查，不依赖原生短路触发 owner 查询。缓存标量不是权限；消费时
revalidate 或丢弃重查。恢复时重建公开查询绑定与对象，不反序列化查询对象或静默续跑旧语义检查点。Python 对象不是对抗恶意同进程代码的安全沙箱。

清订阅如必须用日期，可在**后端受控内部**采用 Jan-08 00:00 的 bounded_horizon_marker，
仅编码“对已批准时点 t，S<=t 与 model_delisted(t) 同为 False”。它不是市场退市日期、预测或上界。
普通日期离开 getter 后无法自带有效域，必须完成上述事件/查询/缓存/恢复 guards。
记录 engine_projection_kind=bounded_horizon_marker 与适配版本；不在一般 API、repr、事实日志、
all_instruments、benchmark、因子过滤或外部 API 输出它。不改 RQ 内核、不删清理 listener、不靠
取消订阅规避、不构造伪装 datetime 的通用对象。listed_at/active_at/de_listed_at 共用有界投影，
不能遗留默认无界 listed_at。

消费者必须固定库版本、数据版本与使用声明；该投影不负责交易执行、恢复账户或认证原生调用。
