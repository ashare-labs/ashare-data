# BR1 有界上市状态：0.6.0.dev3

新增可固定身份的公共条件查询。**绝对退市日期保持 None/unknown；严格 D1 四项 unknown 和
执行许可 false 不变。** 本轮没有修改 RQ/A宽 adapter，没有重跑两订单场景或恢复失败账户。

## 证据与版本

已接受 A-NORMAL 覆盖 01-02、03、06、07 四日普通适用情形，假设无未识别的恢复/重新上市及改变
准入的例外。原公开 instrument 保留普通 A 股身份、1999-11-10 初始上市日及历史板块推断，源停牌/ST
为 False。这些材料仅支持**声明假设下**派生四日 model_listed=True/model_delisted=False，
不是严格历史挂牌证明。成交量、bar 存在、ST 正常本身均不是未退市证明。

复用原 BR1 profile `ded0d9dc…`、严格组件和价格 ID。新的 `br1-listing-policy.json` 独立固定 SHA；
查询契约 hash 绑定完整策略、原 BR1 dataset/profile、严格组合、价格和官方事实身份。
旧数据 ID、原 manifest、已有 API 输出不变；**消费方追加 pin 本契约 hash 和 dev3 wheel/commit**。
旧 dev2 pin 不认证新接口。这是固定数据的新查询契约，不导入新事实或发布新数据快照。

固定 RQ 订阅清理直接比较 de_listed_date<=trading_dt，需要当前退市谓词；非零股票仓位结算查询
de_listed_at(next_trading_date)，需要真实后继状态。独审归因报告 SHA
`d44003895a3f83d82ee200ec42a2f937d4a89e3dcc04ff2cf24fbfa4c3b01cd1` 与 RQ wheel SHA
进入策略。归因审查不是新增市场事实认证；本实现也尚待独审。

## 公共接口

```python
br1 = Store(root).br1(br1_dataset_id, mode="conditional_research")
contract = br1.listing_contract()  # 供冻结、独审
listing = br1.listing(contract_sha256=PINNED_LISTING_CONTRACT_SHA256)
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
| br1.listing_contract() | BR1ListingContract，独立契约身份、读域、事件日、允许请求、完整策略及依赖 |
| br1.listing(contract_sha256=...) | 显式固定身份后得到 BR1ListingView；错误 pin 拒绝 |
| listing.descriptor() | 重新校验并读取契约 |
| listing.status(*, security, current_date, target_date, role, context_sha256) | 校验双日期、用途、调用方上下文摘要，返回 BR1ListingProjection |
| listing.successor(*, security, current_date, candidate_date, context_sha256, n=1) | 验证实际引擎候选后继；必须等于 owner next_open 且严格晚于当前日，不修正、不补数 |
| listing.revalidate(projection, *, security, current_date, target_date, role, context_sha256) | 用最新请求重查并比对全部字段，返回新投影；拒绝缓存漂移和序列化 dict |
| listing.require_execution() | 始终 BR1_LISTING_REVIEW_REQUIRED；无 force/allow_unknown |

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
每次查询/缓存复核重开固定 BR1，验证全部嵌套对象及原状态/权益停止条件。
新资格事实、已退市/未挂牌/特别状态或冲突应退出当前 profile 并另审。离线组件不会监控外部公告，
不能发现未交给它的新证据。原 instrument/price_limits/event_absence/adjusted_prev_close 不升级。

## 错误与后继边界

稳定 DataError：BR1_LISTING_BINDING、POLICY_INTEGRITY、SECURITY、DATE、SCOPE、ROLE、CONTEXT、
CALENDAR、SUCCESSOR、CONFLICT、CACHE_MISMATCH、REVIEW_REQUIRED（全部带 BR1_LISTING_ 前缀）。
原 BR1/D1 完整性、源状态/事件错误原样传播。

RQ 末端 next(Jan-07)=Jan-07，本接口因 next_open=null/非严格后继拒绝；next(Jan-08)=Jan-07，
因当前日越界拒绝。不能只检查返回日落在四日范围内。n 仅接受精确 int 1，True/1.0/其他值拒绝。

## 后端适配责任（本轮未实现）

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

后端固定新 owner/adapter/RQ/config/checkpoint 身份，独审后才另行运行批准的实验。本轮测试
不替代首日结算、T+1、卖单、实际 AD08、费用/账本、恢复去重验证。

CLI：br1-listing contract --dataset ID --mode conditional_research；status 或 successor
另加 --contract SHA --call request.json；全局 --store DIR。成功 exit 0，拒绝 exit 2；
查询成功仍 execution_permission=false。示例 examples/br1_listing_demo.py。
