# 维护与查询本地日线库

本地库保存显式采集的多来源原文、来源行和修订，适合长期日线维护。维护 CLI 的显式 `--root` 优先；未提供时读取共享部署清单的日线资产。只有完全未配置清单时才使用 `~/.local/share/ashare-data`。SDK 的构造参数继续显式选择库；发现配置不会搬库、更新数据或启动后台服务。

## 本地日线

```sh
UV_PROJECT_ENVIRONMENT=.venv-local-data uv sync --locked --no-config --python 3.12 --extra local-data
scripts/local-daily.sh --help
scripts/local-daily.sh --root "$DATA_ROOT" update --start 2010-01-01
scripts/local-daily.sh --root "$DATA_ROOT" status
scripts/local-daily.sh --root "$DATA_ROOT" resume --retry-failed
```

shell 入口只选择 `.venv-local-data` 中的 CLI，可用 ASHARE_LOCAL_DATA_ENV 指定已有环境。DATA_ROOT 是用户明确选择的数据目录；先查看 `--help` 确认本次范围。`update` 默认从 2010-01-01 至源时钟下最新可采集日，18:00 前不把当天纳入完成范围。日线、证券清单、日历和源累计因子分别存储，不按价格自动推导状态。

| 操作 | 用途与边界 |
|---|---|
| update / resume | 显式采集或继续已登记任务；成功原件和修订留存，重复数据去重 |
| update --repair-gaps | 对未知缺口实际请求；来源仍缺行就保持未知 |
| supplement --source sohu / tencent | 对显式证券/日期补采独立来源，不自动改写原来源行 |
| `scripts/complete_local_daily_batch.py` | 按显式参数完成 HTTP、BaoStock 和搜狐批次；它不是定时服务 |
| `scripts/accept_local_daily.py` / `audit_local_daily.py` | 为指定库生成本次覆盖/一致性检查，不能用旧报告数值证明新库 |

HTTP 任务和 BaoStock 队列分开；HTTP 默认3 workers、主机间隔1秒、有限超时和最多3次尝试。401/403/429 暂停该主机，连续传输失败进入30分钟冷却。根目录 STOP 允许正在进行的请求结束后停下；完整批次还使用 STOP_BAOSTOCK。重开会把未完成 running 工作恢复为 pending，不能靠删除状态绕过失败。

本地目录包含 daily.sqlite3、raw 压缩原文及摘要、reports、任务/源状态。SQLite WAL 支持采集时读取，写者锁保护任务；源码或安装包不携带这些数据。不要清理活跃锁、原件、回执或失败诊断，不把库目录作为测试/构建输出。

## 查询与来源选择

```python
from ashare_data.storage.local_daily import LocalDailyStore

store = LocalDailyStore("data/local-daily", readonly=True)
try:
    result = store.get_price("600000.XSHG", start="2020-01-02", end="2020-01-10")
    print(result)
finally:
    store.close()
```

日线原价、股数、人民币元、源前收及累计因子分别保留；数值与缺失原因按来源返回。默认 auto 为整个请求范围选择观测天数最多的一份来源，平局按金额覆盖；它不是逐字段多数票或质量认证。source 显式选择不意味着其他来源被删除。相同来源修订另存，不覆盖旧观察。

主范围为上交所主板与深交所 00xxxx，包含区间内已退出证券。universe 默认在 2021-04-06 前排除原中小板 002/003；main_board_only=False/--include-sme-history 可查其谱系。显示名称是本次资料的名称，name_is_historical=False，不回填成历史名称。

覆盖分母依赖交易日历、上市/代码有效区间，不能用 bar 数倒推日历。unique_security_days 与多来源 bar_rows 分开；未请求缺口为 awaiting_collection，已请求无记录为 missing_unknown_not_proven_suspended。零量和缺行都不自动证明停牌；原件超范围仍保留，但不进入当前覆盖。

## 显式兼容补字段

