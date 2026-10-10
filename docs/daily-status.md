# 历史日线状态公共契约（0.8.3.dev2）

本增量复用BaoStock已接入的匿名公共SDK、固定回执和离线capture，不按三股写特判。每次显式一个证券、至多31个自然日；不支持分钟状态、历史证券池或自动补数。字段定义来自[官方历史K线文档](https://www.baostock.com/mainContent?file=stockKData.md)：`tradestatus=1`正常交易、`0`停牌；`isST=1`是、`0`否。文档没有明确盘中部分停牌细节或ST子类别。

| 公共入口 | 行为 |
|---|---|
| `BaoStockSource.fetch(store, kind="daily_status", security=..., start_date=..., end_date=...)` | 显式串行网络采集，返回capture_id；只请求date/code/tradestatus/isST/adjustflag，d频率、原价标记3，不重采OHLCV |
| `BaoStockSource.get_status(security, store=..., start_date=..., end_date=..., require_known=False)` | 显式采集后返回DailyStatusResult，report.network_used=True |
| `Store.import_baostock_capture(directory)` | 显式离线导入worker原始回执目录；重算原始帧/请求身份/SDK行/时钟/hash，不执行目录内代码；幂等发布 |
| `Store.baostock(capture_id).get_status(require_known=False)` | 固定版本离线读取；返回frozen DailyStatusResult，rows为DailyStatusRow元组；不触发网络 |
| `view.get_status(require_known=True)` | 任一日任一字段未知则DAILY_STATUS_UNKNOWN；成功只表示这些源值已取得，不代表可交易 |
| `view.descriptor()/lineage()/quality()/coverage()` | 原Bao公共回执与质量；状态投影的逐日缺口位于result.report.coverage |

`row.suspended`与`row.is_st`分别为frozen DailyStatusFlag，包含source_field/raw_value/value/state。suspended由tradestatus=0映射True、1映射False；is_st由isST=1映射True、0映射False。两字段独立，不由OHLC、成交量、当前名称或证券基本资料status推断。

| 字段状态 | 含义 |
|---|---|
| source_observed | 原始字符串为文档定义的0/1；是否合成另由row.evidence_kind与report.quality说明 |
| field_not_requested | 旧capture没有请求该字段；不是来源返回空值 |
| row_missing_unknown | 请求自然日无行；不判定休市、停牌、退市或无事件 |
| source_empty | 返回的该字段是空字符串，raw_value仍保留空串 |
| unsupported_enum | 原始值存在但不在已支持0/1集合；value=None，原串保留 |

响应缺列/列数异常不伪造行，原始回执保存后按协议/来源错误拒绝公开正常结果；不静默填0。投影内部也识别response_field_missing，但正常协议校验通常会更早拒绝此异常。

每行保留security、trade_date、capture_id、raw_record_sha256、raw_response_sha256、response_received_at，以及`source_fields`独立副本。`raw_json`为原始行规范JSON，原协议bytes/hash在capture追溯中；`to_dict()`按既有Projection规则输出ISO日期。

## 日期、知识时点和覆盖

trade_date是来源日线标签。response_received_at是该行所在原始响应最后一字节的本次接收时刻，不是历史发布/生效时刻。historical_available_at和historical_eligible仍None；intraday_halts_verified和execution_permission仍False。

0.8.3.dev2的投影策略`daily-source-status-2`把逐页请求、首字节和末字节时间绑定至已校验的wall_clock_events，检查带时区ISO格式；缺字段、非法文本或事件不一致报`DAILY_STATUS_CLOCK_INVALID`。明确记录的时钟失败保留None/unknown。真实压缩帧的顶层`response_completed_at`仍为None；末字节接收记录不等于已验证完成。本版另修复日期上界9999-12-31的空结果投影，返回未知网格，不再溢出。两项来自dev1独立审查，真实九行沿用原件，无重采。

请求起止日期包含端点，投影列出其自然日网格；它不是交易日历，不按工作日猜开闭。`requested_values_known=True`只表示网格中两字段均有已知源值；原query_complete、market_coverage、complete_history和PIT质量不提升。

本轮真实样本为600000.XSHG、000001.XSHE、300750.XSHE的2026-09-28至30，各3行，均返回tradestatus="1"、isST="0"。源状态只作为source_claim_unverified研究读取；三份响应是96压缩帧，外层完整性仍未验证。`view.at(..., visibility="received")`对这些样本继续RECEIPT_NOT_VISIBLE，`verified`继续PIT_UNAVAILABLE，即使当前有真实接收时间也不放宽。

官方[调整记录](https://www.baostock.com/mainContent?file=modifyRecord.md)记载过日线交易状态纠错、isST缺值补充和日线补充；当前历史查询不能冒充历史首次版本。[格式说明](https://www.baostock.com/mainContent?file=dataExplain.md)称停牌日线有行、退市后无行，仍不能从任意缺行反推具体原因。

## CLI和最小示例

```sh
ashare-data --store /新的目录/store init
ashare-data --store /新的目录/store baostock-import /已采集的worker证据目录
ashare-data --store /新的目录/store baostock-query statuses --capture <完整ID>
ashare-data --store /新的目录/store baostock-query statuses --capture <完整ID> --require-known
# 明确发起网络；使用已有官方0.9.4 SDK，无账号/密码/密钥。
ashare-data --store /新的目录/store baostock-fetch daily_status \
  --security 600519.XSHG --start 2020-01-02 --end 2020-01-03 --sdk-path /已有SDK根目录
```

`--require-known`仅用于statuses；分钟标签筛选参数不能用于日状态。来源失败仍保存失败capture并报SOURCE_REQUEST_FAILED，空成功响应按每日未知报告。非法kind、证券、频率、超31日及错误参数在联网前拒绝；无自动重试或换源。

新capture版本`baostock-daily-status-1/receipt-4.1`与旧daily/minute版本分开。旧daily请求字段、manifest/capture ID和get_price输出保持不变；旧daily可投影tradestatus，isST为field_not_requested。basic/minute不能冒充日状态，新status不能get_price。发布中断通过原recover_baostock显式恢复；损坏转aborted。导入只有结构/原文绑定验证，不为用户自制回执认证市场真实性。

离线示例见[examples/daily_status.py](../examples/daily_status.py)。真实市场回执外置，通过ASHARE_DAILY_STATUS_EVIDENCE提供给三项真实回归；无样本明确skip，其余合成控制只验证错误和缺失边界。未修改上层、交易、F2或既有上市事实。
