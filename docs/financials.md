# 财务数据与质量台账

本篇说明三大财务报表（资产负债表、利润表、现金流量表）与分红数据的标准化 schema、披露时间可见性规则，以及本地财务主库 `LocalFinancialStore` 的跨源对账与质量修复机制。

## 财务报表 Schema 与单位规范

A数达将来自 CMES 底库、Tushare（`ashare_data.financials.tushare_reference`）、BaoStock 与东方财富/新浪的财务数据统一映射至三张核心报表与分红表：

| 报表类别 | 标识（`statement`） | 主键维度 | 数值与单位约定 |
| --- | --- | --- | --- |
| 资产负债表 | `balance_sheet` | `(security, report_period, source,announce_date)` | 金额为人民币元（`Decimal`），股本为股 |
| 利润表 | `income_statement` | `(security, report_period, source, announce_date)` | 报告期累计发生额（元），每股指标为元/股 |
| 现金流量表 | `cash_flow` | `(security, report_period, source, announce_date)` | 报告期累计现金流（元） |
| 财务指标与分红 | `indicators` / `dividends` | `(security, report_period, ...)` | 保留原始字段、分红预案进度、登记日与除权除息日 |

- **报告期（`report_period`）与公告日（`announce_date`）分离**：季度/年度截止日（如 `2023-12-31`）仅标识会计统计区间，不作为数据可见时间。
- **重述与多版本保留**：同一报告期如有业绩快报、正式年报及后续会计差错更正重述，按各自的公告日期与接收顺序保留独立版本。

---

## 查询模式与披露日可见性

通过 `LocalFinancialStore` 或 `MarketRelease` 查询财务点位时，支持三种显式可见性模式（`mode`）：

```python
from ashare_data import LocalFinancialStore

fin = LocalFinancialStore("/path/to/financial-store")
point = fin.query_point(
    security="600000.XSHG",
    field="net_profit",
    period="2023Q4",
    as_of="2024-05-01T09:00:00+08:00",
    mode="official_daily",
)
```

| 模式（`mode`） | 可见性规则与适用场景 |
| --- | --- |
| `official_daily`（默认） | 基于经核实的官方披露日期（日频精度），要求 `announce_date < as_of_date`（或当日盘后可见），过滤晚于查询时点的重述版本 |
| `historical_pit` | 严格要求具备精确披露时间戳证据；若来源仅有无时区的日期标签或缺失首次上网时刻，返回不可用（`PIT_UNAVAILABLE`） |
| `source_research` | 按指定 `source`、报告期或原生报表行标题（`statement` + `title`）读取完整研究记录，附上披露状态与跨源差异说明 |

---

## 本地财务质量台账与跨源修复（`LocalFinancialStore`）

`ashare_data.financials` 提供从多源采集到质量分类台账的完整管线：

| 模块 | 职责说明 |
| --- | --- |
| `ashare_data.financials.local_store` | `LocalFinancialStore` 存储管理、单点查询（`query_point`）与覆盖度查询（`coverage`） |
| `ashare_data.financials.ledger` | 财务覆盖分类台账与一致性校验（合并原验证规则），区分正常覆盖、已退役代码、跨源别名、停牌/退市及未解决缺口 |
| `ashare_data.financials.official_daily` | 官方披露日对齐、跨期填充规则与扩展访问接口 |
| `ashare_data.financials.tushare_reference` | Tushare 凭据管理（macOS Keychain / 环境变量）、三大报表与分红参考采集 |
| `ashare_data.financials.expansion` | 多源扩展采集与 HTTP 退避限流控制 |
| `ashare_data.financials.source_repair` | 针对缺失报告期或跨源冲突证券的定向补采与修正批次处理 |
| `ashare_data.financials.pit_visibility` | 报告期链条完整性（如连续 8 季度单季利润推导）与披露时点可见性判断 |

### 覆盖分类状态

`coverage(security=..., day=...)` 对每个证券与报告期返回明确的台账状态：
- **已就绪（`covered`）**：报表核心字段与有效披露日均已验证；
- **证券身份豁免**：包括未上市阶段、已退市证券、已退役旧交易代码及经审核的同公司跨源别名映射；
- **待修复缺口（`unresolved_gap`）**：在役上市期间缺失应披露报表或跨源数值冲突，保持显式缺失状态，不以零值或前值静默填充。
