# 本地日线与分钟主库

本篇介绍本地全量行情主库 `LocalDailyStore`、多源日线增量维护，以及面向下游消费的不可变产品发布接口 `MarketRelease` 与 `LocalMinuteProduct`。

## 本地日线主库（`LocalDailyStore`）

`LocalDailyStore` 管理本地 SQLite/Parquet 混合存储的日线、复权因子、证券目录与来源回执，支持从历史底库初始化并按交易日增量更新；只读部署入口也可通过 `ashare_data.product.LocalDailyProduct` 访问。

```python
from ashare_data.storage.local_daily import LocalDailyStore

store = LocalDailyStore("/path/to/ashare-data-local")
status = store.summary()
df = store.get_price(
    "600000.XSHG",
    start="2024-01-02",
    end="2024-03-29",
    source="auto",
)
```

### CLI 常用命令

```sh
# 查看主库状态与覆盖报告
ashare-data local-daily status --root /path/to/ashare-data-local
ashare-data local-daily report --root /path/to/ashare-data-local

# 按证券或日期查询日线与代码变更别名
ashare-data local-daily query --root /path/to/ashare-data-local --symbol 600000.XSHG --start 2024-01-01 --end 2024-01-31
ashare-data local-daily query-alias --root /path/to/ashare-data-local --symbol 601313.XSHG

# 执行增量更新或断点续跑
ashare-data local-daily update --root /path/to/ashare-data-local
ashare-data local-daily resume --root /path/to/ashare-data-local
```

### 多源日线对账与增量策略

- **底库与当期增量**：历史底库提供基线数据，日常增量通过新浪、腾讯、东方财富及 BaoStock 等公开接口采集并保存原始响应回执。
- **跨源对账（Reconciliation）**：同一证券交易日存在多个来源时，系统按预设容差核对 OHLC、成交量（股）与成交额（元），保留各来源原始记录并在元数据中标明主选来源或冲突原因。
- **代码变更与别名映射**：吸收合并或更名上市证券（如历史代码切换）通过显式别名表维护原代码有效期与承继关系，查询时按请求日期返回对应有效阶段的行情。

## 早期历史

针对 2005 年以前或特殊板块（如早期 PT/退市整理期）的跨源历史记录，`query-early` 提供按原始来源保留的探索性查询：

```sh
ashare-data local-daily query-early --root /path/to/ashare-data-local \
  --symbol 000001.XSHE --start 1995-01-01 --end 1995-12-31 --diagnostics
```

- **数据特征**：早期历史记录按 `(symbol, date, source)` 保留各供应商原始数值，不同来源在早年可能存在手/股单位切换或除权口径差异，因此标记 `strict_usable=False`，不自动缩放或合成总收益。
- **缺口归因**：早年深圳市场或特殊转让阶段缺乏完整独立官方休市表时，基于上交所日历代理计算的缺失日期仅作为待核查缺口展示，附 `--diagnostics` 可查看详细归因状态。

---

## 正式产品发布（`MarketRelease`）

`MarketRelease` 将日线主库、分钟分块、交易日历、证券生命周期、公司行动、财务三表及原生宏观数据封装为带 `manifest_sha256` 的不可变版本化发布，供 `ashare-quant` 等消费者通过只读文件映射直接读取，无需长连接占用主库 SQLite 写锁。

```python
from ashare_data.storage.market_release import MarketRelease

release = MarketRelease(
    root="/path/to/releases",
    release_id=1,
    expected_manifest_sha256="<64位SHA256>",
)

# 读取日线（bars_1d）或分钟线（bars_1m）及发布回执
bars = release.read("bars_1d", "600000.XSHG", "2024-01-02", "2024-01-31")
receipt = release.receipt()

# 读取财务与原生宏观绑定视图
fin = release.financial()
macro = release.native_macro()
```

### 发布构建、备份与增量应用

通过 `ashare_data.product.release_cli` 管理发布生命周期（全局参数 `--root` 须置于子命令之前）：

```sh
# 备份与恢复已提交的正式发布闭包
python -m ashare_data.product.release_cli --root /path/to/releases backup --destination /path/to/backup
python -m ashare_data.product.release_cli --root /path/to/backup restore --destination /path/to/new-releases

# 按冻结计划增量替换受影响的证券分块并发布新版本
python -m ashare_data.product.release_cli --root /path/to/releases apply --plan /path/to/plan.json
```

- **单写入者与原子发布**：发布写入通过 `ReleaseWriter` 与目录锁串行推进，先写入不可变 Parquet/JSON 数据块，校验完整父版本摘要后再原子更新版本清单。
- **补充源合并**：主发布支持将主历史观测与无重叠的 `free-stockdb` 补充记录合并；分钟补充仅按整只证券整日缺失补齐，不做盘中插值。

---

## 本地分钟产品读取（`LocalMinuteProduct`）

`LocalMinuteProduct` 为部署清单中登记的多种分钟数据源提供统一的只读查询入口：

| 来源标识（`source`） | 数据特征与用途 |
| --- | --- |
| `tdx` | 默认来源；基于本地已校验的通达信 1m/5m 分钟柱捕获 |
| `canonical` | 主发布合并分钟序列；仅在 `default_minute_source: canonical` 时作为 `auto` 默认源 |
| `free_stockdb` | 已发布的整日补充分钟数据 |
| `neigezhu` | 经外部清单与质量覆盖层（`quality_overlay.json`）审查的原生高频分钟归档，保留 09:30 开盘观察与行级准入诊断 |

查询接受单只证券与带时区、精确到整分钟的闭区间 `[start, end]`，原样返回 `source`、`binding`、`rows` 与质量诊断报告 `report`。
