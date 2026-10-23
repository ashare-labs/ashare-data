# 冻结发布中的日期证券目录

`ReleasedDailyProduct(config).catalogue(as_of="2024-03-04", limit=500, offset=0)`
使用与 `prices()` 相同的已记录上市、退市、代码有效区间筛选证券。调用只读发布的元数据，
不会扫描行情文件、补采数据或推进发布版本。省略 `as_of` 时原有目录接口保持不变。

响应保留 `receipt` 中的发布身份、分页参数、`total` 和 `securities`。每行包含
`security`、`start_date`、当前记录的名称和 `name_is_historical=False`。
无法确认上市区间的证券列入 `unknown_listing_securities`，不默认为当日上市。
退市边界是否包含当日，沿用既有 `listing_facts.body.out_inclusive` 规则。

这是基于固定发布记录的回溯目录。它不保证历史名称、当时可知的公告时间、完整历史证券集合，
也不表示指数历史成分。`historical_pit_verified` 明确为 `False`。
策略消费者必须自行约束查询日期不晚于其模拟时钟，并向调用方保留上述覆盖说明。
行情查询仍使用 `prices` / `minute_prices`；目录成员资格不要求已有完整行情，也不授予执行权限。
