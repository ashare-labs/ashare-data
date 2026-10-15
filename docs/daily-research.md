# 固定日频研究快照

`DailyResearchView` 接受一个本地 `binding.json` 和外部固定的完整 SHA256。
打开时核验清单内每个原文、回执、解码结果和数据文件，随后只读内存中的固定版本。
返回对象均为独立副本；改文件或更新上游不会改变已打开的视图，重新打开会重新校验。
安装包仅含代码，不附第三方行情或私人证据。

```python
from ashare_data import DailyResearchView

view = DailyResearchView("/本地数据包/binding.json", expected_binding_sha256="交接的完整64位哈希")
print(view.descriptor())
print(view.quality())
engine_input = view.read_window("w2020", role="daily_strategy_retrospective_research")
history = view.history("w2020", "000001.XSHE",
                       as_of="2020-02-03T09:00:00+08:00", count=120)
opening = view.opening("w2020", "000001.XSHE", as_of="2020-02-03T09:30:00+08:00")
```

`examples/daily_research.py binding.json --sha256 <固定哈希>` 演示三个窗口的重开、
120 日预热、开盘价、收盘估值和默认未知状态。AQ 可以直接调用这些公开接口；
本增量不修改 AQ，不复制 AQ 策略或引擎。

| 入口 | 边界 |
|---|---|
| `descriptor()` | 单位、固定股票与窗口、假设、限制、生产者和版本 |
| `quality()` | 逐证券/窗口覆盖、停牌承接、原价/量额独立点核验、日历交叉核对 |
| `lineage()` | 原始文件哈希、URL、请求参数、实际抓取时段、复用来源及失败回执 |
| `get_trade_days(window_id, start_date=..., end_date=...)` | 固定实际源日期，包含预热和输出末日的后继交易日；越界拒绝 |
| `read_window(window_id, role=...)` | 引擎侧完整输入导出，`strategy_visible=False`，须与策略查询对象分开 |
| `history(window_id, security, as_of=..., count=..., fields=...)` | 只提供模拟时钟下已收盘的日线；缺行/预热不足拒绝，不静默跳过 |
| `opening(...)` / `closing(...)` | 当日研究开盘价最早09:30、收盘价最早15:00；分别只返回对应价格 |
| `state(..., accept_model=False)` | 默认盘前停牌和限价均未知；显式 opt-in 才返回 `status=modeled` 的研究模型 |
| `corporate_actions(..., as_of=...)` | 按公告日期的次日开始可见；保留支付日期与未知日内时刻，不推断提前到账 |

时间采用 Asia/Shanghai。上述09:00、09:30、15:00都是明确的事后研究时钟，
**不是历史接收、交易所发布或可执行成交时钟**。当前抓取时间只在来源回执中。
API 的 `historical_pit`、`execution_permission` 始终为 false。

原始 OHLC 单位为元/股、volume 为整数股、money 为元，均保留源精度。
只有独立停复牌证据、源行缺失和相邻真实收盘都存在时，才可标记
`documented_halt_carry`：OHLC承接前收、量额为显式无交易投影的零、`traded=False`。
它不成为真实行情或撮合价。未知缺日不允许这样处理。

`history(adjustment="pre", basis_date=...)` 用源累计因子 `F(d)/F(basis)`；
`post` 用 `F(d)/F(最早源归一日)`。基准日不能超过模拟时钟最后已完成交易日，
未来因子事件在计算前排除。只调整价格，股数和金额不变。它不等于 JQ 因子/量调整规则，
也不证明全公司行动链。策略 manifest 若选择 raw，应保持 `adjustment=None`。

盘前状态模型有单独名称及假设 ID。历史 ST/限价未知时不会默认填成普通股事实。
本次本地交付可以显式研究“普通主板、非ST、10%涨跌幅、分位四舍五入”的条件，
但它不是源限价。源独立 prevclose 标记优先于上一原始收盘；不从当日 OHLC 反推。
例：格力2022-04-08每股毛分红1元，发行人扣除回购股份后用于除息价的扣减为
0.936124元；源标记32.14元与原始前收33.08元分开保存。现金到账日不能自动升级为
开盘前现金可用，递延持有期红利税也仍需消费者明确建模。

生成工具 `scripts/build_daily_research.py <目录>` 只读取已采集原文：
固定策略 manifest、`raw/`、`derived/`、经原文核对的 `facts.json`。
`scripts/decode_research_daily.cjs <目录>` 只执行固定 SHA 的既有纯 KLC 解码器，
不执行行情响应。原文及解码器另行本地交付，不随 wheel 分发。
重新采集应使用新目录和新绑定，不能覆盖已经交付的版本。
生成器只允许字节完全一致的重建，拒绝覆盖不同内容；`node scripts/decode_research_daily.cjs <目录> --verify`
可从原始字节重新解码并核对已有结果，不写入数据。

公开合成边界测试：`python -m pytest tests/test_daily_research.py`。
真实离线验收可设置 `ASHARE_DAILY_RESEARCH_PACKAGE=/本地数据包`，不会访问网络。
真实数据的覆盖矩阵与原文哈希在包内 `reports/coverage.json`、`binding.json`，
按固定样本保留失败和限制，不以收益或完整性选择股票。

预热长度由研究包的 `warmup_sessions` 声明，离线构建脚本读取 `data_requirements.warmup_open_sessions_before_start`；不再强制每个策略准备 120 日。至少保留一个前交易日、输出窗口和一个后继日，日期及数量须与来源日历一致。查询时 `history(count=N)` 仍检查当时实际可见的 N 条，不足则明确拒绝。120 日仍是上方示例的模型需求。
