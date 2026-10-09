> 0.8.0.dev1 保留本文消费类型与角色约束，新增可移植来源与组合流程见 [生产契约](portable-m2/contract.md)。
> 下文74文件固定输入描述仅适用于旧v1导入；新v2产品由公共组件生成，身份动态计算，无旧清单白名单。

# M2 公开契约 v1（0.7.0.dev1，候选待独审）

这是日频离线条件研究数据产品；不是原生执行器。仅600000.XSHG、m2a/w1/w2，显式固定版本和四项ack。
严格D1、旧BR1、received/verified、分钟/paper不扩权；没有网络fallback。独立新分支基于281908，稳定目录不修改。

## 入口与返回

| Python入口 | 返回与限制 |
|---|---|
| `Store.import_m2(directory)` | 固定74文件来源包的不可变dataset_id；核原文闭包、wire/SDK、源日历、事实/规则/事件；不联网；重复幂等 |
| `Store.m2_profiles(dataset_id)` | tuple[M2Document]：两份已实现候选profile名称及精确SHA，供调用方显式选择；不选latest |
| `Store.m2(dataset_id, *, profile_sha256, mode)` | M2View；mode只接受明确`conditional_research`，没有默认opt-in |
| `view.descriptor()/profile()/quality()/lineage()/capabilities()` | 不可变M2Document，`to_dict()`返回隔离副本；原质量和扩窗证据引用可检查 |
| `view.windows()` | tuple[M2Document]，每窗完整geometry、源P/next、所有读角色、owner pin、spec/plan/query SHA |
| `view.assumptions(window_id)` | 按固定顺序返回四个完整AssumptionRef；每项有ID/version/content_sha256/window_id |
| `view.use(window_id, *, assumption_ack, consumer_binding)` | M2UseView；只接受精确有序四项ack与此owner/选中窗口/plan的M2ConsumerBinding |
| `use.envelope()` | 有稳定envelope_sha256的候选数据使用回执；backend_execution_authorized=false；external_product_review=PENDING |
| `use.context(**fields)` | 与固定consumer声明合并生成M2ReadContext；aware时间统一UTC六微秒；每次query再核时序/读域 |
| `use.decision_prev_close(context, *, call)` | M2Read，`.value`是Decimal；仅P raw close×有限ratio1，P行引用；仅strategy/DECIDE |
| `use.execution_bar(context)` | M2Read；T15的engine raw OHLC、volume整数股、amount原值或null；raw源字段放sources |
| `use.valuation_bar(context)` | M2Read；AFTER_TRADING/SETTLEMENT当前T>=15估值；不开放terminal价格 |
| `use.trading_state(context)` | M2Read；SUBMIT_MATCH的源停牌/ST、source preclose、owner限价、tick/lot/T+1、纯嵌套listing |
| `use.listing(context, *, target_date, role)` | M2Read；仅WindowPlan列明tuple；绝对退市日null/unknown，不拿bar存在证明挂牌 |
| `use.successor(context, *, candidate_date, n=1)` | M2Read；n严格int1、源next严格前进；同日/倒退/周末/越界拒绝，不修正RQ回退 |
| `use.modeled_actions(context)` | M2Read；有限假设下模型空事件、已知事件ID和域；verified_absent=false，已知反证拒绝 |
| `use.verify(read, *, context)` | 重新构造全部query/result/sources/Evidence/Auth并逐项比较；返回完整匹配M2Read |
| `use.revalidate(read, *, context)` | **只返回新的M2Document授权**；原四个稳定部分必须不变。用`read.with_authorization(auth)`组合后再verify |
| `use.require_execution()` | 恒抛M2_BACKEND_ADMISSION_REQUIRED |
| `Store.m2_snapshots()/recover_m2()` | 本地发布目录/显式prepared恢复；这是数据发布恢复，不是原生交易恢复 |

