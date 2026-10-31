# 数据源与免费增量采集

本篇涵盖显式选择的数据源适配器（BaoStock、TDX、zzshare）、聚宽观察录制导入、盘前/盘中观察模块，以及面向 198+19 项公开数据的 `free-updates` 增量采集与原生宏观发布。

## BaoStock 采集与离线读取（`BaoStockSource`）

安装 `local-data` extra 后，`BaoStockSource` 调用固定版本的官方 `baostock==0.9.4` SDK 采集日线（≤31 自然日）、5m/15m/30m/60m 分钟线（≤2 自然日）、证券基本资料、交易日历、逐日停牌/ST 状态与源前收，并将原始请求、响应帧和回执封存为 `capture_id`。

```python
from ashare_data import BaoStockSource, Store

store = Store.init("data/bao-store")
result = BaoStockSource().get_price(
    "600000.XSHG", store=store, start_date="2024-10-08", end_date="2024-10-09"
)
capture_id = result.report["capture_id"]

view = store.baostock(capture_id)
print(view.get_price().data)
```

| 接口 | 用途 |
| --- | --- |
| `BaoStockSource(sdk_path=None, timeout=15)` | 绑定当前环境或显式路径的 BaoStock SDK，单连接串行采集 |
| `fetch(store, kind=..., ...)` | 按 `kind`（`daily`、`minute`、`security_info`、`calendar`、`daily_status`、`daily_preclose`）采集并保存 `capture_id` |
| `Store.import_baostock_capture(directory)` | 离线校验 SDK 指纹、协议帧、请求参数与哈希并幂等导入 |
| `Store.baostock(capture_id)` | 打开离线只读视图；支持 `.at(as_of, visibility="source_label")` 按源标签过滤 |
| `Store.baostock_snapshots()` / `recover_baostock()` | 列出本地捕获记录 / 恢复中断的 `prepared` 记录 |

CLI 入口：`ashare-data baostock-fetch`、`baostock-import`、`baostock-query`（示例见 `examples/baostock_demo.py`）。

---

## 通达信分钟与分时（TDX）

### TDX 分钟柱（`TDXBarSource` / `TDXBarView`）

`TDXBarSource` 通过调用方提供的 `eltdx==3.0.3` 环境连接指定 TDX 节点（协议 `0x052d`），采集原生 1m/5m K 线并保存逐页二进制载荷与回执；离线读取通过 `TDXBarView` 完成，无需安装 SDK。

```python
from ashare_data import TDXBarSource, TDXBarView

source = TDXBarSource(host=chosen_node, python_executable=sdk_python)
captured = source.fetch("000001.XSHE", directory="captures/tdx", frequency="1m", count=800, max_pages=1)

view = TDXBarView("captures/tdx", expected_sha256=captured.manifest_sha256)
result = view.get_price(mode="retrospective")
```

- 单次每页至多 800 条、每次至多 32 页（25,600 条）；价格由整数毫元转为 `Decimal`，`volume` 为协议股数并保留 `volume_lots`。

### TDX 分时点价（`import_tdx_points`）与分钟会话（`TDXMinuteSessionView`）

- **分时点价**：`Store.import_tdx_points(TDXPointInput(**receipt), source_kind="local_tdx_history_capture")` 导入单日 `eltdx.minutes.history(include_raw=True)` JSON（每日 240 个点，对应 `09:31..11:30` 与 `13:01..15:00`），通过 `store.tdx_points(id).read(security, at=..., role="retrospective_point_research")` 精确读取。
- **分钟会话视图**：`TDXMinuteSessionView` 组合多日已校验的 `TDXBarView`（至多 10 只证券、31 个交易日），按显式 `read_purpose`（`opening_price`、`minute_history`、`valuation_only`、`closed_daily`、`closing_auction_matching`、`jq_daily_initial_capacity`）和模型时钟提供回测读取。

---

## 可选日线研究源（`ZzshareSource`）

`ZzshareSource` 默认关闭，显式传入 `enabled=True` / `enable_research=True` 时可采集或导入 1–31 个自然日的单证券日线捕获：

```python
from ashare_data import ZzshareSource, Store

store = Store.init("data/zz-store")
capture = ZzshareSource(enabled=True).fetch(
    store, security="600000.XSHG", start_date="2024-10-08", end_date="2024-10-10"
)
view = store.zzshare(capture.capture_id, enable_research=True)
```

CLI 入口：`ashare-data zzshare-fetch`、`zzshare-import`、`zzshare-query`、`zzshare-snapshots`、`zzshare-recover --enable-research`。

---

## 阶段观察与聚宽响应录制

### 阶段观察模块（`ashare_data.observations.*`）

提供显式单次执行的盘前准备、开盘核验、盘中刷新与收盘确认工具：

