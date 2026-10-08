# 公开源、单位和覆盖证据

核验日期：2026-10-08，均为无需账号的低频小样读取。原始响应、完整执行日志和本机路径只保存于私有交付证据，不随源码分发。

## 固定来源

直接行情使用新浪移动端 `CN_MarketDataService.getKLineData`：

`https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData`

参数为 symbol（例如 sh600000）、scale（240/1/5）、ma=no、datalen。实测三个周期均 HTTP 200；纯 JSON 安全解析，不执行 JavaScript。响应标签和量额字段保留原值。

新浪另一个 `money.finance.sina.com.cn` 的 `CN_MarketData.getKLineData` 本机1分钟请求返回 HTTP 200/null，不能当作成功，也不是连接故障。这个旧入口未纳入运行时回退。腾讯日线也取得小样，但未纳入本版默认源，避免暗中混源。

证券名称来源为 `https://hq.sinajs.cn/list=sh600000`，只解析预期的字符串赋值，不执行脚本。日历来源为[上交所年度休市安排](https://www.sse.com.cn/disclosure/dealinstruc/closed/)；本次为2026年度，包含七组休市区间，加周末得到年度交易日。页面格式或范围改变时明确失败。

## 单位依据

从新浪自身[行情页面](https://finance.sina.com.cn/realstock/company/sh600000/nc.shtml)所引用的显示代码核对：

- [fixTotalShare.js](https://n.sinaimg.cn/finance/stock/hq/src/fixTotalShare.js?v=1.3.2)：volume 显示配置 shift=-2，标签为“手”；amount 标签为“元”。说明该股票报价原值是股和元。
- [datas/k.js](https://finance.sina.com.cn/sinafinancesdk/js/datas/k.js)：CN 分钟 K 数据统一乘0.01后进入图表手数；与原始股数映射一致。
- [datas/hq.js](https://finance.sina.com.cn/sinafinancesdk/js/datas/hq.js)：股票行情的总量按100换为手，金额沿用原始字段。
- 对同一股票、同一天，日 K 成交量与股票原始日行情成交量精确相等；分钟价格×原始股数与金额数量级一致。后者仅为交叉校验，单位依据来自上述源显示规则，不靠大小猜测。

因此 volume 保留为股，不再乘100；分钟 amount 映射 money，单位元。日 K 没有 amount，不由价格乘成交量补造。

## 已知缺口

同日48条5分钟量额之和与日行情不完全相等：成交量少52,300股，金额少507,179.1008元。该差异可能与集合竞价或源聚合有关，本次未证明原因。不能用这组分钟数据声称整日成交全覆盖。

1分钟最新标签为14:57、15:00，后者对应收盘竞价的边界仍需专门比对。不会生成14:58/14:59，也不会简单减一分钟当作已验证 bar_start。14:55与聚宽策略可见 bar 的等价性尚未验收。

有界近期接口不能保证2026-04-09至09-30的完整分钟历史；超出窗口的查询必须报错。交易日历不是个股停牌证明，当前名称不是历史成员资格证明。返回源数据不代表数据已经满足策略回测或 PIT 的完整验收要求。
