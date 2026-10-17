# 显式条件研究投影

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
