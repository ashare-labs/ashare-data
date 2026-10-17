# 本地批量日线维护

此入口把真实日线、复权依据和历史股票范围长期保存在独立目录，默认
`~/.local/share/ashare-data`。它使用已有 A 数达证券代码格式、不可变原文写入器、
BaoStock 固定 SDK 身份与协议校验器；批量目录使用 SQLite WAL，查询时无需联网。
原有 `Store` 不可变研究版本和其他运行环境不受影响。

## 安装与日常入口

在本功能所在 checkout 中为数据维护创建专用环境，不更新原有 `.venv`：

```sh
UV_PROJECT_ENVIRONMENT=.venv-local-data uv sync --locked --no-config --python 3.12 --extra local-data
./scripts/local-daily.sh --help
./scripts/local-daily.sh update
./scripts/local-daily.sh status
./scripts/local-daily.sh report
```

`scripts/local-daily.sh` 默认使用这个专用环境；如需复用已验证的独立环境，可用
`ASHARE_LOCAL_DATA_ENV` 指定其绝对路径。
`update`、`resume` 和补采命令会联网并写库；`query`（含兼容模式）、`query-alias`、
`universe`、`factors` 与 `status` 使用只读连接。可先用 `--help` 查看参数，再显式选择维护操作。

日常完整增量维护依次运行下面两步：先推进范围并更新 HTTP 日线、名单与复权快照，
再补 BaoStock 的新增时段并生成最终审计。第二步的批次脚本使用数据库已经记录的截止日，
不会自行推进日期；已成功归档的历史请求会跳过。

```sh
./scripts/local-daily.sh update
.venv-local-data/bin/python scripts/complete_local_daily_batch.py
```

`update` 默认范围自 2010-01-01 至中国时区最近可采集日，18:00 之前不将当天当成完整日线。
第一次覆盖全部已发现证券；以后只为尚未请求的时段建任务，并观察当天的完整复权因子版本。
同一天重复执行，已成功的任务不重复请求、不重复插入。同一因子集合复用版本 ID。
成功返回不代表逐日覆盖齐全，必须同时看 `coverage.csv` 与 `gaps.csv`。

```sh
# 中断后继续，完成的历史请求保持不动
./scripts/local-daily.sh resume
# 显式重试有限失败任务
./scripts/local-daily.sh resume --retry-failed
# 只为本地仍缺的交易日期段建补取任务
./scripts/local-daily.sh update --repair-gaps
# 固定窗口或受控验证；全量正常运行不需要股票数/历史窗口门槛
./scripts/local-daily.sh update --start 2010-01-01 --end 2026-10-14
# 为全部已发现证券补取独立的搜狐原始价量及成交额
./scripts/local-daily.sh supplement --source sohu
# 针对旧代码补取腾讯原始价量；按年度请求以避免来源 640 行上限截断
./scripts/local-daily.sh supplement --source tencent --symbol 601313.XSHG
```

首次启用搜狐补采后，后续 `update` 自动为它规划新增日期；已有来源不被覆盖。
该来源有自己的任务 ID、缺失结果和重试记录，`resume` 同样可以续取。
这里的 `resume` / `--retry-failed` 只调度 HTTP 来源。BaoStock 的任务与空响应记录由
专用采集器维护，按下文的 `--all-bars` / `--repair-gaps` 续取，不会被改送其他来源。
带 `--symbol` 的来源补采只处理指定证券，不会为后续更新自动启用全市场的额外来源。
腾讯没有提供的成交额保持空值；不由价格和成交量倒算。

首轮已有数据库和范围时，可用一个有限批次完成剩余工作：

```sh
.venv-local-data/bin/python scripts/complete_local_daily_batch.py
```

它同时启动单连接 BaoStock 和搜狐补采，然后续取既有缺口，等待本轮 BaoStock 结束，
最后导入并审计。`batch-state.json` 记录每步进程、日志和退出码，区分完成、仍有来源缺口、
暂停与错误。它没有循环排期或新建调度。暂停整个批次需同时创建 `STOP` 和 `STOP_BAOSTOCK`；
删除两者后重新运行该命令即可复用已有进度。