跨源补齐必须选择 `get_price(...,mode="compatible")` 或 `query --mode compatible`；它不会修改原库行或覆盖表。基础来源依次考虑 baostock、eastmoney、sina、sohu、tencent，再考虑已验证别名；显式 source 是优先选择，不是无条件跳过验证。

- 候选必须同证券/同日/原价和已知单位，OHLC 及所有共同非空字段 Decimal **精确相等**。不按容差抹平差异。
- 只补缺失 volume/amount/preclose/tradestatus/is_st，不覆盖已有值。未知仍 None；不得用收盘价乘量造金额。
- amount 的合理性还需正量与 high/low 的分位展示界限；它不是允许其他字段误差的容差。量是“股”也不证明来源逐股精度。
- 来源明确停牌，或已核对公告与来源交易状态冲突，就阻断补值；可信身份的坏 OHLC 行仍可贡献停牌阻断，不被选择性忽略。
- 每个补值保留 field_provenance、完整 observations/conflicts。组合行的单一 raw ID/hash/time 为 None，不能假装来自一份原子响应。

返回区分 native_observed_days、returned_days、missing_days 和补字段计数；没有原生行的别名停牌占位只进入 metadata.unfilled_days，不制造交易行或增加原生覆盖。相关证据缺失、hash 不符会报错，不能静默使用更宽松规则。`scripts/accept_local_compatible.py` 针对明确本地库生成当前验证。

## 早期历史

`query-early` / `get_early_observations(security,start,end,source=None,diagnostics=False)` 默认范围为1990-12-01～2009-12-31，两端包含；单传一端不改变另一端默认。现代 query/query-alias 仍默认从2010-01-01开始。早期范围外日期拒绝，strict_usable=False。默认返回独立 HTTP 来源行；diagnostics=True 才显示未准入 BaoStock 和 HTTP 异常行。

`python -m ashare_data.market.early_history` 的 prepare 使用已归档名单，http/plan-tencent 显式采集、优先复放固定原件；连续3项服务错误或401/403/429停止该源本段提交，未发出任务为 deferred_source。STOP_EARLY 停止后续提交。retry-invalid-sohu 仅重放可隔离异常行的旧失败，不重试身份错误。早期新值不覆盖已有同源同日观察，不加入现代公共日历；周末原观察也不因现代日历删除。

BaoStock 早期工具用 archive_only=True，原件进入 raw/early-baostock，有限尝试完成后 --import-only 导入 early_diagnostics，不写 bars；分页结束/双解码一致仍不能解决 type96 外层未知。代理日历 gap 不是已证实漏采；成功空集保持 empty_unknown。1900-01-01因子仅是基准占位，不是公司行动。

来源最早可见日只是这次获取到的起点，不是市场最早成交日。BaoStock 基本资料/逐月名单、交易所清单和发行人公告分别记录，供应商 outDate 不会自动覆盖交易所/发行人事实。已核对 issuer_exit_notice 修订保留原清单来源，例行刷新不抹除修订。

改代码期间必须有显式代码区间和公告证据。旧代码捕获、换算单位和有效日期校验后才能参与别名查询；不能把供应商按新名称回填的旧名单作为历史名称证明，也不能因正式退市日仍有供应商条目而重新纳入股票池。

早期采集/查询入口和参数见 `scripts/collect_early_baostock.py --help`、`scripts/report_early_history.py --help` 与 LocalDailyStore 的公开方法；接口不会自动请求任意历史。别名和边界证据在调用方 reports 中，按固定原文/hash 复验，不从本机路径或旧报告结论推导资格。

累计后复权因子用于价格调整，不是完整现金/股份事件账本。分红支付、法定权益日期与价格调整生效可不同；股本、配股、到账及可卖日期由 [公司行动](corporate-actions.md) 的独立事实处理。来源分歧保留原值与诊断，不自动按多数票改历史。

## 查询与发布产品

`LocalDailyProduct` 从显式选择的 `daily.sqlite3` 提供证券搜索、分页覆盖和原价查询。每个请求使用只读 SQLite 事务，返回 JSON 数值字符串、缺口、原始来源行 hash、响应摘要与读回执；不联网、不搬库。目录中的来源范围是公共日历内的已有观察，名称不是历史名称；实际查询还受上市和代码有效期约束。日历之外的早期原观察仍保留在主库，由早期观察接口读取，不能当作完整产品日历。

