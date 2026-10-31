# 财务和交易参考正式表

`ashare_data.product.financial_release` 使用现有 `MarketRelease`、不可变 Parquet、
SQLite 元数据和固定 DuckDB 版本。候选生成与共用发布分开执行；生成候选不会改动
行情 `CURRENT.json`、部署清单或采集安排。

## 表和身份

| 粒度 | 接口 |
| --- | --- |
| 公司/来源主体与证券关系 | 现有元数据内增加 `release_issuers`、`release_issuer_instruments`；SQL 为 `reference.issuers`、`reference.issuer_instruments` |
| 内容版本、抽取版本 | `financial.report_versions`、`financial.extractions` |
| 科目和上下文 | `financial.observations` 长表、`financial.contexts` 视图 |
| 披露、更正、历史可见证据 | `financial.disclosure_events`、`financial.links`、`financial.visibility` |
| 日历、限价、股份、因子 | `reference.exchange_calendar`、`price_limits`、`share_observations`、`factor_observations` |
| 公司行动与辅助状态 | `corporate_action.events`、`reference.security_status` |
| 来源与缺口 | 复用 `source_assets`、`data_batches`、`batch_inputs`、`source_aliases`、`release_contracts`；另有 `meta.financial_captures`、`meta.financial_coverage`、`quality.financial_quarantine` |

公司节点不等于证券 ID。未核实的供应商主体保留 `source_entity_unverified_legal_identity`，
不按名称或换码推定同一法律主体。官方字段复核建立限定报告期的证券关系，不延伸到其他时期。
点查询同时限定冻结证券 ID 和原始来源代码；即使行情注册表将历史换码列为同一证券 ID，
也不会自动读取另一代码的财务或参考观察。直接 SQL 关联时应保留同样的来源代码约束。
日线自带 `tradestatus/is_st` 通过 `reference.daily_attached_status` 视图复用。

内容 ID 排除抓取时钟、解析器和响应内的分页位置；原响应位置在来源关系中保留。
同内容再次抓取增加 capture；更换解析器增加 extraction。科目唯一键表达抽取输出，
不是跨来源经济事实去重。更正必须有精确字段/期间/主体关系，校验端点及循环；
没有明确替代关系的多个版本返回冲突。

金额用十进制文本完整保存。原词面、JSON 类型、规范数值、单位及状态分别存储；
字段缺失、null、空字符串、零不混淆。未核实单位仍保存可解析的原数值，
`canonical_value_decimal_text` 保持空。上市前、旧代码退出后、部分返回、
负股本和股份分类冲突均保留，不通过补零或延长有效期进入默认计算。

## 生成候选

使用明确的已发布父版本与 hash，输出目录必须独立。

```sh
PYTHONPATH=src python -B -m ashare_data.product.financial_release \
  --market-root "$DATA_ROOT/market" \
  --release-id RELEASE_ID --manifest-sha256 PARENT_SHA256 \
  prepare --output "$DATA_ROOT/financial-reference/table-candidates/BUILD" \
  --batch CLOSED_SOURCE_BATCH \
  --official-batch CLOSED_CNINFO_BATCH \
  --daily-review-spec REVIEW_SPEC_JSON \
  --share-resolutions EXISTING_SHARE_RESOLUTION_BUNDLE
```

各类参数均可重复，只传当前需要的来源。普通 `--batch` 支持已存在的新浪、
日历、Tushare 报表/指标投影/分红/复权/限价及辅助日状态规范记录。
`--official-repair` 接受已冻结的缓存修复包，原位核验所引用的公告响应和回执，
仅接入返回代码明确的公告事件。相关代码、冲突及拒绝记录进入隔离表，分页缺口
进入覆盖表；不修改原公告批次，也不生成财务字段或历史可见资格。
导入前取得源分区共享锁，拒绝仍在写入、未访问或运行中的任务。明确失败和空返回
进入覆盖表，未知 schema 或无法映射的记录进入隔离表，原件不丢弃。
首次导入核验回执、原件和规范文件 hash、源字段数值及 Parquet 逐值往返。
源响应原位引用，不复制日线库、分钟库或原始公告 PDF。

已经明确停止且不会恢复的旧批次，可显式使用 `--stopped-batch`。此入口要求原批次的
`STOP`、`worker-exit.json` 及可获得的共享写入锁，冻结所有现存回执哈希和未访问任务清单。
只导入已取得的原件；未访问任务只产生 `unvisited_stopped` 覆盖记录，不生成捕获或事实，
也不触发采集。中断重跑时，停止证据或回执集合发生变化会拒绝复用候选。

