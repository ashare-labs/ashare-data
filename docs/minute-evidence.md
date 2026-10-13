# 分钟来源证据与历史日状态

`MinuteEvidenceView` 读取带独立 SHA256 的离线来源包，提供分钟原文、日状态、交易所日历、
行动查询覆盖和逐项准入报告。它组合现有 `HistoricalMarketView`，复用 Sina 解码器、
历史包哈希/回执/路径校验及限价算法，没有修改原有价格和状态接口。

当前扩展格式 `ad.minute-source-evidence.v1` 是**证据检查与重放接口**。
它保留未证实区间和时点，不能导出可执行的 `ad-minute-backtest.v1`。
本续批建立在历史独审修复提交之上，继承 N/A 限定和状态/公告/股票池依赖冲突拒绝。
所有返回都是独立副本；加载和查询不访问网络，也不执行包内脚本。
完整市场材料随外部交付包单独提供，不进入 wheel。原生空集合修复的 15 组最小真实
请求/响应回归样本进入源码与 sdist，完整采集回执和其他来源仍在外部证据包中。

冻结提交 `720ff0a4c9ed0c73870b76240a3692e8d484d838` 已通过独立有界代码验收，见
[本地整合记录](historical-minute-integration.json)。封存包内的候选版本与待审标记作为生成时状态保留；
代码验收没有改变 G03～G11 九项门禁。原三股两日真实分钟回测仍为 BLOCKED，等待合格数据。

免费来源续包通过 `scripts/supplement_minute_evidence.py` 合并到固定 r2 基础包，复用
`seal_minute_evidence` 校验封存。固定输入指纹防止默默换基；原状态、行动、消费者契约、
历史时点及三股两日范围保持相同，只扩展来源引用和门禁说明。新增 TDX 逐笔与行动记录、
CNINFO 公告、跨源开盘反例及 BaoStock 原生空集合的旧失败/新解析证据均可按引用检查。
`scripts/verify_minute_supplement.py` 从原始字节复核新包并检查继承边界。
成功解析空集合不会关闭 G10，原始 1,422 个目标标签也不会切换成更小的建议窗口。

## 读取与消费

```python
from ashare_data import MinuteEvidenceView

view = MinuteEvidenceView(
    "/path/to/package/binding.json",
    expected_binding_sha256="<由独立交付清单提供的 SHA256>",
)
state = view.state("600000.XSHG", "2026-10-09",
                   require_fields=["listed", "suspended", "is_st", "previous_close"])
rows = view.bars("600000.XSHG", "2026-10-09")
calendar = view.calendars()
coverage = view.label_coverage()
actions = view.corporate_actions(
    ["600000.XSHG", "000001.XSHE", "000002.XSHE"],
    start="2026-10-08", end="2026-10-12",
)
gates = view.admission_report()
```

调用者应先检查 `ready_for_execution`、字段的 `status`、`source_quality` 和缺失原因。
`view.export_backtest()` 返回 `DataError(code="MINUTE_GATES_BLOCKED")`，其 `details`
含完整准入报告。不能把本接口的证据行直接作为引擎输入。

`visibility="posthoc"` 明确采用事后证据；`received` 要求 `knowledge_at` 不早于包的实际
采集完成时间；`verified` 拒绝为 `PIT_UNAVAILABLE`。`historical_available_at` 与
`replay_effective_at` 均为 `None`，采集时间不代表历史首次可见时间。
绑定的 AQ r2 候选 `ac8a8d9…` 已提供 `posthoc_daily_replay` 编码，但默认关闭且待独审。
只有消费者明确选择模型后，才可在另一个字段生成所属日期 09:00 的模型时钟；
本来源接口不替用户启用模型，也不修改 null。状态模式不豁免分钟发布时点或限价等其他门禁。

## 字段与覆盖边界

| 查询/字段 | 含义与限制 |
| --- | --- |
| `state(...).listed` | 当日源证券名册的成员资格；不认证交易所法律上市状态或连续上市历史。 |
| `suspended` | 当日名册 `tradeStatus` 和日状态观测；不证明盘中从未临停。 |
| `is_st` | 当日日 K 源 `isST`。本批压缩帧外层完整性未知，保留 `source_claim_unverified`，不越过原状态源门禁。 |
| `previous_close` | 交易所日历前一交易日的 Sina 原始日收盘价；保留小数字符串。 |
| `source_preclose` | BaoStock 不复权源字段，仅作对照。数值相同不能证明它就是除权后的交易所限价参考价。 |
| `reference_price/upper_limit/lower_limit` | 交易所参考价及公司行动边界不足，本批保持 `None`。 |
| `limit_model_candidate` | 以原始前收为假设的普通主板 10% 候选，按分 HALF_UP/至少一分计算。校验器重新计算，且强制 `execution_usable=False`。 |
| `bars(...).source_row` | 原始 OHLC、volume、amount 和源标签文本，附响应哈希、观测 ID、源行索引及相对证据路径。 |
| `interval_start/interval_end` | 未验证时为 `None`。`require_intervals=True` 拒绝，不从时间标签猜测边界。 |
| `amount_unit` | `None`；既有提供者声明 CNY 单列为 `provider_declared_amount_unit`。相对量价一致性不认证绝对单位。 |
| `label_coverage()` | 只检查每日期望 237 个标签，不把标签齐全升级为区间覆盖。 |
| `corporate_actions(...).rows` | 已取得事件中，登记、除权、派现、到账、可交易任一已知日期落在查询窗口内的记录。 |
| `linked_events` | 有界来源包保留的关联事件，包括窗口前事件；不是完整公司行动库。 |
| `source_queries` | 逐证券/源报表的完整页数、源计数、日期列非空/空值计数和命中日期。日期统计窗口由 `coverage_start/end` 标明。 |

