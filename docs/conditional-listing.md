# 条件上市状态

`ConditionalListingView` 对条件模型输出有限挂牌谓词和严格前进的后继日期，绝对退市日仍为 unknown。`ConditionalView.listing_contract()` 返回本版本策略哈希；`listing(contract_sha256=...)` 要求调用方显式锁定它。来源事实、允许日期与 consumer context 每次重验；不扩张为全市场生命周期。

## 公共接口

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

## 错误与后继边界

稳定 DataError：CONDITIONAL_LISTING_BINDING、POLICY_INTEGRITY、SECURITY、DATE、SCOPE、ROLE、CONTEXT、
CALENDAR、SUCCESSOR、CONFLICT、CACHE_MISMATCH、REVIEW_REQUIRED（全部带 CONDITIONAL_LISTING_ 前缀）。
原 CONDITIONAL/FACTS 完整性、源状态/事件错误原样传播。

RQ 末端 next(Jan-07)=Jan-07，本接口因 next_open=null/非严格后继拒绝；next(Jan-08)=Jan-07，
因当前日越界拒绝。不能只检查返回日落在四日范围内。n 仅接受精确 int 1，True/1.0/其他值拒绝。

## 消费者责任

context_sha256 是**调用方声明的摘要，不是 owner 验证的运行许可**。固定算法应至少纳入新 owner
契约/wheel、adapter/RQ/config/run 身份、实际 phase、完整模型时刻、event cursor 和恢复 generation。
数据方不控制这些运行状态；若后端重放旧摘要和旧日期，owner 无法知道实际引擎已经前进。
必须从受控运行入口获得新上下文，不能从缓存结果抄回。

实际事件推进、getter/谓词、最终缓存消费及恢复入口都检查当前日与目标日。零仓位、空订阅及
缺失/额外订阅路径也必须检查，不依赖原生短路触发 owner 查询。缓存标量不是权限；消费时
revalidate 或丢弃重查。恢复时重建公开查询绑定与对象，不反序列化查询对象；旧 ceb6e9…
失败检查点不能在新语义下静默续跑。Python 对象不是对抗恶意同进程代码的安全沙箱。

清订阅如必须用日期，可在**后端受控内部**采用 Jan-08 00:00 的 bounded_horizon_marker，
仅编码“对已批准时点 t，S<=t 与 model_delisted(t) 同为 False”。它不是市场退市日期、预测或上界。
普通日期离开 getter 后无法自带有效域，必须完成上述事件/查询/缓存/恢复 guards。
记录 engine_projection_kind=bounded_horizon_marker 与适配版本；不在一般 API、repr、事实日志、
all_instruments、benchmark、因子过滤或外部 API 输出它。不改 RQ 内核、不删清理 listener、不靠
取消订阅规避、不构造伪装 datetime 的通用对象。listed_at/active_at/de_listed_at 共用有界投影，
不能遗留默认无界 listed_at。

消费者必须固定库版本、数据版本与使用声明；该投影不负责交易执行、恢复账户或认证原生调用。
