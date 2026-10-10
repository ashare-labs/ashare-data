# 固定 JQ 查询观测：本地研究接口

`Store.import_jq_queries` 导入摘要固定的本地事件文件，`Store.jq_queries` 只读重开。此接口覆盖已采集的有限查询，不连接 JQ，不实现完整 JQ SDK，也不承担 AQ 交易逻辑。行情和完整证券列表留在私有 store，不进入 Git 或安装包。

```python
from ashare_data import Store, JQObservationInput

store = Store.init("/private/query-store")
dataset_id = store.import_jq_queries([
    JQObservationInput(
        case_id="B25", security="000001.XSHE",
        session_date="2024-06-13", session_end_date="2024-06-14",
        events_path="/private/B25/events.jsonl", events_sha256="完整SHA256",
        script_path="/private/B25/jq.py", script_sha256="完整SHA256",
    ),
], source_kind="local_jq_backtest_observation")
view = store.jq_queries(dataset_id)
queries = view.describe()["queries"]  # owner 管理接口，不能暴露给策略
snapshot = view.read(queries[0]["query_id"], as_of="2024-06-14T15:30:00+08:00")
```

输入仍为 `JQObservationInput`，1–10 份，每份至多 8 MiB、1000 事件，运行范围最多 31 个自然日。查询结果最多 6000 行。原始文件与脚本都验 SHA-256；只读取明确的数据请求、数据响应、元数据、日历和采集时钟事件白名单。严格核对嵌套字段、请求响应关系、分块序号、总数与唯一键，拒绝缺块、冲突或夹带订单字段。脚本不执行；账户、订单、成交、题库标准答案不进入产品。

结果通过 `result.status` 区分 `observed_rows`、`observed_empty`、`observed_source_error`。价格、金额、因子保留源 JSON 的精确十进制字符串；查询产品允许至多 64 位精度，不将复权因子截到行情接口的 12 位。`money` 保留原字段名和元的源约定，volume 为股，价格为元/股。源 `dtypes` 存在则保留，未采集则为 null。NaN/Inf 只接受源的显式 `{"nonfinite":"nan"}` 等标签，不替换成零；空响应保持空列表，停牌填充由请求的 `fill_paused/skip_paused` 和源结果共同表达。所有查询结果 `matching_eligible=false`；行情撮合仍使用独立的稀疏行情接口。

同一历史标签的 raw/pre/post/factor 在不同观察时刻是不同快照，不去重成一套复权因子，也不重算价格。`adjustment_anchor` 是该快照的 JQ 观察日期，原始 `fq`、查询区间与所有字段完整保留，不认证为某个官方复权基准。

`view.query(api, parameters, as_of=..., observed_at=None)` 按**完整参数**匹配实际已观察的请求；返回该时钟下最新一致快照，或 unavailable。可用 `observed_at` 指定一个实际观察时钟。get_price 的 end_date、start_date 接受源格式日期/时间或带 offset 的时间；请求必须显式包含 security、fields、frequency、fq、end_date，并保留源请求里的 count、skip_paused、fill_paused 等字段。不会把参数不同的结果冒充同一请求。

get_price 的未来截止时钟与非法字段由 AD 自身代码分别抛出 `DataError("JQ_FUTURE_QUERY", ...)`、`DataError("JQ_QUERY_FIELD", ...)`，失败后可以继续正常查询。源异常只保留为 `source_error` 数据及 audit 证据，不抛出源异常文本来冒充 AD 行为。`read(query_id)` 是精确源快照读取，`query` 是带 AD 请求验证的入口。

`observed_at/available_at` 是 JQ 回测模拟时钟，读取不得早于观察时刻。C01–C05 若有 `capture_start/end.observed_wall_utc`，另存真实 `physical_capture_interval`，与模拟时钟严格区分；没有墙钟的源保持 null。`published_at`、revision_id 不因存在采集时钟而变为已知。所有产品 `historical_pit=false`；回读验证只证明源传递保真，不是独立市场事实或当时可得性认证。

C01 的完整性只指固定查询返回的 5108 行均齐全，不是对全市场证券域独立审计。每个元数据行另外标记未来 end_date 观测和 `end_date_is_historical_pit_fact=false`，保留 2200-01-01 等源值，禁止升级成当时已知退市事实。C03 2000 年起查询而首条为 2005 年，只说明实际返回范围，不能证明更早无数据。C04 指数成员按查询日期和观察日期保留；C05 是一次固定快照，不是持续更新或修订历史。

日历只提供源明确列出的 dates 和 `source_previous_date/previous_date_anchor`，没有列出的日期不自动判为休市。bootstrap 的交易日历/公司行动未知项不由此暗中改成已验证或无事件。

`describe()` 和 `source_evidence()` 属于 owner 管理/审核通道，包含整个固定产品的快照描述和源错误。AQ 应将它们与策略可见查询分开。市场值的公开接口不意味着把私有行情发布到互联网。
