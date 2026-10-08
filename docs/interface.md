# 高级离线快照接口契约

普通行情用户请使用[直接 get_price 接口](live-api.md)，无需本页的手工导入流程。

本页只描述高级离线存储 API。数据流：显式本地 JSON 导入 → 规范化 Parquet → 验证 → 发布不可变快照 → 固定快照查询。
不包含网络采集、账号、交易、回测引擎、UI 或 HTTP 服务。

```python
store = Store.init(path)                 # 唯一显式建库入口
batch = store.import_bundle(bundle)     # 内容寻址，重复输入幂等
report = store.validate([batch], requirements=claims, policy="observed")
snapshot_id = store.publish(report["id"])
data = store.snapshot(snapshot_id)      # 不支持 latest 或自动换源
data.bars(symbols, start, end, quality="observed", strict=True, as_of=None)
data.coverage(symbols, start, end, quality="observed", as_of=None)
data.instruments(as_of=timestamp)
data.calendar(start_date, end_date)
data.sessions(date)
data.lineage()
data.quality()
store.snapshots()
store.recover()
```

- 原生 bars 时间选择为 `start <= bar_start < end`，必须带时区且对齐分钟。
- 完整 bar 必须满足 `bar_end <= observed_at`，无论质量类别、available_at 是否未知。
- `as_of` 是可见性截止，额外要求 `bar_end <= as_of`、`available_at <= as_of`。
  只有收集时间而无历史可见时间，不能向过去声称可见。
- 时间规范为 Asia/Shanghai，原始标签、标签类型和语义证据另存；未经验证的结束标签
  可以记录候选起止区间，但 quality 必须是 unverified，严格查询不可用。
- volume 单位股，amount 单位人民币元，价格为未复权人民币元；源值与源单位保留。
- quality 是 observed / inferred / unverified / synthetic；observed 仅代表导入者提供了
  来源及语义证据的声明，不代表本库独立审计过交易所。合成数据必须明确选择 synthetic。
- 查询不触发任何导入；空白日历不当作休市，缺证券不当作已退市，缺 bar 不做前值填充。
- coverage 在明确日历时段内计算分钟集合；无日历证据或无预期分钟必须明确返回未知。
- validate 必须声明非空覆盖要求；缺口、同一证券分钟多来源冲突或质量不足阻断 publish。
  发布只证明声明的小范围，不代表整个市场或任意范围齐全。
- instrument 历史查询要求 universe 的时间窗口及可见性证据；一组今日股票不能回填。
- 公司行动/因子独立 schema：symbol, effective_at, available_at, observed_at, source_id,
  event_id/revision, raw_fields；factor 还需 basis_date, adjustment_kind, factor_value。
  首版 corporate_actions / adjustment_factors / adjusted bars 一律 Unsupported，不能当作空事件。

返回 Python 列表/字典，Decimal 数值在 CLI JSON 中为字符串。所有返回保留 snapshot_id；
错误为 `DataError(code, message, details)`，CLI 错误输出到 stderr 并退出 2。
calendar 日期范围首尾均包含；sessions 交易时段为半开区间。

聚宽风格 SDK 是单独的 `ashare_data.compat.JQStyle`，绑定 Snapshot 后使用；未来回测上下文
负责策略时钟、账户、订单及 history，SDK 本身不引入这些状态。

安全边界：单本机发布写入者、非对抗式本地文件系统。内容 hash 检测篡改但不是数字签名；
不声称能抵御同用户恶意修改整套目录。固定快照与 PIT 可见性是两个独立概念。

## 实际 Python 方法与返回值

