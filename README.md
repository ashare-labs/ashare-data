# A数达 0.8.7.dev1：多证券研究输入 preflight

新增 `Store.preflight_research()`、严格JSON请求入口和 `research-preflight` CLI。显式绑定本地固定来源，按证券、锚点和前日/当日/后继用途列出源值、单位、来源hash、质量与缺口；缺项不补默认值，研究值齐全也不授予执行或PIT资格。

[公共类型与错误契约](docs/research-preflight.md) · [离线示例](examples/research_preflight.py)

本轮优先回应AQ的600000.XSHG/2026-09-29三日研究请求，另两股只复用已有材料验证通用接口。未重采已有价格/状态/preclose；同范围日历恢复两次会话均DNS失败，新增真实行0。固定代码、实样请求、作者验证及独审见包外preflight-r1-evidence；旧版本和现有执行owner pin保持冻结。

## 0.8.6.dev1：固定源日历与开市邻日

新增 `Store.baostock(capture_id).get_calendar()` 和 `get_calendar_links(trading_dates=[...])`。
逐日保留源开闭声明、原字段、摘要及接收时间；在同一个capture内给出有据的前一/当前/后一开市日。缺行、未知、关闭锚点或捕获边界不足明确拒绝，不按工作日补数据。

[公共类型、CLI与错误契约](docs/source-calendar.md) · [离线示例](examples/source_calendar.py)

旧get_trade_days和capture格式保持兼容。这是来源日历研究接口，不改变M2固定窗口、历史资格/PIT或执行门禁；本轮新窗口采集会话失败，仅复用已有真实三日日历验证，长假控制为合成。固定身份及实际测试/独审以包外calendar-r1-evidence为准。

以下保留已冻结0.8.5.dev2及更早阶段的功能说明和发布记录，不代表当前候选继承其准入。

## 0.8.5.dev2：可选 zzshare 本地研究适配器

新增默认关闭的 zzshare 日线研究源：显式匿名采集、原文回执离线导入、固定 capture 重开和精确数值公共查询。需 `enable_research=True` 或 CLI `--enable-research`；现有默认源路由保持原样。限价是 `source_claimed`，规则推算结果不填充；不授予官方限价、PIT、完整覆盖或执行许可。

[zzshare 公共契约与错误边界](docs/zzshare.md) · [离线重开示例](examples/zzshare.py)

以下保留冻结基线的既有 preclose 功能说明；旧数据版本及独立发布记录不因本增量重新获得准入。

dev2 修正 `baostock-query --require-known` 帮助文案，明确同时适用于日状态和源 preclose；数据契约、查询逻辑和快照格式沿用已冻结 dev1。

提供通用 `daily_preclose` 显式采集、原始回执导入及固定capture离线查询，保留旧daily/daily_status/minute profile。三股2026-09-28至30共九个真实源值已取得，定义和回执先于实现保存。没有把前日close替代preclose。

[完整公共契约](docs/preclose.md) · [离线示例](examples/preclose.py) · [已有日状态契约](docs/daily-status.md)

```python
from ashare_data import Store, SourcePrecloseResult

store = Store.init("/新的本地目录/store")
sid = store.import_baostock_capture("/已取得的worker回执目录")
result = store.baostock(sid).get_preclose(require_known=True)
assert isinstance(result, SourcePrecloseResult)
for row in result.rows:
    print(row.security, row.trade_date, row.preclose.raw_value, row.preclose.value)
    print(row.preclose.state, row.price_basis, row.response_received_at)
```

查询不联网；主动采集为 `BaoStockSource.get_preclose(...)` / CLI `baostock-fetch daily_preclose`，固定读取为 `baostock-query preclose --capture ID [--require-known]`。单位人民币每股，Decimal保留源精度；自然日缺行、未请求、空串、非法数值和零分开报告。

price_basis=`source_reference_adjustflag_3`：源请求采用adjustflag=3，但除权除息时源preclose可以不同于前日实际收盘。它不是已认证的限价参考价，不能用已知值或require_known成功推无事件、历史资格/PIT或执行许可。末字节接收记录与已验证完成、历史发布时点分开；96帧完整性限制沿用。

当前代码/wheel身份、三份真实样本及测试/独审状态由外置preclose-r1-evidence交付记录绑定。只验证对应影响面，不把上一版本1812项或本轮定向测试称全量。没有新依赖、上层/F2/交易、限价计算或事件覆盖实现；旧候选冻结。下文和PUBLIC_RELEASE.json/PUBLIC_CONTENT.sha256保留历史发布预览，不代表当前候选整体准入或公开发布。

