# 参考数据与公司行动

本篇整合交易日历、逐日停牌/ST 状态、源前收参考价、上市事实、历史市场状态视图（`HistoricalMarketView`）以及公司行动与复权价格计算。

## 基础参考数据（`BaoStock` 固定捕获）

基于 [BaoStock 捕获](providers.md#baostock-采集与离线读取baostocksource) 的三类参考查询均返回不可变元组结果，保留原始字段字符串、状态枚举与响应哈希：

| 查询类型 | 采集 `kind` 与读取方法 | 核心返回字段与语义 |
| --- | --- | --- |
| 停牌与 ST 状态 | `kind="daily_status"` → `view.get_status(require_known=False)` | 返回 `DailyStatusRow`（`suspended`、`is_st`）；按 BaoStock 定义 `tradestatus=0/1` 映射停牌 `True/False`，`isST=1/0` 映射 `True/False` |
| 源前收参考价 | `kind="daily_preclose"` → `view.get_preclose(require_known=False)` | 返回 `SourcePrecloseRow`（`preclose` 为 `Decimal`）；源 `preclose` 在除权除息日由供应商调整，区别于前一日实际未复权收盘价 |
| 交易日历与邻接日 | `kind="calendar"` → `view.get_calendar()` / `view.get_calendar_links(trading_dates=[...])` | 返回自然日开闭市标记 `is_trading_day`，以及每个锚点开市日严格相邻的 `previous_open` 与 `next_open` |

状态字段统一使用 `source_observed`、`source_zero`、`field_not_requested`、`row_missing_unknown`、`source_empty`、`unsupported_enum` 区分真实观测值与各类缺失情形；传入 `require_known=True` 时遇到未知项会抛出对应 `DataError`。

---

## 上市事实、有界日历与盘前参考

- **上市事实（`listing_fact`）**：`Store.import_listing_evidence(directory)` 导入经哈希锁定的上市证据包，通过 `store.listing_fact(snapshot_id).get(security, on_date, field="initial_listing_date")` 读取首次上市日期或年份。
- **有界日历（`BoundedCalendarView`）**：`BoundedCalendarView(binding_path, expected_binding_sha256=...)` 读取深交所月度 `jybz` 自然日标志并与新浪开市日交叉核对，提供单月窗口内任意日期的前后最近开市日查询。
- **盘前除息参考价（`PremarketReferenceView`）**：`PremarketReferenceView.read(security, as_of=..., role="premarket_reference_reconstruction")` 读取新浪 KLC 独立编码的 `prevclose` 并核对相邻现金分红除息日与毛分红金额一致性。

---

## 历史市场状态与分钟覆盖门禁

### 历史市场状态视图（`HistoricalMarketView`）

`HistoricalMarketView` 读取 `ad.historical-market-binding.v1` 绑定包，为日频研究与回测提供跨源核对后的日历、证券生命周期、停牌状态与涨跌停价格限制：

```python
from ashare_data import HistoricalMarketView

view = HistoricalMarketView("binding.json", expected_binding_sha256="<64位SHA256>")
state = view.read_daily_state("600000.XSHG", "2024-01-03", role="historical_market_retrospective_research")
```

- **证券池与退市/吸收合并追踪**：结合上交所/深交所与巨潮目录，显式记录首次上市日、退市日及吸收合并承继关系（如原代码注销与新代码承继的有效期边界）。
- **逐日状态与限价计算**：在源停牌/ST 标记与前收基础上，按板块规则（主板常规 10%、ST 5% 等）派生研究参考涨跌停价，并保留无涨跌幅限制阶段（如上市首日或股改复牌日）的显式豁免标记。

### 分钟证据门禁（`MinuteEvidenceView`）

`MinuteEvidenceView` 校验分钟级回测所需的日历连续性、每交易日 240 根分钟槽完整性、收盘竞价零量阶段标注及与日线 OHLCV 的对账结果，供下游分钟引擎准入预检使用。

---

## 公司行动与复权价格计算

### 公司行动历史台账（`CorporateHistoryView`）

`CorporateHistoryView` 读取 `ad.corporate-history-binding.v1` 绑定包，整合巨潮、Tushare、BaoStock 与本地主库的分红送转配股记录：

```python
from ashare_data import CorporateHistoryView

corp = CorporateHistoryView("binding.json", expected_binding_sha256="<64位SHA256>")
actions = corp.read_actions("600000.XSHG", start_date="2020-01-01", end_date="2024-12-31")
```

- 每条事件记录完整保留公告日（`notice_date`）、股权登记日（`record_date`）、除权除息日（`ex_date`）、派息日（`pay_date`）与新增股份上市日，区分普通现金分红、送股、转增股本、配股与特殊重组事件。
- CLI 入口：`python -m ashare_data.corporate_actions.corporate_history_cli --help`。

### 复权价格计算

系统支持两种复权消费方式：

1. **基于来源累计复权因子（`CMESFactorView` / 新浪累计因子）**：
   - 前复权价格：`P_pre(t) = P_raw(t) * F(t) / F(basis_date)`
   - 后复权价格：`P_post(t) = P_raw(t) * F(t) / F(anchor_date)`
   - 仅调整价格序列，成交量与成交额保持原始未复权值；前复权严格以查询截止/基准日 `basis_date` 截断未来因子，避免引入基准日之后的除权信息。
2. **基于公司行动事件的账户直接记账**：
   - 在回测引擎中直接消费未复权价格（`fq=None`）与 `CorporateHistoryView` 事件表，在登记日收盘确认权益基数、除权除息日调整持仓成本与应收款、派息/上市日结算到账。
