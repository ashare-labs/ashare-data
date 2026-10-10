# 限定引擎研究日历

`BoundedCalendarView` 读取摘要固定的本地 `ad.bounded-calendar-binding.v1`，
只支持一个已覆盖月份内的限定日期范围和指定深市证券，不联网、不解释账户或回调结果。

```python
from ashare_data import BoundedCalendarView

view = BoundedCalendarView(binding_path, expected_binding_sha256=binding_sha256)
days = view.read_carry("B21-qingming", "000001.XSHE",
                       role="engine_calendar_retrospective_research")
day = view.read_calendar("B21-qingming", "000001.XSHE", "2024-04-04",
                        role="engine_calendar_retrospective_research")
```

每次读取重验绑定与证据SHA。主源是深交所按月响应的完整逐日 `jybz` 标志；
所有自然日期必须连续齐全，重复、漏日、未知标志、字段污染、来源冲突均拒绝。
可绑定独立保存的新浪开市日期作为核对源；不会使用JQ查询数组、工作日推算或预期回调结果生成日历。

`calendar.daily_states` 含限定范围内全部自然日，`calendar.links` 和 `open_sessions` 只含开市日。
`read_calendar` 对开市和休市日均返回严格早于／晚于该日期的最近源开市日。
外端点可位于公开读取范围外，但必须由同一真实月份响应覆盖；缺失时拒绝，绝不回落到当前日期。

绑定须声明精确证券、窗口ID、月份、公开起止日期、`primary_source` 的相对路径／SHA／URL／采集起止时刻，
以及固定 `role=engine_calendar_retrospective_research`、`historical_pit=false`、`strategy_visible=false`。
证据文件限于绑定目录内；`kind=synthetic_fixture` 仅用于测试并在返回值中显式标出。

source_capture_time 为实际采集时刻，不是2024年历史发布时刻。接口只授权引擎事后日历研究读取，
策略、撮合和旧 lifecycle_research 角色均拒绝。角色约定不是进程隔离安全边界。
价格、公司行动、上市连续性和现金交收资格仍未知，不能由开市日推定。新接口不改变既有绑定。
