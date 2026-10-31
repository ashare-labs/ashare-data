# 在线行情与不可变快照

本篇介绍在线近期行情客户端 `Client`、高级离线快照存储 `Store` / `DataView`，以及面向回测引擎的分钟研究导出接口 `export_minute_research`。

## 在线行情客户端（`Client`）

`Client` 默认使用新浪公开行情接口获取近期日线、1 分钟和 5 分钟 K 线，并可将每次原始观测持久化到本地缓存目录。

```python
from ashare_data import Client

client = Client(cache_dir="./cache")

# 查询近期日线
daily = client.get_price(
    ["600000.XSHG", "000001.XSHE"],
    start_date="2024-10-08",
    end_date="2024-10-15",
    frequency="daily",
    fields=["open", "close", "high", "low", "volume"],
)

# 带详细覆盖与谱系报告的查询
result = client.query_price(
    "600000.XSHG",
    count=10,
    frequency="1m",
    fields=["open", "close", "high", "low", "volume", "money"],
    allow_partial=True,
)
print(result.data)
print(result.report["query_status"])
```

### 查询与缓存参数

| 方法 / 参数 | 说明 |
| --- | --- |
| `Client(cache_dir=None, timeout=15)` | 初始化客户端；指定 `cache_dir` 时自动记录原始响应与接收时间 |
| `get_price(security, start_date=None, end_date=None, frequency="daily", fields=None, count=None, fq=None, allow_partial=False, query_limits=None)` | 返回 `pandas.DataFrame`；单标的以 `time` 为索引，多标的返回 `time`、`code` 列 |
| `query_price(...)` | 参数与 `get_price` 相同，返回 `QueryResult(data, report)`，包含逐行来源哈希与覆盖诊断 |
| `at(as_of, visibility="verified")` | 返回绑定查询截止时钟的视图；`visibility` 可选 `"verified"`、`"received"`（按本机接收时间过滤）或 `"assumed"` |
| `save_research(store)` | 将当前缓存中的合格新浪观测封存至 `Store`，返回不可变 `dataset_id` |

- **字段与单位**：价格为未复权人民币元/股，成交量（`volume`）为整数股，分钟成交额（`money` / `amount`）为人民币元。新浪日线接口不含成交额字段，请求日线 `money` 会返回 `UNSUPPORTED_FIELD`。
- **复权说明**：在线接口仅提供原始未复权行情（`fq=None`）；复权计算参见 [参考数据与公司行动](reference-and-actions.md#复权价格计算)。

### 量额诊断

新浪分钟成交量与成交额累计值可能与日线存在统计口径或竞价阶段差异。可通过 `ashare_data.market.turnover_diagnostics` 对齐检验分钟汇总与日线量额，诊断结果保留原始差异，见 `examples/amount_comparison.py`。

---

## 不可变快照存储（`Store` / `DataView`）

高级离线存储 API 支持将本地 JSON 数据包（bundle）导入、规范化为 Parquet、按覆盖要求校验并发布为内容寻址的不可变快照。

```python
from ashare_data import Store

store = Store.init("./store")                     # 初始化空存储目录
batch_id = store.import_bundle(bundle_dict)       # 内容寻址导入，重复导入幂等
report = store.validate([batch_id], requirements=claims, policy="observed")
snapshot_id = store.publish(report["id"])

with store.snapshot(snapshot_id) as data:
    bars = data.bars(["600000.XSHG"], start, end, quality="observed", strict=True)
    cov = data.coverage(["600000.XSHG"], start, end, quality="observed")
    cal = data.calendar("2024-01-01", "2024-01-31")
```

### 核心方法

| 方法 | 返回值与说明 |
| --- | --- |
| `Store.init(root)` / `Store(root)` | 创建新存储目录 / 打开已有存储目录 |
| `import_bundle(dict)` | 导入符合 `schema_version=1` 的字典（见 `tests/fixtures/snapshots/golden.json`），返回批次 SHA-256 |
| `validate(batch_ids, requirements, policy="observed")` | 校验覆盖完整性、来源冲突与质量等级，返回验证报告 |
| `publish(report_id)` | 将通过校验的报告原子发布为不可变快照，返回 `snapshot_id` |
| `snapshot(snapshot_id)` | 打开只读 `DataView`（支持上下文管理器，关闭时释放内存 DuckDB） |
| `DataView.bars(symbols, start, end, quality="observed", strict=True, as_of=None)` | 查询 `[start, end)` 左闭右开区间内的分钟柱；`strict=False` 时返回可用记录并在 `coverage` 中列出缺口 |
| `DataView.coverage(...)` / `instruments(as_of)` / `calendar(...)` / `sessions(date)` | 查询覆盖明细、当日证券集合、交易日历与交易时段 |
| `Store.snapshots()` / `Store.recover()` | 列出全部快照状态 / 恢复中断的 `prepared` 事务 |

### Bundle 字段与质量等级

- **时间与可见性**：每根完整分钟柱须满足 `bar_end <= observed_at`。若提供 `available_at`，须附带 `visibility_evidence` 且满足 `bar_end <= available_at <= observed_at`。带 `as_of` 查询时同时要求 `bar_end <= as_of` 且 `available_at <= as_of`。
- **质量等级（`quality`）**：
  - `observed`：具备明确来源与时间语义声明的本地观测；
  - `inferred`：派生或重建数据；
  - `unverified`：时间区间等语义尚未独立验证；
  - `synthetic`：合成测试样本（需显式指定 `policy="synthetic"`）；
  - `research`：查询策略组合，允许读取 `observed` / `inferred` / `unverified`。

---

## 分钟研究导出（`export_minute_research`）

`export_minute_research(snapshot, metadata)` 从已发布快照中导出符合 `ad-minute-research.v1` 规范的 JSON 字典，供下游回测引擎（如 `ashare-quant`）预检与消费：

```python
from ashare_data import export_minute_research

with store.snapshot(snapshot_id) as snapshot:
    document = export_minute_research(snapshot, metadata)
```

- **输入 `metadata`**：需提供 `real_market_data`、`securities`、`days`、`sessions` 以及日历、逐日状态、前收和公司行动等配套研究证据。可选传入 `bar_annotations["证券|带时区标签"]` 标注零成交分钟阶段（如 `closing_auction`）。
- **输出结构**：由导出器生成 `schema`、`producer`、`minute` 与排除自身后的规范化摘要 `content_sha256`。每根分钟线在 `evidence.ad_row` 中保留原始行与 `source_fields`；`replay_bar_start` / `replay_bar_end` 记录回放模型下的分钟起止区间。
