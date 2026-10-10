# D1 有限事实组件（0.6.0.dev1）

本版本是离线数据所有者组件，**D1 执行准入为 BLOCKED**。只审阅
`600000.XSHG`、2020-01-02～01-07、01-03买100股/01-06卖100股、
初始零股份/零待收权益的固定研究请求；不运行引擎，也不修改 A宽。

## 冻结输入和结论

- 价格版本：`31bc25c99e00c8b5c43f8d77daeae9ae9bc41116f40802b0aad10567e6259973`。
- 官方证据包 manifest SHA256：`499cc2b4e2985f6597f1d8923fbf1878fcb148f4a0764835607cb3919b040d82`。
- 后端 D1 v2 plan SHA256：`8863e4598b8c70f1813a2c86ce2ef0abb4e568828bc495ad769bca1101208bcd`。
- 查询修复提交 `9306b27` 保持冻结；本版在独立分支追加。
- 150个文件按字节验证；16项事实、5个已知事件、248条公告元数据。
  元数据分页完整不等于所有经济事件已排除；原包不声称完整历史。

| 后端必需字段 | 本次采纳 | 依据和边界 |
|---|---|---|
| identity | supported | 年报明确普通A股600000、上交所；事后文件，不冒充历史可得 |
| raw_bars | supported | 冻结Bao原价日线四个交易日；volume股、price元/股，最终性未知 |
| calendar | supported | 固定源日历含六个自然日；01-04/05休市 |
| instrument | unknown，阻断 | 身份与1999上市日已知；历史板块仍为推断，未解决窗口内上市状态转换/例外 |
| trading_rules | supported | 2018-08-20生效的普通A股竞价100股买入单位、0.01元tick、T+1可卖约束；不代表限价或账户资格 |
| suspended | supported | 原始tradestatus=1，source_observed；不是分钟停复牌时间线 |
| st_status | supported | 原始isST=0，source_observed；不由成交量或当前名称推断 |
| price_limits | unknown，阻断 | 普通10%公式和舍入已知，每日普通规则/例外及除权基准未获确认 |
| event_absence | unknown，阻断 | 有限审阅未识别相关事件，尚不足签发冻结契约要求的有限无相关权益证明 |
| adjusted_prev_close | unknown，阻断 | 四个调用tuple有条件候选；依赖上述价基期间无调整证明，未生成因子1 |
| owner_scope | supported | 仅准许本固定版本逐字段事后研究读取；不代表执行许可 |

receipt、historical_pit、market_authenticity、closing_queue、personal_fees 为后端声明的模型假设，
保留 unknown/不验证但不作为额外数据门槛。amount 为 not_used，原值保留。

**有限缺口的含义。** 年报第8.1.1及10.4节只覆盖2019，不能扩展到2020年1月。
季度股本为取整比较，不能独自排除抵销动作。248条标题及精选正文没有提供按事件类型闭合的
覆盖证明。需要的后续材料是本证券、本持有资格窗口01-03～01-06及价基依赖期12-31～01-07的
普通股登记/除权、送转/拆并/配股、增发/其他价格调整及特殊上市状态证据；窗口取得权利的
延后兑现应按登记资格跟踪，不按支付日期过滤。无需证明全市场、全历史或verified PIT。
未来补充证据须新审阅配置、新组件、新组合ID，不能改写现有版本。

## Python契约

所有结果为 frozen dataclass/tuple；`to_dict()` 返回独立可序列化副本（Decimal为字符串、日期为ISO）。
原始字段保存在不可变JSON字符串及 `source_fields` 副本，来源含原路径、SHA、URL、定位、2026读取时刻，
历史available_at为空。查询默认**离线、事后研究**；本接口不提供策略时钟/PIT视图，不可直接作策略输入。

