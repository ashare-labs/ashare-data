# 持久研究行情 v1：公开接口契约

版本0.4.0.dev1，新增公开接口如下；既有 dev4 行为保持。

| 入口 | 精确形态及返回 |
|---|---|
| 显式新浪拉取并封存 | `store.fetch_price(security, start_date=None, end_date=None, frequency="daily", count=None, timeout=15)` → dataset_id；一只或最多10只显式股票，沿用现有Sina Client，每股一个有界请求，不分页/换源/补数 |
| 保存已有新浪观测 | `client.save_research(store)` → dataset_id；无网络；固定本Client已有观测集合，原文与观测日志进入独立对象引用，不要求A宽读缓存 |
| 导入本地Bao日线 | `store.import_research(paths, *, format="baostock_daily", calendar_path=None)` → dataset_id；只导入success、daily、flag3响应，校验请求/行证券与日期范围；全部保持未知历史接收，不采用新SDK采集器 |
| 公开离线重开 | `store.research(dataset_id)` → ResearchView；校验manifest及所有对象hash/兼容版本，缺失/篡改拒绝，不下载 |
| 描述符 | `view.descriptor()` → dict；identity_kind、dataset_id、manifest_sha256、source、objects、scope、guarantees、reopen、schema/parser/policy版本；原Client query_snapshot_id单列 |
| 多时钟 | `view.at(as_of, *, visibility="verified")` → 新ResearchView；同一dataset_id锁住观测集合，默认verified明确拒绝，可显式assumed/received；旧回填没有接收时间则received拒绝 |
| 聚宽风格行情 | `view.get_price(security, start_date=None, end_date=None, frequency="daily", fields=None, count=None, fq=None, *, require_complete=False, require_fresh=False, require_final=False, require_tradable=False)` → ResearchResult，含`.data` DataFrame、`.report`及`.to_dict()`；默认OHLCV，money/amount同义但不能同传；源标签闭区间；count/start互斥 |
| 覆盖/证据 | `view.coverage(security,start_date,end_date,frequency="daily")` → 报告；`view.quality()`、`view.lineage()` → 数据集质量/谱系；不自动补数据 |
| 目录/恢复 | `store.research_snapshots()`、`store.recover_research()` → 本地目录/恢复报告；不会把研究版本列为旧正式Store published snapshot |

ResearchResult默认价格/金额Decimal、股数整数，Naive索引仍明确Asia/Shanghai源标签。to_dict的rows包含源标签和代码，Decimal在CLI输出字符串。report保留原始source_rows、逐行hash和观测来源、未知字段、研究假设、覆盖与全部未支持保证；数据集可读不等于执行许可。

Sina重开复用dev4观测筛选/冲突/闭合规则；多时钟只换查询条件，不重开浮动cache。默认查询只读已封存原文；原形成中记录不随墙钟成熟。新捕获/导入产生新ID；旧ID不变。没有新源自动回退。Bao导入仅个人研究的旧回填，available_at/response_completed_at为null，不从文件时间或任务时间推回历史知识。

严格verified报PIT_UNAVAILABLE；require_final报BAR_NOT_FINAL。Bao require_complete/fresh/tradable在当前证据不足时分别明确拒绝；可同时返回源日历下六日格一致的source_grid_complete，不能将其提升为供应商/交易真值。Sina带有已封存CoverageContract时沿用该声明契约保证并保留synthetic/observed证据等级，否则拒绝。未实现公司行动、历史股票池、限价、RQ交易投影或订阅流。

CLI新增：`research-fetch`、`research-import`、`research-describe`、`research-price`、`research-snapshots`、`research-recover`，均使用显式`--store`；只有research-fetch联网。确切参数由--help和实现README同时给出。

## 可运行调用

```python
from ashare_data import Store

store = Store.init("./research-store")
# 显式网络操作，每股一个有界窗口；不是任意历史分页。
sina_id = store.fetch_price("600000.XSHG", frequency="daily", count=2)

# 只读既有 Bao 日线 JSON；不运行 Bao SDK，不信任其中任务时间。
local_id = store.import_research(["daily-sh.600000.json", "daily-sz.000001.json"],
                                 calendar_path="calendar.json")
# 另一个进程只需 store 路径和完整 dataset_id，不访问 Client 私有缓存。
view = Store("./research-store").research(local_id)
descriptor = view.descriptor()
result = view.get_price(["600000.XSHG", "000001.XSHE"],
                       start_date="2019-12-30", end_date="2020-01-07",
                       fields=["open", "close", "high", "low", "volume", "money"])
print(result.data)
print(result.report["provenance"][0]["coverage_report"])
# 旧回填的显式研究假设：日线仅在源日期结束后可见。
historical = view.at("2020-01-04T00:00:00+08:00", visibility="assumed")
```

