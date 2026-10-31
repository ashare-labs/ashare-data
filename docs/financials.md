# 查询财报版本、单季与 TTM

本指南介绍基于 `FinancialPIT` 的财报时点查询，以及公开源财务/交易参考扩采、官方公告日粒度核验与 Tushare 参考补采接口。正式 Parquet / DuckDB 财务与参考表的构建、合并与查询见 [财务和交易参考正式表](financial-tables.md)。

## 财务时点查询（FinancialPIT）

`FinancialPIT` 在已有 `FinancialLedger` 上提供多证券、多字段查询。可按历史截止时点取当时符合可见性规则的 YTD 财报版本，也可转单季或计算 TTM；每个结果保留公告日期、来源页码、源文件 SHA256、修订关系及全部计算输入。

```python
from ashare_data import FinancialPIT
from ashare_data.financials.ledger import RESEARCH_MODE

view = FinancialPIT.from_file(
    "data/financial/pack.json",
    evidence_root="data/financial",
    expected_sha256=pack_sha256,
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

输入完整且可见时返回十进制字符串金额；缺少所需报告时返回 `missing`，不会补零或回退旧期。省略 `period` 时查询各股最新一期（同一证券多个字段的最新可见报告期必须一致，否则返回缺失）。输入兼容 `ad.financial-ledger.v1`：`FinancialPIT(pack)` 用于调用者已装载的账本（标记 `source_files_verified=false`），`from_file` 则逐项核验声明的证据字节与源清单。

| 参数 | 规则 |
| --- | --- |
| `securities`、`fields` | 单字符串或非空、不重复的字符串序列；输出按证券×字段保持矩形排列 |
| `period` | 可省略；指定时为标准自然季度末 `YYYY-MM-DD` |
| `as_of` | 带时区的 ISO 时刻，或日粒度模式下的标准日期（表示**上海该日 00:00**） |
| `mode` | 必须显式传 `DAILY_MODE`、`RESEARCH_MODE` 或 `"strict"`，无自动回退 |
| `basis` | `"YTD"`、`"quarter"`、`"TTM"`；默认 YTD |

支持合并报表、人民币元的五个累计流量字段：`operating_revenue`（营业收入）、`total_operating_revenue`（营业总收入）、`net_profit`（合并净利润）、`parent_net_profit`（归母净利润）、`net_operate_cash_flow`（经营现金流净额）。每行 `status` 为 `ok`、`missing` 或 `visibility_unproven`；格式/口径错误、未知字段、重复版本或歧义修订链抛 `AcceptanceError`。

## 日期、版本与计算约定

- **`RESEARCH_MODE`（`reported_date_next_day_incomplete_archive`）**：只有公告日期证据的报告，在公告日的**后一自然日上海 00:00**纳入（例如 2024-07-05 披露的财报在 `2024-07-06T00:00:00+08:00` 可见）。
- **`DAILY_MODE`（`official_date_next_verified_session`）**：提供官方日期证据下的历史日粒度查询。公告当日不纳入，从后续已验证的本交易所交易日开始使用，结果以 `daily_pit_usable=true` 标识。
- **`strict`**：要求带时区 cutoff 以及精确 `published_at`、`available_at`、原始首次发布证明与完整修订证据，可用时刻取后两者较晚值。

版本按原报告与明确替代链选择，而非按抓取时刻排序。单季为本季累计减上季累计（Q1 直接取累计）；TTM 要求连续四季完整，并校验四个单季之和等于 `本期累计 + 上年全年 - 上年同期累计`（年末 TTM 为全年值）。所有项在同一 cutoff 独立选最新可见版本，缺任一季度即返回缺失。

## 公开源财务与交易参考扩采

`ashare_data.financials.expansion` 提供公开源（新浪三大报表、分红配股、流通股本、股本结构及深交所官方月历）的断点采集、离线重放与审计，生成交换档案 `ad.financial-source-report.v1` 与 `ad.corporate-source-observation.v1`。

```sh
PYTHONPATH=src python -B -m ashare_data.financials.expansion prepare \
  --root "$DATA_ROOT/financial-reference/batches/public-v1" \
  --daily "$DATA_ROOT/daily.sqlite3" \
  --start 2010-01-01 --end 2026-10-19