| 入口 | 返回/行为 |
|---|---|
| `Store.import_d1_facts(directory)` | 显式校验并隔离封存固定官方包，返回facts_component_id；不执行包内脚本 |
| `Store.d1_facts(component_id)` | `D1Facts`，`descriptor()`、`records()`、`evidence(path)`；证据只读字节 |
| `Store.compose_d1(price_dataset_id, facts_component_id)` | 固定两组件、review policy及backend plan的组合dataset_id；不提升原价格质量 |
| `Store.d1(dataset_id)` | 每次重开验证目录、manifest、全部对象及两组件关联，返回`D1View` |
| `view.descriptor()/coverage()/lineage()/quality()` | typed范围、组件hash及质量；`complete=false`、PIT/finality未验证 |
| `view.instrument()` | 身份/上市日期、带推断标签的board candidate；historical_eligible=None |
| `view.rules()` | 普通A股竞价lot、tick、T+1及版本/窗口；正常限幅仅条件规则 |
| `view.calendar()` / `view.bars()` / `view.statuses()` | 有界typed源日历、四日原价OHLCV及源状态；不联网补数 |
| `view.source_prev_close(trade_date)` | 源preclose及原文证据；不名为调整后前收 |
| `view.known_events()/event_coverage()` | 五个已知事件与有限检索范围；不能把列表当完整事件集 |
| `view.limit_candidates()/prev_close_candidates()` | 显式conditional候选，`engine_value=None`、`eligible_for_engine=false` |
| `view.price_limits(trade_date)` / `view.corporate_actions()` | 本版抛出 `D1_FACT_UNAVAILABLE`；不返回伪造空事件或限价 |
| `view.adjusted_prev_close(**call)` | 先核对完整四种AD08调用tuple；未审tuple报`D1_UNREVIEWED_CALL`，已审仍报`D1_FACT_UNAVAILABLE` |
| `view.admission()/owner_receipt()` | 17项逐字段结论，11必需项中7支持4未知；回执兼容后端提议的envelope |
| `view.require_fact(key)` / `view.require_execution(plan_sha256=...)` | 严格字段/执行闸；执行始终BLOCKED，不存在force或allow_unknown |
| `Store.d1_snapshots()/recover_d1()` | 固定组件与组合目录；显式恢复prepared，缺失/损坏引用则aborted |

AD08完整参数：`security, trade_date, history_dt, adjust_orig, frequency='1d', field='close',
bar_count=1, include_now=False, skip_suspended=False, adjustment_requested='pre'`。
四个 `(trade_date, history_dt, adjust_orig)` 分别为 `(01-02,12-31,01-02)`、
`(01-03,01-02,01-03)`、`(01-06,01-03,01-06)`、`(01-07,01-06,01-07)`（2019-12-31除外均2020）。
不能把RQ的`get_prev_close(adjust_type='none')`当raw调用；固定RQ实现内部仍请求pre。

## CLI和存储

`--store DIR d1-import-facts PACKAGE`、`d1-compose --price ID --facts ID`、
`d1-query DATASET --dataset ID`、`d1-snapshots`、`d1-recover`。
`d1-query`选择descriptor/instrument/rules/bars/calendar/statuses/events/event-coverage/
limit-candidates/prev-close-candidates/coverage/lineage/quality/admission/owner-receipt。
admission返回诊断并exit 2；其他只读诊断成功exit 0。未知/缺对象/越界/不支持皆为稳定DataError JSON。

原件进入content-addressed对象；SQLite只记不可变manifest和prepared/published/aborted日志。
同一Store共用单写者锁，发布仅在全部引用验证后可见。中断不自动恢复，不覆盖旧对象。
manifest只含确定的内容和版本标识，不含本机导入路径或时间。证据包内原始路径仅作来源文本保留，
永不据此读取外部文件。导入只接受此审阅包的精确manifest hash；自行改facts并重算hash不构成owner采纳。

普通Python对象不是防恶意同进程执行的沙箱；源码/安装环境及存储可信边界须由调用端保护。
消费者应从`Store.d1(ID)`公开接口取得回执，不能信任调用者粘贴的JSON。
本版owner接口存在不代表后端绑定已接通；后端原有D1执行及30项正式验收仍NOT_RUN。
