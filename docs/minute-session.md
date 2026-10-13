# 原生分钟会话研究契约

`TDXMinuteSessionView` 只读取固定 SHA 的既有 `TDXBarView` 捕获，不联网。
调用者必须显式选 `model="tdx-full-session-retrospective.v1"`。这是一份事后研究模型，
源 bar_start/bar_end/available_at/historical_available_at 保留 null，不提升原严格门禁。

```python
from ashare_data import TDXMinuteSessionView, TDXSessionSource

view = TDXMinuteSessionView(
    [TDXSessionSource("000001.XSHE", "/path/to/capture", "<manifest-sha256>")],
    execution_days=["2026-10-09", "2026-10-12"], history_days=["2026-10-08"],
    model="tdx-full-session-retrospective.v1",
)
document = view.export()       # owner 全量导出，不是策略历史快照
raw = view.export_bytes()      # 确定性 JSON；不受本地目录或导出时刻影响
opening = view.read("000001.XSHE", trade_date="2026-10-09", purpose="opening_price",
                    as_of="2026-10-09T09:30:00+08:00")
```

同一来源每个声明日须有09:31–11:30、13:01–15:00恰好240个目标标签；缺标签、错股、错频率、
无源日期或hash不符均拒绝，不补数。一次最多10股、31个选定日（63个自然日内）。
历史预热只能早于首执行日，单独存入 history_minutes，主 minute 数组仅含执行日。

导出schema为 `ad-tdx-minute-session.v1`，其中 `session_observations` 使用
`ad-minute-session-observations.v1`，与 AQ R5 的 SessionData 消费形状相同。
它包含 opening_prices、closing_marks、daily、history_minutes、history_sessions；
minute和closing_auction_inputs在外层。每条分钟保留源/manifest/payload/行hash、原行位置和实际接收时刻。

| read purpose | 模型可读时钟与限制 |
|---|---|
| opening_price | 09:30起，仅09:31原条open的单值开盘代理；没有high/low/close/量额，非已证明的09:30历史观测 |
| minute_history | 只返回源标签≤as_of的完整研究分钟；历史预热不成为执行事件 |
| valuation_only | 15:00起，只给真实15:00条close；不参与订单撮合，不能用14:56代替 |
| closed_daily | 15:00起，close取15:00原条，volume严格汇总240条（含零量）；附标签和原行hash清单 |
| closing_auction_matching | 显式研究角色，保留14:57边界及14:58–15:00记录，逐条到模型闭合时才可读；不是交易所真实撮合认证 |
| jq_daily_initial_capacity | 显式引擎事后政策，同日可提前取volume分母；不返回日close，strategy_visible=false、non_pit=true |

首条、连续候选、14:57边界、收盘竞价候选的phase均是研究分类，phase_verified=false。
零量/零额原条照留且有zero_volume标记，不能删除、改成正数或解释为已证明停牌。
正常日数据读取的replay_available_at不早于15:00；引擎事后容量另列model_access_at，不改该策略时钟。
角色是调用契约，不是进程安全沙箱；owner导出应交给消费层执行同样的时钟与用途隔离。

`examples/tdx_minute_session.py CONFIG OUTPUT` 提供离线导出。CONFIG指定sources列表
（security/directory/manifest_sha256）、execution_days、history_days、model；目录相对CONFIG解析。
已存在的输出拒绝覆盖。源码和wheel不含真实行情，状态/行动/日历仍由既有数据契约提供，原未知保持原样。
