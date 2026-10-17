# 有限事实组件

`FiniteFacts` / `FiniteFactsView` 是特定证据清单和有限日期范围的严格只读模型，仍限制 600000.XSHG 及 2020-01-02～07；不是通用历史事实数据库。原件、价格和日历的来源 pin 是证据身份，不是新代码的验收结果。缺失的历史资格、相关事件完整性及复权等价不被推断补齐。

版本 0.10.0.dev1 使用功能名称和新的 owner/schema 身份。`Store.import_fact_component(directory)` 导入核对原件，`Store.compose_facts(price_dataset_id, component_id)` 生成本版本新 ID；旧数据目录与旧消费者继续使用原固定运行环境。不得覆盖旧包或变更原件再重算 pin 冒充原证据。

## Python契约

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

## CLI和存储

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
本版owner接口存在不代表后端绑定已接通；后端原有FACTS执行及30项正式验收仍NOT_RUN。
