# 固定 capture 源日历与开市邻日（0.8.6.dev1）

用于扩展研究窗口的数据输入。源日历按请求中的自然日保存，不以周末/工作日习惯推算；没有证券参数，也不能据此推导任何证券当天上市、停牌或可交易。现有 M2 固定证券/窗口和执行门禁不变。

| 公共接口 | 行为 |
| --- | --- |
| `BaoStockSource.fetch(store, kind='calendar', start_date=..., end_date=...)` | 已有显式串行匿名采集；最多31自然日，原参数、回执、capture格式不变 |
| `Store.baostock(capture_id).get_calendar(require_known=False)` | 新增 `SourceCalendarResult`，rows为frozen `SourceCalendarRow`元组，包含请求的全部自然日 |
| `view.get_calendar(require_known=True)` | 空成功响应的每日未知均拒绝；缺行/错误枚举等非空畸形响应仍由既有校验拒绝 |
| `view.get_calendar_links(trading_dates=[date或YYYY-MM-DD,...])` | 新增 `SourceCalendarLinksResult`；1–31个唯一锚点，保持输入顺序。每个锚点须为源声明开市日，前后开市日必须在同capture内有据 |
| CLI `baostock-query calendar --capture ID [--require-known]` | 输出typed日历JSON，退出0仅表示成功读取 |
| CLI `baostock-query calendar-links --capture ID --dates ...` | 输出邻接关系JSON；不接受require-known，因为合法邻接本身要求路径已知 |

`get_trade_days()`原有DataFrame和过滤行为不变，没有新增网络自动入口、补采、换源、跨capture拼接或latest选择。只增加公共读取投影，未修改旧存储schema/capture ID；复现时需同时固定wheel与投影策略 `source-calendar-1`。

## 类型和证据

`SourceCalendarRow.calendar_date`为源日期标签。`row.is_open`是 `SourceCalendarFlag`：保留 `source_field='is_trading_day'`、原字符串 `raw_value`、bool或None的value和state。原"1"为True、"0"为False；空成功响应缺行时value为None、state为 `row_missing_unknown`，不是False。

每行保留capture_id、raw_record_sha256、raw_response_sha256、response_received_at、evidence_kind和raw_json。`source_fields`返回独立副本，原文bytes在原capture lineage中。收到时间绑定同页原始wall-clock事件；有带时区ISO但与事件不一致仍拒绝。明确记录的时钟失败保持None，不猜补时间。

`historical_available_at=None`、`historical_pit=False`、`security_eligibility=None`、`exchange_certified=False`、`execution_permission=False`始终保留。`exchange_scope='provider_calendar_unspecified_exchange'`表示本接口没有独立认证交易所范围；不会自动变成SSE/SZSE/BSE个股通用资格。没有日内sessions或竞价时段推断。

`SourceCalendarLink`包含trading_date、previous_open、next_open、capture_id，以及从previous到next的**全部自然日** `evidence_rows`。前后邻日均不包含锚点本身，所有跨过的日期必须为已知关闭日。即使捕获前/后一天通常开市，只要越出已捕获范围就拒绝。返回是 `derived_from_source_calendar`，不是M2执行plan或官方日历认证。

所有结果的report均返回独立字典，包括原request、coverage、quality、network_used=False和局限。`requested_values_known=True`只证明请求网格的源枚举已知，不等于全市场完整性、最终性、历史当时可见或历史资格。链条不因当前收到完整type-34帧而升级PIT。

## 错误边界

| DataError.code | 条件 |
| --- | --- |
| `INVALID_ARGUMENT` | 非bool require_known，非list/tuple或0/超31日期，重复锚点，datetime、紧凑/非规范日期、非法CLI参数组合 |
| `QUERY_KIND_MISMATCH` | 将行情、日状态、preclose、证券资料capture用于日历 |
| `SOURCE_REQUEST_FAILED` | 会话/协议/身份失败，或既有协议校验已经拒绝重复/越界响应；失败capture仍保留 |
| `SOURCE_SCHEMA_ERROR` | 来源日历缺自然日或含不支持枚举等结构问题；不静默补成关闭 |
| `SOURCE_CALENDAR_UNKNOWN` | 空响应投影含未知，或锚点/邻接路径状态未知 |
| `SOURCE_CALENDAR_NOT_OPEN` | 来源明确声明锚点关闭 |
| `SOURCE_CALENDAR_OUT_OF_SCOPE` | 锚点不在capture请求区间 |
| `SOURCE_CALENDAR_BOUNDARY_UNKNOWN` | 捕获范围内没有所需前一/后一开市日；details.direction区分previous/next |
| `SOURCE_CALENDAR_CLOCK_INVALID` | 逐页时间与事件不绑定、缺字段、非法/无时区文本 |

既有 `INTEGRITY`、`BAOSTOCK_NOT_PUBLISHED`、`PIT_UNAVAILABLE`、`RECEIPT_NOT_VISIBLE`仍适用。批量锚点遇任何失败即整体报错，不返回未声明的部分成功。导入不替原件证明市场真实性。

## 离线示例

```python
from ashare_data import Store

view = Store('/已有本地目录').baostock('完整capture_id')
calendar = view.get_calendar(require_known=True)
links = view.get_calendar_links(trading_dates=['2026-09-29'])
for row in links.rows:
    print(row.previous_open, row.trading_date, row.next_open)
    print([day.raw_response_sha256 for day in row.evidence_rows])
```

日期仅是已有三日真实样本的示例，不表示任意capture都覆盖它。可运行 [examples/source_calendar.py](../examples/source_calendar.py) 并传已有store/capture/日期。

本轮新增2026-09-21至10-09采集尝试在匿名会话阶段返回10002007网络接收错误，业务查询未启动。没有重试或改网；该窗口新真实覆盖未取得。产品验证复用已有09-28至30的真实三行及09-29邻接关系，长假/周末开市等控制样本明确为合成。采集失败记录、固定代码身份、源码/wheel测试和定向独审由包外 `calendar-r1-evidence` 交接，不借用旧版本通过结论。
