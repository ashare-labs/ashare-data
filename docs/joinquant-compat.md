# 与 jqdatasdk 的对应关系

目标是逐步扩展可迁移的本地行情接口。本版提供独立的 A数达原生 API，尚非 `jqdatasdk` 的直接替代品。

依据 JoinQuant 官方仓库固定版本
[`ba61143a6504bb1f797b16b8f9d23a21909c4f60/api.py`](https://github.com/JoinQuant/jqdatasdk/blob/ba61143a6504bb1f797b16b8f9d23a21909c4f60/jqdatasdk/api.py)。
客户端源码只能证明签名和客户端处理，不能证明服务端停牌、复权及分钟端点行为。没有登录聚宽或绕过线上文档的访问限制。

| 参数/行为 | 官方 SDK 基线 | A数达直接接口 |
|---|---|---|
| security | 单代码或列表 | 单股/最多10股，沪深 A 股代码子集 |
| start_date/count | 互斥 | 互斥；count 1..1000；两者缺省时20条 |
| end_date | 可缺省 | 当前上海时间；裸日期包含当天 |
| frequency | daily 默认、若干周期 | daily默认；1d、1m/minute、5m |
| fields | 默认价量额及可选其他字段 | 默认 OHLCV；money 仅分钟源有值时支持 |
| fq | 默认 pre | **默认 None；只支持原始价**。pre/post 显式报错，不偷换 |
| panel | 签名 True；现代 pandas 客户端切到 False | 默认/仅 False；单股时间索引，多股 time/code 长表 |
| skip_paused | 默认 False | 默认/仅 False，没有停牌标记证据 |
| fill_paused | 默认 True | **默认/仅 False**；不填任何缺行或价格 |
| round | 默认 True | **默认/仅 False**；保留源精度，原始字符串附在 attrs |
| 分钟语义 | 服务端选择及可见性需线上核验 | 源标签首尾包含；不声称14:55与聚宽一致 |
| get_trade_days | numpy 日期数组及更广历史范围 | Python date 列表，上交所当前发布年度 |
| get_security_info | 更全面证券元数据 | 当前名称/代码/交易所，上市日期等未知为 None |
| get_all_securities | 历史/全量集合 | 未支持，改用显式证券 get_security_info，不伪造历史池 |

`ashare_data.compat.JQStyle` 是原有的**高级离线快照适配器**，仍返回带 snapshot_id 的字典，不是上表中的直接接口。它的固定分钟边界来自用户导入的已验证契约，不应套到新浪源上。旧快照的字段隔离、完整分钟观测时间检查继续生效。

后续对齐需分别补齐可验证的前后复权、停牌、历史证券池、更多频率及字段，最后以同股同日的实际服务端返回验证时间/数值语义。不能仅因函数签名相似就宣称全面兼容。本轮不提供策略时钟、订单、账户、回测引擎、UI 或 HTTP 服务。
