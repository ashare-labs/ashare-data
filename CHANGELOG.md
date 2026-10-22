# 更新记录

## 0.10.0.dev19

- Add optional offline single-factor diagnostics backed by pinned Alphalens Reloaded 0.5.0: lagged session labels, Pearson IC, Spearman RankIC, quantile returns/spreads, stability, turnover and explicit cohort cost sensitivity.
- Preserve signal-date membership before label filtering, reject ambiguous quantile ties, expose coverage, and keep incomplete quantile returns undefined. No execution simulation or market-data acquisition is introduced.

## 0.10.0.dev18

- Connect ordinary daily backtests to release-pinned formal status, price limits and corporate source evidence; preserve legacy reference groups and explicit missing-data blockers.
- Bound unresolved old stock-credit dates by observed tradability without inventing dates; serialize repeated publication reads with the existing write lock.

## 0.10.0.dev16

- Validate financial relationship endpoints one type at a time, retaining exact endpoint checks without two simultaneous unions of the full financial fact tables. Reuse verified dev15 candidates without reimporting their captured responses.

## 0.10.0.dev15

- Import retained responses from explicitly stopped source batches while recording unvisited tasks only as coverage gaps. Freeze stop evidence and receipt inventories for resumable imports.
- Keep the published financial schema and reader contracts unchanged; preserve source values, unknown units, historical visibility restrictions and prior release objects.

## 0.10.0.dev14

- Publish reviewed source-unit rules with their immutable proof closure; exact source-field queries use the release-bound annotations while preserving original financial observations, missing values and historical visibility restrictions.
- Append bounded market calendar sessions from retained, agreeing source calendars; verify the complete metadata delta before one atomic publication with unchanged market facts.
- Preserve prior releases, accept interrupted publication retries and reject dropped unit evidence.

## 0.10.0.dev13

- Accept the reviewed financial reference and recovered official-index candidates without re-parsing prior frozen inputs. Retain source-code quarantine and incomplete index coverage.
- Keep JSON consumer stdout free of DuckDB progress output; preserve returned financial values and version bindings.

## 0.10.0.dev12

- Add resumable recent daily/minute collection and exact source-to-release admission with immutable response and destination evidence.
- Add release-pinned Sina minute selection and explicit recent daily observation captures; preserve prior releases and source contracts.
- Make repeated replacement plans idempotent and update coverage only for changed scopes.
- Compose reviewed financial/reference candidates into the same immutable release; retain source-code identity, capture cutoffs, unit restrictions and prior market facts.
- Resolve incremental raw-response provenance in daily read receipts, including native Sohu batch positions.


## 0.10.0.dev5

- Integrate formal daily publication and generic source-labelled minute research exports in one paired runtime; retain the existing source, quality and visibility boundaries.

## 0.10.0.dev4

新增正式日线产品目录、只读查询与发布：大库行情通过显式参考集合接入普通条件研究，保留逐日交易状态、派生价限、独立公司行动计划和可验证来源绑定。缺失或冲突的数据明确拒绝，价格诊断保留为独立模式。统一数据根及资产配置，支持可恢复发布和来源确证。

修复 TDX 子进程入口导入旧模块的回归，新增真实进程启动测试。

## 0.10.0.dev3

- 增加公开 `export_minute_research`：从固定快照生成有界来源标签研究文档，保留原始/派生字段、质量、未知可见时间和来源摘要；不授予执行权限。

将 docs 从 48 个文件收敛为 19 篇任务指南，合并重复入口并移除两份可生成覆盖 CSV；明确两份开发 CJS、随包 JS、QuickJS 与 Node 的职责和依赖，修复导航及源码文档引用。

恢复 Eastmoney 原生 fields1 的 f1/f2 协议标识，新增沪深请求构造回归，防止业务命名整理改变线协议。移除 CLI 帮助中的旧固定验收计数；因子采集工具继承调用方代理配置，移除写死的本机端口。包版本/runtime 身份更新；数据、owner/schema/policy 版本与冻结交付保持原义。

## 0.10.0.dev2

将通用 TVMA6 来源区间、舍入输出比较和汇总放回 `factors/joinquant/amount_comparison.py`，测试直接使用安装包公开入口。保持原数值边界与未认证声明，不恢复历史批次 API。

修复包门面的 `catalog` 函数遮蔽同名模块的问题，常规模块导入可调用目录查询。增加离线金额示例和隔离 wheel 入口验证。包版本和代码身份更新，有限事实、条件与可移植研究的既有 owner/schema/policy 版本保持原义。

## 0.10.0.dev1

按功能组织行情、参考数据、公司行动、财务、因子、存储、提供者、观察和研究代码。D1/BR1/M2 类型与方法改为有限事实、条件研究、可移植研究名称；旧模块不提供兼容别名。F1/F2 因子改为明确字段和计算入口，Alpha191 正式编号保留。

移除历史实验和验收输出，将仍有用途的精确算术留在运行包、比较工具留在 scripts、回归反例收敛到合成测试。修正包资源、子进程入口及安装后命令；文档只描述当前契约，增加轻量结构校验。

新 owner、资源和代码指纹属于本版本，不继承旧运行验收。数据库和冻结账户不迁移；调用者按[迁移说明](docs/migration.md)创建新环境与新输出。此前版本的具体变更和材料保留在 Git 历史。
