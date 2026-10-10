# 公司行动与复权：预留 schema，首版未启用

当前 `snapshot.corporate_actions(...)` 和 `snapshot.adjustment_factors(...)` 都抛出
`DataError(code="UNSUPPORTED")`；`bars(adjustment=...)` 只接受 None。
未导入数据不能返回空列表来暗示“没有公司行动”，更不能伪造 factor=1。
本文件为下一版数据契约草案，首版 importer 不接受公司行动/因子顶层字段。

公司行动记录的拟定字段：

| 字段 | 类型/约束 |
|---|---|
| symbol/event_id/revision/source_id | 证券、源事件稳定标识、修订号、来源标识，联合键不可重复 |
| action_type | cash_dividend / split / bonus / rights 等明确事件类型，不认识的值 unsupported |
| effective_at | 市场生效时间，aware timestamp；日期事件需另声明其日期边界含义 |
| announcement_at | 公告发布时刻；无法取得可为 null，不推断为零点 |
| available_at | 当时可知时刻，必须有可追溯证据才能用于历史 as_of |
| observed_at | 本系统观测时刻，不能替代 available_at |
| cash_per_share/currency | 元/股、CNY；无此项用 null，不能把 null 当 0 |
| share_ratio/rights_price | 明确基于旧股/新股的定义及单位，不靠字段名猜测 |
| supersedes / raw_fields / evidence | 被替代修订、完整源字段、来源位置和内容 hash |

调整因子记录的拟定字段：symbol、effective_at、available_at、observed_at、source_id、revision、
basis_date、adjustment_kind、factor_value、formula_version、included_event_ids、raw_fields、evidence。
必须声明乘法方向、价格/股数/成交额如何处理、参考日以及所纳入事件的可见截止。
原始价永不覆盖；前复权/后复权的派生结果应引用原价 snapshot 与因子 snapshot。

拟定查询形态（尚未实现）：按 symbols、明确有效时间范围、as_of、snapshot 查询事件；
派生价格必须另外传 adjustment_kind 与 basis_date。有效时间与可见时间独立过滤，修订选择有版本证据。
迟发公告、更正和除权除息当天盘中边界要独立验收，不能通过简单累乘就承诺聚宽 fq 等价。
