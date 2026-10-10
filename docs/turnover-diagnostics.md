# 固定原文离线量额诊断（0.8.1.dev1）

本候选新增 `reconcile_sina_day` 和 `reconcile-files`，解决已封存旧日的量额证据只能由外置脚本重算、现有 `Client.reconcile_day` 依赖当前quote的限制。`Client.reconcile_day` 行为不变。0.8.0.dev2 / 0c984cde 已冻结，本候选不继承其独审准入或既有M2 pin。

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

```python
from pathlib import Path
from ashare_data import reconcile_sina_day
report = reconcile_sina_day(
    "600000.XSHG", "2026-10-08",
    minute_body=Path("sina-1m.raw").read_bytes(),
    five_minute_body=Path("sina-5m.raw").read_bytes(),
    quote_body=Path("sina-quote.raw").read_bytes(),
)
print(report["comparisons"]["1m"]["reference_minus_bars_volume"])
# 在本轮私有真实原件上为52300；不是补数指令。
```

## 本轮真实证据与可用性

2026-10-10对600000有界复查，正常网络仅4个公开请求：新浪1m/5m，东方财富1m/5m；均HTTP200且JSON业务数据非空。此前沙箱内4次尝试在DNS阶段失败，未拿到HTTP响应，另行保留。没有登录、付费、新密钥、修改网络配置或遭拒后更换端点。

- 新浪目标日1m 238行、5m 48行，与10月8日封存的选中行逐字段相同。日quote仍分别多52300股、507179.1011/507179.1008元。48组1m→5m量与OHLC相等；amount合计差0.0003元不能解释残差。
- 新东方财富1m目标日241行，amount合计1106982193元等于旧新浪日累计；241行open均为0。原始手数合计1150265，按其文档声明的手×100为115026500股，比日累计少350股，不能当逐股精确或完整OHLC替代。
- 新东方财富5m 48行，amount合计1107119933元。与既有BaoStock的47格amount完全相等，仅15:00格多137740元；这与旧新浪尾字段金额数值一致，但尚未证明其包含盘后交易的语义。手数也不能推定逐股精确。
- 09:35、11:10两处high在东方财富与既有TDX相符，均比BaoStock高0.01。多个来源同值不等于独立交易所真值；高价差仍未关闭。

公开方法和字段参考：[AKShare股票文档](https://akshare.akfamily.xyz/data/stock/stock.html)、[东财适配开源实现](https://github.com/akfamily/akshare/blob/main/akshare/stock_feature/stock_hist_em.py)。其文档已说明近期1m历史open可能为0；AKShare只是读取工具，不增加源底层独立性或数据权利。未安装或执行该库，未新增运行依赖。

当前可用：显式源限定的离线研究、原文追溯、数量/金额差分。当前不通过：52300股根因解释、完整成交量额、精确1m替代、14:55已闭合/PIT、目标2026-04-09至09-30完整1m历史。免费来源提供了新对照，尚无可安全修正新浪bar的交易明细证据。F2因子未实施。