```python
from ashare_data.product import LocalDailyProduct
from ashare_data import Store

product = LocalDailyProduct.from_deployment("data/deployment.json")
print(product.catalogue(query="600519", limit=20, offset=0))
print(product.prices(["600519.XSHG", "000858.XSHE"], "2024-02-19", "2024-02-28"))
selection = (["000651.XSHE", "000858.XSHE"], "2022-04-01", "2022-04-15")
print(product.coverage(*selection))  # 只读检查，不创建 Store 或账户
release = product.publish("data/releases", *selection)  # 默认 covered_research
view = Store("data/releases").local_daily_product(release["dataset_id"])
print(view.describe())
portfolio_input = view.read()
schedule = view.read_corporate_schedule()
coverage = view.read_coverage()
# AQ 产品入口验证这份发布，再用现有 corporate_export.attach 附加账户模型后导入。
```

日期是调用示例，必须在所选库和证据包中确实覆盖；没有数据就报缺口。`source="auto"` 在用户选定窗口内为每个证券选择一整份行情来源，发布固定这些来源后再读取预热；某来源预热不足就拒绝，不用其他来源拼补或静默替换。可显式指定来源重新查询。每次查询最多 100 个证券、100000 个证券交易日；普通交易状态准入最多 50000 个证券交易日（每个交易日核对 BUY/SELL）。每份输入不超过 64 MiB；这些是资源预算，不是固定证券或日期白名单。

发布必须具备窗口之前完整的 120 个源日历交易日、窗口所有真实行情与后继日历。缺行情、非法 OHLC、无来源回执或上市范围之外会返回 `PRODUCT_DATA_GAPS` 和具体字段/日期；起止须是实际交易日。缺日历/预热返回 `PRODUCT_SESSION_UNAVAILABLE` / `PRODUCT_WARMUP_UNAVAILABLE`。主库目录不能作为发布输出目录，失败的输入不初始化 Store。

普通发布 `covered_research` 通过指定参考集合的 `DailyResearchView + DailyExecutionView` 或 `HistoricalMarketView` 公共接口验证所选证券日，并从 `CorporateHistoryView.export_aq` 冻结完整公司行动响应。主库原始行情始终作为价格来源；参考包快照不替代行情。主库与参考的日历、停牌、ST、preclose，以及原始 OHLC 与限幅不兼容时给出具体缺口。已知停牌保留 true；未知字段、上市/退市边界、源冲突和当前不支持的无限价状态不会准入。参考缺口返回 `PRODUCT_COVERAGE_GAPS`，不会自动改用其他集合或诊断。

普通基底保留结构化 `reference_evidence`：原始状态行、规则与 assumption_ids、一次批量准入响应、绑定身份和读取回执。`derived` 限幅继续标为规则模型，不能变成交易所直接源限价。公司行动独立存放于 `corporate-schedule.json`，AD 不计算账户资金/股份。AQ 附加自己的税前、日期级到账模型；源的 PIT、执行许可和全类型完备性 false 标记仍保留。合法空窗口仅是来源范围无事件，不等于全市场不存在行动。

**价格诊断**仍可显式选择 `mode="price_only_no_limits"`：排除公司行动现金/股份和价限，不能解释为总收益或完整经济回测。该模式保留源 `tradestatus`；缺标记时正成交量只支持“事后已发生交易”模型，零量不推断停牌。未配置参考包仍可查价格和主动选择诊断，但不能得到普通发布。

同一 `ashare-deployment.v1` 清单可同时供 AD/AQ/web 选库。AD 独立入口的优先级为 `--deployment` > `ASHARE_DEPLOYMENT` > `ASHARE_DATA_ROOT/deployment.json` > `$XDG_CONFIG_HOME/ashare/deployment.json`（默认 `~/.config/ashare/deployment.json`），显式 `--root` 则直接选择主库。`data_root` 相对清单目录解析，资产相对 `data_root` 解析；正式主库可登记为原位绝对路径，无须复制。维护 CLI 与产品 CLI 共享发现规则，损坏清单不会回退旧库；直接构造 `LocalDailyStore()` 的 SDK 默认目录保持不变。

