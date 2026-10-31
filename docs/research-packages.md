# 研究数据包与可移植组件

本篇汇总离线研究数据集（`ResearchView`）、独立归档查询（`IndependentQueryView`）、高频分钟研究视图（`NeigezhuMinuteView`）、日频回测输入预检，以及可移植研究组件（Portable）的生产与消费契约。

## 固定研究数据集（`ResearchView`）

`Store` 支持将新浪在线拉取行情、已有客户端缓存或本地 BaoStock 日线 JSON 封存为内容寻址的研究数据集，并在不同进程中离线重开：

```python
from ashare_data import Store

store = Store.init("./research-store")

# 1. 在线拉取新浪近期行情并封存（每股一个有界窗口，至多 10 只股票）
sina_id = store.fetch_price("600000.XSHG", frequency="daily", count=5)

# 2. 导入本地 BaoStock 日线 JSON（adjustflag=3）
local_id = store.import_research(
    ["daily-sh.600000.json", "daily-sz.000001.json"],
    format="baostock_daily",
    calendar_path="calendar.json",
)

# 3. 离线重开与多时钟查询
view = Store("./research-store").research(local_id)
result = view.get_price(
    ["600000.XSHG", "000001.XSHE"],
    start_date="2019-12-30",
    end_date="2020-01-07",
    fields=["open", "close", "high", "low", "volume", "money"],
)
historical = view.at("2020-01-04T00:00:00+08:00", visibility="assumed")
```

- **不可变副本**：`result.data`、`result.report`、`view.descriptor()`、`view.lineage()` 与 `view.coverage(...)` 每次调用均返回深拷贝副本，调用方修改返回值不会污染内部缓存。
- **CLI 命令**：`ashare-data --store DIR research-{fetch,import,snapshots,describe,price,recover}`。

---

## 独立归档与高频分钟研究查询

### 独立归档视图（`IndependentQueryView`）

`IndependentQueryView` 读取 `binding.json` 绑定的多源历史归档（新浪 KLC、东方财富、TDX 开盘竞价、Dolt 归档提交、巨潮公司行动及指数历史成分候选），所有查询显式传入 `role="independent_retrospective_query_research"`：

```python
from ashare_data import IndependentQueryView

view = IndependentQueryView("binding.json", expected_binding_sha256="<64位SHA256>")
role = "independent_retrospective_query_research"

daily = view.read_daily("B27-daily", "000001.XSHE", source="sina-pingan", role=role)
adjusted = view.read_adjusted("B25-adjustment", "000001.XSHE", basis_date="2024-06-14", fq="pre", role=role)
members = view.read_index_members("C04-members", "000300.XSHG", as_of_date="2024-06-14", source="...", role=role, allow_uncertified=True)
```

还支持 `read_paused`（停牌跳过/前值填充研究策略）、`read_opening`（09:25 开盘竞价记录）、`read_corporate_actions`、`read_halt_events`、`read_action_evidence`、`read_archive_daily` 与 `read_archive_calendar`。

### 原生高频分钟研究视图（`NeigezhuMinuteView`）

`NeigezhuMinuteView` 读取部署清单中登记的 `minute-hf-download` Parquet 资产，并校验外部审查的 `file_manifest.csv` 与 `quality_overlay.json`：

```python
from ashare_data import NeigezhuMinuteView

view = NeigezhuMinuteView(
    entry="/path/to/deployment.json",
    manifest="/path/to/file_manifest.csv",
    expected_manifest_sha256="<64位SHA256>",
    review="/path/to/quality_overlay.json",
    expected_review_sha256="<64位SHA256>",
)
result = view.get_price(
    "600000.XSHG",
    mode="retrospective",
    start="2026-06-02T09:30:00+08:00",
    end="2026-06-02T15:00:00+08:00",
)
```

每行附带 `price_status`（`invalid` / `conflict` / `unverified`）、`activity_status`、`diagnostics` 与針對撮合执行（`execution`）和最新估值（`fresh_valuation`）的独立准入裁定 `use_decision`。

---

## 日频回测输入预检与执行视图

- **研究输入预检（`Store.preflight_research_request`）**：按证券、起止日期、所需字段与日历捕获逐项检查数据就绪状态，返回 `readable`、缺失字段与阻塞原因，供 `ashare-quant` 诊断接口调用。
- **日频研究与执行视图（`DailyResearchView` / `DailyExecutionView`）**：读取经哈希固定的日频研究包与执行包（由 `scripts/build_daily_execution.py` 构建），按模拟阶段（盘前 `09:00` 读前收与状态、收盘 `15:00` 读当日 OHLCV 与限价、日终估值与交割）提供只读行情、交易状态、涨跌停限价与公司行动查询。
- **有限日频研究视图（`FiniteFacts` / `ConditionalView`）**：位于 `ashare_data.research`，提供单标的（`600000.XSHG`）有界窗口下的基础事实读取与显式条件假设验证（示例见 `examples/golden.py`）。

