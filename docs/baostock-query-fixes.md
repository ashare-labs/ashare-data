# BaoStock 截止筛选与证券资料校验（0.5.0.dev3）

本轮基于冻结 `71c6b4a1656c49908feea28c2c7b083c51b32d54`，仅修复独审 M-CUTOFF 与 B-META。既有 capture、分钟清单 v2、日线清单 v1 及原始行不重写；reader 校验政策变化由包版本和 `baostock-basic-validation-1` 标识。没有新增采集或依赖。

## 公开接口

| 接口 | 精确含义 |
|---|---|
| `view.get_price(end="2026-09-30T14:55:00+08:00")` | 仅分钟：上海源标签 **≤ end**；默认包含端点。原标签、毫秒、价格和逐行 hash 不变 |
| `view.get_price(end=..., end_inclusive=False)` | 严格 **< end**；参数必须是真正 bool |
| `view.at(as_of, visibility="source_label", inclusive=True)` | 固定分钟版本的研究标签上限；后续 get_price、lineage、coverage 都遵守上限。没有验证历史可见性 |
| `source.get_price(..., frequency="5m", end=..., end_inclusive=True)` | 起止 `start_date/end_date` 仍控制整日有界采集，end 只控制返回结果；非法 end 在 fetch 前拒绝。完整采集证据照常封存 |
| `view.at(as_of, visibility="received")` | 保持本机完整接收和存储可见性门禁；现有96分钟/日线因外层校验未知仍拒绝，不回退 source_label |
| `view.at(as_of, visibility="verified")` | 继续 `PIT_UNAVAILABLE` |

`end/as_of`（source_label 模式）接受带时区 Python datetime，或 `YYYY-MM-DDTHH:MM:SS[.ffffff]Z/±HH:MM`；转为 Asia/Shanghai 比较。不接受纯日期、无时区时间、只有时分、非法偏移或超过6位小数，不隐式丢弃精度。end_date 仍只接受 YYYY-MM-DD，不能用它传盘中时间。

各次上限取交集，不会因后续更晚 end/as_of 或不传 end 重新放宽。相同上限有任一排除端点即排除。窗口前的上限返回空表，窗口后的上限返回现存全部行，均不补数或推断没有市场数据。此上限允许历史/未来研究标签选择，不以“晚于当前时间”制造发布证据。无上限仍是完整固定版本研究读取。

筛选后的 quality/coverage 中 `selection` 单独记录模式、上海上限和端点；`closed_bar_verified/historical_pit_verified/actual_visibility_verified` 均为 false。`bar_start/bar_end/available_at` 仍未知。coverage 的分钟网格诊断只检查上限以内；原 query_complete 仍指原始采集回执，不表示筛选后覆盖已认证。筛选后的 lineage 只返回选中原行和 hash，不附含窗口外原行的完整 receipt；完整证据须另开未筛选 view 审计。

在已存600000 2026-09-30样本上，14:55包含端点时，5/15/30/60m应分别返回47/15/7/3行，末标签14:55/14:45/14:30/14:00。14:55.001晚于14:55，不得因舍掉毫秒被纳入。结束标签网格只是推断；这些结果不证明对应bar已闭合、源当时已发布或聚宽口径一致。

这不是恶意策略沙箱：持有原始Store和capture_id的调用者仍能显式另开完整版本。策略端须仅获得所批准的带上限接口；本轮没有开发A宽访问控制。

## 证券资料

原字符串必须保留；支持的reader枚举为 `type∈{"1","2","3"}`、`status∈{"0","1"}`。这是本版本支持集合，不声称源未来不会增加类型。其他值（含空状态、空类型、数值/布尔类型、999）拒绝，不转正常。已有股票样本不能证明其他类别真实覆盖；合法枚举组合以合成测试检查。

`ipoDate/outDate` 非空时必须为严格 YYYY-MM-DD 合法日期；两者均存在时退出日期不能早于上市日期。空日期保留空串并在 `missing_basic_dates` 明示未知，不填2999或当前日；status=0与空outDate仍是源退出标志加未知退出日期。日期不用于回填历史资格。合法日期和枚举也不等于可信历史证券主表。

非法资料仍封存原文并显示 `source_schema_invalid`；`get_security_info()` 抛 `SOURCE_SCHEMA_ERROR`，details带 capture_id、校验政策和状态。已封存旧非法资料重新读取也会被当前reader隔离；原 capture ID/hash 不改。源码新政策须与旧reader版本分别固定。

## CLI（离线）

```sh
ashare-data --store ./bao-store baostock-query price --capture CAPTURE_ID \
  --end '2026-09-30T14:55:00+08:00'
ashare-data --store ./bao-store baostock-query price --capture CAPTURE_ID \
  --as-of '2026-09-30T06:55:00Z' --visibility source_label
# 排除恰好14:55的标签
ashare-data --store ./bao-store baostock-query price --capture CAPTURE_ID \
  --end '2026-09-30T14:55:00+08:00' --end-exclusive
```

`--visibility`非默认值须同时给`--as-of`；`--end-exclusive`须同时给`--end`。两个时间参数同传时取交集。对日线、资料、日历附加分钟标签上限报 `UNSUPPORTED_FREQUENCY`；错误模式不静默忽略。示例 `examples/baostock_cutoff.py` 只重开已有store，不采集。

兼容边界：旧压缩合成夹具在71c6b4a已更早报非法帧，其原断言差异继续保留；本轮仅修正协议模块过时注释，不改字节解析。压缩外层校验、PIT、最终性、1m、完整历史、交易准入仍未解决；600000分钟额与日线差−0.38元保持。全套旧独审不是本轮新独立验收。