```json
{
  "schema": "ashare-deployment.v1",
  "data_root": ".",
  "assets": {
    "main": {"kind": "local_daily", "path": "ad/market/local-daily", "role": "market", "retention": "preserve"},
    "releases": {"kind": "store", "path": "ad/releases", "role": "publication", "retention": "preserve"},
    "research": {"kind": "daily_research", "path": "ad/reference/research", "binding": "binding.json", "binding_sha256": "<固定64位SHA>", "role": "market_reference"},
    "execution": {"kind": "daily_execution", "path": "ad/reference/execution", "binding_sha256": "<固定64位SHA>", "role": "market_reference"},
    "corporate": {"kind": "corporate_history", "path": "ad/reference/corporate", "binding_sha256": "<固定64位SHA>", "role": "corporate_source"}
  },
  "profiles": {
    "product": {
      "kind": "product", "daily_asset": "main", "release_store_asset": "releases",
      "control_db": "aq/product/control.sqlite3", "paper_root": "aq/product/paper",
      "api_port": 18839, "paper_port": 18818, "web_port": 41839, "default_dataset_id": null,
      "reference_sets": {"daily-evidence": {"kind": "daily_execution", "research_asset": "research", "execution_asset": "execution", "corporate_history_asset": "corporate"}},
      "default_reference_set": "daily-evidence"
    }
  }
}
```

```sh
python -m ashare_data.product --deployment "$DEPLOYMENT" catalogue --query 600519
python -m ashare_data.product --deployment "$DEPLOYMENT" prices \
  --security 600519.XSHG --start 2024-02-19 --end 2024-02-28
python -m ashare_data.product --deployment "$DEPLOYMENT" coverage \
  --security 000651.XSHE --start 2022-04-01 --end 2022-04-15
python -m ashare_data.product --deployment "$DEPLOYMENT" publish \
  --security 000651.XSHE --start 2022-04-01 --end 2022-04-15
python -m ashare_data.product --deployment "$DEPLOYMENT" publish --mode price_only_no_limits \
  --security 600519.XSHG --start 2024-02-19 --end 2024-02-28
```

SHA 占位符须替换为经过核对的绑定身份。另一类集合为 `{"kind":"historical_market","market_asset":"market","corporate_history_asset":"corporate"}`，对应 `historical_market` 资产。`--reference-set` 或 `from_deployment(..., reference_set=...)` 只使用明确选择的集合；未指定时使用配置默认值。直接 Python 调用可传 `references={name,kind,research?/execution?/market?/corporate:{binding_path,binding_sha256}}`，各 binding_path 是包内入口文件。

发布追加到独立 Store 的 `catalog.sqlite` 和内容寻址目录；普通目录内为 `input.json`、`corporate-schedule.json`、`coverage.json`、`manifest.json`。manifest 分别绑定路径、字节 SHA 和大小；`coverage_sha256` 是完整 coverage 去掉该字段后的规范 JSON 语义摘要，`coverage_file_sha256` 是文件字节摘要。`source_bindings` 按 research/execution/market/corporate 角色保存 kind 和 binding_sha256，不把机器绝对路径用作源身份。Store 重读校验文件、模式、范围、参考集合、逐日源字段投影和 schedule；并发相同发布会串行校验并重用同一内容身份。

复制 Store 后，读取冻结输入和恢复已有任务均不要求原 PDF 在线。任务恢复还需要 AQ 控制状态、策略、日志、账户、native 检查点、未结权益和回执，不能只备份 SQLite。要在恢复环境进行新发布，需要保留所选参考包的 binding 及全部被绑定文件，以及正式行情库；小型参考包应随能力备份保存。主库原始响应压缩正文继续原位保留，发布回执引用 SHA；完整主库源备份须另外保存一致 SQLite 快照、raw、采集状态和库外证据。

