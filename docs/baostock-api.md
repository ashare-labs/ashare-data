# BaoStock API

BaoStock 是显式选择的独立数据通道。默认 `get_price()` 仍使用新浪，两者不会自动回退或拼接。需要官方 `baostock==0.9.4` SDK；安装命令见 [README](../README.md#可选数据源与研究检查)。本库校验 SDK 的固定源码指纹，不捆绑 SDK，也不读取账号或密钥。

## 采集与固定读取

```python
from pathlib import Path
from ashare_data import BaoStockSource, Store

path = Path('.data/bao')
store = Store(path) if path.exists() else Store.init(path)
result = BaoStockSource().get_price(
    '600000.XSHG', store=store,
    start_date='2026-09-28', end_date='2026-09-30',
)
print(result.data)
capture_id = result.report['capture_id']
print(Store(path).baostock(capture_id).get_price().data)  # 离线重开
```

日期只作调用示例，不承诺该范围一定可得。采集为独立子进程、匿名单连接，每次一证券/一类业务，无自动重试或换端点。应关闭其他并发 BaoStock 采集任务；本机锁不能约束其他软件的独立连接。

| 接口 | 返回与范围 |
| --- | --- |
| `BaoStockSource(*, sdk_path=None, timeout=15)` | 默认在当前环境寻找 SDK，也可指定已有官方源码目录 |
| `source.get_price(security, store=..., start_date=..., end_date=..., frequency='daily')` | 显式采集后返回 `ResearchResult`；report 含 capture_id |
| `source.get_security_info(security, store=...)` | 证券资料来源声明，不作为历史资格证明 |
| `source.get_trade_days(store=..., start_date=..., end_date=...)` | 显式来源日历查询 |
| `source.fetch(store, kind=..., ...)` | 保存原始请求、响应和回执，返回 capture_id；kind 由对应业务文档定义 |
| `Store.import_baostock_capture(directory)` | 显式离线导入合法 capture，重复输入幂等；校验失败不升级为可读成功 |
| `Store.baostock(capture_id)` | 核验固定完整 SHA256 版本，返回 `BaoStockView`，不联网 |
| `view.get_price()` / `get_security_info()` / `get_trade_days()` | 离线读取对应类型；错误种类明确拒绝 |
| `view.descriptor()` / `lineage()` / `quality()` / `coverage()` | 来源、原始行、完整性及覆盖报告 |
| `store.baostock_snapshots()` / `recover_baostock()` | 本地目录与显式中断恢复，不重新采集 |

失败采集会保留失败版本和诊断，异常 details 中可取得版本 ID；失败记录不等于成功空数据。价格为 Decimal，volume 为整数股，amount 为人民币元。原字段、原价和源前收分别保留。

### 有界行动查询的原生空集合

底层 `capture_query` 的 `query_dividend_data`（type14）和 `query_adjust_factor`（type16）
允许已核实格式的原生空数据段：服务端成功码、完整且已核实的字段列表、长度、终止符、CRC、
请求身份及终止分页均须通过。5 个真实空集合报文和 10 个非空对照保存在
本地忽略的真实验证材料目录（不随软件分发），含原始请求/响应 SHA256、原回执指纹及原 SDK 行。

成功空查询沿用 `complete_empty_unknown`，仅表示该参数范围内查询完整返回零行。
缺失/异常字段仍判无效报文，错误码仍判 `provider_error_response`，接收不完整仍判传输失败。
`complete_history`、`confirmed_no_events`、`strict_admitted` 均保持 false，历史可见时点保持 null。
此修复不扩大公开 `BaoStockSource` 接口，也不改变压缩 type96 的外层完整性未知状态。
旧回执不原地重写；`scripts/verify_baostock_native_empty.py` 用已保存原文离线重放 pinned SDK，
另存新回执，并明确区分原采集时间、此次离线重放时间和未知的历史市场可见时间。

## 日线、分钟与截止时间

日线最多31个连续自然日。分钟显式指定 `5m`、`15m`、`30m`、`60m`，最多2个连续自然日；**不提供 BaoStock 1m**，不自动聚合或降级频率。

分钟原 `time` 必须是17位时间标签，毫秒保留，按 Asia/Shanghai 解释。`bar_start`、`bar_end`、`available_at` 未证时保持未知。价格、volume、amount 的原字符串及逐行摘要可追溯；源标签不自动变为已认证的结束标签。

```python
result = BaoStockSource().get_price(
    '600000.XSHG', store=store, start_date='2026-09-30', end_date='2026-09-30',
    frequency='5m', end='2026-09-30T14:55:00+08:00', end_inclusive=True,
)
```

`start_date/end_date` 决定整日采集范围，`end` 仅筛选返回标签，不缩减已封存原文。包含/排除端点、策略时钟和错误码详见 [分钟时间边界](baostock-time.md)。合法但偏离假设网格的标签保持原样，并列入覆盖诊断；缺记录不解释为无交易。

## 状态、源前收和日历

日状态使用 `get_status()`，源前收使用 `get_preclose()`，日历使用 `get_calendar()` / `get_calendar_links()`。它们返回保留原字段、时间和未知原因的类型化结果，见 [日状态](daily-status.md)、[preclose](preclose.md)、[日历](source-calendar.md)。

完整性和可见性按原回执保留。压缩响应的外层校验仍未验证，读取成功不提升为历史 PIT 或最终性；`verified` 明确拒绝，`received` 也须满足本机接收与存储可见性条件。分钟与日线金额可能不一致，本库不改写差异。服务能力与使用边界见 [数据来源](sources.md)。