默认 3 个 HTTP 工作线程，每主机请求起点相隔 1 秒；可配置 `--workers`、`--interval`、
`--timeout`。单个 HTTP 请求最多 3 次有限退避。401/403/429 会停止该主机本次运行；
连续传输失败会让主机冷却 30 分钟，冷却状态保存于 `source-state.json`。
这不清空待取股票，也不改变其他来源的记录。没有账号、Cookie、代理轮换或收费端点。

需要优雅暂停时在数据根目录创建 `STOP`，当前几个请求落库后退出；删除该文件后执行
`resume`。直接终止进程也可恢复：入库和任务完成在同一事务中，遗留 `running` 在重开时
回到 `pending`。写入器使用独占锁；`status`、`query` 可在采集中读取。

## 数据位置与查询

目录仅包含 `daily.sqlite3`、`raw/`、`reports/`、运行日志及少量状态文件。
`raw/` 保存 SHA256 标识的 gzip 原始响应和当日请求缓存；不得随源码公开提交。
`reports/summary.json` 是覆盖统计，`coverage.csv` 逐股统计，`gaps.csv` 逐股连续缺口，
`jobs.csv` 保留每次请求的范围、来源、结果与失败原因。采集中定期更新覆盖报告，
最终退出再更新；大批次按约 10% 的任务进度生成报告，避免反复扫描全库拖慢补缺。
`status` 显示即时计数；报表含生成时间。
`bar_rows` 是保留来源的记录数，`unique_security_days` 才是去掉跨源重复的证券日期数。
来源落在交易日历之外的记录仍保留在原文和数据库中，但从查询与覆盖排除，不自动改日期；
`source-boundary-observations.csv` 的 `reason` 区分日历异常与上市/代码边界，查询属性也列出排除数量。
`coverage` 是保留的完整谱系；`main_board_at_date` 和 `main-board-coverage.csv` / `main-board-gaps.csv`
仅统计当时属于主板的日期。原中小板在合板前已退市的代码作为辅助历史保留，主板范围按日期、代码和上市事实重新计算。
尚在队列中的日期标为 `awaiting_collection`，已经请求仍缺失的日期才列作未知缺口。
分母是注册上市存续期与代码有效期内的开市日，包含停牌日；“已观察”可包括来源明确标识
的停牌占位行，不等同于实际有成交的天数。最终核对使用独立重算与分层样本：

```sh
.venv-local-data/bin/python scripts/audit_local_daily.py
.venv-local-data/bin/python scripts/accept_local_daily.py
```

前者核验原文哈希、报文和重复记录；后者重算唯一证券日、列出剩余价量/成交额缺口，
核对历史名单以及全部复权因子原文，并按退市、代码变更、近期 IPO 等分层比较日线来源。
`acceptance-gap-dispositions.csv` 将剩余日期分为公告证明未交易、尚待采集/导入、请求失败、
来源已返回但未给出行情等情况。只有 `issuer-suspension-evidence.json` 中经核对且原始 PDF
哈希仍完整的公告区间才可归入已证实未交易；没有价格行的日期仍不计为行情覆盖。
另有明确署名交易所、经逐段核对的公告转载，保存在
`exchange-republished-suspension-evidence.json`，按单独的
`exchange_notice_republication_nontrading_without_bar` 类别列出，不冒充发行人原始 PDF。
例如 601313 在 2012-04-05 的全天停牌有全景网当时转载的上交所停复牌公告明确记载；
验收核对原文哈希及已审核的日期、代码和停牌文字。CSV 的 `evidence_sha256` 可追溯这类原文。
成功的空响应与请求错误分别统计，不把来源缺记录当作网络失败或停牌证据。
成交额空值另在 `acceptance-amount-gap-dispositions.csv` 中按来源明确停牌、有成交活动、
状态冲突和状态未明分别统计。部分停牌占位行的量与额本就为空，不把它们改写成零，
也不与有正成交量却缺成交额的日期混为一类。
`tradestatus` 仍是供应商原始观察，不能单独视为实际成交证明。例如 600656 的
2016-05-12 行量、额均空，却标为交易状态 1；发行人公告已明确整理期于前一日结束。
该来源状态分歧保留在 `trade-status-followup.json`，没有改写源标记或补造成交额。
另如 002647 在 2025-03-12 被 BaoStock 标记停牌，新浪却重复了前一日的价量；
发行人的停牌与复牌公告都明确证实当日停牌。原文报文、公告及逐日观察保存在
`source-status-conflict-002647.json`，成交额缺口按停牌与成交活动信号冲突单列，
不将新浪重复量视为停牌日确有成交的证据。