可离线检查任意已配置范围及行情来源一致性，不采集或复制主库：

```sh
python scripts/verify_local_daily_product.py --deployment "$DEPLOYMENT" \
  --security 000333.XSHE --security 000651.XSHE --security 000858.XSHE \
  --window 2020-02-03:2020-02-14 --window 2022-04-01:2022-04-15 \
  --output data/verification
```

该检查验证数据发布和冻结重读；原生账户、故障恢复与资金成交一致性由 AQ 独立验收。

来源协议和解码依赖见 [提供者](providers.md)、[开发工具职责](development.md)。行情/可见性保证见 [行情查询](market-data.md)，历史股票池严格接口见 [历史市场](historical-market.md)。

## 行情发布与恢复

正式行情采用两个逻辑事实表：`market.bars_1d`、`market.bars_1m`。底层是按证券、系列排序的不可变 Parquet 分块，由固定文件清单定义一个 release。DuckDB 1.4.3 负责查询；SQLite 继续保存小型元数据和既有账户账本。读者打开时固定 release 和元数据摘要，构建下一版不会改变正在运行的查询或回测。

### 选择依据

同一有界真实样本包含全市场一个月与少量证券多年历史，共 32,860,481 行。证券优先 DuckDB 原生文件 1,166,028,800 字节，日期优先 1,223,438,336 字节，Parquet 361,699,364 字节。单股多年查询首次/暖查询分别约 0.009/0.005 秒、0.331/0.057 秒、0.023/0.019 秒；全市场日扫描分别约 0.102/0.059 秒、0.011/0.010 秒、0.282 秒。首次查询使用新进程，但没有清空操作系统缓存，不能称为物理冷盘测试。

Parquet 查询进程峰值约 86 MB，两个原生布局约 821/858 MB。试验验证了逐键逐值双向对账、分块替换、旧清单读取与恢复。原生 DuckDB 写进程会阻止独立进程同时打开该库；不可变文件允许导入、API、回测各自打开只读查询连接。基于容量与进程访问方式选择 Parquet，不增加数据库服务。同一已排序样本的补充写入测试为原生库加载并 checkpoint 6.214 秒、Parquet 写出 4.824 秒；原生单行修改并 checkpoint 0.017 秒，Parquet 改一行并重写该证券 971,041 行分块为 0.175 秒、12,423,812 字节。该测量不含首次解析/排序，过程峰值内存约 1.934 GB，试验目录峰值约 4.312 GB。完整实测报告随首轮发布证据冻结，不能把样本秒数外推成全库性能承诺。

全量 R1 的清单摘要为 `99f0ed21634408c7b325f851518f9db5ed596484c1b0a143dc11c79e6c543391`。事实文件 44,946,435,908 字节，证据 1,578,062,169 字节，元数据 44,462,080 字节；导入批次峰值 RSS 为 4,025,466,880 字节。全分钟 SQL 视图首次验证所有对象摘要并建立视图耗时 25.105 秒，其后全市场 2024-07-01 日查询为 2.864/2.812 秒、7 月查询为 3.069/3.051 秒，查询进程峰值 RSS 为 1,051,951,104 字节。该实测与独立恢复/回测同时进行，未清空操作系统缓存。

两个真实原生回测窗口（2020-02-03 至 02-07、2024-01-02 至 01-05）在原始库与 R1 分别执行，价格逐值、持仓、订单、成交、费用、净值与决策信号均一致，重启结果一致。范围为明确选择的 `price_only_no_limits_not_total_return` 研究诊断，普通回测所缺的交易参考不会因此被视为齐全。

### 目录与正式入口

部署文件的 `assets` 登记一个 `kind: market_release` 的受控目录，产品 profile 的 `market_release` 必须同时固定 `asset`、正整数 `release_id` 和 `manifest_sha256`。`default_minute_source: canonical` 选择主分钟系列；`auto` 只表示这个冻结默认选择，不会因空结果或来源故障转用另一来源。