以下为已有公共生产能力说明。

从用户合法取得的市场原文与有来源的事实解释生成不可变数据产品；无需旧机器私有实验包。
新增公共 `import_m2_sources/import_m2_component/validate_m2/compose_m2/export_m2`，既有 M2 消费接口不变。

先读 [公共生产契约](docs/portable-m2/contract.md)、[完整组件格式](docs/portable-m2/component-format.md)。
运行 [公共端到端示例](examples/portable_m2.py)：

```sh
python examples/portable_m2.py --store /新的私有目录/store \
  --market-recipe /用户目录/market-recipe.json --facts-recipe /用户目录/facts-recipe.json \
  --window m2a --window w1 --window w2 --output /新的私有目录/export
```

准备目录必须有可合法使用的实际原文及明确解释；示例不附行情，不伪造必需事实。缺少ST、前收、日历或权益/规则依据时返回缺口并阻断。
软件许可不授予行情或官方原文的再分发权，输出默认仅供用户私下验证；提交与wheel不含这些文件。
查询/组合/导出不联网，不自动换源。新增网络采集不是本版生产流程的必要步骤；既有采集若缺所需字段，同样被阻断。

0.8候选不修改公开0.7代码、旧wheel或A宽前端；新产品需新的数据/profile/ack身份及独审，不继承旧PASS。
原生执行许可仍为false，四项owner假设不放宽，PIT/可见性/权益完整性未知不变。generation/cursor/epoch的历史递增约束由宿主Session负责。

以下为已有模块的历史说明；新M2生产入口以以上契约为准。

# A数达（ashare-data）

**0.7.0.dev1 开发预览**：用于少量、明确范围的 A 股行情查询和离线研究。普通查询直接从新浪公开行情接口取数；可选 BaoStock 通道提供有界日线、5/15/30/60分钟、证券资料和交易日历。它不是交易执行器，也不能直接替换 `jqdatasdk`。

本次公开运行代码与已审候选 `75c19209276625975cb788604cfd4c4c09e94fba` 相同。M2 owner API／公开 codec 的独审结论为有限条件通过；固定 M2 数据包不随源码公开，因此克隆仓库即可尝试普通取数，但不能直接重放 M2。[发布与独审附录](docs/releases/0.7.0.dev1.md) · [许可状态](docs/licensing.md)

## 安装

需要已安装的 Python 3.12 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。在一个新目录中执行：

```sh
git clone --branch release/0.7.0-dev1-preview https://github.com/ashare-labs/ashare-data.git
cd ashare-data
uv sync --locked --no-config --python 3.12
```

依赖从官方 PyPI 按 `uv.lock` 安装到项目 `.venv`，不修改全局 Python。首次安装需要正常联网；不要加 `--offline`，除非已准备完整缓存。项目声明 Python 3.11～3.13；本预览实际验证环境为 macOS arm64／Python 3.12，其他平台尚未验证。本页后续命令均在项目根目录执行；Windows 的环境可执行文件位于 `.venv\Scripts`，Windows 本轮未验收。

这是 Git 源码开发预览，不表示该版本已发布到 PyPI。

## 第一次真实取数：新浪日线

查询浦发银行最近三条日线，不需要账号、API key、手工导入或数据库初始化：

```sh
.venv/bin/python - <<'PY'
from ashare_data import get_price

prices = get_price("600000.XSHG", count=3)
print(prices)
print(prices.attrs)
PY
```

返回北京时间日期索引的 DataFrame，默认字段为 `open`、`close`、`high`、`low`、`volume`；价格单位为元／股，成交量单位为股，默认不复权。`attrs` 保存来源、实际观察时间、原始字段及缺口信息。行情来自实际网络响应，不足、超时或源拒绝会报错，不返回合成行情，也不会静默换源。

近期分钟行情可用同一入口：

```sh
.venv/bin/ashare-data price 600000.XSHG --frequency 5m --count 2
```

新浪支持 `daily`／`1d`、`1m`／`minute`、`5m`，仅源能够提供的有限近期窗口；分钟可额外查询 `money`（元），日线不提供成交额。`count` 与 `start_date` 互斥，时间按 Asia/Shanghai 解释。完整参数与错误码见[直接行情接口](docs/live-api.md)。

## 可选：BaoStock 官方 SDK 与有界日线

