# A数达（ashare-data）

## 项目简介

A数达提供 A 股行情查询、来源核验和固定版本的离线研究数据，也提供命令行工具。可以直接查询少量行情，保存原文和版本，再检查覆盖、单位、时间及缺失字段。

当前开发版本为 **0.10.0.dev2**，按行情、参考数据、公司行动、财务、因子、存储和提供者组织接口。此版本有不兼容改名，旧运行环境与冻结数据保持独立；迁移见 [新版接口与身份迁移](docs/migration.md)，代码职责见 [目录说明](docs/architecture.md)。

默认行情使用新浪公开源。BaoStock、TDX、zzshare 和已有 JQ 观测需显式选择。原价、成交量股、成交额元分别保留来源，失败不会自动换源或补造记录。

## 安装指南

需要 Python 3.11～3.13 和支持 `fcntl` 的 Unix 环境。当前验证平台为 macOS arm64 / Python 3.12；其他平台需另行验证。使用 [uv](https://docs.astral.sh/uv/getting-started/installation/) 在独立项目环境安装：

```sh
git clone https://github.com/ashare-labs/ashare-data.git
cd ashare-data
# 切换到已经核对的候选提交后记录身份
git rev-parse HEAD
uv sync --locked --no-config --python 3.12
.venv/bin/ashare-data --version
.venv/bin/ashare-data --help
```

首次安装需要联网。依赖按 `uv.lock` 固定，不修改全局 Python；需要本地批量采集或 BaoStock 时，使用 `uv sync --locked --no-config --extra local-data` 安装可选依赖。源码、wheel 和 sdist 均不携带实际行情、公告全文、数据库或账户材料。

开发版应固定到已验证的完整提交或 wheel 摘要，不能把旧 wheel 与新文档混用。历史版本请查看对应 Git 标签和说明；当前候选尚未发布或合并。

## 快速开始

查询最近三条日线会发起真实网络请求，无需账号或密钥：

```python
from ashare_data import get_price

prices = get_price("600000.XSHG", count=3)
print(prices)
print(prices.attrs)  # 来源、单位和实际观察时间
```

```sh
.venv/bin/ashare-data price 600000.XSHG --count 3
```

返回 pandas DataFrame，默认列为 open、close、high、low、volume。条数只代表源返回的记录，不代表完整交易日历史。网络、限流和源格式错误明确报错。

无需联网的最小示例：

```sh
.venv/bin/python examples/golden.py --store /tmp/ashare-data-example
```

该示例使用合成数据演示导入、校验、发布及读取，不提供生产准入。输出目录需尚不存在。

## 使用指南

### 按功能选择入口

| 需求 | 当前入口与文档 |
| --- | --- |
| 直接行情和缓存 | `Client` / `get_price`；[行情 API](docs/live-api.md) |
| 长期本地日线、显式跨源补齐 | `ashare_data.storage.local_daily.LocalDailyStore`；[本地库](docs/local-daily.md)、[早期来源](docs/local-early-history.md) |
| 源停牌/ST、前收、日历、上市事实 | [日状态](docs/daily-status.md)、[前收](docs/preclose.md)、[源日历](docs/source-calendar.md)、[上市事实](docs/listing-fact.md) |
| 固定历史状态及股票池 | `HistoricalMarketView`；[历史市场](docs/historical-market.md) |
| 公司行动、历史股份与事件身份 | `CorporateHistoryView`；[行动历史](docs/corporate-history.md) |
| 财报版本、单季、TTM | `FinancialPIT`；[财务时点契约](docs/financial-pit.md) |
| 因子目录与计算 | `ashare_data.factors.catalog` / `libraries`；[19 个具名研究字段](docs/factors.md)、[Alpha191](docs/alpha191.md) |
| BaoStock / TDX / zzshare | [BaoStock](docs/baostock-api.md)、[TDX 分钟](docs/tdx-bars.md)、[zzshare](docs/zzshare.md) |
| 保存固定研究数据 | `Store.fetch_price` / `Store.research`；[研究 API](docs/research-api.md)、[输入预检](docs/research-preflight.md) |
| 多股日频研究和执行字段诊断 | `DailyResearchView` / `DailyExecutionView`；[日频研究](docs/daily-research.md)、[用途门禁](docs/daily-execution.md) |
| 不可变数据发布 | `Store.import_bundle` → `validate` → `publish`；[快照接口](docs/interface.md) |
| 有限条件研究 | [有限事实](docs/finite-facts.md)、[条件投影](docs/conditional-research.md)、[可移植组件](docs/portable/contract.md)、[消费契约](docs/portable-research.md) |
| 实际时钟观察及分钟证据 | [观察入口](docs/observations.md)、[分钟证据](docs/minute-evidence.md)、[分钟会话](docs/minute-session.md) |
| 已有 JQ 固定观测 | [稀疏行情](docs/jq-observations.md)、[完整请求观测](docs/jq-query-observations.md)；不连接 JQ |

### 缓存与固定版本

```python
from ashare_data import Client, Store

client = Client(cache="data/sina")
prices = client.get_price("600000.XSHG", count=3)
replay = Client(cache="data/sina", cache_mode="only").get_price("600000.XSHG", count=3)

store = Store.init("data/research")  # 新目录；已有目录用 Store(path)
dataset_id = store.fetch_price("600000.XSHG", frequency="daily", count=3)
result = Store("data/research").research(dataset_id).get_price("600000.XSHG", count=3)
print(dataset_id, result.report)
```

保存完整 dataset_id。`cache_mode="only"` 和固定版本读取不联网，缺失或损坏会报错。保存成功不等于来源真实、完整或历史可见。

### 本地维护与开发

`ashare-data local-daily --help` 和 `scripts/local-daily.sh --help` 是维护入口，采集命令会写库并联网。默认根目录为 `~/.local/share/ashare-data`；已有库必须显式传 `--root`，升级不会寻找、搬移或覆盖旧库。

开发工具统一在 `scripts/`。`python scripts/check_structure.py` 检查目录、内部导入和文档链接；完整流程见 [贡献约定](CONTRIBUTING.md)。测试只使用合成或最小协议输入，源采集需另行显式执行。

## 限制与说明

- 不保证全市场、完整历史、分钟闭合网格、完整公司行动或最终定稿；未知字段保留缺失，不自动补零、拼接或推断停牌。
- 源 preclose、前一日实际 close、复权前收与交易所参考价不同。公司行动日期、到账和可售时点分别检查，不替代账户结算。
- 收到数据的时间不等于历史发布时间。计算、哈希或离线测试通过不认证 PIT、来源真实性、供应商因子等价或可执行准入。
- 有限事实、条件策略及观察政策仍有限定证券/日期，改名不会扩展覆盖。旧冻结账户和证据需留在原固定环境，新运行记录新代码与输入身份。
- 本库不提供回测引擎、交易系统、HTTP 服务或 UI，不是 jqdatasdk 的完整替代。参阅 [接口差异](docs/joinquant-compat.md)、[数据来源](docs/sources.md)与[许可说明](docs/licensing.md)。自有源码的许可状态和第三方资源许可分别说明。