资产目录仅包含：

- `objects/`：内容摘要命名的事实分块、元数据及证据。
- `releases/00000001.json`：不可变完整文件清单，含上一版清单摘要。
- `CURRENT.json`：串行发布后的原子指针。未完成对象不会成为默认可见数据。
- `work/`：可恢复批次日志与明确受控的暂存。

首轮构建预算 200 GiB，保留至少 150 GiB 磁盘空闲；查询/导入限制线程、内存和溢写空间。达到预算停止新分块，已完成分块可续跑。源 SQLite、HF、free-stockdb、TDX 原件保持原位。不要删除原件、已发布对象或旧清单；备份只复制清单引用的正式对象，不产生第二套全量 raw 库。

### 数据解释与输入去向

日线采用冻结的全行来源顺序 baostock、eastmoney、sina、sohu、tencent。同键冲突是明确研究选择，不是交易所真值认证；不按查询窗口重新排序，不跨源填字段。每条原始输入保留互斥的去向：采用、同值重复、已裁决未用、未决隔离、格式拒绝。目标唯一键数另计。

首轮日线 38,736,814 条输入：采用 10,736,431，同值重复 13,057，已裁决未用 20,638,484，隔离 7,348,842，格式拒绝 0。旧 14,394,671 是原始证券日键统计，既不是输入行数，也不是已验证目标键数。四条历史代码链汇合 3,694 个重复身份日后，共 14,390,977 个稳定身份日键；其中采用 10,736,431，未决 3,654,381，已裁决未用 165。2010 年以前单位/复权尚未证实的记录及无依据修复的异常保留在原件与精确去向清单中。

日线价格与金额采用无隐式舍入的 `DECIMAL(38,12)`，成交股数仅在整数无损时转 BIGINT。HF 保持原始 DOUBLE 二进制值和 BIGINT 股数，包括 FLOAT32 提升后的小数尾数；时区转换保留同一时刻，不移动 09:30 标签。来源时区、倍率、原字段解释放在冻结来源/映射契约中。目标系列只定义目标语义。TDX 十进制源另属系列 3，不与 HF 系列 2 混为已验证同区间行情。

`MarketRelease.read` 返回各系列本来的 float 或 Decimal。通用 SQL 分钟视图的价格/金额使用 `UNION(binary_value DOUBLE, fixed_value DECIMAL(38,12))`，防止混合查询悄悄把十进制舍入为浮点。算术应先限定系列，再用 `union_extract(close_price, 'binary_value')` 或 `fixed_value` 提取对应类型。

证券 ID 在后续发布中不复用。四条有保留发行人证据的历史代码链汇合到稳定 ID；源文件以新代码回填历史的别名与交易所代码有效期分表。其他身份保持 provisional，不把未知历史代码复用说成已核实。元数据、日历、来源契约及质量规则随发布冻结。

首轮 HF 的 5,795 个文件共 3,475,824,481 条输入，采用 3,475,793,900 条、隔离 30,581 条；没有同值重复、已裁决未用或格式拒绝。TDX 独立系列采用 960 条，因此两张正式逻辑表分别有 10,736,431 个日线键和 3,475,794,860 个分钟键。全部四类来源合计 4,085,305,933 条范围内输入，每条均有互斥去向。

HF 最后观察日 2026-08-07 仅到 10:21，不能当作完整日。09:30、午间、收盘边界与区间开闭仍有未决语义。零量不证明停牌，缺行不补成零量。覆盖检查包含整个交易日无行和全部缺失证券；来源已声明分母、完全缺失与分母未知分别统计，分钟预期行数保持未知，不硬套 240/241。

HF 覆盖分母按来源冻结：14,502,748 个预期证券日中，14,427,205 个有采用行情、75,543 个全日无采用行情；69,928 个来源确认停牌日为不预期存在，另有 263,414 个证券日的预期状态未知。该分母不是独立交易所历史全市场认证，不能从“有行”推导日内完整。