每个完成的分片保存断点。中断后用同一参数重跑；已经完成的回执与对象须仍匹配。
映射代码或输入集合改变时使用新的候选目录。保留 8 GiB 空闲空间；不会删除旧源材料。
`candidate.json` 包含逐表行数、输入处置对账、父版本、实现 hash 及数据对象，
`BUILD-PROGRESS.json` 只记录本次离线入表进度。

大批次的全表去重校验仍限制内存，但会使用磁盘临时空间。
`ASHARE_DUCKDB_TEMP_DIRECTORY` 可指定数据根下的独立临时目录，
`ASHARE_DUCKDB_MAX_TEMP_DIRECTORY_SIZE` 可指定本次查询的磁盘上限（默认 8 GB）。
`ASHARE_DUCKDB_MEMORY_LIMIT` 可显式覆盖本次离线作业的连接内存预算；
未设置时保留各调用方原有内存上限。离线协调任务使用临时目录上下文，成功或失败后关闭连接并清理。

`REVIEW_SPEC_JSON` 包含 `official_root`、`review`、`review_sha256`、
`reference_root`、`reference_tasks` 和可选 `correction_reviews`。
它调用现有 `official_daily.reader` 核验 PDF、字段、索引、更正范围和交易日证据，
不能仅靠候选记录里的认证布尔值取得历史资格。

## 读取与关联

```python
from ashare_data.storage.market_release import MarketRelease
from ashare_data.product.financial_release import FinancialTables

release = MarketRelease(market_root, release_id, expected_manifest_sha256=manifest_sha256)
view = release.financial()  # 已发布表
# 尚未共用发布时，显式查看候选：
# view = FinancialTables.prepared(candidate_root, release)

value = view.point(
    "603169.XSHG", "net_operate_cash_flow", "2015-03-31",
    source="cninfo", as_of="2015-06-08T00:00:00+08:00", mode="official_daily",
)
with view.sql(market_tables=("bars_1d",), securities=["603169.XSHG"]) as db:
    rows = db.execute("""
        SELECT json_extract_string(r.body, '$.security') AS source_security,
               o.period_end, o.canonical_field, o.value_decimal_text
        FROM financial.observations o
        JOIN reference.issuer_instruments r USING(issuer_id)
        JOIN reference.instruments i USING(instrument_id)
        WHERE json_extract_string(r.body, '$.security')='603169.XSHG'
    """).fetchall()
```

SQL 公开保留的源观察供审查，不能把全部 SQL 行当成已批准计算值。`point` 返回明确状态：

- 默认 `historical_pit` 只接受精确历史可见证明，供应商快照和日粒度证据不会自动升级。
- `official_daily` 使用现有官方日期及后续已核交易日证据，遇未核清的相关更正会阻断。
- `source_research` 要求截止时刻不早于捕获，并保留源日期、单位和版本冲突限制。
- `listed_security` 在当前源研究基础上再检查该代码的报告期存续边界。

`common_indicator` 只支持净利率和资产负债率，要求同源版本、单位、范围、期间相容；
分母为零和缺输入返回状态，不补值。原有 `FinancialPIT` 的单季/TTM 入口保持原契约。

`reference_point` 提供精确日期的日历、股本、因子、限价、状态或行动字段读取，
必须显式选择来源和捕获截止时刻。它不做向前填充、不拼接因子系列，也不提供未证明的历史可见性。
现金分红分别返回数值单位和税务口径，复权因子分别返回无量纲比值和供应商计算基准。
原负股本字段仍为空；经审阅恢复的同日源点值使用独立来源 `sina_reviewed_share_point`，
其可用时刻不早于两份所需证据的较晚捕获时刻。

## 由发布协调方合并

```python
from ashare_data.storage.market_release import MarketRelease, ReleaseWriter
from ashare_data.storage.financial_release import compose

parent = MarketRelease(market_root)
with ReleaseWriter(market_root) as writer:
    manifest = compose(
        writer, parent, candidate_root, expected_parent_sha256=parent.sha256,
    )
    if manifest is not None:  # 已合并过同一候选时返回 None
        pointer = writer.publish(manifest, expected_parent=parent.release_id)
```

