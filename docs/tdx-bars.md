# TDX 原生分钟研究接口

`TDXBarSource` 使用用户已安装的 eltdx 3.0.3，通过明确指定的公共 TDX 节点读取
`0x052d` 原生 1m/5m OHLC、量、额。它不是 `minutes.history` 分时点，也不重建 OHLC。
没有自动安装、节点探测、换源、复权、5m 拆1m或补数。eltdx 现有许可证限个人非商业研究；
AD 不捆绑该 SDK，也不把软件许可解释为行情再分发授权。

```python
from ashare_data import TDXBarSource, TDXBarView

source = TDXBarSource(
    host="116.205.183.150:7709",  # 显式选择；示例不保证节点持续可用
    python_executable="/path/to/existing/eltdx-environment/bin/python",
)
view = source.fetch("000001.XSHE", directory="captures/pingan-new",
                    frequency="1m", count=3200, max_pages=4)
print(view.describe())
print(view.get_price(mode="retrospective").data)

# 把此次采集返回的 SHA 单独保存。离线重开不需要 SDK，不联网。
saved_sha = view.manifest_sha256
offline = TDXBarView("captures/pingan-new", expected_sha256=saved_sha)
selected = offline.get_price(mode="retrospective", labels=[
    "2024-03-04T09:58:00+08:00", "2024-03-04T09:59:00+08:00",
    "2024-03-04T10:00:00+08:00",
])
print(selected.report["missing_requested_labels"])
```

采集单证券、单节点、单连接，默认请求间隔至少1秒；每次最多32页、25,600条。
单页最多800条；`start_offset` 是协议偏移，范围0..65535，不是日期。
按实际返回行数推进，短页继续；重复/交叠、反向页、空页、授权/限流及其他错误立即停止，
不会无限翻页。`next_offset` 仅表示此次成功页之后的偏移；新的盘中请求可能发生窗口移动，
不能据此假定跨批次无缝续传。

`stop_reason` 分别记录 `requested_count_reached`、`local_page_limit`、
`protocol_offset_limit`、`remote_empty_page`、重复/反向页、超时和来源错误。
本地限额和远端空页均不证明全历史完成，`complete_history` 始终为 false。
若未满足 count，默认读取报 `TDX_PARTIAL_CAPTURE`；可显式 `allow_partial=True` 读取已接受页。
拒绝页原文仍保留，但不会进入返回数据。失败/空采集可用 `describe()` 查看回执。

`source.get_price(..., directory=..., mode="retrospective")` 合并显式采集与读取。
其 `start/end/labels` 只筛选采集到的源标签，不是远端历史日期查询；不会为日期过滤额外翻页。
`start/end` 为含时区、含端点的分钟标签，`labels` 为至多1000个精确标签。
返回 `ResearchResult`，价格使用原整数毫元转 Decimal；volume 使用协议解码原量（股），
保留 `volume_lots`，money 为源独立金额（元），不使用价格乘数量生成金额。
量额保留上游浮点编码精度，不保证交易所零碎股/金额精度。

每批次写到新目录，保留请求、逐页 `.bin` 协议 payload、SDK 原字段 JSON、采集/接收时钟、
SDK版本/源文件及native二进制哈希、分页日志和固定 manifest SHA256。这里的原文不是完整
TCP抓包；报文证券/偏移身份依赖请求上下文和 SDK，不是服务器独立回显。
读取重验所有哈希、原记录拼接、数量、价格字段及跨页时序；不是独立协议解码或市场真值认证。

研究读取与严格执行门禁分开。`mode="retrospective"` 必须显式填写；源标签不推成已认证的
bar_start/bar_end/available_at，不承诺历史首次可见、无修订、状态、价界、公司行动或成交能力。
原 `MinuteEvidenceView.export_backtest()` 及聚宽对齐要求保持原样。

冻结提交 `171359e2b96aae6ea2fc3d45334fbb7484385c34` 已完成独立有界验收，来源与复验见
[本地整合记录](tdx-native-integration.json)。本次实证仅覆盖指定节点的两股、各98个源日期，
不是全源或全日历认证；B27 标签仍缺失，完结 K 线、历史 PIT 与严格交易未认证。
原 G03～G11 九项分钟门禁和三股两日真实分钟回测的阻断状态保持不变。