```sh
./scripts/local-daily.sh query --symbol 600000.XSHG --start 2026-10-01 --source eastmoney
./scripts/local-daily.sh query --symbol 600003.XSHG --start 2010-01-01 --source baostock
./scripts/local-daily.sh universe --end 2010-01-04
./scripts/local-daily.sh factors --symbol 600000.XSHG
```

```python
from ashare_data.storage.local_daily import LocalDailyStore

store = LocalDailyStore(readonly=True)
prices = store.get_price("600000.XSHG", start="2020-01-01", source="auto")
# 数值以十进制字符串保真存储；需要浮点计算时由调用者显式转换。
versions = store.factor_snapshots("600000.XSHG")
factors = store.get_factors(versions[0]["id"])  # 固定所选观察版本
pool = store.universe("2010-01-04", main_board_only=True)
store.close()
```

新代码回填的旧期间数据需要显式别名入口、原文校验及有效期依据；不自动合并为原代码行情。

```sh
./scripts/local-daily.sh query-alias --symbol 601313.XSHG \
  --capture ~/.local/share/ashare-data/raw/bao-alias-sh601360-old-period \
  --start 2013-05-27 --end 2013-05-28
```

Python 入口为 `store.get_alias_price(security, capture, start, end)`，必须显式选择归档版本。
每次查询重新核验原始报文及保留的代码变更公告哈希，并按旧代码有效期和交易日历筛选。
每行明确列出 `symbol`、`provider_symbol`、`source=baostock_alias`、归档和公告身份；
价格、量额来自同一根原始日线。这是后来代码下的回溯数据，不是当时以旧代码发布的报价。
该入口不写入原生 `bars`，不改变普通查询、原生缺口或覆盖率，也不承诺历史 PIT。

每个日线行含来源、响应 ID、观察时间和内容哈希。主键为 `(symbol,date,source)`；同源修订
保留于 `bar_revisions`。默认不把几个源的不同字段拼成一根 bar，不静默提供混源复权价格。
`get_price` / `query` 默认 `source=auto`，为整个所选区间选择观测日期最多的单一来源，
相同时优先选择成交额较完整者；结果的 `source` 列及 DataFrame 属性明确返回所选来源。
需要固定可复现来源时显式传 `source`。
供应商可能在正式退市日仍返回停牌占位行。这些原文和记录保留在库中，但普通价格查询
按上市区间过滤，正式退市日不返回为可用价格；范围外来源行单列于
`source-boundary-observations.csv`，不计入覆盖和停牌统计。仅 BaoStock 发现的旧代码边界
按下述明确的 inclusive 规则处理。
SQLite 可直接只读查询；SQL 中 `volume`、`amount` 为十进制文本，计算需显式 CAST。

## 显式兼容补齐查询

普通 `get_price` 和 `query` 现在接受 `mode="compatible"` / `--mode compatible`。
默认 `mode="single"` 保留原整段单源选择、返回字段与 CLI 列表结构，已有回测输入不变。
兼容模式只读取现有库、日历、上市区间和证据，不联网，不导入或重写原始记录。

```sh
ashare-data local-daily query --mode compatible \
  --root ~/.local/share/ashare-data --symbol 601313.XSHG \
  --start 2013-05-27 --end 2013-05-28 \
  --alias-capture ~/.local/share/ashare-data/raw/bao-alias-sh601360-old-period

# 不提供 alias-capture 时，只使用库内原代码的来源。
ashare-data local-daily query --mode compatible --source sina \
  --symbol 600656.XSHG --start 2016-05-10 --end 2016-05-11
```

```python
from pathlib import Path
from ashare_data.storage.local_daily import LocalDailyStore

store = LocalDailyStore(readonly=True)
try:
    prices = store.get_price(
        "601313.XSHG", start="2013-05-27", end="2013-05-28",
        mode="compatible",
        alias_captures=[Path.home() / ".local/share/ashare-data/raw/bao-alias-sh601360-old-period"],
    )
    amount_source = prices.iloc[0]["field_sources"]["amount"]
    diagnostics = prices.attrs  # 包含覆盖、未返回日期及每个日期的状态依据
finally:
    store.close()
```

