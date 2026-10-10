# 固定通达信历史分时点

本地开发接口接收用户已保存的、明确日期的 `eltdx.minutes.history(..., include_raw=True)` JSON 导出。AD 不连接通达信、不安装或捆绑 eltdx，也不随软件分发行情。只支持离线研究，源的访问和使用条件仍适用。

```python
from ashare_data import Store, TDXPointInput

# 参数来自自己的固定采集回执，不以历史交易日冒充实际抓取时间。
source = TDXPointInput(**source_receipt)
store = Store.init("my-point-store")
dataset_id = store.import_tdx_points(source, source_kind="local_tdx_history_capture")
view = Store("my-point-store").tdx_points(dataset_id)
result = view.read(
    source.security, at=f"{source.session_date}T13:01:00+08:00",
    role="retrospective_point_research",
)
```

`TDXPointInput` 必须提供证券代码、交易日、源文件绝对路径和 SHA256、节点地址、实际采集开始/结束时间、解码器版本、源码提交和二进制 SHA256。输入仅支持一个交易日、最多 240 点、1 MiB；公开方法不接受任意额外字段。测试夹具需显式标记 `synthetic_fixture`。

导入核对源 JSON 哈希、原始响应哈希、报文条数、每个原始记录的三项编码整数、价格差分累计值和序号标签。源数据只能含行情字段，不能加入成交答案。读取每次验证固定对象哈希，返回独立字典；不修改来源，且不会访问网络。

`view.describe()` 给出来源、单位、覆盖和缺项。`view.read(security, at=..., role=...)` 只查完全一致的标签；无记录返回 `NO_EXACT_SOURCE_POINT`，不使用最近值或日线收盘价。只有 `retrospective_point_research` 角色可读。`at` 是请求的源标签，不是历史可见时钟。

| 字段 | 含义和限制 |
| --- | --- |
| `price` | 保留报文价格，元/股；AD 不复权。不能仅由此证明供应商复权保证或 JQ 分钟收盘语义 |
| `volume_lots` | 源分钟成交量，整数手；源精度为一手 |
| `volume_shares` | `volume_lots * 100` 的单位转换，不能恢复不足一手的源精度 |
| `source_label_at` | 请求日期加协议序号：0→09:31、119→11:30、120→13:01、239→15:00，北京时间 |
| `observed_at` | 实际采集完成时间；历史发布时间未知 |
| `executable` | 零成交量为 false；其余为 null，不认证可交易 |

响应报文本身不回显证券、日期或逐点时刻；这些来自请求与解码规则。240 点完整网格也不等于完整分钟 OHLCV：开高低、明确闭合分钟收盘语义、成交额、买卖盘口、停牌、ST、涨跌停和历史到达时间均未知。13:00、09:30 和午休没有源点，不补造。

该产品始终 `historical_pit=false`、`strategy_visible=false`。消费者若采用事后点价进行条件研究，必须自行明确模型假设；AD 不提供撮合实现或执行准入，也不会将新源并入既有 JQ 数据身份。

参考：[eltdx 项目与许可](https://github.com/electkismet/eltdx)、[pytdx 历史分时接口](https://pytdx-docs.readthedocs.io/zh-cn/latest/pytdx_hq/)。源文件、节点交叉核验和实际采集回执由数据所有者另行绑定。
