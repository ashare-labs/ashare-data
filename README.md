# A数达（ashare-data）

## 项目简介

A数达是一个用于 A 股行情查询和离线研究的 Python 库，也提供命令行工具。你可以直接获取少量行情，将需要的数据保存为固定版本，再检查来源、单位、覆盖范围和缺失字段。

当前源码版本为 **0.9.1.dev13（开发版）**，本页介绍这一版本的接口。已发布的稳定基线为 **v0.9.0**；开发版的本地日线维护、兼容补齐、因子和有限历史研究能力不都包含在该标签中。版本变化见 [更新记录](CHANGELOG.md)。

默认行情接口使用新浪公开源；BaoStock 和 zzshare 需要显式选择。价格保持原始价，成交量统一为股，成交额统一为元。查询失败会明确报错，不自动切换数据源或补造记录。本地日线库还提供显式兼容模式，按严格一致性规则补齐缺日或空字段，并返回逐字段来源和冲突。

## 安装指南

需要 Python 3.11～3.13、[uv](https://docs.astral.sh/uv/getting-started/installation/) 和支持 `fcntl` 的 Unix 环境。当前验证平台为 macOS arm64 / Python 3.12；原生 Windows 暂不支持，可在 WSL 的 Linux 环境中安装，WSL 尚未做实机验证。

取得开发版源码后，记录提交、安装锁定依赖并确认版本：

```sh
git clone https://github.com/ashare-labs/ashare-data.git
cd ashare-data
git rev-parse HEAD
uv sync --locked --no-config --python 3.12
.venv/bin/ashare-data --version
.venv/bin/ashare-data --help
```

依赖按 `uv.lock` 安装到项目 `.venv`，不修改全局 Python。首次安装需要联网。后续命令均在项目根目录运行，使用 Unix shell 和路径。开发版仍可能调整接口，复现实验时应同时记录源码提交和数据版本。

如需稳定基线，可从 [v0.9.0 源码](https://github.com/ashare-labs/ashare-data/tree/v0.9.0) 或 [GitHub Releases](https://github.com/ashare-labs/ashare-data/releases) 获取对应版本，并遵循该版本的 README；不要将旧版 wheel 与本页开发接口混用。本项目尚未发布到 PyPI，请不要把同名包当作本项目的安装来源。

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
| 维护长期本地日线库 | `ashare-data local-daily` | [历史名单、日线、复权依据和缺口报告](docs/local-daily.md)；需要 `local-data` 可选依赖 |
| 只读本地历史与兼容补齐 | `LocalDailyStore.get_price()` | 默认整段单源；显式 `mode="compatible"` 保留逐字段来源、状态依据和冲突 |
| 原生通达信分钟研究 | `TDXBarSource` / `TDXBarView` | [显式采集与离线读取](docs/tdx-bars.md)；1m/5m 原生 OHLC，分页范围以回执为准 |
| 完整分钟会话研究 | `TDXMinuteSessionView` | [开盘代理、15:00估值、日量及历史预热](docs/minute-session.md)；显式模型与分角色时钟 |
| 当前证券资料、年度交易日历 | `get_security_info()` / `get_trade_days()` | 当前资料不是历史股票池；日历范围受源限制 |
| 保存可重复读取的研究数据 | `Store.fetch_price()` → `Store.research(dataset_id)` | 显式采集，固定版本离线读取 |
| 重开固定多股日频研究包 | `DailyResearchView(binding, expected_binding_sha256=...)` | [预热、时钟边界、来源与显式假设](docs/daily-research.md)；本地开发接口 |
| 查询固定历史状态、证券池与行动证据 | `HistoricalMarketView` | [有界历史接口](docs/historical-market.md)；未知与同键冲突保留，事后证据不证明历史 PIT |
| 有限范围的真实时钟观察 | `python -m ashare_data.daily_observation` / `phase_observation` / `intraday_observation` | [日线观察](docs/current-daily-observation.md)、[分阶段条件](docs/conditional-phase-contract.md)及[盘中观察](docs/intraday-clock-refresh.md)；依赖行动基线的阶段需另行提供[本地证据](docs/calendar-pilot-scope.md#本地证据与公开安装包)，缺失时阻断 |
| 核对固定分钟来源与执行门禁 | `MinuteEvidenceView` | [分钟证据诊断](docs/minute-evidence.md)；当前九项门禁未满足，执行导出仍阻断 |
| 查询 BaoStock 数据 | `BaoStockSource` / `Store.baostock(capture_id)` | 有界日线、5/15/30/60m、日状态、源前收和日历 |
| 检查多证券研究输入 | `Store.preflight_research()` | 显式绑定版本，逐证券、日期和用途列出值与缺口 |
| 读取已有 JQ 稀疏行情 | `Store.import_jq_observations()` → `Store.jq_observations(dataset_id)` | 本地开发接口；保留来源、可见时钟和独立容量用途 |
| 读取固定 JQ 查询观测 | `Store.import_jq_queries()` → `Store.jq_queries(dataset_id)` | [请求、快照、元数据与日历契约](docs/jq-query-observations.md)；不连接 JQ |
| 读取固定通达信分时点 | `Store.import_tdx_points()` → `Store.tdx_points(dataset_id)` | [原始报文、实际采集时钟和精确标签](docs/tdx-points.md)；仅事后研究，无 OHLCV 合成 |
| 因子库统一目录 | `ashare_data.factors.libraries()` / `catalog(library=...)` | [聚宽主因子库（部分实现）与 Alpha191](docs/alpha191.md#两库统一目录)；按来源命名空间区分，保留各库计算入口和限制 |
| Alpha191 公开公式研究 | `Alpha191(...).compute()` | [本地 Python 接口、191 项覆盖/依赖状态](docs/alpha191.md)；默认 183 定义 + 8 原文歧义；[显式版本](docs/alpha191-definitions.md) 提供全部 191 条确定计算路径，146 须给参数；输入和数值约定固定，不承诺聚宽逐值一致 |
| 聚宽主因子库（部分实现）：原十二槽 | `ashare_data.f1` | [财务、换手、市值、TVMA6 及价格候选的限定研究范围](docs/f1-research.md)；价格候选的条件支持与独立源失败分别保留，非全市场或严格 PIT 认证 |
| 按历史截止查询财报版本 | `FinancialPIT.from_file(...).query(...)` | [多公司财务 PIT、单季和 TTM](docs/financial-pit.md)；明确日期边界、修订来源与缺失，首批为官方公告有界样本 |
| 聚宽主因子库（部分实现）：F2 六项 | `FactorResearchView.from_file(...).query(...)` | [BIAS5、CCI20、PSY、速动、杠杆及单季现金利润比](docs/f2-research.md)；12股真实输入，聚宽兼容及历史PIT未验证 |
| 自有数据的严格快照 | `Store.import_bundle()` → `validate()` → `publish()` | 检查覆盖与质量后发布不可变版本 |

多股研究可显式用 `allow_partial=True` 保留可用记录并检查逐证券状态；`QueryLimits` 可调整本地批量预算，不能扩展来源覆盖。直接查询的完整参数和错误码见 [行情 API](docs/live-api.md)。可运行 `.venv/bin/ashare-data --help` 查看命令，再用子命令的 `--help` 查看参数。

### 本地批量维护

[长期本地日线库](docs/local-daily.md)支持现存及历史退市/改代码标的的采集、复权依据版本、断点续取、每日增量和逐股缺口报告。实际覆盖以本地报告为准，不代表全部历史已齐全。为维护任务建立专用环境后，可先查看入口：

```sh
UV_PROJECT_ENVIRONMENT=.venv-local-data uv sync --locked --no-config --python 3.12 --extra local-data
./scripts/local_daily.sh --help
```

`./scripts/local_daily.sh update` 会联网采集并写入本地库，默认数据目录为 `~/Workspace/ashare-data-local`。
完整增量还包括 BaoStock 续取与最终审计，步骤见[日常维护说明](docs/local-daily.md#安装与日常入口)。本地库与原始采集归档独立于源码保存。

已有本地库可用 `ashare-data local-daily query --mode compatible` 显式补齐兼容来源的日期和空字段，
并查询逐字段来源、状态证据与冲突；默认单源行为保持不变。旧代码归档可通过
`--alias-capture` 接入同一查询，需原文校验和公告有效期，见[补齐查询说明](docs/local-daily.md#显式兼容补齐查询)。

[2010 年前主板扩展](docs/local-early-history.md)使用 `local-daily query-early` 显式读取
独立来源观察，保留早退证券、历史周末、单位和上市阶段不确定性。未认证的 BaoStock
响应和异常 HTTP 行仅在 `--diagnostics` 中展示。来源行数与去重证券日数分别统计；
代理日历缺口保留，全部行 `strict_usable=false`，不自动复权或承诺完整可回测、全市场历史 PIT。

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

高级条件研究可使用 [M2 消费接口](docs/m2-api.md)和[可移植组件](docs/portable-m2/contract.md)。它们只支持指定证券、窗口和显式假设，需要用户另行提供合法取得的输入；M2 真实材料不随软件分发，也不授予执行权限。旧的有限事实接口见 [D1](docs/d1-api.md)、[BR1](docs/d1-br1-api.md)和[上市事实](docs/listing-fact.md)。

## 限制与说明

- 不承诺全市场、完整历史、完整分钟网格或最终定稿。新浪日线不提供成交额；未知字段保持缺失，不从其他源静默补齐。
- 不提供完整复权因子、全市场公司行动覆盖或完整历史股票池。限定证据包可查询历史源股票池及已取得的行动，见[历史市场接口](docs/historical-market.md)。原始价、源 preclose 和复权前收含义不同，不能互相替代。
- 收到数据的时间不等于历史发布时间。研究输入齐全不代表 PIT 成立、当时可交易或获得执行准入；分钟标签也不能直接证明聚宽 14:55 的闭合 bar 语义。
- 本项目不包含回测引擎、交易系统、HTTP 服务或 UI，也不是 `jqdatasdk` 的完整替代品。参数差异见 [聚宽兼容说明](docs/joinquant-compat.md)。
- 公开数据源可能限流、停机或变更格式。来源及使用边界见 [数据来源](docs/sources.md)；自有源码尚未指定开源许可证，软件与行情数据的许可分开处理，见 [许可说明](docs/licensing.md)。

运行离线回归：`.venv/bin/python -m pytest -q`。公开回归使用自写合成输入，不采集行情，也不需要试点公告原文。源码、wheel 和源码包均不附实际采集响应、公告全文、发行人 PDF、本地批量库或个人运行归档。测试夹具不提供生产准入，相关边界见[许可说明](docs/licensing.md)。

文档中的历史验收计数对应各自冻结材料和窗口；离线回归通过不代表真实跨日账户验收、全市场覆盖或历史 PIT 认证。查询和观察接口也不会自动启动调度或交易。

固定研究包的模拟消费见[执行输入与用途门禁](docs/daily-execution.md)：`DailyExecutionView` 复用原快照，并分别检查状态、限价、现金与股份可用时点。