`M2Document.data`为不可变规范bytes；`sha256`是完整文档摘要。带内部身份字段的对象须使用其指定字段：
例如WindowPlan的`window_plan_sha256`、Evidence的`evidence_sha256`，这些摘要排除自身，不等于整个文档`.sha256`。
OwnerPin不带自摘要，所以`view.descriptor().sha256`就是owner_pin_sha256。

## 身份与命名，供A宽明确映射

OwnerPin字段：`version/runtime_code_sha256/dataset_id/manifest_sha256/profile_sha256/query_contract_sha256/parser_sha256/rule_policy_sha256`。
`runtime_code_sha256`覆盖安装包所有.py/.json的相对名与原始字节hash。源码与wheel必须相同。
commit/tree/wheel SHA在**外部release receipt**中绑定OwnerPin；它们不回填包内参与自hash。用户/后端据固定release receipt及独审核验安装身份。

Owner window ID是`m2a/w1/w2`字符串，不是后端WindowPlan摘要。ConsumerBinding字段：
`owner_pin_sha256/window_id/window_plan_sha256/run_binding_sha256/run_id/consumer_request_sha256/strategy_version_id/assumption_ack_bundle_sha256`。
`run_binding_sha256`为后端固定DataBinding声明，`run_id`另行区分实际session；不能以相同request代替run身份。
owner核声明一致性，后端负责实际request→策略source/规范参数、selected window、七项ack及独审/policy关联。

ReadContext在ConsumerBinding之上增加：`current_date/logical_at/phase/query_end/visibility/consumer/event_cursor/restore_generation/epoch`，
以及计算所得`context_sha256`。wire解析必须验证它；`consumer`只接受strategy/engine，visibility只接受assumed。
owner不认证宿主时钟诚实，也不把这些调用方声明当密码学签名或执行权限。

稳定与变化字段：

| 对象 | 稳定/变化范围 |
|---|---|
| Query / EconomicResult / Sources | 固定operation/T/P/role/call、经济值和单位、原文/行引用；同用途缓存不允许来源行漂移，即使值相同 |
| ReadEvidence | 固定owner/data/profile/query/parser/rule、选中WindowPlan、完整query/result/source摘要、角色/日期、质量、四项假设引用；**不含run/epoch/generation/context/native ID** |
| ReadAuthorization | 引用稳定Evidence，包含完整当前context和fresh_read/cache_revalidation类型；run/request/strategy/ack声明不能偷偷换；epoch/generation/cursor改变则新hash |
| Backend native observation | 后端另行记录实际调用链/首读事实、原返回表示及策略消费；绝不放回owner hash。本库codec报告native_call_observed=false |

`use`绑定的是本次selected window。别窗即使属于同一目录且T/P相同，也在读取原文前拒绝；别窗Evidence/Auth不可复用。
同一源物理行可复用，但需要重新取得对应窗口的合法证据。已提交09决策的历史回执留在checkpoint，不换成新读；
未提交重放/缓存消费取得当前授权，来源/Evidence不能改变。09/15两阶段原子持久化和账户归属由后端负责。

## R1/R4：规范编码与摘要输入域

公开`ashare_data.m2_types`的`canonical_bytes/canonical_hash/decimal_text/aware_time/exact_int`，以及
`m2_payloads.query_payload/economic_payload/query_contract`是唯一owner codec，不使用Pydantic默认dump，也不借后端digest。
规范：UTF8，键排序，紧凑分隔，禁止NaN/Inf/重复JSON键；datetime先转UTC，固定六微秒Z；Decimal无指数/无多余尾零。
JSON时间/价格字符串须先经过相应typed codec；generic canonical不会猜普通字符串的含义。
整数参数严格`type(x) is int`，bool/float/Decimal/数字字符串均拒绝。源volume原字符串在source parser核整数字符串后转股数；这不同于调用参数隐式转型。