| 模块 | 用途 |
| --- | --- |
| `ashare_data.observations.daily` | 日线与日历原始响应采集、固定 packet 生成与比较 |
| `ashare_data.observations.phases` | 准备（`admission`）与开盘（`opening`）阶段的证券状态、参考价、限价与行动检查 |
| `ashare_data.observations.intraday` / `intraday_refresh` | 盘中行情观察、源时间与接收时间记录、显式查询窗口刷新 |
| `ashare_data.observations.close_confirmation` | 收盘观察证据核对 |
| `ashare_data.observations.response_replay` | 固定响应离线重放 |

### 聚宽观察与录制行情导入

将本地已采集的聚宽回测事件文件（`events.jsonl`）与脚本摘要导入 `Store`，用于离线重放与对照：

```python
from ashare_data import JQObservationInput, Store

store = Store.init("data/jq-store")
obs_input = JQObservationInput(
    case_id="sample",
    security="000001.XSHE",
    session_date="2024-03-04",
    events_path="events.jsonl",
    events_sha256="<64位SHA256>",
    script_path="jq.py",
    script_sha256="<64位SHA256>",
)

# 1. 稀疏行情观察（支持 strategy / matching / early_order / opening_observation / final_valuation / engine_capacity / initialization 角色）
ds_obs = store.import_jq_observations([obs_input], source_kind="local_jq_backtest_observation")
view_obs = store.jq_observations(ds_obs)

# 2. 完整 API 请求快照观察（按完整 get_price 等参数匹配已观察响应）
ds_qry = store.import_jq_queries([obs_input], source_kind="local_jq_backtest_observation")
view_qry = store.jq_queries(ds_qry)

# 3. 录制行情视图（合并当前价、分块 get_price 与盘前参考）
ds_rec = store.import_recorded_market([obs_input], source_kind="local_jq_backtest_observation")
view_rec = store.recorded_market(ds_rec)
```

- 导入器严格校验 `events.jsonl` 与脚本的 SHA-256，仅提取行情与元数据事件白名单（忽略订单与账户事件），保留 `Decimal` 字符串精度与 JQ 模拟观察时钟（`observed_at`）。

---

## 免费公开源增量采集（`free-updates`）与原生宏观发布

`free-updates` 子系统管理 CMES 候选菜单的首批 198 项与第二批 19 项公开数据（涵盖宏观经济、Shibor/LPR/国债收益率、行业与概念板块、ETF/可转债/期货、融资融券、涨跌停池、龙虎榜、十大股东、巨潮公告与公司资料等）。

### 目录、单次采集与队列调度

```sh
# 浏览目录与生成采集计划
ashare-data free-updates catalog
ashare-data free-updates plan --rows 17 18 19 --context scope.json
ashare-data free-updates collect --root captures/cmes-free --rows 17 18 19 --context scope.json
ashare-data free-updates query --root captures/cmes-free --row 17 --limit 5
ashare-data free-updates report --root captures/cmes-free --output acceptance.json

# 冻结队列与定时配置执行
ashare-data free-updates prepare-queue --config queue.json --save
ashare-data free-updates run-queue --queue <saved-json> --execute
ashare-data free-updates run-scheduled --config schedule.json --execute
ashare-data free-updates run-manual --config profile.json --execute
ashare-data free-updates manual-query --snapshot <snapshot-dir> --offset 0 --limit 100
```

- **批量与多源注册表调度**：
  - 股票/ETF/指数/可转债批量采集由 `ashare_data.providers.free_updates.free_update_batch` 驱动（配置 schema `ad.security-update-batch.v1`）。
  - 第二批 19 项增量由 `ashare_data.providers.free_updates.supplemental_updates` 驱动，支持 `depends_on` 依赖检查（确保可转债、PE/PB、公司资料、分红配股、题材成分与美债等前置项当日完成后再生成成员行情队列）。
  - 多源并发注册表通过 `scripts/run_daily_update_registry.py --profile registry.json --execute`（schema `ad.daily-update-registry.v2`）管理按观察库隔离的每日请求数与响应字节预算（`budget_scope: observation_store`）、来源级冷却与根目录 `STOP` 优雅暂停。

### 交接包与原生宏观发布（`native-macro`）

经校验的宏观采集结果可导出为不可变交接包，并合入 `MarketRelease` 供正式查询：

```sh
# 从观察库导出交接包并预检
ashare-data free-updates prepare-handoff --root <observation-store> --rows ... --execute
ashare-data free-updates preview-intake --bundle <bundle-dir> --coordination <writer-json>

# 准备并查询已发布的原生宏观序列
ashare-data native-macro prepare ...
ashare-data native-macro query --release-root /path/to/releases --release-id 1 --manifest-sha256 <SHA256> ...
```
