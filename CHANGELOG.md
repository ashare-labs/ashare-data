# 变更记录

当前版本：**0.10.0.dev98**（以 [`pyproject.toml`](pyproject.toml) 为准）。

## 0.10.0.dev20 – 0.10.0.dev98

- **本地主库与正式发布（`MarketRelease`）**：完成 CMES 日线/分钟主库、免费源增量、Tushare/BaoStock 参考与财务数据的统一快照发布与增量合并；支持 `LocalMinuteProduct`（TDX、`free_stockdb`、`neigezhu`）与 `native_macro` 原生宏观序列发布查询。
- **免费源增量采集（`free-updates`）**：实现首批 198 项与第二批 19 项公开数据源的目录管理、冻结队列、断点续采、独立回执校验、交接包导出与多源并发调度注册表。
- **财务质量台账（`LocalFinancialStore`）**：建立资产负债表、利润表、现金流量表及分红数据的跨源对账、分类台账、官方日频填充与修正链。
- **因子库扩展与评价**：整合聚宽 19 项命名研究因子、GTJA Alpha191（`gtja191.public.v1` / `gtja191.explicit.v1`）与 Qlib Alpha158（`qlib.alpha158.v0.9.7`），并提供基于 `alphalens-reloaded==0.5.0` 的离线因子评价接口。
- **包结构与文档收敛**：将 `providers/` 与 `financials/` 内部模块按数据源与职责归并，清理历史代号与碎片文档，统一收敛为 12 个功能子包与 8 篇领域文档。

## 0.10.0.dev1 – 0.10.0.dev19

- **功能域重组**：将早期按开发阶段命名的模块（`d1`、`br1`、`m2`、`f1`、`f2`）迁移至 `market`、`reference`、`corporate_actions`、`financials`、`factors`、`storage`、`providers`、`observations`、`research`、`product`、`cli` 功能子包。
- **研究归档与可移植组件**：提供 `ResearchView`、`IndependentQueryView`、`BaoStockSource`、`TDXBarSource`、`ZzshareSource` 及 Portable 组件生产与消费契约。

## 0.5.0

- 初始公开版本：支持新浪近期日线/分钟行情查询、本地 SQLite/Parquet 缓存与不可变快照存储（`Store` / `DataView`）。
