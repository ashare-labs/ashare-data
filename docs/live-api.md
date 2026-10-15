# 直接行情接口
独立策略时钟、保证检查、固定观测和覆盖检查见 [数据准入契约](data-admission.md)。


这是 A数达原生 API，借鉴 jqdatasdk 的参数与表结构。它直接访问固定公开源；`Store` 的离线契约单独保留。

| 接口 | 返回及支持范围 |
|---|---|
| `get_price(security, start_date=None, end_date=None, frequency="daily", fields=None, skip_paused=False, fq=None, count=None, panel=False, fill_paused=False, round=False, *, cache=None, cache_mode="prefer", cache_ttl=300, timeout=15, strict=False)` | pandas DataFrame；单股时间索引，多股 time/code 长表 |
| `Client(cache=None, cache_mode="prefer", cache_ttl=300, timeout=15)` | 复用可选缓存配置；同名查询方法 |
| `get_security_info(security, **client_options)` | 当前代码、名称、交易所、源报价时间和来源字典；上市/退市日期未知返回 None |
| `get_trade_days(start_date=None, end_date=None, count=None, **client_options)` | `list[datetime.date]`，上交所当前发布年度；首尾包含 |
| `Client.calendar()` | 当前年度日期列表及来源 URL、观察时间、响应 hash |
| `capabilities()` | 不联网，返回频率、字段、边界和未支持能力 |
| `get_all_securities(types=None, date=None)` | 明确报 UNSUPPORTED_UNIVERSE；替代为显式证券 get_security_info |

## 查询规则

- 证券限沪深 A 股：60/68开头加 `.XSHG`，00/30开头加 `.XSHE`；最多10个，串行低频请求。暂不接收指数、基金或北交所代码。
- 频率为 daily/1d、1m/minute、5m；默认字段 OHLCV，分钟还可请求 money。不提供未验证的 factor/paused/涨跌停等字段。
- 未给 start_date/count 时，默认 count=20；count 为1..1000。start_date/count 互斥。end_date 默认北京时间当前时刻；裸结束日期包含该日，裸起始日期从该日开始。禁止未来日期。
- 原始响应最多1023条，不能分页追溯任意历史。为了找到指定日期，可能请求一个有界近期窗口。起点早于可见源窗口、筛选后为空或不够 count 都报错，不用空表表示成功。
- count 计源实际记录，不代表连续交易分钟；返回后须检查 attrs 中的来源窗口及 irregular_intervals。缺停牌证据，无法承诺区间完整性。`strict=True` 拒绝同一交易时段中已发现的非等间隔标签，竞价差异也拒绝；它不是完整性证明。
- 时间索引不带时区，解释为 Asia/Shanghai；保留原始标签。分钟边界与上游可见性未证，不公开推导的 bar_start/bar_end；available_at=None，point_in_time_verified=False。闭合判断取查询时刻、请求开始时刻和响应观测时刻的最早值（completion_cutoff）。旧缓存没有请求开始字段时使用已有 observed_at。日线对应日期的15:05必须不晚于该限时；分钟源标签也不得晚于限时。缓存不会仅因查询时钟走到收盘或下一天而升级旧记录；新响应仍不代表供应商最终值或聚宽可见性已获验证。
- 数值为 DataFrame 浮点价格/金额及整数股数；原始数值字符串保留在 attrs.provenance[].source_rows，未应用额外四舍五入。
- 多股按 time、code 排序。默认任何一只失败时整次调用抛错。显式 `allow_partial=True` 可保留其他证券和不足 count 的实际记录，逐项状态见下方；已成功请求的响应仍可缓存。

## 缓存与网络

默认不创建目录。传 cache 后，以 URL 定位请求，以 SHA256 保存不可变原始响应对象；请求索引用临时文件+原子替换更新。不是签名，也不是 PIT 数据证明，不将未知历史可见时间填成观测时间。新响应保存 request_started_at 和 observed_at，原有缓存保持只读兼容；抓取期间跨越闭合边界时仍使用较早的请求时刻。

`prefer` 在300秒有效期内复用，否则请求网络。`only` 永不联网，允许读过期响应并在元数据中返回年龄。`refresh` 强制读取源。非固定上下文的only读取要求相同源 URL；日期/count 改变导致窗口参数改变时，可能需要重新取数。不会悄悄回退旧缓存。多个同时刷新是最后写入索引生效，对象仍保留。固定client.at上下文则固定全部已有观测，按实际标签选择最新合格版本，可组合不同datalen窗口；多响应结果保留逐行溯源，不假造单一响应hash，详见[缓存版本契约](data-admission.md)。

