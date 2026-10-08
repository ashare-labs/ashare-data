> **0.6.0.dev1：D1有限官方事实与固定组合版本。** [公共接口及分字段裁决](docs/d1-api.md) · [有限缺口与未批准的研究假设提议](docs/d1-evidence-options.md)。
> 7/11必需字段可作限定事后研究读取；历史资格、每日限价、权益无事件证明、AD08前收仍阻断。`complete=false`；没有执行引擎或放行D1。
> `Store.import_d1_facts`显式封存已审本地包，`Store.compose_d1`固定价格/事实/计划，`Store.d1(ID)`离线返回typed字段与owner回执。
> 所有D1读操作不联网；原始价格ID不变。正向事实、源观察、历史推断和候选值分开保留。

> **0.5.0.dev3：分钟盘中源标签截止筛选与证券资料校验。** [接口、错误与兼容说明](docs/baostock-query-fixes.md)。
> 14:55 截止不会返回更晚标签；闭合、实际可见性与严格 PIT 仍未获证明。

> **0.5.0.dev2：BaoStock 5/15/30/60分钟有界研究查询。** [分钟接口与实测边界](docs/baostock-minutes.md)。
> 仅显式证券、短日期窗口；原标签不平移，压缩外层校验未知继续明示。没有1m或完整历史保证。

> 0.5.0.dev1 新增 BaoStock 免费日线、显式证券资料、短窗口日历及不可变请求证据。
> [接口契约](docs/baostock-api.md) · [来源与许可边界](docs/baostock-source.md)。日线压缩外层校验待验，允许明确标注的研究读取；不提供历史 PIT 或 BaoStock 1m。

> 0.4.0.dev2 修复研究结果深层引用隔离，详见 [隔离修复说明](docs/research-isolation-fix.md)。
> 持久研究数据集：[公开研究接口契约](docs/research-api.md)。
> `Store.fetch_price` 显式有界拉取新浪；`Store.import_research` 只读导入已核实 Bao 原价日线。
> `Store.research(dataset_id)` 跨进程离线重开，`get_price` 返回 Decimal 行情和缺口/谱系报告。
> 这是研究数据读取内核；严格 PIT、最终性及缺证据的交易准入继续拒绝。

# A数达（ashare-data）

## 项目简介

A数达是直接获取 A 股行情的 Python 库和命令行工具。调用 `get_price` 即可从公开行情源取得数据，无需账号、手工导入、初始化数据库或指定快照。

当前支持沪深 A 股的日线、近期1分钟和5分钟行情，单股、多股、日期筛选及按条数查询；可选择本地缓存，用同一接口离线读取。另提供证券名称查询、上交所年度交易日历，以及高级离线数据管理能力。

接口名称、证券代码和 DataFrame 形态向聚宽本地 SDK 靠拢，长期目标是逐步扩展兼容范围。当前 **0.5.0.dev3 为 BaoStock 截止筛选与资料校验修复候选**，尚不能直接替换 `jqdatasdk`，支持范围见[接口说明](docs/live-api.md)。

## BaoStock 分钟直接查询

```python
from ashare_data import BaoStockSource, Store

store = Store("./bao-store")  # 首次使用先 Store.init("./bao-store")
source = BaoStockSource(sdk_path="/path/to/existing/baostock-0.9.4")
result = source.get_price("600000.XSHG", store=store,
                          start_date="2026-09-30", end_date="2026-09-30",
                          frequency="5m")  # 同样支持15m / 30m / 60m
print(result.data)
print(result.report["coverage"]["minute_labels"])

# 固定版本离线重开，不触发网络，不需要SDK
capture_id = result.report["capture_id"]
result = Store("./bao-store").baostock(capture_id).get_price()
```

```sh
.venv/bin/ashare-data --store ./bao-store baostock-fetch minute --frequency 5m --security 600000.XSHG --start 2026-09-30 --end 2026-09-30 --sdk-path /path/to/existing/baostock-0.9.4
.venv/bin/ashare-data --store ./bao-store baostock-query price --capture CAPTURE_ID
```

此入口显式使用BaoStock，最多2个连续自然日；默认`frequency="daily"`保持日线接口。
下面的通用顶层`get_price`沿用原新浪通道，不会自动替换来源。