BaoStock 不随默认安装或公开源码捆绑。若要使用此通道，从其[官方 PyPI 项目](https://pypi.org/project/baostock/0.9.4/)安装固定 `0.9.4` wheel；公开来源说明见[平台介绍](https://www.baostock.com/mainContent?file=home.md)。在上面的项目环境中执行：

```sh
uv pip install --no-config --python .venv/bin/python --index-url https://pypi.org/simple --only-binary :all: --no-deps baostock==0.9.4
```

这是可选环境安装，不改变 `pyproject.toml` 或锁文件。再次 `uv sync` 可能移除未声明的 SDK，届时可重新执行此命令。A数达会校验 SDK 的逐源码指纹；官方 wheel SHA-256 为 `0bf71c6069ab5890ff3596632f9c3f8f1fbc6bfcac582c2f9d6a5c11ab2cfaf8`。已在其他目录准备 SDK 时，也可显式传 `sdk_path`，详见[接口契约](docs/baostock-api.md)。

只运行一个 BaoStock 采集进程，关闭其他并发 BaoStock 查询。下面只查询一只证券、三个自然日：

```sh
.venv/bin/python - <<'PY'
from pathlib import Path
from ashare_data import BaoStockSource, Store

root = Path(".data/bao-store")
store = Store(root) if root.exists() else Store.init(root)
result = BaoStockSource().get_price(
    "600000.XSHG", store=store,
    start_date="2026-09-28", end_date="2026-09-30",
)
print(result.data)
print(result.report)

# 固定版本可离线重开；这一行不再次采集
capture_id = result.report["capture_id"]
print(Store(root).baostock(capture_id).get_price().data)
PY
```

每次只查询一个证券、一个业务种类。日线最多31个连续自然日；分钟通过 `frequency="5m"`（或15m／30m／60m）显式选择，最多2个连续自然日。没有 BaoStock 1m。日期仅作示例，不表示任何日期均可取得完整数据。源压缩外层校验、历史可见性等未知状态会保留在报告中；免费查询不代表拥有数据再分发权。[来源与许可边界](docs/baostock-source.md) · [分钟与截止时间](docs/baostock-query-fixes.md)

## 缓存与离线研究

新浪查询默认不落盘；显式传缓存目录后，可重开同一请求：

```python
from ashare_data import Client

client = Client(cache=".data/sina")
online = client.get_price("600000.XSHG", count=3)
offline = Client(cache=".data/sina", cache_mode="only")
saved = offline.get_price("600000.XSHG", count=3)
```

`only` 不联网，缺失或损坏明确报错；`refresh` 才强制重新查询，网络失败不会静默使用过期值。需要持久、内容寻址的研究数据集时，使用 [Store 研究接口](docs/research-api.md)。策略时钟 `Client.at(...)` 及各项保证见[数据准入契约](docs/data-admission.md)；`assumed`／`received` 都不等于严格 PIT。

## M2 与验证范围

M2 仅支持 `600000.XSHG`、日频、`m2a/w1/w2` 三个固定窗口及显式条件假设。它需要另行授权取得的、精确摘要匹配的固定输入包；仓库不提供该包、下载地址或自动采集替代品。`examples/m2_prepare_bundle.py` 是维护者对已有材料的离线打包工具，不是数据下载器。[M2契约](docs/m2-api.md)中的冻结 `PENDING` 等字段原样保留，后续独审通过由[独立附录](docs/releases/0.7.0.dev1.md)关联，不回写证据。

公开测试使用合成或协议夹具，不会主动采集真实行情：

```sh
.venv/bin/python -m pytest -q
```

缺少私有固定材料的测试会明确跳过，不能把跳过计为通过。独审阶段使用授权固定包执行过的结果与本次发布验证分别记录在[发布附录](docs/releases/0.7.0.dev1.md)。

## 当前边界

- 普通 `get_price` 固定使用新浪；BaoStock 必须显式选择，不自动切换或混合来源。公共服务可能限流、停机或改变格式。
- 不承诺全市场、完整历史、完整分钟成交、最终定稿或历史当时可见。1分钟源的14:57／15:00标签及竞价缺口保留原样，不补造记录。
- 不提供前后复权、完整公司行动、历史股票池、聚宽等价、生产 SLA、模拟盘或实盘准入。交易日历为上交所当前发布年度；证券资料不作为历史资格证明。
- 公开仓库不包含实际采集响应、M2固定数据包、实验数据库、检查点或 SDK 本体。软件依赖许可和行情来源条件独立适用；项目尚未指定自有源码开源许可证，详见[许可状态](docs/licensing.md)。

[聚宽参数差异](docs/joinquant-compat.md) · [新浪来源与单位](docs/sources.md) · [离线快照](docs/interface.md) · [历史阶段验证记录](docs/validation.md)
