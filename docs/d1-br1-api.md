# D1-BR1：显式条件研究投影

本profile已获委托方明确接受用于独立条件研究验证；**公共实现仍待独立复核，执行许可为false**。
严格D1 `29ed74c`及原事实保持BLOCKED。BR1不认证历史收益、PIT或真实可成交；本库不运行账务或引擎。

| 条目 | 冻结范围 |
|---|---|
| 证券/预定意图 | 600000.XSHG，2020-01-03 15:00+08买100股；01-06 15:00+08卖100股。意图不保证成交 |
| 读范围 | 01-02暖启动、01-03/06、01-07后继；12-31只作01-02前收依赖 |
| 初始条件 | 10000.00元、0股份、0既得/待收权益；关闭真实交易、paper、一般日期/标的 |
| A-EQ | 接受有界官方审阅作为有限相关权益无遗漏的模型假设；只派生模型空事件，不改verified_absent=false |
| A-NORMAL | 四日普通规则适用是假设；不得关闭源停牌/ST、手数、tick、T+1或限幅检查 |
| A-AD08 | 依赖A-EQ；四个完整tuple的有限raw/pre等价；绝对因子unknown，不能推广pre=none |
| 原质量 | complete=false、source_claim_unverified、historical_pit=false、finality=UNVERIFIED不变 |

`src/ashare_data/br1-profile.json`保存完整假设、范围、固定输入hash、规则版本、允许tuple和停止条件。
它的精确SHA进入组合manifest。另绑定原严格D1组合、原价格ID、官方包和独立收口建议hash。
它是独立owner profile，不声称后端已有相同新plan或执行绑定；后端必须显式锁定本profile hash。
本版另绑定后端准备profile `9383a66ecfc7ca5c768b0b49bcc8b1a13a63285710c8fcacdbc35458dfd4f5d8`；
两份profile字段结构不同，hash不应互换。A宽`c53ffde`仍只有准备契约，没有真实codec或执行许可。

| 公共接口 | 行为 |
|---|---|
| `Store.compose_br1(strict_dataset_id, *, mode, assumption_ids)` | mode必须显式为conditional_research，三个假设必须齐全且唯一；返回新的组合ID |
| `Store.br1(ID, *, mode)` | 仅显式conditional_research可重开；验证全部嵌套清单和输入，再验证状态/事件停止条件 |
| `view.descriptor()/profile()/admission()/owner_receipt()` | typed身份、完整profile、投影权限/执行拒绝及依赖回执；无严格“全通过”回执 |
| `view.instrument()/rules()/bars()/calendar()/statuses()` | 原事实和规则；instrument返回历史资格假设标签，不改原instrument事实 |
| `view.modeled_events()` | `assumed_no_relevant_events`的模型空tuple，绑定A-EQ；完整经济事件集仍unknown |
| `view.price_limits(date)` | 带A-EQ/A-NORMAL依赖的typed模型上下限，使用原source preclose、Decimal四舍五入 |
| `view.adjusted_prev_close(**call)` | 仅四个精确tuple，返回带A-EQ/A-AD08标签和来源的有限值；其余拒绝 |
| `view.require_execution()` | 始终BR1_REVIEW_REQUIRED；独审前没有放行开关 |
| `Store.br1_snapshots()/recover_br1()` | 独立发布日志/显式恢复；底层仍共用同一Store单写者锁 |

所有投影可`.to_dict()`；不是策略时钟视图，不可向预定策略暴露当天执行数据。策略意图隔离与原生
停牌/ST、手数、tick、T+1、限幅及原计划的日量参与限制由后端实施和独审；owner回执列明required_native_checks，
不会以“模型限值内”替代原生检查或强制成交。

BR1的日期参数接受严格`datetime.date`或等价`YYYY-MM-DD`字符串，不接受datetime隐式截日。
这样A宽`AD08Call.model_dump()`的date可原样传递；JSON CLI使用ISO日期。证券、频率、字段、
bar_count、两个bool及adjustment_requested均精确比较，不推导日期或把none改成pre。
严格D1已冻结接口仍使用ISO字符串且缺事实时报错；codec可显式用`model_dump(mode="json")`。
required_native_checks是要求，不能直接复制为后端“检查已启用/已通过”的证据。

CLI：`br1-compose --strict ID --mode conditional_research --assumption A-EQ --assumption A-NORMAL --assumption A-AD08`；
`br1-query DATASET --dataset ID --mode conditional_research`；`br1-snapshots`、`br1-recover`。
price-limits需`--date`；adjusted-prev-close需`--call`完整tuple JSON。admission诊断exit 2（执行仍拒绝）。

**停止条件**：源码/日期/价基/hash或身份冲突；任何四日停牌、ST、未知状态；已知事件登记或
调整生效落在资格/价基区间；tuple越界；profile/假设缺失。新矛盾材料必须生成新审阅版本，
不能修改固定包后重算hash授权自己。此离线组件不监控未来新增公告，不能声称自动发现外部新事件。
实际订单未按计划成交或后端违反原生检查时，后端停止本profile并记录失败，不对齐目标现金强制成交。

原D1接口返回的events、candidate、verified_absent和complete不变；BR1派生结果另命名、另存版本。
用户批准假设不等于独立实现审阅已通过；本版没有模拟成交、安装后端依赖或改动后端。
