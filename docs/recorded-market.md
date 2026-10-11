# 固定 recorder-v2 行情与查询

`Store.import_recorded_market([...], source_kind=...)` 接受 1–10 个 `JQObservationInput`，由 `Store.recorded_market(dataset_id)` 重开。输入原始事件和策略均需完整 SHA-256。接口处理当前价、分块 get_price 请求响应及实际 day_start 的前日信息，不存账户、订单、成交或标准答案；版本一旧接口保持兼容。

分块响应必须按 offset 完整、连续，字段严格匹配请求；跨块重名标签、缺块、冲突当前价、非有限数、夹带字段、源/产品篡改均拒绝。精确请求与原观察时钟绑定，不补查询或数据行。`current` 仅精确时点；`query` 由 AD 自身检查未来截止及未完成当日日线字段，10:00 的 open 可以读，high/low/close/volume/money 拒绝；不会把 JQ 源异常字符串作为 AD 的运行异常。

引擎与策略用途分离：`market(... role=matching)` 只给截至读数时已观察的原始分钟；`retrospective_capacity` 只给同日全日 volume；`retrospective_minute` 明确允许事后读取已固定的精确分钟用于有限重建。它们不证明历史 PIT。`engine_quote` 仅对存在精确分钟的时点，将该 close 与同日更早已观测限价合并，明确记录限价沿用假设与来源，不延续缺失的最新价。策略 current/query 不接触事后通道。角色是调用契约，不是进程安全沙箱。

独立日线、发行人报告继续通过既有 `IndependentQueryView` 读取。数据缺口由 AD 返回 unavailable；AQ 不维护第二份行情账本。软件包与公开源码不包含本地原始行情或回测账户记录。