Query严格字段：`schema/operation/security/current_date/target_date/role/call/n`。schema=`m2.query.v1`。
call仅decision_prev_close非null且为完整AD08；n仅successor非null且为int1。其余多余参数拒绝。

EconomicResult公共字段：`schema/operation/security/current_date/target_date/role`，schema=`m2.result.v1`。
各operation的**完整额外字段白名单**由`query_contract()['result_fields']`机器可读发布：

| operation | 额外经济字段 |
|---|---|
| decision_prev_close | value/unit/value_basis/scoped_ratio |
| execution_bar/valuation_bar | open/high/low/close/volume/amount/price_basis/price_unit/volume_unit/amount_unit/bar_start/bar_end |
| listing/successor | model_listed/model_delisted/absolute_delisting_date/absolute_date_status/evidence_status |
| trading_state | suspended/is_st/state_evidence_status/lower_limit/upper_limit/source_preclose/price_tick/buy_round_lot/resale_rule/formula_version/rule_policy_sha256/listing |
| modeled_actions | modeled_events/known_event_ids/coverage_evidence_sha256/verified_absent/source_completeness/status/price_basis_domain/registration_domain |

嵌套listing同为完整纯listing结果，禁止任何层级receipt/evidence/auth/context；两个domain只能start/end，事件列表本profile必须空。
Sources另含源对象/行hash与engine原字段，**不进result_sha256**，单独以source_references_sha256进入Evidence。
原始source bytes/hash从不改编码；规范经济价格相同不表示原始来源相同。

计算顺序：Query/Result/Sources各自规范hash → Evidence排除自身`evidence_sha256` → Context排除自身`context_sha256`
→ Authorization引用Evidence，排除自身`authorization_sha256` → M2Read运输对象。
WindowSpec先固定，profile引用Spec；OwnerPin后生成；WindowPlan引用OwnerPin/Spec/query，排除自己的window_plan_sha256；
Envelope引用Plan/Owner，排除自己的envelope_sha256。外部独审、后端DataBinding不反向进入owner对象。
黄金向量由`examples/m2_demo.py`生成，含嵌套listing、两个generation授权、时区和十进制等价向量。

## R2/R3：范围和事实假设

| 窗口（2020） | 执行日 | 暖启动 | 终后继 | 价基域 | 保守登记域 |
|---|---|---|---|---|---|
| m2a | 01-03/06 | 01-02 | 01-07 | 01-02～06 | 01-03～07 |
| w1 | 01-03～16，10日 | 01-02 | 01-17 | 01-02～16 | 01-03～17 |
| w2 | 01-06～17，10日 | 01-03 | 01-20 | 01-03～17 | 01-06～20 |

逐T的P/next源自包含闭市日的真实源日历。补字段9行与OHLCV9行分别封存，保留各自捕获身份，不称原子快照。
来源包绑定原官方manifest、相关facts/events及其原文、两份01-02已封存公告、规则原文、独审报告和新expansion-annex。
01-20具体修订仅新增商品期货ETF当日回转，不将普通A股变T+0；w2该日只用于后继挂牌。事实原件不改旧application_window，扩窗通过独立附录与新假设声明。

A-EQ/A-NORMAL/A-AD08/A-VIS各为`m2.a-*.v1`，每个窗口内容hash独立。旧7月事件改为本次有限建模域外，
不假定已退出。已知登记进入价基/可能持有/后继域，即使支付在域外仍拒绝；已知事件关键日期未知也拒绝。
初始零仓/旧权益、期末可持股；不推演期后无限权益。不消费amount，不用close×volume伪造。

| phase | consumer/角色和查询边界 |
|---|---|
| INITIALIZE | 首个T且09前，engine只查warmup/current挂牌和固定权益；query_end=P日23:59:59.999999+08 |
| DECIDE | T09，strategy仅P close；engine可查声明的有界挂牌/权益，不能取T行情；query_end=P日末 |
| SUBMIT_MATCH | T15，engine可读T raw OHLCV、状态/限价；query_end=T15 |
| AFTER_TRADING | 当前T>=15，engine估值/挂牌/权益；query_end=T15 |
| SETTLEMENT | 当前T>=15，engine估值仅T，后继仅挂牌；query_end=T15，terminal不新增事件或行情 |