单请求最多2 MiB，默认15秒；timeout 可显式设为本机时间表示范围内的有限正秒数，cache_ttl 可设为有限非负秒数。这些是等待与缓存复用预算，不是行情新鲜度保证；同进程请求间隔至少1秒。不重试、不绕过重定向/403/配额、不持有凭据或执行源脚本。连接故障与源能力问题分别报告。

| 错误 | 含义及可用替代 |
|---|---|
| NETWORK_ERROR | 连接/超时问题；检查正常网络，或显式使用已有 only 缓存 |
| SOURCE_ACCESS_DENIED / RATE_LIMITED | 源拒绝访问/配额；停止本次请求，不换主机绕行 |
| SOURCE_HTTP_ERROR | 源 HTTP 错误，保留状态码 |
| SOURCE_NO_DATA | 仅 HTTP成功且 JSON null/空数组；核对代码或周期，不当作网络故障 |
| SOURCE_SCHEMA_ERROR / SOURCE_FIELD_MISSING | 服务错误对象、非数组 JSON、格式变化或字段缺失；不当作空行情。response_kind 区分 service_error_object 与 unexpected_json_type |
| NO_COMPLETED_BARS | 响应抓取时没有可确认结束的记录；到相应时点后可显式 refresh，only 不联网升级旧数据 |
| COVERAGE_INCOMPLETE | 日期超出窗口或条数不足；缩小范围，不代表全历史可用 |
| IRREGULAR_SOURCE_GRID | strict 模式拒绝不规则间隔；false 可取原样并检查间隔元数据 |
| UNSUPPORTED_ADJUSTMENT | 不支持 pre/post；原始价研究可显式 fq=None，复权策略不能据此验收 |
| CACHE_MISS / CACHE_CORRUPT | 无缓存/校验失败；可显式联网 refresh，only 模式不自行补数据 |
| CALENDAR_COVERAGE | 请求超出当前年度，不推测其他年份 |

高级离线快照查询继续完全离线，使用 [Store 契约](interface.md)。直接行情缓存不会自动发布或修改该快照库。


## 部分结果与本地预算

`get_price(..., allow_partial=True)` 及固定版本 `ResearchView.get_price(..., allow_partial=True)` 返回实际可用记录，并在 `attrs` / `report` 的 `query_status` 中逐证券标明 `ok`、`partial`、`unavailable` 或 `not_attempted`。缺口保留原错误码、请求条数和可用条数；`partial_result=true` 表示有缺口或未完成项。全无数据时返回带列名的空表和错误状态。`ok` 只表示请求取得了所需记录，不证明完整历史、PIT、定稿或可交易。

附带覆盖契约时，已确认的不完整覆盖也会记入 `COVERAGE_INCOMPLETE` issue 和 `partial` 状态，保留原记录及覆盖报告。覆盖报告的 `known_incomplete` 与整体 `status` 分别表达确定缺口和未知范围：例如某日有已确认缺口、另一日日历未知时，`known_incomplete=true`、`status=unknown` 同时保留，查询状态为 `partial`、`partial_result=true`。纯未知覆盖不新增研究门槛；未经验证的标签语义、不可见日历或未知证券状态不会被算作确定缺口。`require_complete=True` 继续拒绝不完整或未知覆盖。

这个模式不能与 `require_complete/fresh/final/tradable` 同时使用。缺少所请求字段时该证券标为 unavailable，可改为只请求已有字段；不会把缺值当零。来源冲突、无效格式与损坏仍抛错。429 或访问拒绝会保留已完成结果并停止后续请求，剩余证券标为 not_attempted；不会自动重试或换源。

默认 10 只证券、count 1000、固定观测 100000 行是本地资源预算。需要更大的研究批次可明确配置：

```python
from ashare_data import Client, QueryLimits
client = Client(query_limits=QueryLimits(max_securities=50, max_count=2000))
# prices = client.get_price(my_securities, count=2000, allow_partial=True)
```

顶层 `get_price` 和 `ResearchView.get_price` 也接受 `query_limits`。固定 client.at 上下文及覆盖评估沿用同一预算；直接调用 `CoverageContract.assess` 也可传入 `query_limits`，不传时仍默认 count 上限 1000。调整预算不会扩大新浪单次 1023 条源窗口，不合成历史或跳过来源检查。CLI `price` / `research-price` 支持 `--allow-partial`、`--max-securities`、`--max-count`、`--max-observation-rows`，JSON 输出仍包含完整状态。
