# 财务数据与质量台账

本篇说明三大财务报表（资产负债表、利润表、现金流量表）与分红数据的标准化 schema、披露时间可见性规则，以及正式发布财务表（`FinancialTables`）、有界版本归档（`FinancialPIT`）与跨源质量修复机制。

## 财务报表 Schema 与单位规范

A数达将来自 CMES 底库、Tushare（`ashare_data.financials.tushare_reference`）、BaoStock 与东方财富/新浪的财务数据统一映射至三张核心报表与分红表：

| 报表类别 | 标识（`statement`） | 主键维度 | 数值与单位约定 |
| --- | --- | --- | --- |
| 资产负债表 | `balance_sheet` | `(security, report_period, source, announce_date)` | 金额为人民币元（`Decimal`），股本为股 |
| 利润表 | `income_statement` | `(security, report_period, source, announce_date)` | 报告期累计发生额（元），每股指标为元/股 |
| 现金流量表 | `cash_flow` | `(security, report_period, source, announce_date)` | 报告期累计现金流（元） |
| 财务指标与分红 | `indicators` / `dividends` | `(security, report_period, ...)` | 保留原始字段、分红预案进度、登记日与除权除息日 |

- **报告期（`report_period`）与公告日（`announce_date`）分离**：季度/年度截止日（如 `2023-12-31`）仅标识会计统计区间，不作为数据可见时间。
- **重述与多版本保留**：同一报告期如有业绩快报、正式年报及后续会计差错更正重述，按各自的公告日期与接收顺序保留独立版本。

---

## 查询模式与披露日可见性

### 1. 正式发布财务表读取（`MarketRelease.financial()` / `FinancialTables`）

通过已冻结的 `MarketRelease` 打开 `FinancialTables`（`ashare_data.product.financial_release.FinancialTables`），按显式来源（`source`）、截止时点（`as_of`）与可见性模式（`mode`）查询规范化财务指标或原生报表字段：

```python
from ashare_data.storage.market_release import MarketRelease

release = MarketRelease("/path/to/releases", release_id=1, expected_manifest_sha256="<64位SHA256>")
fin = release.financial()

point = fin.point(
    "600000.XSHG",
    "net_profit",
    "2023-12-31",
    source="tushare",
    as_of="2024-05-01T09:00:00+08:00",
    mode="official_daily",
)
cov = fin.reference_coverage(security="600000.XSHG", day="2024-05-06")
```

| 模式（`mode`） | 可见性规则与适用场景 |
| --- | --- |
| `historical_pit`（默认） | 严格要求具备精确披露时间戳证据（`verified_exact_publication`）；若来源仅有无时区的日期标签或缺失首次上网时刻，返回 `visibility_unproven` |
| `official_daily` | 基于经核实的官方披露日期（日频精度），要求披露门限 `<= as_of`，过滤晚于查询时点的重述版本 |
| `source_research` | 按指定 `source`、捕获时点（`first_captured_at <= as_of`）及原生报表行标题（`source_point(..., statement=..., title=...)`）读取研究记录 |
| `listed_security` | 在捕获时点与公告日期检查基础上，校验报告期处于证券有效上市存续期内 |

### 2. 有界版本台账查询（`FinancialPIT`）

针对独立的 `ad.financial-ledger.v1` 版本归档包，`FinancialPIT` 提供多公司 YTD、单季（`quarter`）与经年报桥接校验的 `ttm` 查询：

```python
from ashare_data import FinancialPIT

pit = FinancialPIT.from_file("pack.json", evidence_root=".", expected_sha256="<64位SHA256>")
res = pit.query(
    ["000001.XSHE"],
    ["net_operate_cash_flow", "parent_net_profit"],
    as_of="2024-05-01T09:00:00+08:00",
    basis="quarter",
    mode="strict",
)
```

---

## 财务质量台账与跨源修复管线

`ashare_data.financials` 与 `ashare_data.product.financial_release` 提供从多源采集到质量分类台账的完整管线：

| 模块 | 职责说明 |
| --- | --- |
| `ashare_data.product.financial_release` | `FinancialTables` 正式发布读取、单点查询（`point` / `source_point`）、常用财务比率（`common_indicator`）及参考覆盖查询（`reference_coverage` / `reference_point`） |
| `ashare_data.financials.pit` | `FinancialPIT` 版本归档加载（`from_file`）与 YTD / 单季 / TTM 推导查询（`query`） |
| `ashare_data.financials.ledger` | `FinancialLedger` 版本台账、报告期链条完整性校验与可见性模式定义 |
| `ashare_data.financials.tables` / `schema` | 财务与参考表规范化 schema、上下文哈希与字段定义 |
| `ashare_data.financials.official_daily` | 官方披露日对齐、跨期填充规则与扩展访问接口 |
| `ashare_data.financials.tushare_reference` | Tushare 凭据管理（macOS Keychain / 环境变量）、三大报表与分红参考采集 |
| `ashare_data.financials.expansion` | 多源扩展采集与 HTTP 退避限流控制 |
| `ashare_data.financials.source_repair` / `quality_repair` / `unit_resolution` | 针对缺失报告期、跨源冲突或源单位的定向补采、质量修复与单位解析 |

### 覆盖分类状态

`fin.reference_coverage(security=..., day=...)` 与财务质量台账对每个证券与日期/报告期返回明确的分类状态：
- **已就绪（`covered`）**：报表或参考核心字段与有效披露/采集证据均已验证；
- **证券身份豁免**：包括未上市阶段、已退市证券、已退役旧交易代码及经审核的同公司跨源别名映射（`covered_identity_alias`）；
- **待修复缺口（`unresolved_gap`）**：在役上市期间缺失应披露报表或跨源数值冲突，保持显式缺失状态，不以零值或前值静默填充。