A-VIS明确这是T15及日末**事后日线模拟**，不是源当时已发布/接收的证明；后端A-CLOSE不能代替A-VIS。
available_at=null、historical_pit=false、finality=UNVERIFIED、daily_query_complete=false、verified_absent=false、
绝对退市日unknown保持不变。后端三假设A-CLOSE/A-COST/A-POOL及七项bundle由后端负责。

## 原生参数/返回codec（本轮不启动RQ）

`m2_codec.native_ad08_call(..., time_policy='rq641_shanghai_midnight')`接收完整实际tuple，支持Python date、午夜datetime、
零纳秒pandas.Timestamp；naive只按显式上海午夜政策解释，aware转上海后须午夜。非午夜或多余形状拒绝。
`history_array(read,use=...,context=...)`验证回执后返回float64一维length1；不代表RQ已调用。
`consume_previous(native_value,read=...,use=...,context=...)`先消费native值，再与owner Decimal精确比较，返回M2ConsumedPrevious。
仅Python float/NumPy float64标量或float64一维length1数组；bool/int/Decimal替身/float32/空/多项/NaN/Inf拒绝。
无epsilon和舍入；不改RQ内部float。后端仍须实际证明DataProxy→history_bars→owner并记录实际消费链。

## CLI

先显式`ashare-data --store PATH init`。统一入口：
`ashare-data --store PATH m2 {import,profiles,query,canonical,snapshots,recover} --request REQUEST.json`。
所有请求严格字段；错误exit2保留DataError.code/message/details与Python cause链，查询成功exit0不授执行许可。

`import`请求`{"directory":"/固定来源包"}`；`profiles`请求`{"dataset_id":"SHA"}`；snapshots/recover为`{}`。
query元数据请求dataset_id/profile_sha256/mode/operation（descriptor/profile/windows/quality/lineage/capabilities）；
envelope增加window_id/assumption_ack/consumer_binding；读操作再增加context和parameters。
parameters为方法的显式关键字（无参时`{}`），verify/revalidate使用`{"read":完整M2Read}`。
canonical请求`{"kind":"result|query|context|call","payload":...}`；返回同Python规范bytes文本和SHA，不需要Store。
具体可运行构造见demo，后端不得猜签名或私读输入文件来补事实。

## 主要错误

M2_EXPLICIT_MODE_REQUIRED、M2_ASSUMPTION_REQUIRED、M2_PROFILE_UNREVIEWED、M2_WINDOW_UNKNOWN、M2_IDENTITY_MISMATCH、
M2_ROLE_SCOPE、M2_CLOCK_INVALID、M2_LOOKAHEAD、M2_VISIBILITY、M2_UNREVIEWED_AD08、M2_SOURCE_CONFLICT、M2_DATA_MISSING、
M2_STATE_UNKNOWN、M2_UNIT_UNKNOWN、M2_RULE_EXCEPTION、M2_RELATED_EVENT、M2_SUCCESSOR_INVALID、M2_RECEIPT_MISMATCH、
M2_CONTEXT_STALE、M2_INTEGER、M2_DECIMAL、M2_DATE、M2_SCHEMA、M2_ENCODING、M2_NATIVE_TIME、M2_NATIVE_VALUE、
M2_NATIVE_MISMATCH、M2_INTEGRITY、M2_UNREVIEWED_INPUT、M2_NOT_PUBLISHED、M2_RECOVERY_REQUIRED、M2_BACKEND_ADMISSION_REQUIRED。
底层文件缺失/拒绝还可保留D1_INPUT_INVALID/INTEGRITY等既有读取/不可变写错误；不要把错误归类为“无信号”。