`get_price` 标量证券返回 time 索引；证券列表返回 time/code 列。price/money 是 Decimal；volume 是整数股。`to_dict()` 返回 Python 字典（时间/Decimal 保留类型）；CLI 用 ISO 时间和十进制定点字符串编码。`.data` 与 `.report` 可改，不会修改已封存版本；描述符和谱系返回副本。

无策略时钟的研究查询读封存的合格观测。新浪多观测取每标签最新合格接收版本；同刻异值报 `OBSERVATION_CONFLICT`。不同日线原文同标签异值且接收时间未知报 `VERSION_ORDER_UNKNOWN`，应分别导入成两个版本。`.at()` 改查询时钟与可见性，dataset_id 不变；`received` 表示本机当时已经收到，并不证明历史供应商 PIT。Bao assumed 用源日期日终作为保守研究模型，未证明交易所日线最终性。

`.report.provenance[].records[]` 保留证券、源标签、完整源行、行 hash、原文 hash、接收完成时间、available_at 和未知 bar_start/end；新浪另保留逐行观测 ID、冲突历史和贡献响应。`available_at` 仅新浪 received 模式可指本机接收时间，其余为 null。源原文的其他字段（例如 Bao tradestatus、isST、preclose）仅是源声明，不升级为准入证据。

### 严格错误与限制

| 请求 | 明确结果 |
|---|---|
| `.at(t)` 默认 verified | `PIT_UNAVAILABLE` |
| Bao `.at(t, visibility="received")` | `RECEIPT_TIME_UNKNOWN` |
| `require_final=True` | `BAR_NOT_FINAL` |
| Bao `require_complete/fresh/tradable=True` | `COVERAGE_UNKNOWN` / `FRESHNESS_UNKNOWN` / `TRADING_STATUS_UNKNOWN` |
| `fq="pre"` / `"post"` | `UNSUPPORTED_ADJUSTMENT` |
| 缺请求字段（包括金额） | `SOURCE_FIELD_MISSING`；不补零 |
| 数据不足 count / 空日期区间 | `COVERAGE_INCOMPLETE` |
| Hash 损坏 / 版本不兼容 | `INTEGRITY` / `RESEARCH_VERSION_UNSUPPORTED` |
| 未提交版本 / latest | `RESEARCH_NOT_PUBLISHED` / `INVALID_ID` |

Bao 日期区间可返回部分实际记录，缺口在逐证券 coverage_report 中明确列出；没有完整源日历时 expected/missing/source_grid_complete 均未知，不按工作日推断。源日历匹配仅 `source_grid_complete=True`，`complete=False` 保持；只要要求强完整性仍拒绝。Sina 无 CoverageContract 时 coverage 保持 unknown；已有契约会随 save_research 封存并沿用 dev4 判断，契约等级也保留，不能把 synthetic 声明当实测。

研究对象最多128个、每个2MiB、10只证券；Bao 合计最多10000行，每响应请求最多367自然日；每次 get_price 的 count 为1..1000。此版本保存原始 JSON 和内容寻址清单，SQLite 记录 prepared/published/aborted；已有严格 Parquet/DuckDB 快照路径不变。研究 ID 不会混进正式 `snapshots()`。这一步没有新增分析物化或日线聚合层。

发布按原文→prepared目录→原子清单→published 完成；中断后用 `recover_research()` 校验原文并完成 prepared，损坏者标 aborted。无目录引用的残留原文不影响读者，本版不提供垃圾清理。已发布文件不覆盖；兼容变更需提高 parser/policy 版本。校验能检测内容变化，不证明供应商真实性或提供加密签名。

### CLI

```sh
ashare-data --store ./research-store init
ashare-data --store ./research-store research-fetch 600000.XSHG --frequency daily --count 2
ashare-data --store ./research-store research-import daily-sh.600000.json daily-sz.000001.json --calendar calendar.json
ashare-data --store ./research-store research-snapshots
ashare-data --store ./research-store research-describe --dataset FULL_SHA256
ashare-data --store ./research-store research-price 600000.XSHG 000001.XSHE --dataset FULL_SHA256 --start 2019-12-30 --end 2020-01-07 --fields close volume money
ashare-data --store ./research-store research-recover
```

只有 `research-fetch` 联网；其失败不会发布半个多证券数据集，也不会自动重试、换源或扩大采集。未提供 HTTP 服务、UI、策略引擎、聚宽全 API、历史股票池、证券属性/限价/公司行动执行投影。此次解决 A宽 AD-D1 持久公开重开、AD-D2 描述符和 AD-D3 原生日线读取；不宣称完整 A宽交易验收通过。数据清洗和准入在 A数达；A宽使用公开 ResearchView/ResearchResult 做薄映射。
