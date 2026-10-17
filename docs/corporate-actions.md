# 公司行动和通用复权边界

独立便携证据包现在可通过 [HistoricalMarketView](historical-market.md) 查询已取得的现金
分红、送股和转增事件；其覆盖、日期适用范围、单位和日内时点不足分别返回。它没有启用
下述旧 snapshot 存储入口，也没有实现复权因子或全市场行动覆盖。

当前 `snapshot.corporate_actions(...)` 和 `snapshot.adjustment_factors(...)` 都抛出
`DataError(code="UNSUPPORTED")`；`bars(adjustment=...)` 只接受 None。
未导入数据不能返回空列表来暗示“没有公司行动”，更不能伪造 factor=1。
当前可用的行动查询、历史股份、事件身份和导出约定见[公司行动历史](corporate-history.md)。通用 snapshot 的复权入口继续拒绝 UNSUPPORTED，不能把有限源事件和因子观察当作全市场自动复权能力。
