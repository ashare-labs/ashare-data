# 源 preclose 公共契约（0.8.4.dev1）

本增量只提供来源前收字段，不计算限价、复权因子或完整事件覆盖。三股九行真实回执先取得，再实现通用查询；不按证券特判。每次显式一证券、至多31自然日，固定请求 `date,code,preclose,adjustflag`、日线频率和adjustflag=3。旧daily、daily_status、minute请求字段和capture版本不改。

| 入口 | 语义 |
|---|---|
| `BaoStockSource.fetch(store, kind="daily_preclose", security=..., start_date=..., end_date=...)` | 显式匿名采集，返回capture_id，无自动重试或换源 |
| `BaoStockSource.get_preclose(security, store=..., start_date=..., end_date=..., require_known=False)` | 显式采集后返回SourcePrecloseResult，network_used=True |
| `Store.import_baostock_capture(directory)` | 复用有界离线原始回执导入、完整性验证、幂等及显式恢复 |
| `Store.baostock(capture_id).get_preclose(require_known=False)` | 固定capture离线读取；SourcePrecloseResult.rows为frozen SourcePrecloseRow元组 |
| `get_preclose(require_known=True)` | 任一请求自然日值为未知即SOURCE_PRECLOSE_UNKNOWN；成功仅表示源数值已知，不放行参考价或交易 |
| CLI `baostock-fetch daily_preclose` / `baostock-query preclose --capture ID [--require-known]` | 与库同义，读取不联网 |

`row.preclose`为SourcePrecloseValue，保留source_field、raw_value、Decimal value和state。row同时保留security、trade_date、request_adjustflag、source_adjustflag、unit、price_basis、definition_version、capture_id、raw_record_sha256、raw_response_sha256、response_received_at、evidence_kind、source_fields独立副本。to_dict将Decimal转字符串，日期转ISO；不使用float或舍入。

状态区分source_observed、source_zero、field_not_requested、row_missing_unknown、source_empty、invalid_numeric。零是Decimal("0")及明确source_zero，不伪装缺失；负数、指数/NaN/Infinity/空白等不支持数值保留原串，value=None。仅支持长度不超过64的非负十进制定点串，不强制源值四位截断。require_known只拒绝未知，不能用其成功替代数值适用性审查。

请求起止包含端点；自然日缺行不推休市、停牌或前收延续。旧日线/日状态回执没有请求preclose时返回field_not_requested，不联网补齐；基本资料和分钟不能作为日preclose查询。新preclose回执不能get_price，因为没有OHLCV。新capture版本为`baostock-source-preclose-1/receipt-4.1`，投影策略`source-preclose-1`。

## 字段定义与价基

[BaoStock官方历史行情定义](https://www.baostock.com/mainContent?file=stockKData.md)明确区分来源前收与前一天实际收盘：发生除权除息时，来源前收会按分配/配股等进行计算。[官方复权说明](https://www.baostock.com/helpdocs/pdf/BaoStock%E5%A4%8D%E6%9D%83%E5%9B%A0%E5%AD%90%E7%AE%80%E4%BB%8B.pdf)第二页的不复权示例也展示了这种差异。

因此price_basis固定为`source_reference_adjustflag_3`，表示adjustflag=3请求返回的源参考字段；**不表示未调整的前日实际收盘**。原adjustflag字符串保留。单位CNY/share（人民币每股报价），无数值换算；定义版本`baostock-preclose-definition-1`及原文SHA保存在报告中。

reference_price_eligible和historical_available_at仍None；historical_pit、events_complete、execution_permission仍False。不认定九日无除权事件，不把preclose直接用作限价参考价，也不与前一行close对齐/回填。

## 时钟、来源和验证边界

response_received_at是行所属页的末字节接收记录，并绑定原始时钟事件，格式/对应关系错误报SOURCE_PRECLOSE_CLOCK_INVALID；记录的时钟失败保留None。它不是历史发布或已验证完成时间。三份真实96压缩帧的response_completed_at仍None、query_complete=False；received及verified知识时钟继续拒绝，不提升PIT或最终性。

来源定义、三份原始回执和实际测试均放在外置preclose-r1-evidence。本轮只验选定影响面，最终报告列明实际/未运行suite；既有上市、M2、上层执行不因本轮通过自动获得准入。合成控制只证明错误/未知边界，不代表市场覆盖。

```python
from ashare_data import Store

store = Store.init("/新的本地目录/store")
sid = store.import_baostock_capture("/原始worker回执目录")
result = store.baostock(sid).get_preclose(require_known=True)
for row in result.rows:
    print(row.security, row.trade_date, row.preclose.raw_value, row.preclose.value)
    print(row.price_basis, row.source_adjustflag, row.preclose.state)
print(result.report["coverage"])
```
