# A数达（ashare-data）

## 项目简介

A数达提供 A 股行情查询、来源核验和固定版本的离线研究数据，也提供命令行工具。可以直接查询少量行情，保存原文和版本，再检查覆盖、单位、时间及缺失字段。

当前开发版本为 **0.10.0.dev3**，按行情、参考数据、公司行动、财务、因子、存储和提供者组织接口。此版本有不兼容改名，旧运行环境与冻结数据保持独立；迁移见 [新版接口与身份迁移](docs/migration.md)，代码职责见 [目录说明](docs/development.md#目录与依赖)。

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

| 读者任务 | 指南与入口 |
|---|---|
| 少量行情、缓存、覆盖及可见性 | [行情查询](docs/market-data.md)；Client / get_price |
| 维护长期本地日线及早期观察 | [本地日线](docs/local-daily.md)；LocalDailyStore |
| 读取停牌/ST、前收、日历、上市事实 | [参考数据](docs/reference-data.md) |
| 历史状态、股票池及分钟证据 | [历史市场](docs/historical-market.md) |
| 公司行动、历史股份及事件身份 | [公司行动](docs/corporate-actions.md)；CorporateHistoryView |
| 财报版本、截止、单季和 TTM | [财务](docs/financials.md)；FinancialPIT |
| 具名因子与 Alpha191 公式 | [因子目录](docs/factors.md)、[Alpha191](docs/alpha191.md) |
| 显式选择 BaoStock、TDX、zzshare | [提供者](docs/providers.md) |
| 固定研究数据和独立归档查询 | [研究数据](docs/research-data.md) |
| 日频研究、执行字段和输入预检 | [输入准备](docs/execution-inputs.md) |
| 导入、验证、发布不可变数据 | [快照](docs/snapshots.md)；Store |
| 读取有限事实和条件投影 | [有限研究](docs/finite-research.md) |
| 生产与消费可移植研究输入 | [组件生产](docs/portable-components.md)、[消费契约](docs/portable-research.md) |
| 当前时钟观察和已有 JQ 响应 | [观察数据](docs/observations.md) |
| 修改、构建、升级或分发 | [开发与工具](docs/development.md)、[迁移](docs/migration.md)、[来源与许可](docs/sources-and-licensing.md) |

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
- 本库不提供回测引擎、交易系统、HTTP 服务或 UI，不是 jqdatasdk 的完整替代。参阅 [接口差异](docs/market-data.md#风格适配)、[数据来源](docs/sources-and-licensing.md#来源边界)与[许可说明](docs/sources-and-licensing.md#许可与分发)。自有源码的许可状态和第三方资源许可分别说明。
