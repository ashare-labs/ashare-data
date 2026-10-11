# 第二批独立参考补充

`PremarketReferenceView` 保留现金分红策略 `sina_native_prevclose_known_cash_research.v1`，新增明确选择的现金加转增策略 `sina_native_prevclose_known_cash_capitalization_research.v1`。两者都只投影固定源中独立编码的目标除权日 prevclose；不读取目标日 OHLC 生成参考价，也不从公式舍入生成价格。

新增策略要求提前公告、相邻登记与除权交易日、现金支付/新增股到账/可交易日期一致、无送股及正转增比例。理论 `(前日未复权收盘-每股毛现金)/(1+转增比例)` 仅作一源价格单位以内的冲突检查。错证券、单位、复权、日历、源摘要、日期、比例、缺失标记或非有限值拒绝，不兜底。

B45 外置研究证据使用发行人 2016-025《2015年年度权益分派实施公告》，刊于[证券时报 2016-06-08 B142](https://epaper.stcn.com/paper/zqsb/page/1/2016-06/08/B142/20160608B142_pdf.pdf)。现金发放、转增到账、无限售新增股份可上市交易均为 6/16；其前一交易日为 6/15。09:00 适用性是明确研究假设，2026 年取得的历史文件没有历史收件/修订证明，不能提升为 PIT。

`scripts/verify_sina_premarket.cjs DIRECTORY [EX_DATE PREVIOUS_DATE]` 可在离线环境重解码全部固定 KLC 字节，并审计指定日独立 prevclose 标记；默认仍是原 B26 日期。切换 binding 必须重新核验原始字节及解码结果。

缺少的三个分钟估值点通过既有公开 `Store.import_tdx_points` / `tdx_points.read(role="retrospective_point_research")` 保存和读取。源为免费 TDX 历史分钟点，两个节点一致只代表同供应商的重复验证。源响应没有回显证券/日期，日期跟随请求、时间标签来自协议序号。原始报文、解码版本与二进制摘要、请求、采集时钟、股/手单位均保持可追溯；不得把点构造为完整 OHLC 或供历史策略撮合。研究源、数据库和账号结果不加入公开分发包。

账户、订单与成交仍归 AQ 原生引擎；AD 不存参考账户答案，也不承担第二份资金账本。