## BaoStock 显式研究查询

使用已有官方0.9.4 SDK（不自动安装或登录付费账号）：

```python
from ashare_data import BaoStockSource, Store

store = Store.init("./bao-store")  # 新建一次；以后使用 Store("./bao-store")
source = BaoStockSource(sdk_path="/path/to/existing/baostock-0.9.4")
result = source.get_price("600000.XSHG", store=store,
                          start_date="2026-09-28", end_date="2026-09-30")
print(result.data)
print(result.report)  # 单位、原始/待验质量、完整性、capture_id

# 以后无需 SDK 和联网：固定版本重新验证磁盘证据
capture_id = result.report["capture_id"]
result = Store("./bao-store").baostock(capture_id).get_price()
```

`source.get_security_info("600000.XSHG", store=store)`查询显式证券资料；
`source.get_trade_days(store=store, start_date="2026-09-28", end_date="2026-09-30")`查询源交易日。
当前资料不回填为历史股票池。每次仅一个证券和最多31个自然日。

```sh
.venv/bin/ashare-data --store ./bao-store baostock-fetch daily --security 600000.XSHG --start 2026-09-28 --end 2026-09-30 --sdk-path /path/to/existing/baostock-0.9.4
.venv/bin/ashare-data --store ./bao-store baostock-query price --capture CAPTURE_ID
.venv/bin/ashare-data --store ./bao-store baostock-snapshots
.venv/bin/ashare-data --store ./bao-store baostock-recover
```

采集失败也封存失败版本；descriptor给出status，行查询抛出`SOURCE_REQUEST_FAILED`。
业务种类错误、超范围参数、SDK指纹不符在请求前拒绝。恢复不会重试远程请求。

## 安装指南

需要 uv 和已安装的 Python 3.12。在交付源码的项目目录中执行：

```sh
uv sync --locked --no-config --python 3.12
```

依赖锁文件使用官方 PyPI，环境安装到项目的 `.venv`，不修改全局 Python 环境。项目声明 Python 3.11～3.13；当前实测 macOS arm64 / Python 3.12。安装和查询命令在项目根目录执行。

## 快速开始

直接获取浦发银行最近三条日线：

```sh
.venv/bin/python - <<'PY'
from ashare_data import get_price

prices = get_price("600000.XSHG", count=3)
print(prices)
PY
```

返回以北京时间日期为索引的 DataFrame，列为 `open`、`close`、`high`、`low`、`volume`。价格单位为元/股，成交量为股。数据来自实际网络请求，数值随源更新；请求失败会抛出明确错误，不返回合成行情。

获取最近两条5分钟记录：

```sh
.venv/bin/ashare-data price 600000.XSHG --frequency 5m --count 2
```

## 使用指南

### 查询行情

```python
from ashare_data import get_price

# 最近两条1分钟源记录；包含成交额（元）
minute = get_price("600000.XSHG", frequency="1m", count=2,
                   fields=["close", "volume", "money"])

# 指定结束日期，向前取条数；须在源可提供的近期窗口内
history = get_price("600000.XSHG", end_date="2026-09-30", count=2)

# 多股返回 time、code 和所选字段组成的长表
multiple = get_price(["600000.XSHG", "000001.XSHE"], frequency="5m", count=2)
```

也可使用 `start_date` 与 `end_date` 查询区间，两端均包含；`start_date` 与 `count` 互斥。分钟查询按源时间标签筛选，不平移标签或补齐缺行。无时区时间按北京时间解释。默认保留源实际报价，`fq=None` 表示不复权。

`prices.attrs` 保存来源、请求时间、原始字段、单位、缓存状态和不规则时间间隔。当前源只暴露有上限的近期窗口；超出窗口或条数不足会报 `COVERAGE_INCOMPLETE`。返回记录不代表区间内每分钟或每个交易日都完整。

### 使用缓存

```python
from ashare_data import Client

client = Client(cache=".data/market")
online = client.get_price("600000.XSHG", frequency="5m", count=2)

offline = Client(cache=".data/market", cache_mode="only")
saved = offline.get_price("600000.XSHG", frequency="5m", count=2)
```

