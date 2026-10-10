# zzshare 可选研究适配器

默认关闭，所有导入、读取、采集和恢复操作都要显式启用。该源没有加入现有 Client/get_price 默认路由，不静默切换或覆盖 BaoStock/Sina。用途限于明确标注来源的本地研究；没有官方限价、PIT、完整覆盖或执行许可。

| 公共入口 | 契约 |
|---|---|
| `ZzshareSource(enabled=True).fetch(store, security=..., start_date=..., end_date=...)` | 单证券、模式0、1–31自然日、一次匿名普通HTTP请求；封存原文/回执，返回capture_id；失败也封存并抛DataError（details含capture_id） |
| `Store.import_zzshare_capture(directory, enable_research=True)` | 离线读取 business.raw 和 business.receipt.json；验证请求/摘要，幂等发布 |
| `Store.zzshare(capture_id, enable_research=True)` | 只读、固定完整SHA256版本；不支持latest |
| `view.get_daily(strict=True)` / `get_price(strict=True)` | 同一typed日线研究结果，保留Decimal与原始数字文本；strict拒绝缺自然日或未知必需字段 |
| `view.descriptor()/coverage()/quality()/lineage()` | 独立副本，读取不联网 |
| `Store.zzshare_snapshots(enable_research=True)` / `recover_zzshare(enable_research=True)` | 列出版本或显式恢复中断发布；旧发布不改写 |
| CLI `zzshare-capabilities`；`zzshare-import/fetch/query/snapshots/recover --enable-research` | capabilities可在关闭状态查看；其余命令需显式启用 |

原文中的数值用JSON数字token文本和Decimal双重保留，不截断精度或经过float64。`source_fields`返回独立字典，其中原JSON数字投影为Decimal；整段HTTP原字节和JSON指针仍是原文依据。`source_claimed_limits`只表示服务声称的上下限；`rule_derived_limits=None`表示未计算，不把一致性检查当认证。单位使用服务文档的人民币每股/股/元解释，同时保留来源定义等级；上游与服务端生成方法明确UNKNOWN。因子只留源字段，不提供复权查询。

缺自然日不推断休市、停牌或沿用前收；显式`strict=False`可查看部分数据和unknown状态。`requested_dates_present=True`仅表示请求自然日有返回记录，不表示市场覆盖完整。原状态0/1不转换为可成交性。`historical_available_at=None`，历史PIT、完整覆盖、事件覆盖、执行许可始终False。没有as_of/复权/分钟查询接口；不提供假定的回溯可见性。

发布使用独立SQLite表和内容寻址对象/清单，prepared→published；恢复需显式命令。成功和失败HTTP回执均可封存；拒绝、限频、重定向、连接失败、截断、API错误、畸形JSON和模式/证券/日期不匹配均有明确错误。限频保留Retry-After而不重试。查询是固定本地快照，无自动补数据。

| DataError.code | 含义 |
|---|---|
| `ZZSHARE_DISABLED` | 未显式启用研究源 |
| `ZZSHARE_ACCESS_DENIED` / `ZZSHARE_RATE_LIMITED` | 401/403 或429，停止且不重试 |
| `ZZSHARE_REDIRECT` / `ZZSHARE_HTTP` / `ZZSHARE_NETWORK` | 重定向、其他HTTP错误或连接/读取失败 |
| `ZZSHARE_TRUNCATED` / `ZZSHARE_API` / `ZZSHARE_SCHEMA` | 超限、业务代码错误或结构/身份错误 |
| `ZZSHARE_LENGTH_MISMATCH` | HTTP声明长度与实际捕获不一致 |
| `ZZSHARE_COVERAGE_UNKNOWN` / `ZZSHARE_FIELDS_UNKNOWN` | strict查询缺自然日或必需字段 |
| `ZZSHARE_PRICE_INVALID` | 已知OHLC数值关系错误，strict=False也不放行 |
| `ZZSHARE_INPUT` / `ZZSHARE_RECEIPT` / `INTEGRITY` | 输入、请求回执或摘要校验失败 |
| `ZZSHARE_NOT_PUBLISHED` / `ZZSHARE_RECOVERY_REQUIRED` | 未发布，或需要显式恢复 |

发布状态仅表示证据已封存，不表示价格有效。失败证据可以查询descriptor/lineage/coverage；读取daily仍报原失败代码。HTTP获取时间是本机记录的读取结束时钟，既不是历史发布时间，也不证明源数据最终性。

```python
from ashare_data import Store, ZzshareSource

store = Store.init('/新的本地目录/research-store')
sid = store.import_zzshare_capture('/固定证据/capture', enable_research=True)
view = Store(store.root).zzshare(sid, enable_research=True)
result = view.get_daily()
for row in result.rows:
    print(row.trade_date, row.close.raw_token, row.close.value)
    print(row.source_claimed_limits.high.value, row.rule_derived_limits)
print(result.report)
print(ZzshareSource.capabilities())  # 无网络
```

```sh
ashare-data --store ./research-store init
ashare-data --store ./research-store zzshare-import /固定证据/capture --enable-research
ashare-data --store ./research-store zzshare-query daily --capture 完整ID --enable-research
```

当前投影为 `zzshare-projection-2`，清单仍为 `zzshare-research-1`：相同原文/回执保持相同 capture_id；结果同时标注投影版本，复现时还需固定 wheel。旧文件不改写。每个已知 OHLC 比较对独立检查，不因其他字段缺失而放行矛盾。

新 HTTP 捕获保留有序 `response_header_pairs`（敏感头不保存），`response_headers` 为兼容字典且必须与前者一致。任何重复 Content-Length（即使值相同）、非法长度或实际长度不符均拒绝。历史仅字典回执仍可离线导入；其重复头可见性标记 `legacy_mapping_duplicate_visibility_unknown`，不伪造丢失的头部证据。

包括未知字段在内，JSON 从根节点深度0起最大深度64，所有数字须可表示为有限 Decimal；超限查询报 `ZZSHARE_SCHEMA` 并保留 capture_id 和封存原文，strict=False 也不放行。合法高精度数值仍按原 token 保留。