free-stockdb 在冻结的 5,890 个权益代码范围内完成原始键审计：日线 17,429,373 行、分钟 553,314,305 行均保留隔离状态；基金等未纳入该权益范围，不能把范围计数称为整个 LevelDB 的独立全市场覆盖。与现有源同代码同标签重叠的日线为 13,219,692 行、分钟为 495,157,676 行；分钟中 56,504,126 行的六个原生数值相同。日线十进制文本与二进制浮点没有被武断标成相等或不等。这些重叠统计是诊断，稳定身份别名另行冻结；单位/区间未证实的记录不进入目标去重选择。财务、交易参考、因子本轮不导入；未来财务的来源修订版本和解析版本必须分开，context 绑定报告及抽取版本。

### 查询

```python
from ashare_data.storage.market_release import MarketRelease

market = MarketRelease("/controlled/market", release_id=1,
                       expected_manifest_sha256="<frozen manifest SHA256>")
rows = market.read("bars_1d", "600036.XSHG", "2024-01-02", "2024-01-05", series_id=1)
with market.sql(tables=("bars_1d",), securities=["600036.XSHG"]) as db:
    values = db.execute("SELECT trade_date, close_price FROM market.bars_1d ORDER BY trade_date").fetchall()
```

`quality.bar_day_checks`、`reference.instruments`、`reference.instrument_identifiers` 和小型 `meta` 登记由同一清单加载。日线产品、分钟产品和研究发布通过 `LocalDailyProduct.from_deployment`、`LocalMinuteProduct` 实际读取这些分块。回执和研究输入含 release 摘要；账户运行时不随行情读者变更。

### 构建、增量与恢复

在选定受控目录运行首轮构建；重复命令从已验证的批次日志恢复，不增加重复事实。

```sh
python scripts/merge_market.py --entry "$ENTRY" --root "$MARKET" core
python scripts/audit_stockdb_merge.py --entry "$ENTRY" --root "$AUDIT"
python scripts/finalize_market.py --entry "$ENTRY" --root "$MARKET" --stockdb-audit "$AUDIT" --publish
python -m ashare_data.product.release_cli --root "$MARKET" verify
python -m ashare_data.product.release_cli --root "$MARKET" --release-id 1 backup --destination "$BACKUP"
python -m ashare_data.product.release_cli --root "$BACKUP" restore --destination "$RESTORE"
```

首轮脚本只针对已经冻结的来源清单；原件改变会阻断恢复，不能在相同 source version 下静默接着读。备份目标带来源清单标记，可用相同命令继续未完成复制。恢复先校验全部对象，再原子发布目标指针；旧 release 和当前值都保留。原始来源另按登记的保留策略保存；正式备份不声称复制了外部原件。

全量恢复演练校验 27,931 个正式对象、46,568,960,157 字节，在同一隔离目标复制两个对象后主动中断，再续跑完成。随后仅对一条真实行作明确标记的合成修订，验证旧 R1、新 R2 与重复提交无变化；正式 R1 未变。整个演练约 159.103 秒，演练副本在验收后清理，报告保留。合成修订没有写回正式行情。

后续增量使用 `release_cli apply --plan plan.json`。计划 schema 为 `ad-market-replacement-plan.v1`，必须包含父清单摘要、变更原因、来源证据文件及其 SHA256、新元数据文件及其 SHA256、互斥输入去向和 `chunks`。每个 chunk 明确 table/series_id/instrument_id，以及完整替换 Parquet 路径和 SHA256，或显式 `delete: true`。只重写受影响证券分块。旧来源/批次登记不可改；变更值使用属于下一 release 的新批次，未变化值保留原批次。全行相同的重复输入为无变化操作。

文件清单保留被替换块，从而重现旧版本；本实现没有混用稀疏行历史后删除旧文件。发布时检查重复逻辑键、引用、输入去向、逐值回转和父版本 CAS。这是受控管道保证，事实文件没有数据库物理 PK/FK 索引。局部修订会保留旧块，有明确容量代价。

本地 release 重现不等于严格市场 PIT。修订公开可知时间未知时保持未知，不能拿本地导入时间或 bar 结束时间代替。