PYTHONPATH=src python -u -B -m ashare_data.financials.expansion run \
  --root "$DATA_ROOT/financial-reference/batches/public-v1"

PYTHONPATH=src python -B -m ashare_data.financials.expansion audit \
  --root "$DATA_ROOT/financial-reference/batches/public-v1" --verify --write
```

下游读取使用 `ashare_data.financials.expansion_access.SnapshotReader`：

```sh
PYTHONPATH=src python -B -m ashare_data.financials.expansion_access \
  --root "$DATA_ROOT/financial-reference/batches/public-v1" \
  --task sina_income-sh600176 --period 2025-09-30 --field net_profit \
  --purpose source_research --as-of 2026-10-19T12:30:00+00:00
```

| 用途（`purpose`） | 语义与限制 |
| --- | --- |
| `historical_pit`（默认） | 要求 `historical-bindings.json`（`ad.expansion-historical-bindings.v1`）绑定的官方历史发布与版本证据；缺少绑定时拒绝 |
| `source_research` | 读取当前已捕获快照（查询时刻须晚于捕获或 `usage-reviews.json` 复核时刻），不代表历史当时可知 |
| `listed_security` | 在 `source_research` 基础上要求报告期落在该原代码上市存续区间内 |
| `source_audit` | 查看完整原始记录、缺失与异常，不返回可计算数值 |

对于股本结构页中存在负值但页面内嵌图表 XML 与流通股接口同日唯一记录严格吻合的点值，可通过 `ashare_data.financials.share_resolution.ShareResolutionReader` 按 `purpose="listed_security"` 或 `"source_audit"` 显式读取，原表负值字段本身仍保持未知。

## 历史限价与参考观察批次

`ashare_data.financials.limit_expansion` 复用 `providers.zzshare` 的严格解析规则，按不重叠区间采集 `high_limit/low_limit/is_paused/is_st/factor`（`ad.daily-reference-observation.v1`），读取入口为 `ReferenceReader`：

```sh
PYTHONPATH=src python -B -m ashare_data.financials.limit_expansion prepare \
  --root "$DATA_ROOT/financial-reference/batches/limits-v1" \
  --baseline-root "$DATA_ROOT/financial-reference/batches/public-v1"

PYTHONPATH=src python -B -m ashare_data.financials.limit_expansion read \
  --root "$DATA_ROOT/financial-reference/batches/limits-v1" \
  --symbol sh600000 --date 2010-01-04 --field high_limit \
  --as-of 2026-10-19T13:00:00+00:00
```

## 官方公告索引、日粒度查询与 Tushare 接入

### 官方公告核验与日粒度历史读取

`ashare_data.financials.official_expansion`（搭配系统 Swift 编译的 `scripts/extract-official-pdf.swift`）核实官方机构 ID 并分页抓取定期报告、更正与修订索引。经逐字段版面核验（`ad.official-field-review.v1`）后，可通过 `official_daily.reader` 在 `DAILY_MODE` 下执行有证据边界的日粒度历史查询：

```python
from ashare_data.financials.official_daily import reader
from ashare_data.financials.ledger import DAILY_MODE

view = reader(
    official_root, field_review,
    reference_root=reference_root, reference_tasks=reference_tasks,
)
result = view.query(
    securities, fields, period="2015-03-31",
    as_of="2015-06-08", mode=DAILY_MODE,
)
```

### Tushare 凭据配置与按字段补采

Tushare 参考采集通过官方 HTTPS 接口运行（`ashare_data.financials.tushare_reference`、`tushare_fundamentals`、`tushare_dividends`、`tushare_gaps`）。凭据支持终端隐藏输入（`--prompt-token`）、环境变量（`--token-env`）或由 `scripts/tushare-keychain.swift` 与 `scripts/configure_tushare_keychain.py` 管理的 macOS 登录钥匙串项（服务 `ashare-data.tushare`、账户 `tushare`），不将明文 Token 写入命令行参数、配置文件或日志。