兼容模式按证券日选择基础观察，默认顺序为 `baostock → eastmoney → sina → sohu → tencent → baostock_alias`。
该顺序优先使用含显式状态和量额的原代码来源，回溯别名放在最后；不代表高优先级永远正确。
显式 `source` 在兼容模式中表示优先来源，其他来源可以补其缺日或空字段；它不再是单源过滤器。
同优先级归档以观察内容身份排序，全部版本和分歧仍返回，不偷偷选择“最新”归档。

基础行必须通过已知原始单位、有限数值、OHLC 次序、标记和值域检查。
只有四个未复权 OHLC 和所有双方非空字段都以 Decimal 精确一致时，才允许补
`volume`、`amount`、`preclose`、`tradestatus`、`is_st` 的空值。
已有值不会被覆盖，价格字段来自同一根基础日线；整日回退同样从一根完整观察开始。
量额须对应正成交量，成交额还须在最高/最低价与成交量对应范围内，价位检查容纳一分钱显示误差。
金额供给方可以没有成交量，此时使用基础行已经确定的正成交量进行组合校验；
供给方若有成交量，仍须与基础行精确一致。不会因另一行有金额就推造未知成交量。
字段拼接本身没有误差容限：搜狐/腾讯显示精度导致的差异也保留为冲突，不悄悄四舍五入。
多个合格候选对待补字段有分歧时，该字段继续为空。

库内原代码记录沿用既有采集适配器的未复权、元/股、股、元契约，返回原文哈希与响应身份；
普通查询不会逐次重解全库原文。未知来源、异常量额单位/非有限数值不会用于补齐。
别名必须显式选择归档，每次复用 `get_alias_price` 的报文哈希、请求身份和公告校验，
同时受日历、上市区间以及 `[issuer_ipo, effective_date)` 约束，返回历史代码、供应商代码和公告身份。
该回溯映射不声称历史时点可知，也不自动跨不同代码的复权基准。

输出 DataFrame 增加以下诊断；兼容模式 CLI 返回 `{"rows": [...], "metadata": {...}}`，
其中 `metadata` 与 DataFrame 的 `attrs` 对应，包含未返回日期的全部观察和原因。

| 字段 | 含义 |
| --- | --- |
| `base_source` / `base_observation_id` | 整根基础观察的身份 |
| `field_sources` | 每个字段独立的来源、响应/原文、观察时间；别名另含归档、公告和有效期；空值为 null |
| `filled_fields` | 此次确实补入的字段 |
| `observations` | 当日各来源原值与身份，包括未选用和拒绝的候选 |
| `conflicts` / `has_conflicts` | 逐字段分歧、候选拒绝原因、状态冲突；包括未参与拼接的来源差异 |
| `trade_state` / `status_evidence` | 来源活动、明确停牌、状态冲突或未知；并列公告事实/推断和证据哈希 |
| `completion` | `original`、`fields`、`fallback_day` 或 `alias_day` |

补入字段后，`source="composed"`，顶层 `row_hash`、`response_id`、`observed_at` 为空，
避免把派生行冒充成一条原始响应。完整原值在 `observations`，返回值的身份以 `field_sources` 为准。
日历开市和来源活动都不是可交易性保证；不要仅凭返回了一行就认定该日确有成交。

来源身份可信的观察明确停牌，或本地已审阅公告与来源活动冲突时，禁止补字段。
未知单位/复权来源和未经验证的别名只保留在观察与拒绝诊断中，不能提供成交或停牌状态。
可信来源即使 OHLC 损坏，其明确停牌标记仍作为阻断证据；状态冲突身份只列实际参与判定的观察。
状态证据复用 `reports/issuer-suspension-evidence.json`、交易所公告转载、代码变更公告，
以及 `source-status-conflict-002647.json`、`trade-status-followup.json` 中的有界事实。
查询重新核验原文哈希，记录报告哈希；相关证据损坏会报错，不静默退回更宽松的规则。
600656 的“最后交易日之后”明确标作推断，区别于发行人明确停牌。
没有原生行的别名停牌占位行只进入 `metadata.unfilled_days`，不制造交易行或增加原生覆盖率。
NULL 始终保持 NULL，未使用收盘价乘量替代成交额，也未将未知空值归零。