---

## 可移植研究组件（Portable）生产与消费

Portable 子系统（`ashare_data.research.portable*`）将本地原始响应（`market` 组件）与结构化事实审阅记录（`facts` 组件）校验合成为自包含的可移植数据包，支持导出到空目录并在另一台机器离线重开。完整端到端流程见 `examples/portable_research.py`。

### 生产与发布接口

| 方法 | 说明 |
| --- | --- |
| `Store.import_portable_sources(kind, classification, documents, claims=None)` | 从用户文件配方自动计算 SHA-256 与字节数并封存组件，返回 `component_id` |
| `Store.import_portable_component(directory)` | 导入含 `component.json`（`schema="portable.component.v1"`）与原件的组件目录 |
| `Store.portable_component(component_id)` | 读取并校验已封存的 `PortableDocument` |
| `Store.validate_portable(price_dataset_id, *, facts_component_id, calendar_component_id=None, state_component_id=None, window_ids, mode)` | 校验价格、日历、状态与事实覆盖一致性，返回带 `report_id` 的校验报告（缺口返回 `status="BLOCKED"`） |
| `Store.compose_portable(...)` | 参数与 `validate_portable` 相同；通过时发布可移植数据集并返回清单，阻断时抛出 `PORTABLE_COMPOSITION_BLOCKED` |
| `Store.export_portable(dataset_id, directory)` | 将可移植产品及最小原件闭包导出至新空目录 |
| `Store.import_portable(directory)` | 在新 `Store` 中离线校验全部原件与派生规则并导入可移植数据集 |

- **组件与数值约束**：价格、金额与前收使用 `decimal_text`（最长 48 字符，至多 8 位小数，禁止非有限数）；成交量为 1–24 位非负整数股字符串。`facts` 组件须提供 `security_identity`、`initial_listing_date`、`buy_round_lot`、`price_tick`、`normal_daily_limit_ratio`、`same_day_resale` 六项带原文定位引用的声明及窗口筛查记录（`coverage`）。
- **协议世代标识说明**：`ashare_data.research`（`portable.py`、`conditional.py`、`finite_facts.py`、`conditional_listing.py`）中的 `VERSION / OWNER_VERSION = "0.10.0.dev1"` 以及 `ashare_data.reference.listing` 中的 `OWNER_VERSION = "0.8.2.dev1"` 是冻结研究包与上市事实的**协议 schema 世代号**（下游 `data-runtime.json` 中记录为 `portable_owner_version`），用于校验历史冻结夹具契约，不随 `ashare-data` 发行包版本（如 `0.10.0.dev98`）递增。

### 消费与验证接口

```python
view = store.portable(dataset_id, profile_sha256=profile_sha, mode="conditional_research")
assumptions = view.assumptions("two_day")
use = view.use("two_day", assumption_ack=ack_refs, consumer_binding=binding)

ctx = use.context(
    current_date="2020-01-03",
    logical_at="2020-01-03T09:00:00+08:00",
    phase="DECIDE",
    query_end="2020-01-02T23:59:59.999999+08:00",
    visibility="assumed",
    consumer="strategy",
    event_cursor=0,
    restore_generation=0,
    epoch=1,
)
prev_close = use.decision_prev_close(ctx, call=ad08_call)
verified = use.verify(prev_close, context=ctx)
```

- **阶段读取方法**：`decision_prev_close`（`DECIDE` 阶段策略读取前收）、`execution_bar`（`SUBMIT_MATCH` 阶段引擎读取当日 OHLCV）、`valuation_bar`（`AFTER_TRADING` / `SETTLEMENT` 估值）、`trading_state`（停牌/ST、源前收与限价）、`listing`、`successor`、`modeled_actions`，以及重校验接口 `verify` / `revalidate`。
- **规范化编码与原生 Codec**：`ashare_data.research.portable_types`（`canonical_bytes`、`canonical_hash`、`decimal_text`、`aware_time`、`exact_int`）与 `ashare_data.research.portable_codec`（`native_ad08_call`、`history_array`、`consume_previous`）负责跨进程确定性哈希与 RQAlpha float64 返回值精确比对。

### CLI

统一通过 `ashare-data --store PATH portable <subcommand> --request REQUEST.json` 调用：

```sh
ashare-data --store ./store init
ashare-data --store ./store portable import-sources --request recipe.json
ashare-data --store ./store portable validate --request validate.json
ashare-data --store ./store portable compose --request compose.json
ashare-data --store ./store portable export --request export.json
ashare-data --store ./store portable query --request query.json
ashare-data --store ./store portable canonical --request canonical.json
```

各子命令请求 JSON 字段与对应 Python 方法的关键字参数一致；校验阻断或请求错误以退出码 `2` 返回结构化 `DataError`。
