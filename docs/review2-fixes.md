# 独立审阅后的第二轮修复：0.3.0.dev2

基线commit为`2d0c19e1e9bef0661760a2bfb7a7f9b5d277487c`。其独立68项结论为45 PASS、12 SAFE_LIMITED、9 FAIL、2 BLOCKED_BINDING；自带206项通过不能抵消反例。本轮在后续普通子提交修复，未更改原独立测试、原候选或历史证据。

## 修复与接口变化

| 项目 | 处理与验证 |
|---|---|
| R1 / S17：晚到大窗口遮蔽旧窗口 | received按收到截止时间先选可用集合，再选可复用窗口；晚到精确URL也不能遮蔽可用异窗口。覆盖大小反向、精确URL、全不可见反例；合法回放非空，旧缓存不变且不联网 |
| R2 / S18：正Decimal转float下溢成0 | OHLC须处于float64正常正数表示范围；在缓存写入前拒绝次正规/下溢输入。原Decimal文本保留；最低正常范围及普通价格仍可用 |
| R3：水位与paper准入混淆 | trading_status独立决策；freshness分别输出源水位、选中窗口水位、状态与组合admissible。require_fresh现在同时要求三者；require_tradable只要求当前声明状态 |
| 盘中停复牌 | CoverageContract v2用带available_at/evidence的半开生效区间；不扩大成整日。复牌后旧bar stale，新闭合bar可用，未知历史不冒充全天覆盖 |
| 休市、午休、停牌及空覆盖 | 明确closed_market/session_break/suspended；已证空集合可完整，但不等于当前可交易。未知日历单列CALENDAR_UNKNOWN |
| 只有未来标签 | unknown，FUTURE_SOURCE_LABEL；已有合法当前闭合bar时保留它，并报告被排除未来标签 |
| R4：日线过宽说明 | README及元数据明确旧研究入口15:05只是软件阈值、closure_verified=False；as_of模型区间过滤及最终性另说 |
| D02：请求外新bar为旧窗口背书 | selected_window_status独立检查，require_fresh拒绝旧选中窗口；不加保证的历史研究仍可返回 |

合成事实只验证软件机制；没有新增真实证券状态库、企业行动数据或供应商PIT/最终性能力。[data-admission.md](data-admission.md)为完整接口契约；[paper_admission.py](../examples/paper_admission.py)展示离线正反例。

## 旧验收结果与待裁决项

作者使用原判定器和未改动的68项fixture做离线复跑，绝不代替独立复验：

| 绑定 | PASS | SAFE_LIMITED | FAIL | BLOCKED_BINDING |
|---|---:|---:|---:|---:|
| 原独立v1绑定，原样运行 | 51 | 12 | 3 | 2 |
| 作者建议v2薄绑定，待reviewer裁决 | 54 | 12 | 2 | 0 |

v2建议仅保留原输入effective_start/end到新schema，映射原生tradable/admissible；不读取预期、按case ID分支或实现准入算法。旧绑定遗漏新原生tradable字段，故C09/C10仍失败；旧绑定不能转盘中状态，故C15/F07仍阻塞。全部原始结果保留，未删行凑绿。

- **C14：查询范围冲突。** 输入请求09:30–09:33，额外记录为12:00；assess明确检查请求内闭区间标签，范围外记录列out_of_request_labels，不能当请求内异常。独立S19扩至12:01后原本即能检出。冻结C14继续保留FAIL，需reviewer决定未来版本检查原响应还是查询子集。
- **C15：证据不足。** 日历加入15:05–15:06，但唯一状态事实仅到15:00。v2可无损表达原输入，返回unknown/trading_status_unknown、complete=False，不能把第三方事实延长后声称应有第四根。若另外明确补入该时段的合成trading事实，独立的新作者回归可期望并验证15:06；没有改原fixture。
- **严格A01–A12：能力限制保留。** verified/finalized仍缺供应商证据；12 SAFE_LIMITED包括有效正样本，不能改称PASS或用assumed代替。
- **源量额：SOURCE_UNRESOLVED。** 不改数值、不扣尾字段、不造14:58/14:59，不将安全门禁通过当作真实行情完整性通过。

本轮19项原独立补充检查由作者原样复跑通过，另外3项DIAGNOSTIC单列。新独立验收必须使用本轮精确commit/tree并审核绑定；上述作者复跑不是新的独立结论。