不指定 `cache` 就不落盘。默认缓存有效期为300秒；`only` 仅读取已缓存的同一请求，缺失即报错；`refresh` 明确重新联网取数。网络失败不会静默使用过期缓存或更换来源。历史原始响应对象不会因刷新而被覆盖。记录能否视为结束受抓取时刻限制：时间走到收盘不会把盘中缓存升级为完整日线；可用 `refresh` 获取新的收盘后响应。

### 数据保证与策略时钟

每股元数据现在分别报告行情标签年龄、HTTP观测年龄、覆盖状态及可见性等级。旧 `strict` 仍只检查相邻返回标签；完整标签覆盖使用 `require_complete=True`；`require_fresh=True` 现在同时检查源水位、所选窗口水位和当前可交易状态。`client.trading_status(..., as_of=...)` 独立返回休市、午休、停牌、可交易或未知，不将缺数据当停牌。

策略时钟使用 `client.at(as_of, visibility=...)` 固定本地缓存，查询不联网。可显式选择 `assumed` 做非空历史研究，或 `received` 使用当时本机收到的版本；二者均不冒充PIT。默认 `verified` 要求真实公布/修订证据，当前源缺证据会拒绝。`end_date`仍只负责标签过滤。固定上下文按每个标签选择最新合格观测，保留逐行来源；较早大窗口不会遮蔽较新小窗口。冲突按可见流和所选标签判断，严格较新合格版本可消解旧冲突；结果保留冲突与消解原文证据，当前有效冲突仍拒绝。count=N的完整性检查仅覆盖取得这N个有据槽所需的范围，所需范围内的未知和缺口仍会拒绝。

`acquire-price`显式尝试单窗口补齐，`live-coverage`检查声明事实，`reconcile-day`诊断量额；无法完成的要求返回未满足状态，不造数。详见[数据准入接口与schema](docs/data-admission.md)及[归因与剩余阻塞](docs/data-attribution.md)。

### 证券、日历和命令行

```python
from ashare_data import get_security_info, get_trade_days

info = get_security_info("600000.XSHG")
days = get_trade_days(end_date="2026-10-08", count=2)
```

证券信息提供当前名称及代码，不冒充历史股票池。日历来自上交所当前发布年度的休市安排；暂不支持其他年份及历史公告时点查询。

```sh
.venv/bin/ashare-data capabilities
.venv/bin/ashare-data security 600000.XSHG
.venv/bin/ashare-data trade-days --end 2026-10-08 --count 2
.venv/bin/ashare-data --help
```

`price` 可追加 `--start`、`--end`、`--fields`、`--cache`、`--cache-mode only`。CLI 输出 JSON；错误输出至 stderr，退出码为2。

需要手工管理自有数据时，仍可使用 `Store` 和 `import / validate / publish / query / snapshots`。这些高级操作要求 `--store`，按[离线快照契约](docs/interface.md)执行；普通行情查询不需要它们。

## 限制与说明

- 数据源固定为新浪公开行情接口，不自动换源。无登录、付费、下单或全市场批量下载功能；公共服务可能中断或改变格式。
- 原生默认值与聚宽有明确差异：`fq=None`、`panel=False`、`fill_paused=False`、`round=False`，默认字段为 OHLCV。前后复权、停牌过滤/填充、因子及完整历史证券集合尚不支持；[逐项差异](docs/joinquant-compat.md)列明替代方式。
- 日线源没有成交额，因此 `money` 仅支持分钟查询。无 `as_of` 的研究入口沿用15:05软件阈值，它不证明当天统计范围已经结束或记录已经最终定稿。有 `as_of` 的声明区间模型另行过滤；供应商历史可见性仍未证明。
- 1分钟源存在14:57到15:00的竞价间隔，保留两条原记录，不生成14:58/14:59。14:55与聚宽已闭合 bar 的等价性仍待核验；`strict=True` 会拒绝不规则时间间隔。
- 真实小样通过不代表2026-04-09至09-30完整分钟历史可用，也不代表真实成交全覆盖。本轮未验收完整策略、Windows、大规模性能或生产稳定性，详见[来源与单位](docs/sources.md)和[验证说明](docs/validation.md)。