覆盖按当前请求与有效日期计算，区分 `native_observed_days`、`returned_days`、`missing_days` 和补字段计数。
原库 `coverage` 表不会变更。保守的精确一致规则会留下仍有其他来源数值、但兼容性不足的空值。
对显式本地库可运行 `scripts/accept_local_compatible.py` 生成本次验证，输出放在库的 reports 中；不分发历史验收计数。

## 历史范围与免费来源

主范围为上交所主板 A 股和深交所 `00xxxx` 股票，现存及在区间内终止的证券一起纳入。
从上市日开始，交易所终止上市日作为退出边界。`002xxx/003xxx` 原中小板历史同时保存，
`universe()` 默认在 2021-04-06 前排除它们；`main_board_only=False` 或 CLI `--include-sme-history` 可查询完整谱系，原始数据仍保留这段历史。
显示名称是资料观察时的名称，不伪称历史当日名称。

交易所当前/终止清单仍会遗漏改代码的历史证券。因此另外使用 BaoStock 全量基本资料和
逐月历史名单发现旧代码，原始名单和日期对应的成员/状态分别留在 `source_captures`、
`universe_membership`。来源日期冲突保留在 `metadata_conflicts`，不会用供应商日期覆盖交易所清单。
清单自身也可能与发行人的正式摘牌公告冲突。本轮对全部 15 处双源历史退出日期差异
逐份核对公告，并扩大检查到所有 225 个保留退市标的，又发现 600680 的双源日期一致但
与公告不符，共修正 16 处。相关清单日期均比正式摘牌日晚一个开市日，例如 600190 的清单退出日为
2025-07-28，公告明确为 2025-07-25。以 `issuer_exit_notice` 事实修正同一上市区间的
查询边界，原始清单事实仍留存，后续例行清单刷新也不会抹掉已核实的修正。
公告原文与两方日期见本地 `reports/listing-boundary-followup.json`。
其余逐股公告检索和日期证据见 `reports/delisting-notice-audit.json`，其中 000038 的日期
由公告明确的“整理期届满次一交易日”规则和实际 15 个交易日共同支持，并非公告逐字给出的日期。
历史名单中的显示名称可能由供应商回填当前/最终名称，不当作历史名称使用；例如该源的
2010-01-04 名单中 `sz000001` 显示“平安银行”。`name_is_historical` 因而保持 false。
即使某日有来源名单，历史股票池仍按已知上市区间过滤；供应商在正式退市日保留的条目
原样留存，并列入 `membership-boundary-observations.csv`，不会因此重新进入当日股票池。
对只能由 BaoStock 发现的旧代码，原始 `outDate` 保留且在范围中包括该日；它可能是最后
交易日，法律上的正式退市日期仍需另证，不将此边界伪装为官方终止日期。

公司首次上市日也不一定是当前代码开始使用的日期。`code_events` 保存发行人公告证据，
将 600849→601607、601313→601360、000022→001872、000043→001914 按公告生效日分开。
四组变更经 2010-01-01 至 2026-10-14 的巨潮“证券代码”标题检索全部 97 条结果核对；
这是一份截至该日的核查，未来新增事件仍需更新目录。601313 的报价代码启用边界为
2018-02-28，股份登记变更为 2018-02-23，两个日期分别保存。
供应商把新代码回填到变更前的行情和历史名单时，原始记录仍留存，查询与覆盖按代码有效期过滤。
已核实的代码事件也用于截止日早于切换日的历史窗口；这是回溯重建，不声称当时已知这些公告。
旧代码被来源名单遗漏而新代码被回填时，按公告恢复旧代码，并标明映射依据；不沿用其交易状态。
`delisted_securities` 排除这类纯代码退出，`retired_codes` 同时包含真正终止上市和代码退出。
公告明确的停牌区间可标为 `announced_suspension_without_bar`，不会伪造价格行。

```sh
# 单匿名连接、逐页原报文核验、已完成请求自动跳过
.venv-local-data/bin/python -m ashare_data.providers.baostock_collection --all-bars
# HTTP 采集结束或暂停后，在本地写锁下导入补充材料
./scripts/local-daily.sh import-bao
./scripts/local-daily.sh resume
```