行动接口对缺失日期和未覆盖事件域明确返回 `complete=False`、`absence_proved=False`。
`rows=[]` 只能说明已知事件未命中；不能生成 `source_covered_no_actions`。
全市场股票池、其他板块、上市/退市生命周期及旧样本继续使用
[历史市场接口](historical-market.md) 的独立绑定包。本批包没有新增完整股票池覆盖。

## 固定真实批次的结论

请求 `aq-minute-real-3stocks-20261009-20261012-v1` 固定三股
`600000.XSHG / 000001.XSHE / 000002.XSHE` 和 `2026-10-09 / 2026-10-12`。
保留 1,428 条源行，其中 1,422 条命中请求标签、六条 15:00 尾部在执行窗口外。
没有补造 14:58/14:59，也没有改股票、日期、源原价或金额。

六条首行的 Sina 09:31 K 线量都等于同源分时 09:30 与 09:31 两个正量桶之和。
这是“首条只含连续竞价”假设的反例；其他条的秒级区间也仍未证实。
不能简单减量修复首行 OHLC/金额。另一免费来源的首行开盘字段为 `0.00`，
且区间语义未证实，未将其替换为可执行 OHLCV。

| 门禁 | 当前结果 |
| --- | --- |
| G00 来源身份 | 冻结请求、原始字节、回执、行定位和包哈希已取得。 |
| G01 日历 | 两所来源覆盖 10/8、9、12、13 及 10/10、11 周末闭市，核对前驱交易日。 |
| G02 会话 | 两所 2026 年有效规则支持请求窗口；已核对沪市暂缓实施附件。 |
| G03 区间 | 阻断：首行反例及其余边界未知。 |
| G04 单位 | 量的股单位有源依据；金额绝对单位未独立证实。保留量额对照差异，不改原值。 |
| G05 历史可见性 | 首次发布、修订/最终水位未知，需消费者明确模型。 |
| G06 日状态 | 六组已取得；ST 等压缩帧字段保留原完整性限制。 |
| G07 限价 | 六组实际前收取得；参考价及事件例外未证实，候选限价不准入。 |
| G08 状态重放 | AQ r2 提供默认关闭的显式编码；源时点为 null，仍需独审及模型选择，未授予数据准入。 |
| G09 临停 | 日状态及沪市指定查询范围无条目，仍不能认证盘中无临停。 |
| G10 行动 | 两类源报表完整 17 页/287 行，已知非空登记/除权/派现日不落窗口；到账、可交易日及其他行动域不足。 |
| G11 消费验收 | 尚不能运行真实分钟策略；证据验证通过不等于回测通过。 |

所有公开采集回执都晚于请求日期。交易规则的发布/生效日期单独保留，未被转换成
逐证券历史首次可见时点。原始量额对照存在残差，具体见绑定包
`intervals/amount-consistency-supplement.json`。

## 复建、校验和安装验证

`scripts/build_minute_evidence.py` 是该固定批次的离线组装配方，先验证请求文件哈希，
再组合既有公开 `Client.get_price`、`BaoStockSource.get_status`、日期名册与交易所/
发行人原文。原采集脚本和源回执在单独 acquisition audit 中。

```sh
PYTHONPATH=src python scripts/build_minute_evidence.py \
  --evidence-root /path/to/acquisition \
  --request-root /path/to/minute-real-request-v1 \
  --request-amendment /path/to/request-amendment.json \
  --staging /path/to/new-assembly --destination /path/to/new-package

PYTHONPATH=src python scripts/verify_minute_evidence.py \
  --binding /path/to/new-package/binding.json --sha256 <独立清单哈希>
```

公开 `seal_minute_evidence(source_root, destination)` 读取已整理的 `inventory.json`。
它复用历史包路径/大小/哈希/回执校验，验证语义后才创建新目的目录，拒绝覆盖。
SHA256 证明指定字节的完整性，不认证远端真实性或包内文字结论；独立根哈希必须由
可信交付渠道给定。该 v1 源配置即使字节完整，也始终保留 G03 阻断。

真实验证脚本禁用 socket，逐行对照原请求、重算数量/金额，并检查空值、范围和错误拒绝。
先运行 `pytest`，再构建 wheel/sdist；新虚拟环境安装锁定依赖和 wheel 后，用
`python -I scripts/verify_minute_evidence.py ...` 复验同一包。
