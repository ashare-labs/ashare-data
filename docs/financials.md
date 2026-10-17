# 查询财报版本、单季与 TTM

## 财务时点

`FinancialPIT` 在已有 `FinancialLedger` 上提供多证券、多字段查询。可按一个历史截止时点取当时符合可见性规则的 YTD 财报版本，也可转单季或计算 TTM；每个结果保留公告日期、来源页码、源文件 SHA256、修订关系及全部计算输入。查询实现不含证券硬编码，不计算或更改命名因子。

## 可调用接口

```python
from ashare_data import FinancialPIT
from ashare_data.financials.ledger import RESEARCH_MODE

view = FinancialPIT.from_file(
    "data/financial/pack.json",
    evidence_root="data/financial",
    expected_sha256=pack_sha256,  # 由本次独立输入清单提供
)
result = view.query(
    ["000637.XSHE", "300124.XSHE"],
    ["net_operate_cash_flow"],
    period="2024-12-31",
    as_of="2026-08-07",
    basis="TTM",
    mode=RESEARCH_MODE,
)
for row in result["rows"]:
    print(row["security"], row["status"], row["value"])
```

输入完整且可见时返回金额；缺少所需报告时返回 `missing`，不会补齐。若要查各股最新一期，省略 `period`。同一股多个字段的最新可见报告期必须一致，否则该股全部请求字段返回缺失，避免悄悄混用不同季度或回退旧期。

输入兼容既有 `ad.financial-ledger.v1`：`reports`、`observations`、`sources` 的结构不变。`FinancialPIT(pack)` 可用于调用者已装载的账本，结果标记 `source_files_verified=false`；`from_file` 必须逐项核验所有声明的证据字节，并检查报告 PDF、文本与索引均在源清单中。可用 `expected_sha256` 额外锁定整个转录包。校验哈希不证明源真实性或历史完整性。

| 参数 | 规则 |
| --- | --- |
| `securities`、`fields` | 单字符串或非空、不重复的字符串序列；输出按证券×字段保持矩形排列 |
| `period` | 可省略；指定时为标准自然季度末 `YYYY-MM-DD` |
| `as_of` | 带时区的 ISO 时刻，或研究模式下的标准日期；日期表示**上海该日开始时 00:00** |
| `mode` | 必须显式传 `RESEARCH_MODE` 或 `"strict"`，无自动回退 |
| `basis` | `"YTD"`、`"quarter"`、`"TTM"`；默认 YTD |

首批支持合并报表、人民币元的五个累计流量字段：`operating_revenue`（营业收入）、`total_operating_revenue`（营业总收入）、`net_profit`（合并净利润）、`parent_net_profit`（归母净利润）、`net_operate_cash_flow`（经营现金流净额）。字段彼此不替代。资产负债表时点数、母公司单体报表、非人民币、非自然年口径尚未接入。

每行 `status` 为 `ok`、`missing` 或 `visibility_unproven`。后两类的 `value=None` 且附原因；格式/口径错误、未知字段、重复版本或歧义修订链抛 `AcceptanceError`。金额为十进制字符串；不以 0 代缺失，不转二进制浮点、不追加修约。

## 日期、版本与计算约定

`RESEARCH_MODE` 的全名为 `reported_date_next_day_incomplete_archive`。只有公告日期证据的报告，在公告日的**后一自然日上海 00:00**纳入；它不是交易日推算或实际发布时刻。例：2024-07-05 披露的普利财报，在 `as_of="2024-07-05"`、当日收盘和当日 23:59:59 都不可见，`2024-07-06T00:00:00+08:00` 才可见。

严格模式要求带时区 cutoff，以及报告的精确 `published_at`、`available_at`、原始首次发布证明与完整修订证据；可用时刻为前两者的较晚值。仅有日期或无法证明原始首次发布时刻的资料，在严格模式返回 `visibility_unproven`；已知未来资料仍先排除，不能污染更早查询。结果始终保留 `historical_pit_certified=false`，输入声明和算术检查不签发认证。

版本按原报告与明确替代链选择，而非按本次抓取时刻排序。修改 `captured_at` 不改变历史答案；较晚才采集的原报也不能覆盖已可用的更正。后续报告的比较列只在该后续报告自己的可见边界进入，不倒签到比较期结束日。同日多个版本如果没有精确发布顺序，继承原账本的拒绝规则。

单季为本季累计减上季累计，Q1 直接取累计。TTM 要求连续四季完整，并校验四个单季之和等于 `本期累计 + 上年全年 - 上年同期累计`；年末 TTM 为全年值。所有项在**同一 cutoff**独立选最新可见版本，结果 `terms` 和 `quarters` 保留带正负号的原始输入。缺任一季度即缺失，不仅凭年度桥凑出数值。