BaoStock 采集器独立保存原文，不与 HTTP 进程争写行情库，使用已有的机器单连接锁。
不带 `--all-bars` 时先取完整基本资料，再取退市日线和逐月历史名单；加上该选项则补取
全部范围的原始 OHLCV、成交额及状态。已经归档的区间跳过，`--repair-gaps` 重新规划该源
仍缺的日期段；全量基本资料每天保留新的观察快照。每 50 个请求尝试导入，HTTP 写入器
仍活跃时延后；结束时再尝试一次，也可手工 `import-bao`。连续故障没有新进展时，
第三次失败停止并返回非零退出码，重试间隔 10/30 秒；已成功归档新数据后重新计数。
这样长批次不会因间隔很久的三次偶发断线而停止，也不会在持续失效的端点上无限重试。
不切换身份或端点。`STOP_BAOSTOCK` 可在请求边界停止。
当前 SDK 读取 JSON 时会删除字符串中的空白；仅基本资料/名单的 `code_name` 字段允许对
该已知行为作精确比对，最终保留独立解码的原始名称。其他差异拒绝导入。
压缩日线外层完整性算法仍未认证，沿用已有 AD 的研究数据声明。

| 来源 | 用途与单位 |
| --- | --- |
| 上交所、深交所公开清单 | 上市/退出日期、当前和已终止主板标的；保存获取时点 |
| 新浪历史交易日期 | 源开市日集合；非工作日推算；与独立日历作核对 |
| 东方财富原始日线 | 元/股、不复权；量以源“手”乘 100 转股，保留百股舍入精度；金额元 |
| 新浪历史压缩日线 | 元/股、不复权、量为股；没有的成交额保持 NULL |
| 搜狐历史行情表 | 元/股、不复权；成交量“手”乘 100，成交额“万元”乘 10000；分别为百股、百元显示精度，不代表逐笔准确度 |
| 新浪 hfq 因子 | 整个有效日期/乘数集合按观察版本固定；1900 基准行不是公司行动 |
| BaoStock 日线/状态/因子 | 原始价、股、元；`tradestatus=0` 才是该源明确停牌状态 |
| 腾讯原始日线 | 原代码价量补缺及独立核对；量手乘 100，成交额缺失；原文可能附带除权事件说明 |

复权因子是供应商计算结果，各源可能有不同基准、事件或舍入口径，必须按来源和固定版本使用。
空因子响应不等于没有公司行动。获取时间不是历史发布时间，以上不承诺历史 PIT。
部分旧代码的因子 URL 会继续返回新代码期间的事件。完整原文与版本仍保留，
`factor_snapshots()` 同时给出代码生效/退出边界和退出后因子行数；这些后续事件不属于
已退出的旧报价代码。例如 601313 的来源快照延续到 2026 年，原代码报价期在 2018 年结束。
`reports/factor-event-crosscheck.json` 保存分层事件日期核对；两处未有腾讯事件标记的日期
经发行人公告追查，明细在 `factor-event-followup.json`。000022 的 2018-05-23 除息发生在
停牌期间；000033 的转增权益日期在 2016 年，开盘参考价到 2017-05-24 恢复交易时调整。
因子采用的价格调整生效日不能直接冒充法定权益日期；这项抽查不等于全部公司行动认证。
真实交叉核对也发现少量源价格分歧；许多新浪成交量与另一源的差异符合 float32 舍入特征，
“股”是单位，并不表示供应商逐股精度已获认证。保留原值、差异报告和第三来源核对结果，
不按多数票自动改写历史。

未观察到 bar 的开市日标记 `missing_unknown_not_proven_suspended`，不能由“没有行情”推断
停牌；零成交量也不会自动变为停牌。暂停/退市前长期无交易应以明确状态数据解释。
`--repair-gaps` 对未知缺口作实际请求；若上游仍无数据，缺口继续保留。
搜狐抽样同样存在历史价格、金额差异，且部分股票少于其他来源的观测日；保留独立源，不用金额非空代替质量认证。
在 6 只沪市样本中，2017-03-31 和 2017-04-10 的搜狐价量/金额出现集中分歧，
同期新浪（或东方财富）与 BaoStock 的价格一致。明细保存在本地
`reports/sohu-source-day-followup.json`；默认按日期覆盖选择单一来源，不等同于自动质量认证。
