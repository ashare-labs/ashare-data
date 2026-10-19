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
