# A数达 · ashare-data

## 项目简介

A数达（`ashare-data`）是面向 A 股量化研究的数据基础设施库，提供统一的聚宽风格行情查询、本地日线与分钟主库（`LocalDailyStore` / `MarketRelease`）、多源数据采集（新浪、BaoStock、通达信、免费公开源增量）、参考数据与公司行动、财务三表及因子计算能力。

系统与下游回测引擎（如 `ashare-quant`）解耦：A数达负责原始回执留存、结构化校验、不可变快照发布与点位查询；账户、撮合与策略时钟由消费端独立维护。

## 安装指南

运行环境要求 Python 3.11–3.13（推荐 Python 3.12）与 [`uv`](https://docs.astral.sh/uv/)。

```sh
# 核心安装（含 DuckDB、pandas、numpy）
uv sync --locked --no-dev

# 安装全部可选依赖（AKShare 免费增量、BaoStock/OpenPyXL/QuickJS 本地数据、Alphalens 因子评价）
uv sync --locked --extra free-updates --extra local-data --extra factor-evaluation
```

构建并校验发行包：

```sh
uv build
uv run python scripts/verify_wheel.py dist/ashare_data-0.10.0.dev98-py3-none-any.whl --full
```

## 快速开始

### 在线近期行情查询

默认 `Client` 直连新浪公开接口，支持带本地缓存的日线与 1m/5m 行情读取：

```python
from ashare_data import Client

client = Client(cache_dir="./cache")
df = client.get_price(
    "600000.XSHG",
    count=5,
    frequency="daily",
    fields=["open", "close", "high", "low", "volume"],
)
print(df)
```

### 读取正式本地发布（`MarketRelease`）

当本地已配置 `deployment.json` 或 `LocalDailyStore` 发布目录时，可通过 `MarketRelease` 读取不可变日线、分钟、参考与财务数据：

```python
from ashare_data import MarketRelease

release = MarketRelease("/path/to/releases", release_id=1, expected_manifest_sha256="...")
daily = release.daily("600000.XSHG", start_date="2024-01-02", end_date="2024-01-31")
```

## 使用指南

文档按功能域划分为 8 篇指南：

- [在线行情与不可变快照](docs/market-data.md)：`Client` 近期行情与缓存管理、`Store` / `DataView` 不可变快照导入发布、`export_minute_research` 分钟研究导出。
- [本地日线与分钟主库](docs/local-daily.md)：`LocalDailyStore` 建库与增量更新、`MarketRelease` 统一发布与读取、早期历史及分钟覆盖规范。
- [数据源与免费增量采集](docs/providers.md)：新浪、BaoStock、TDX、zzshare 适配器，聚宽观察录制导入，以及 198+19 项免费公开源增量采集与宏观发布。
- [参考数据与公司行动](docs/reference-and-actions.md)：交易日历、停牌/ST 状态、源前收、上市事实、历史市场状态视图及分红送转复权计算。
- [财务数据与质量台账](docs/financials.md)：三大财务报表 schema、披露日可见性过滤、`LocalFinancialStore` 质量分类与跨源修复。
- [因子库与因子评价](docs/factors.md)：因子目录检索、聚宽 19 项研究因子、GTJA Alpha191、Qlib Alpha158 与 Alphalens 离线评价。
- [研究数据包与可移植组件](docs/research-packages.md)：`ResearchView`、`IndependentQueryView`、`NeigezhuMinuteView`、日频执行输入预检与 Portable 组件生产消费契约。
- [开发验证、来源边界与许可](docs/development.md)：代码结构、验证脚本、内部资源锚点、第三方数据源使用边界与软件许可说明。

## 限制与说明

以下全局约定适用于本库所有模块，各分册不再重复声明：

1. **知识时点（PIT）与时间字段**：系统严格区分行情有效标签（`source_label`）、本地实际接收时刻（`observed_at` / `received_at`）与历史首次公开可得时刻（`available_at`）。除非输入具备经核实的披露/发布时间戳，否则事后采集或回填数据的 `historical_pit` 均为 `False`，接收时间不会倒填为历史可见时间。
2. **代码包与私有数据分离**：Git 仓库、源码包（sdist）与安装包（wheel）仅包含软件代码、公式定义与合成测试夹具（`synthetic`），不包含真实历史行情库、发行人公告原件或账户状态。
3. **职责边界**：所有数据源适配器与视图仅提供带来源摘要的数据读取与质量诊断（`execution_permission=False`），不内置交易策略、账户账本、撮合引擎或自动重试换源逻辑。
