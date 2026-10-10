# A数达（ashare-data）

## 项目简介

A数达是一个用于 A 股行情查询和离线研究的 Python 库，也提供命令行工具。你可以直接获取少量行情，将需要的数据保存为固定版本，再检查来源、单位、覆盖范围和缺失字段。

当前版本为 **0.9.0**。默认行情接口使用新浪公开源；BaoStock 和 zzshare 需要显式选择。价格保持原始价，成交量统一为股，成交额统一为元。查询失败会明确报错，不自动切换数据源或补造记录。

## 安装指南

需要 Python 3.11～3.13 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。以下命令使用 Python 3.12，在新目录安装固定版本：

```sh
git clone --branch v0.9.0 https://github.com/ashare-labs/ashare-data.git
cd ashare-data
uv sync --locked --no-config --python 3.12
```

依赖按 `uv.lock` 安装到项目 `.venv`，不修改全局 Python。首次安装需要联网。后续命令均在项目根目录运行；下面使用 macOS/Linux 路径，Windows 对应的程序位于 `.venv\Scripts`。本版实际验证平台见 [更新记录](CHANGELOG.md)。

也可从 [GitHub Releases](https://github.com/ashare-labs/ashare-data/releases) 下载 wheel，安装到自己的虚拟环境。本项目尚未发布到 PyPI，请不要把同名包当作本项目的安装来源。

## 快速开始

直接查询浦发银行最近三条日线，无需账号、API key、样例导入或数据库初始化：

```sh
.venv/bin/python - <<'PY'
from ashare_data import get_price

prices = get_price("600000.XSHG", count=3)
print(prices)
print("价格：元/股；成交量：股；时间：Asia/Shanghai；不复权")
PY
```

这里会发起真实网络请求。结果为 pandas DataFrame，默认列是 `open`、`close`、`high`、`low`、`volume`；来源、原始字段和观察时间保存在 `prices.attrs`。条数指源实际返回的记录，不代表连续交易日或完整历史。

也可以用命令行执行相同查询：

```sh
.venv/bin/ashare-data price 600000.XSHG --count 3
```

若遇 `NETWORK_ERROR`，先检查正常网络；`SOURCE_ACCESS_DENIED` 或 `RATE_LIMITED` 表示源拒绝或限流，应停止请求。程序不会用样例数据替代真实响应。

## 使用指南

### 选择查询接口

| 需求 | 入口 | 说明 |
| --- | --- | --- |
| 直接查询日线、近期分钟线 | `get_price()` / `Client.get_price()` | 新浪支持 daily、1m、5m；历史窗口有限 |
| 当前证券资料、年度交易日历 | `get_security_info()` / `get_trade_days()` | 当前资料不是历史股票池；日历范围受源限制 |
| 保存可重复读取的研究数据 | `Store.fetch_price()` → `Store.research(dataset_id)` | 显式采集，固定版本离线读取 |
| 查询 BaoStock 数据 | `BaoStockSource` / `Store.baostock(capture_id)` | 有界日线、5/15/30/60m、日状态、源前收和日历 |
| 检查多证券研究输入 | `Store.preflight_research()` | 显式绑定版本，逐证券、日期和用途列出值与缺口 |
| 自有数据的严格快照 | `Store.import_bundle()` → `validate()` → `publish()` | 检查覆盖与质量后发布不可变版本 |

直接查询的完整参数和错误码见 [行情 API](docs/live-api.md)。可运行 `.venv/bin/ashare-data --help` 查看命令，再用子命令的 `--help` 查看参数。

### 缓存与固定版本

临时缓存适合重复同一请求；`only` 模式不联网，缺失或损坏会报错：

```python
from ashare_data import Client

client = Client(cache=".data/sina")
prices = client.get_price("600000.XSHG", count=3)
saved = Client(cache=".data/sina", cache_mode="only").get_price("600000.XSHG", count=3)
```

长期研究需要保存版本 ID，避免后续查询随上游变化：

```python
from pathlib import Path
from ashare_data import Store

path = Path(".data/research")
store = Store(path) if path.exists() else Store.init(path)
dataset_id = store.fetch_price("600000.XSHG", frequency="daily", count=3)
print(dataset_id)  # 保存这个完整 ID

# 下面只读本地固定版本，不再次访问数据源
view = Store(path).research(dataset_id)
result = view.get_price("600000.XSHG", count=3)
print(result.data)
print(result.report)
```

[研究数据 API](docs/research-api.md)说明版本、来源和覆盖报告；[严格快照 API](docs/interface.md)说明导入、校验、发布、中断恢复与质量隔离。两类版本分别管理，不会因保存成功就获得交易或历史可见性保证。

### 可选数据源与研究检查

BaoStock 需要单独安装固定官方 SDK。先在项目环境中执行：

```sh
uv pip install --no-config --python .venv/bin/python --index-url https://pypi.org/simple --only-binary :all: --no-deps baostock==0.9.4
```

SDK 不随本项目捆绑；再次 `uv sync` 后可能需要重新安装。使用方式及单连接限制见 [BaoStock API](docs/baostock-api.md)。日状态、源前收和日历分别提供保留未知状态的查询接口：

- [停牌与 ST 源状态](docs/daily-status.md)
- [源 preclose](docs/preclose.md)
- [源日历与相邻开市日](docs/source-calendar.md)
- [研究输入检查](docs/research-preflight.md)

zzshare 默认关闭，需要显式启用研究模式，见 [zzshare API](docs/zzshare.md)。固定原文的量额诊断见 [量额检查](docs/turnover-diagnostics.md)。

高级条件研究可使用 [M2 消费接口](docs/m2-api.md)和[可移植组件](docs/portable-m2/contract.md)。它们只支持指定证券、窗口和显式假设，需要用户合法取得的输入；仓库不附真实行情或事实包，也不授予执行权限。旧的有限事实接口见 [D1](docs/d1-api.md)、[BR1](docs/d1-br1-api.md)和[上市事实](docs/listing-fact.md)。

## 限制与说明

- 不承诺全市场、完整历史、完整分钟网格或最终定稿。新浪日线不提供成交额；未知字段保持缺失，不从其他源静默补齐。
- 不提供完整复权因子、公司行动覆盖或历史股票池。原始价、源 preclose 和复权前收含义不同，不能互相替代。
- 收到数据的时间不等于历史发布时间。研究输入齐全不代表 PIT 成立、当时可交易或获得执行准入；分钟标签也不能直接证明聚宽 14:55 的闭合 bar 语义。
- 本项目不包含回测引擎、交易系统、HTTP 服务或 UI，也不是 `jqdatasdk` 的完整替代品。参数差异见 [聚宽兼容说明](docs/joinquant-compat.md)。
- 公开数据源可能限流、停机或变更格式。来源及使用边界见 [数据来源](docs/sources.md)；自有源码尚未指定开源许可证，软件与行情数据的许可分开处理，见 [许可说明](docs/licensing.md)。

运行公开测试：`.venv/bin/python -m pytest -q`。测试不会主动采集行情；需要另行提供固定材料的用例会明确跳过；私有组件测试可设置 `ASHARE_M2_COMPONENTS` 指向自己合法取得的 `market/`、`facts/` 目录后运行。具体通过数、跳过原因和真实取数验证范围记录在本版 GitHub Release 中。
