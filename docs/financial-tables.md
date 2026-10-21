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

公司节点不等于证券ID。未核实的供应商主体保留 `source_entity_unverified_legal_identity`，
不按名称或换码推定同一法律主体。官方字段复核建立限定报告期的证券关系，不延伸到其他时期。
点查询同时限定冻结证券ID和原始来源代码；即使行情注册表将历史换码列为同一证券ID，
也不会自动读取另一代码的财务或参考观察。直接SQL关联时应保留同样的来源代码约束。
日线自带 `tradestatus/is_st` 通过 `reference.daily_attached_status` 视图复用。

内容ID排除抓取时钟、解析器和响应内的分页位置；原响应位置在来源关系中保留。
同内容再次抓取增加 capture；更换解析器增加 extraction。科目唯一键表达抽取输出，
不是跨来源经济事实去重。更正必须有精确字段/期间/主体关系，校验端点及循环；
没有明确替代关系的多个版本返回冲突。

金额用十进制文本完整保存。原词面、JSON类型、规范数值、单位及状态分别存储；
字段缺失、null、空字符串、零不混淆。未核实单位仍保存可解析的原数值，
`canonical_value_decimal_text` 保持空。上市前、旧代码退出后、部分返回、
负股本和股份分类冲突均保留，不通过补零或延长有效期进入默认计算。

## 生成候选

使用明确的已发布父版本与hash，输出目录必须独立。大数据写正式数据根下。

```sh
PYTHONPATH=src python -B -m ashare_data.product.financial_release \
  --market-root "$ASHARE_FORMAL_DATA_ROOT/market" \
  --release-id RELEASE_ID --manifest-sha256 PARENT_SHA256 \
  prepare --output "$ASHARE_FORMAL_DATA_ROOT/financial-reference/table-candidates/BUILD" \
  --batch CLOSED_SOURCE_BATCH \
  --official-batch CLOSED_CNINFO_BATCH \
  --daily-review-spec REVIEW_SPEC_JSON \
  --share-resolutions EXISTING_SHARE_RESOLUTION_BUNDLE
```

各类参数均可重复，只传当前需要的来源。普通 `--batch` 支持已存在的新浪、
日历、Tushare报表/指标投影/分红/复权/限价及辅助日状态规范记录。
`--official-repair` 接受已冻结的缓存修复包，原位核验所引用的公告响应和回执，
仅接入返回代码明确的公告事件。相关代码、冲突及拒绝记录进入隔离表，分页缺口
进入覆盖表；不修改原公告批次，也不生成财务字段或历史可见资格。
导入前取得源分区共享锁，拒绝仍在写入、未访问或运行中的任务。明确失败和空返回
进入覆盖表，未知schema或无法映射的记录进入隔离表，原件不丢弃。
首次导入核验回执、原件和规范文件hash、源字段数值及Parquet逐值往返。
源响应原位引用，不复制日线库、分钟库或原始公告PDF。

已经明确停止且不会恢复的旧批次，可显式使用 `--stopped-batch`。此入口要求原批次的
`STOP`、`worker-exit.json` 及可获得的共享写入锁，冻结所有现存回执哈希和未访问任务清单。
只导入已取得的原件；未访问任务只产生 `unvisited_stopped` 覆盖记录，不生成捕获或事实，
也不触发采集。中断重跑时，停止证据或回执集合发生变化会拒绝复用候选。

每个完成的分片保存断点。中断后用同一参数重跑；已经完成的回执与对象须仍匹配。
映射代码或输入集合改变时使用新的候选目录。保留8 GiB空闲空间；不会删除旧源材料。
`candidate.json` 包含逐表行数、输入处置对账、父版本、实现hash及数据对象，
`BUILD-PROGRESS.json` 只记录本次离线入表进度。

大批次的全表去重验收仍限制内存，但会使用磁盘临时空间。
`ASHARE_DUCKDB_TEMP_DIRECTORY` 可指定正式数据根下的独立临时目录，
`ASHARE_DUCKDB_MAX_TEMP_DIRECTORY_SIZE` 可指定本次查询的磁盘上限（默认8 GB）。
`ASHARE_DUCKDB_MEMORY_LIMIT` 可显式覆盖本次离线作业的连接内存预算；
未设置时保留各调用方原有内存上限。先核对主机容量，再为大批次指定预算。
显式传入的临时目录优先于环境变量。离线协调脚本应使用临时目录上下文，
成功或失败后关闭连接并清理；不在工作树或Codex任务目录承载大批次临时数据。

`REVIEW_SPEC_JSON` 包含 `official_root`、`review`、`review_sha256`、
`reference_root`、`reference_tasks` 和可选 `correction_reviews`。
它调用现有 `official_daily.reader` 核验PDF、字段、索引、更正范围和交易日证据，
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

SQL公开保留的源观察供审查，不能把全部SQL行当成已批准计算值。`point` 返回明确状态：

