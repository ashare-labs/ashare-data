# 来源边界、软件许可与数据权利

## 来源边界

数据源由调用者显式选择。直接查询固定使用新浪；BaoStock、zzshare 和 [TDX 原生分钟](providers.md#tdx-分钟柱) 有独立入口，不在失败时自动替换。缓存、原文和事实包保存在用户自己的本地目录。公开源码、wheel 和源码包不分发实际采集响应或公告全文；公开测试使用自写合成输入。

有限日历试点依赖另行取得的固定行动证据，通过 `ASHARE_PILOT_EVIDENCE` 指定目录，并校验既定哈希；缺材料时明确阻断。公开代码仅保留必要的来源链接、清单摘要及自写说明，见[本地证据说明](observations.md)。

### 新浪

行情入口为 `https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData`，使用 symbol、scale、ma、datalen 参数。支持近期日线、1m、5m；有界窗口不能分页追溯任意历史。响应只按 JSON 解析，不执行远端脚本。证券名称使用 `hq.sinajs.cn` 的公开报价字段；交易日历来自[上交所年度休市安排](https://www.sse.com.cn/disclosure/dealinstruc/closed/)，查询受该页面当前年度范围限制。

单位映射依据新浪自身显示实现：[fixTotalShare.js](https://n.sinaimg.cn/finance/stock/hq/src/fixTotalShare.js?v=1.3.2)、[datas/k.js](https://finance.sina.com.cn/sinafinancesdk/js/datas/k.js)、[datas/hq.js](https://finance.sina.com.cn/sinafinancesdk/js/datas/hq.js)。报价原 volume 保留为股，不再乘100；分钟 amount 映射 money，单位为元。日 K 没有 amount，不能用价格乘成交量补造。

分钟量额合计可能不同于日累计，竞价标签和聚合规则也可能有差异。不能仅凭相加或平移时间标签证明整日覆盖、bar闭合或聚宽14:55口径等价。[量额检查](market-data.md#量额诊断)用于保留并定位差异，不自动修正源数据。

### BaoStock

使用[官方 PyPI baostock 0.9.4](https://pypi.org/project/baostock/0.9.4/)；固定wheel SHA256为 `0bf71c6069ab5890ff3596632f9c3f8f1fbc6bfcac582c2f9d6a5c11ab2cfaf8`。本库仅保存源码指纹，不捆绑SDK。调用是单连接、串行、有界匿名查询，参见[平台说明](https://www.baostock.com/mainContent?file=home.md)和[访问规则](https://www.baostock.com/blacklist)。

[官方行情字段说明](https://www.baostock.com/mainContent?file=stockKData.md)列出日线和5/15/30/60分钟，volume以股计、amount以元计；本库请求adjustflag=3。源preclose含义与前日实际close不同，见[源前收契约](reference-data.md#源前收)。BaoStock 1m不在本库支持范围内。

原文、请求身份和接收过程可校验；部分压缩帧的外层校验算法仍未验证。该限制会保留在quality中，读取成功不会提升为PIT、最终性或交易所认证。完整调用范围见[BaoStock API](providers.md#baostock)。

### zzshare

这是默认关闭的可选研究源，必须显式 `enable_research=True`。仅支持有界日线，保留原始十进制值、请求和响应证据；源涨跌停声明仍为 `source_claimed`，不冒充官方限价，详见[适配器契约](providers.md#zzshare)。

### 使用边界

免费、免注册或可查询不等于允许公开复制、转售或无限请求。第三方软件与行情数据的许可独立适用；使用前应核对来源当前条件，本库不替用户取得额外权利。资料名称、当前上市状态或日历开市声明都不能回填成历史股票池或逐日可交易证明。

## 许可与分发

当前 `0.10.0.dev4` 源码仍未指定自有代码的开源许可证。提供源码不授予额外的开源、商业使用或再分发许可；需要这些权限时，应先取得权利人的明确授权。此说明不改变 GitHub 平台条款下已有的权利，也不替第三方重新授权。

项目没有添加 MIT、Apache-2.0、BSD 等自有代码 LICENSE，也没有用第三方软件的许可替代自身许可。后续如选定许可证，应由权利人明确决定并另行公布。

运行依赖由 `uv.lock` 从官方 PyPI 安装，适用各自随包许可。本仓库不捆绑依赖二进制、BaoStock SDK 或 RQAlpha。BaoStock 的官方来源与 SDK 版本见[来源说明](sources-and-licensing.md#来源边界)；软件许可不等于其行情数据的再分发许可。

新浪、BaoStock、交易所网站及公告等材料分别受来源条款和第三方权利约束。免费、免注册或可查询，不表示允许无限查询、公开复制数据包、商业转售或向第三方提供服务。本地批量日线库、个人研究归档和 PORTABLE 真实材料不随软件分发。

明确标注 synthetic 的测试输入是人为构造的验证材料，不是供应商行情或真实交易证据。文档中的接口名称、来源链接、摘要和有限范围说明用于识别实现与审阅边界。

### 有限试点的固定来源材料

有限日历试点的公告目录、正文、发行人 PDF 和接收回执由使用者单独保留在本地。公开源码、Git 发布历史、wheel 和源码包均不包含这些原文，也不包含此前用于本地验收的真实分红、报价与日历响应。公开回归使用重新编写的合成材料，不能当作发行人公告或实际观察。

使用者提供已合法取得的本地材料后，代码仍核验原有固定哈希、来源身份和有限范围；缺少材料不会自动下载或退回合成输入。范围与位置配置见[首次试点说明](observations.md)。校验通过不提供额外的数据再分发授权，也不自动迁移冻结消费者绑定。

### 新浪历史数据解码器

本地日线适配器包含从 AKShare 提取的新浪历史压缩数据解码器。其 MIT 许可及归属说明保存在 `src/ashare_data/providers/sina-decoder-notice.txt`，与解码器一起随安装包分发。代码许可不等于新浪行情数据的分发许可。

### Alpha191 第三方公式材料

`src/ashare_data/factors/alpha191/_formulas.py` 中转换的公式材料源自 DolphinDBModules，
固定提交 `c285bf60208f5c261f6b242c7dbf24717ca71483`，适用 Apache-2.0。
完整原许可、归属、修改说明及逐项来源保存在同目录 `LICENSE.dolphindb`、`NOTICE`、
`sources.json`，随 wheel/sdist 分发。未包含 DolphinDB 运行程序或商业服务。
这些第三方材料的许可不改变项目其它自有源码的许可状态。
研究报告和真实行情仅供本地核对，不随包分发；详见 [Alpha191 来源](alpha191.md)。
