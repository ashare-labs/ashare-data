# 固定原文离线量额诊断

| 接口 | 契约 |
|---|---|
| `reconcile_sina_day(security, trade_date, *, minute_body, five_minute_body, quote_body)` | 三份原始bytes；无网络、文件写入、缓存或自动来源选择。显式证券/日期，返回可JSON编码报告 |
| `ashare-data reconcile-files SECURITY --date DATE --one-minute FILE --five-minute FILE --quote FILE` | 仅读三个本地原文文件；无需Store，拒绝超过2MiB的单份原文。输出同一报告，退出2表示市场验收仍未满足 |
| `report["diagnostic_id"]` | 报告内容hash；每份原文另有bytes/sha256，选中行逐行hash。原文格式变化也产生新身份 |
| `comparisons` | 原始十进制文本求和；1m/5m分别与同日日quote比较，精确保留股数和金额残差 |
| `label_grouping` | 仅按假设 `(5m源标签−5分钟, 源标签]` 逐格检查量额/OHLC。保留未归组标签、假设标签空档；`missing_trade_count=null` |

原文应为新浪既有K线JSON及quote字符串赋值。JSON必须是非空数组，每份最多1023行；严格递增且标签对齐整分钟，5m标签对齐5分钟。字段必须保留字符串，拒绝JSON浮点、指数/空白、非有限数值、缺amount、重复键/行、非法OHLC/股数、错误quote证券或日期。不把空响应或服务错误对象当有效行情。原文内其他日期保留在输入hash中，报告只选择所请求日期。

价格/金额沿用现有数值校验：有限非负且小于1e17，最多6位小数；OHLC为正且有序，股数为整数。不会扩大金额容差、扣尾字段、缩放成交量、用close×volume补amount或制造14:58/14:59记录。

`accepted=false` 始终保留。差额存在时 `status=failed`，合计相等时仍为 `unknown`：文件字节和算术一致不证明真实统计范围、完整性、PIT、最终性或执行资格。K线payload没有证券字段，证券/频率/来源由调用者声明；quote头部和日期只能做有限交叉检查，不能冒充来源认证。`available_at=null`，原文接收时刻应查外部采集回执。

## 数值错误

JSON中的非有限数值（包括额外字段中的`1e999`）返回`SOURCE_SCHEMA_ERROR`；CLI同码退出2。必要行情数值仍要求十进制文本。检查覆盖嵌套字段和未选中日期，有限额外字段保留原文及行hash。

```python
from pathlib import Path
from ashare_data import reconcile_sina_day

report = reconcile_sina_day(
    '600000.XSHG', '2026-09-30',
    minute_body=Path('one-minute.json').read_bytes(),
    five_minute_body=Path('five-minute.json').read_bytes(),
    quote_body=Path('quote.txt').read_bytes(),
)
print(report)
```

文件由用户合法取得，示例不附行情数据。日期必须与原文一致；报告只用于研究诊断，不是补数据指令。