| 方法 | 返回值与边界 |
|---|---|
| `Store.init(root)` / `Store(root)` | 创建空目录 / 只读打开现有目录；已有非空无目录结构时拒绝初始化 |
| `import_bundle(dict)` | batch SHA256；JSON 内容的键排序规范化，列表顺序保留；相同输入幂等 |
| `validate([batch_id], requirements=[...], policy="observed")` | 含 id、passed、coverage、conflicts、quality_counts 的报告；传入的全部批次组成候选集合 |
| `publish(report_id)` | snapshot SHA256；仅 passed 且每个要求至少有一个预期分钟时成功 |
| `recover()` | 待发布版本恢复结果；对象校验失败标 aborted，已发布版本不会被改写 |
| `snapshots()` | id/status/created_at；prepared/aborted 仅可检查，不可查询 |
| `snapshot(id)` | 可用 context manager 关闭的 DataView；加载时验证对象和 manifest hash |
| `bars(symbols,start,end,quality="observed",strict=True,as_of=None,adjustment=None,frequency="1m")` | 包含 rows、coverage、units、snapshot_id 的 dict；strict=False 显式返回缺口，仍不允许来源冲突 |
| `coverage(symbols,start,end,quality="observed",as_of=None)` | expected/present/accepted/missing/excluded/unknown_calendar_dates/unexpected/complete |
| `instruments(as_of=timestamp)` | 对应当地日期的完整证据记录及 scope（sample/all_a_shares），没有当日记录则拒绝 |
| `calendar(start_date,end_date)` / `sessions(date)` | `{snapshot_id, rows}`；包含 evidence 和 available_at |
| `lineage()` / `quality()` | 原始来源契约和 hash / 当前质量计数及已发布验证报告 |
| `corporate_actions(...)` / `adjustment_factors(...)` | 首版总是 DataError(UNSUPPORTED) |

`instruments` 顶层仅含系统生成的 snapshot_id/as_of、声明的数据字段
(effective_date/available_at/observed_at/evidence/scope/instruments) 和 `source_fields`。
`source_fields` 保留完整导入 instrument_set 的深拷贝。所有额外源字段（包括与当前或未来系统元数据
同名的字段）均留在这个命名空间内，不能改变顶层系统结果。输入中同名 source_fields 也只作为
原始载荷的一部分保留。rc1 曾把额外字段展开到顶层，rc2 明确收紧这一形态；已有快照读取也适用。

`DataView.close()` 显式释放内存中的 DuckDB。所有 SQL 参数绑定；没有开放任意 SQL 执行口。
Store.capabilities 是静态接口能力，真实覆盖必须查询固定快照的 coverage，不能仅看 capability。
本版把选择的 Parquet 读入 DuckDB 内存表，目标是小规模可核验内核，未承诺生产级扫描性能。

## 导入 bundle v1

必填顶层键 `schema_version=1, source, bars`；可选 `calendar, instrument_sets`。
未知顶层键拒绝。输入示例见 `examples/fixtures/golden.json`，源特有字段可以留在 source 及 bar 中。
每次最多 100000 行；CLI JSON 文件最多 64 MiB；查询最多 100 标的、2000000 个自然分钟×标的槽。
必须明确选择批次；不会自动把旧批次拼入新快照。

| source 字段 | 约束 |
|---|---|
| id/kind/location | 来源标识、synthetic/local_observation/local_reconstruction、可追溯位置 |
| observed_at | 系统观测时间，aware timestamp，不能用作伪造历史可见时间 |
| label/time_semantics | start 或 end；verified 或 unverified |
| time_evidence | 时间解释的依据；verified 指导入者声明有据，不等于本系统独立认证 |
| volume_unit/lot_size | shares 或 lots；lots 必须明确正整数 lot_size，不默认 100 |
| amount_unit/price_unit/price_basis | CNY 或 CNY_10K；价格 CNY 且 unadjusted |
| unit_evidence | 单位来源证据，禁止从未知字段名猜测 |

每根 bar 必填：symbol、timestamp、open/high/low/close、volume、amount、quality、quality_reason。
所有完整 bar 均要求 `bar_end <= source.observed_at`，独立于 available_at。
available_at 只能缺失或 null 表示未知；空字符串、False、0 是非法时间，不等同于未知。
若提供时间，必须有 visibility_evidence 且 `bar_end <= available_at <= observed_at`。
这三个时刻分别表示完整分钟闭合、证据支持的可见时刻、本系统观测时刻；相等边界合法，按同一瞬时
比较时区偏移。合法历史回填可以远晚于 bar_end 观测，但缺失 available_at 仍保持未知，不自动使用
observed_at 代填，也不能因此通过历史（或任意带 as_of 的）可见性查询。
不以运行时 now 判断历史回填或合成测试是否有效；约束针对记录内部的事件顺序。
source_fields 保存完整输入 bar（包括未知源字段），source_label 保存输入 timestamp，规范时间单独存储。
数值为非负、有限、至多 6 位小数且小于 1e17；OHLC 为正，volume 规范后须为整数股。
本库拒绝隐式舍入；本地探针若处理源浮点金额，会在进入内核前显式 half-even 到 6 位，保留原值和转换记录。

