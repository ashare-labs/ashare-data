# BaoStock 日线/资料/日历子契约（冻结0.5.0.dev1）

0.5.0.dev2的分钟扩展见[分钟接口契约](baostock-minutes.md)；本页保留日线首轮边界。

本轮在 `7533b1f3` 之上增加独立来源通道，不改变旧研究版本或严格快照。
采集只由显式 `fetch` 触发；固定版本的所有读取离线并重新核验磁盘内容。

| 接口 | 契约 |
|---|---|
| `BaoStockSource(sdk_path=..., timeout=15)` | 使用现有官方 0.9.4 SDK；逐文件校验固定官方 wheel 的源码指纹。不安装、不读密钥。独立进程、匿名免费公共入口、单连接、无自动重试/换源。 |
| `source.get_price(security, store=..., start_date=..., end_date=...)` | 显式有界采集，封存后返回 ResearchResult；report 含 capture_id。失败抛 DataError，details 保留失败版本ID。 |
| `source.get_security_info(security, store=...)` / `source.get_trade_days(store=..., start_date=..., end_date=...)` | 显式采集并返回对应 ResearchResult。 |
| `source.fetch(store, kind="daily", security="600000.XSHG", start_date=..., end_date=...)` | 日线原价 flag3，显式一个证券及不超过31个自然日；返回不可变 `capture_id`。 |
| `source.fetch(store, kind="basic", security=...)` | 只查一个显式证券的当前源资料，不形成历史股票池。 |
| `source.fetch(store, kind="calendar", start_date=..., end_date=...)` | 查不超过31个自然日的源日历；不自行生成交易日。 |
| `store.baostock(capture_id)` | 重开固定证据版本；哈希、身份、行语义复核失败则拒绝。 |
| `view.get_price()` / `get_security_info()` / `get_trade_days()` | 返回 `ResearchResult(data, report)`；保留源字段、来源行哈希、质量及单位说明。类型不匹配报错。 |
| `view.descriptor()` / `lineage()` / `quality()` / `coverage()` | 显示真实请求、传输/分页/时钟状态及来源限制；收到有限响应不等于历史覆盖。 |
| `view.at(as_of, visibility="received")` | 只接纳完整且时间证据有效的本地已接收版本。`verified` 历史 PIT 未支持。默认无历史时钟的研究读取仍可用。 |
| `baostock-fetch` / `baostock-query` / `baostock-snapshots` / `baostock-recover` | 显式采集/离线查询/列举版本/发布恢复。失败也封存证据，查询报清晰源错误。 |

固定边界：每次一个业务查询，最多2页、128行、单页4MiB；总进程期限和 socket 期限。
日线 volume=股、amount=元，价格=元/股；保留原始字符串、不复权、不把日标签换成已验证闭合时刻。
证券资料是本次取得的资料；日历是源声明，不证明证券状态或历史可见性。
不支持1m；5/15/30/60分钟留待单独验证。

Receipt v4 的 R1/R2 逻辑沿用；压缩96帧需保持外层完整性未知，除非真实证据及协议检验共同证实。
可解码、请求身份一致且 SDK 行相同的有限日线可以作为明确标注的研究资料；其 `query_complete`、`received` 和严格覆盖不会因此获准。
