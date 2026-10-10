# 数据来源与单位

数据源由调用者显式选择。直接查询固定使用新浪；BaoStock 和 zzshare 有独立入口，不在失败时自动替换。缓存、原文和事实包默认保存在用户自己的本地目录，仓库不分发实际采集响应。

## 新浪

行情入口为 `https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData`，使用 symbol、scale、ma、datalen 参数。支持近期日线、1m、5m；有界窗口不能分页追溯任意历史。响应只按 JSON 解析，不执行远端脚本。证券名称使用 `hq.sinajs.cn` 的公开报价字段；交易日历来自[上交所年度休市安排](https://www.sse.com.cn/disclosure/dealinstruc/closed/)，查询受该页面当前年度范围限制。

单位映射依据新浪自身显示实现：[fixTotalShare.js](https://n.sinaimg.cn/finance/stock/hq/src/fixTotalShare.js?v=1.3.2)、[datas/k.js](https://finance.sina.com.cn/sinafinancesdk/js/datas/k.js)、[datas/hq.js](https://finance.sina.com.cn/sinafinancesdk/js/datas/hq.js)。报价原 volume 保留为股，不再乘100；分钟 amount 映射 money，单位为元。日 K 没有 amount，不能用价格乘成交量补造。

分钟量额合计可能不同于日累计，竞价标签和聚合规则也可能有差异。不能仅凭相加或平移时间标签证明整日覆盖、bar闭合或聚宽14:55口径等价。[量额检查](turnover-diagnostics.md)用于保留并定位差异，不自动修正源数据。

## BaoStock

使用[官方 PyPI baostock 0.9.4](https://pypi.org/project/baostock/0.9.4/)；固定wheel SHA256为 `0bf71c6069ab5890ff3596632f9c3f8f1fbc6bfcac582c2f9d6a5c11ab2cfaf8`。本库仅保存源码指纹，不捆绑SDK。调用是单连接、串行、有界匿名查询，参见[平台说明](https://www.baostock.com/mainContent?file=home.md)和[访问规则](https://www.baostock.com/blacklist)。

[官方行情字段说明](https://www.baostock.com/mainContent?file=stockKData.md)列出日线和5/15/30/60分钟，volume以股计、amount以元计；本库请求adjustflag=3。源preclose含义与前日实际close不同，见[源前收契约](preclose.md)。BaoStock 1m不在本库支持范围内。

原文、请求身份和接收过程可校验；部分压缩帧的外层校验算法仍未验证。该限制会保留在quality中，读取成功不会提升为PIT、最终性或交易所认证。完整调用范围见[BaoStock API](baostock-api.md)。

## zzshare

这是默认关闭的可选研究源，必须显式 `enable_research=True`。仅支持有界日线，保留原始十进制值、请求和响应证据；源涨跌停声明仍为 `source_claimed`，不冒充官方限价，详见[适配器契约](zzshare.md)。

## 使用边界

免费、免注册或可查询不等于允许公开复制、转售或无限请求。第三方软件与行情数据的许可独立适用；使用前应核对来源当前条件，本库不替用户取得额外权利。资料名称、当前上市状态或日历开市声明都不能回填成历史股票池或逐日可交易证明。