calendar 每行有 date/is_open/sessions/available_at/evidence。date 为显式日期，sessions 为当日
互不重叠的 `[aware start, aware end]` 对；开市必须有时段，休市必须无时段。
库不内置交易所完整历史日历。数据来自合成来源时，返回证据明确标明 synthetic。

instrument_sets 每行有 effective_date/available_at/observed_at/evidence/scope/instruments。
一份精确到日的集合只服务该日，禁止根据最近一期集合向前或向后外推。每只证券至少有 symbol/name/type=stock；
其他上市/退市/名称历史字段及源字段可以保留，但首版不据此自动生成历史证券集合。

## 质量和覆盖含义

- observed：导入者声明有证据的直接本地观测。内核检查结构，不认证事实真伪。
- inferred：重建或推断，默认严格查询不可用。
- unverified：时间语义等尚未验证，候选区间仍不得提升为 observed。
- synthetic：与真实市场隔离，必须显式选择 synthetic 策略。
- research 策略允许 observed/inferred/unverified；这个名字不代表数据可以通过策略验收。

missing 按 **该次请求＋日历证据＋质量策略＋可见时间** 计算。停牌也可能形成缺口，首版无停牌
状态资产，既不猜停牌也不补价。complete 仅是分钟槽完整性；若为已知休市/盘外范围，查询可以
返回零槽且完整，但此空范围不能作为发布验收要求。缺日历永远不当作已知休市。
验证报告只承诺 requirements 中的范围；不推导全快照任意区间或全市场完整覆盖。

## 错误与持久化

常用错误码：INVALID_REQUEST/INVALID_TIME/INVALID_DATE/INVALID_SYMBOL/INVALID_NUMBER、
INVALID_OHLC/INVALID_VOLUME/INVALID_VISIBILITY/INVALID_QUALITY、DUPLICATE_BAR、
STORE_NOT_FOUND/DIRECTORY_NOT_EMPTY、BATCH_NOT_FOUND/REPORT_NOT_FOUND、INVALID_ID、
SNAPSHOT_NOT_PUBLISHED、VALIDATION_FAILED、COVERAGE_GAP、CALENDAR_UNKNOWN、
PIT_UNAVAILABLE/UNIVERSE_INCOMPLETE、SOURCE_CONFLICT、UNSUPPORTED、INTEGRITY、WRITER_BUSY。

文件布局：objects/<batch SHA>.json（源 bundle）及 .parquet（规范数据）；manifests/<snapshot SHA>.json；
catalog.sqlite（批次、验证报告、发布状态、任务日志）；writer.lock（本机 advisory flock）。
JSON 与 Parquet 都纳入 manifest 引用 hash。内容 hash 只能证明未改写，不能证明旧版接受的
记录符合修订后的语义契约；rc2 在打开批次供 query/validate/publish/recover 使用时重检输入契约。
遗留的未来闭合矛盾记录会抛 INVALID_VISIBILITY；不修改对象、manifest 或已发布目录状态。
合法 rc1 数据对象、报告与快照 id 均保留。查询重新检查的额外 CPU 开销未作大规模性能验收。
发布顺序：对象持久化 → SQLite prepared 事务 →
manifest 写临时文件/fsync/原子 rename/目录 fsync → SQLite published 事务。
recover 在持有写锁时核对并补齐 prepared；内容不符则 aborted。异常进程退出后 OS 释放写锁。
import 中断产生的无目录引用对象对读者不可见，重复导入相同内容可继续；本轮不实现垃圾回收。
无跨机器/网络文件系统并发保证，无全盘断电与硬件损坏容错认证，无同用户恶意篡改防御承诺。