- 默认 `historical_pit` 只接受精确历史可见证明，供应商快照和日粒度证据不会自动升级。
- `official_daily` 使用现有官方日期及后续已核交易日证据，遇未核清的相关更正会阻断。
- `source_research` 要求截止时刻不早于捕获，并保留源日期、单位和版本冲突限制。
- `listed_security` 在当前源研究基础上再检查该代码的报告期存续边界。

`common_indicator` 只支持净利率和资产负债率，要求同源版本、单位、范围、期间相容；
分母为零和缺输入返回状态，不补值。原有 `FinancialPIT` 的单季/TTM入口保持原契约。

`reference_point` 提供精确日期的日历、股本、因子、限价、状态或行动字段读取，
必须显式选择来源和捕获截止时刻。它不做向前填充、不拼接因子系列，也不提供未证明的历史可见性。
现金分红分别返回数值单位和税务口径，复权因子分别返回无量纲比值和供应商计算基准。
原负股本字段仍为空；已有535条修复点使用独立来源 `sina_reviewed_share_point`，
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
    if manifest is not None:  # 已合并过同一候选时返回None
        pointer = writer.publish(manifest, expected_parent=parent.release_id)
```

协调方须在最新共用父版本上重新组合。证券ID变化会拒绝；行情files、其他evidence、
旧版本及原发布处置清单均保留。新财务Parquet尽可能同盘硬链接，只复制必要元数据。
发布器接受当前映射和明确列入兼容清单的已验收v1映射，未知实现指纹仍拒绝。
新增导入入口不会要求重建已冻结候选；原候选应交给 `compose`，不要按新实现重跑 `prepare`。
发布器校验财务对象集和冻结元数据契约，后续行情更新不能静默丢掉财务表。
部署指向新release及主分支集成由同一协调方执行，避免两个任务覆盖清单。

候选或任务结束均不代表完整历史已取得。每个输入批次的空返回、失败、部分返回和
实际最早日期保留在覆盖表；仍在采集的分区须结束并核验后另生成候选、增量合并。

## 已冻结观察的单位增量

`financials.unit_resolution` 生成单独的 `ad.financial-unit-candidate.v1`。
它只增加小型单位规则和捕获绑定，不重写五千多万条观察，不改变15张正式表，
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
原字段ID、完整标题、币种和合并口径。新浪 `f2/ps4` 是显示精度，不能单独证明单位：
少数股东综合收益金额也存在 `ps4`。此次定义仅使用已经逐页检查的官方合并报表表头、
明确标注元/股的每股收益行、完整原件元数据审计及新浪原值显示函数。表头定义科目
单位类别，显示函数确认万/亿只是展示缩写。这是有证据的来源定义推导，
不是接口直接返回的逐字段单位声明，也不证明供应商数值与官方修订完全一致。

只有审阅清单中列明的原字段组合可以恢复单位；未覆盖的行业科目、旧标题和别名继续
未核。不增加跨来源规范指标映射，不把相近标题合并为同一经济事实。
`financial.unit_resolved_observations` 在原列旁提供 `resolved_unit`、
`unit_ready_value_decimal_text`、`unit_rule_id` 和单位证据时间。
原始 `financial.observations`、`current_source_values` 和现有规范指标入口保持不变。
null、空字符串和零仍有区别；其他质量错误不能通过单位规则解除。

`source_point` 默认仍使用 `historical_pit`，单位证据不能授予历史可见资格。
显式当前源研究查询同时检查源捕获时间与单位证据时间。返回派生的已知单位上下文ID，
同时保留 `base_context_id`，原存储上下文不修改。源字段必须带报表类别与完整标题，
证券换码、版本冲突及上市边界沿用正式读者约束。


通过复核后，`storage.unit_publication` 将候选、原候选清单、原件审计、审阅定义、
引用证据及实现代码复制为小型不可变对象。正式读者仅使用 release 固定的对象清单，
不会重开候选内的外部绝对路径；原始采集原件仍由原有血缘单独保留。每个后续发布
必须保留既有单位合同。首轮单位发布支持同时纳入一个已验证日历计划；行情文件和
财务观察文件不重写。增量限制为 512 MiB，正式存储保留 150 GiB 空闲预算。

```sh
PYTHONPATH=src python -B -m ashare_data.product.financial_release \
  --market-root "$MARKET" --release-id "$PARENT" --manifest-sha256 "$PARENT_SHA" \
  publish-units --unit-candidate "$UNITS" --candidate-sha256 "$UNITS_SHA" \
  --review-script scripts/prepare_sina_unit_review.py --calendar-plan "$CALENDAR_PLAN"
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

单位修订没有增加来源财务事实，也没有声称供应商历史终值当年即可知。现有规范指标
查询继续原路径；未复核字段仍不可计算。重复执行同一父版本、候选和日历计划是
无变化操作；中断留下的对象可复用，发布前失败不改变 CURRENT。