协调方须在最新共用父版本上重新组合。证券 ID 变化会拒绝；行情 files、其他 evidence、
旧版本及原发布处置清单均保留。新财务 Parquet 尽可能同盘硬链接，只复制必要元数据。
发布器接受当前映射和明确列入兼容清单的已验收 v1 映射，未知实现指纹仍拒绝。
新增导入入口不会要求重建已冻结候选；原候选应交给 `compose`，不要按新实现重跑 `prepare`。
发布器校验财务对象集和冻结元数据契约，后续行情更新不能静默丢掉财务表。

## 已冻结观察的单位增量

`financials.unit_resolution` 生成单独的 `ad.financial-unit-candidate.v1`。
它只增加小型单位规则和捕获绑定，不重写原始观察或改变 15 张正式表，
候选需要先显式审阅，再通过受控发布入口加入共用发布。候选检查方式：

```python
reviewed = view.with_units(unit_candidate_root, expected_sha256=unit_candidate_sha256)
value = reviewed.source_point(
    "600012.XSHG", "BASICEPS", "2010-03-31",
    statement="lrb", title="基本每股收益", source="sina",
    as_of=review_cutoff, mode="source_research",
)
```

规则同时绑定原候选的对象集合、抽取映射、批次及捕获回执，以及来源、报表类别、
原字段 ID、完整标题、币种和合并口径。只有审阅清单中列明的原字段组合可以恢复单位；
未覆盖的行业科目、旧标题和别名继续未核。`financial.unit_resolved_observations`
在原列旁提供 `resolved_unit`、`unit_ready_value_decimal_text`、`unit_rule_id` 和单位证据时间。
原始 `financial.observations`、`current_source_values` 和现有规范指标入口保持不变。

通过复核后，`storage.unit_publication` 将候选、原候选清单、原件审计、审阅定义、
引用证据及实现代码复制为小型不可变对象。正式读者仅使用 release 固定的对象清单，
不会重开候选内的外部绝对路径。每个后续发布必须保留既有单位契约。增量限制为 512 MiB，
正式存储保留 150 GiB 空闲预算。

```sh
PYTHONPATH=src python -B -m ashare_data.product.financial_release \
  --market-root "$MARKET" --release-id "$PARENT" --manifest-sha256 "$PARENT_SHA" \
  publish-units --unit-candidate "$UNITS" --candidate-sha256 "$UNITS_SHA" \
  --review-script "$REVIEW_SCRIPT" --calendar-plan "$CALENDAR_PLAN"
```

发布成功后的默认 `release.financial()` 自动绑定已发布单位规则，旧 release 不变。
显式源字段可经公开 CLI 查询；省略 `--mode source_research` 时仍要求历史可见证据：

```sh
PYTHONPATH=src python -B -m ashare_data.product.financial_release \
  --market-root "$MARKET" --release-id "$RELEASE" --manifest-sha256 "$MANIFEST_SHA" \
  source-query --security 600012.XSHG --statement lrb --field BASICEPS \
  --title 基本每股收益 --period 2010-03-31 --source sina \
  --as-of "$REVIEW_CUTOFF" --mode source_research
```

## 有证据绑定的质量与覆盖增量

共用发布可冻结小型单位定义层、经审阅的日粒度可见性记录及参考覆盖台账，原始观察与历史发布保持不可变。`FinancialTables` 与 `ashare-data financial-tables` 默认读取当前发布绑定的规则。

新增单位层必须绑定紧邻的父发布、匹配已审计抽取集合、保留既有规则截止时刻，且与已有规则的 `source/statement/field/title/currency/scope` 键互斥。单位规则不授予历史可见资格或跨源规范指标等价性。

`reference-coverage` 使用交易所代码存续区间、显式源停牌标记、已验证退市日或同一证券在源别名下的精确观察解释参考数据缺口；停牌本身不代表零限价或无涨跌幅限制。`reference-query --family price_limit` 仅沿用该发布台账批准的观察 ID。

经审阅的官方日粒度字段在提供完整交易所日历捕获时，将公告日之后的下一个交易日作为首个可用日（不代表盘中精确发布时刻，也不解除已知修订阻断）。小型封闭增量候选可使用 `financial_release.compose(..., bounded_append=True)`（物理行数上限 10,000 行，完整校验候选与重叠父键/上下文，避免为小型只追加证据增量重排全部历史财务行）。
