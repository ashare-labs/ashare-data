# 独立历史查询产品

`IndependentQueryView` 读取 AD 固定 binding 内的独立真实来源。它不联网，也不读取 JQ
答案。使用角色必须为 `independent_retrospective_query_research`；策略、撮合、初始化角色
均拒绝。每次读取重新校验 binding 和依赖证据 SHA256，路径限制在 binding 所在目录内。
包中没有私人行情；产品目录另行本地交付。

```python
from ashare_data import IndependentQueryView

view = IndependentQueryView("binding.json", expected_binding_sha256="交付的64位SHA256")
role = "independent_retrospective_query_research"
result = view.read_daily("B27-daily", "000001.XSHE", source="sina-pingan", role=role)
```

调用固定产品 ID，避免把局部覆盖扩大为任意日期承诺。证券和来源必须匹配 binding。
日线的 OHLC/volume/money 以十进制字符串返回；`avg` 是标明公式和 34 位十进制精度的
量额比，不是声称源原生 avg。正成交量只能推导“不是全日停牌”。没有证据的涨跌停、
pre_close、factor、open_interest、vwap 是 `unsupported`，与 `nan` 分开。

新浪 KLC 保留逐股量和源成交额解码。成交额含独立编码的残差/差分，不以收盘价乘量
代造金额；目标日期的源金额仅到整数元。东方财富金额保留小数分，量为整手乘100，
不能据此恢复逐股精度。两个来源必须分别选择，接口不拼接或静默合并。

`read_paused("B28-pause", security, skip_paused=..., fill_paused=..., role=role)`
要求独立公告停牌事实、原始行缺失和最近真实收盘价同时存在。skip 优先并返回空集；
fill=True 用最近真实收盘价填 OHLC、量额置零，全部标记 `synthetic_paused_fill` 和
`traded=False`；fill=False 的六个缺失行情字段标记 `nan`，停牌事实仍为 True。
这是声明的 AD 日线研究政策，不是造出可交易行情，也不认证全部 JQ 停牌语义。

`read_adjusted("B25-adjustment", security, basis_date=..., fq=None/"pre"/"post", role=role)`
采用新浪真实累计后复权因子：前复权价格乘 F(行情日)/F(基准日)，后复权乘
F(行情日)/F(固定归一日)。仅调整价格，量额保留原始值；无隐式价格舍入。产品保留截至
基准日的源因子序列与归一日，排除后续因子。该源累计因子不等于已独立重建每一项
公司行动，不宣称完整历史行动链或与 JQ 调整成交量、因子定义等价。

`read_opening("B12-opening", security, role=role)` 只读取 TDX 历史成交包中状态不为8的
09:25开盘成交记录，保留整手量、状态和原始字节。金额未独立观测，保持空值。
它不证明用户委托参与竞价或任何独立撮合行为。

`read_index_members("C04-members", "000300.XSHG", as_of_date=..., source=...,
role=role, allow_uncertified=True)` 读取固定的独立历史成分候选。区间来源按
opt-in 包含、opt-out 不包含选择；日期快照只接受其绑定日期。接口拒绝越界日期、
重复证券、缺失成员以及未明确接受候选质量的调用。成员数和归档一致不能认证完整
官方生效公告链，名称也不保证为历史名称。原始进出日期和源代码保留，只做代码格式
转换；不根据后来的合并事件自动替换历史证券。该接口仅供事后只读比较。

全部来源于当前真实采集时间，历史日期是行情有效日，不是过去到达时间。源发布时间
和修订号未知时保留为空；不得用于策略历史PIT或把全日量提前提供给策略。C03仅承诺
源返回行和窗内覆盖清单；缺日原因未知，不补行，不把首个返回日称作绝对最早日。


0.9.1.dev9 在已发布 v0.9.0 (`eb889877`) 的功能迁移链上继续开发；
`2b454671` 是历史起点，发布版目录与文档整理仍然保留。

`read_daily(..., standardize=True)` 需要产品显式绑定
`raw_identity_previous_session_close.v1` 与固定日历。未复权 factor=1 表示原始价格
恒等变换，不是完整公司行动累计因子；pre_close 明确是上一日历交易日的原始收盘，
不是除权日交易所参考价。上一日没有真实行时保留 unsupported，不跨缺口取更早价格。
默认 False 保持旧输出；money/volume 精度和派生 avg 不变。

`read_corporate_actions(product, security, source=..., role=role)` 分别读取巨潮分红
与配股账目，按除权日限定窗口，保留公告、登记、付款、上市日期角色及原始空值。
不把源空值解释为零，不合并相互冲突的早期事件，不生成累计复权因子。

`read_halt_events(product, security, role=role)` 仅提取绑定证券、固定日期的东方财富
全日停牌事件：原文必须明确同日9:30至15:00停牌一天。页面只用 JSON 解码，
不执行脚本。缺失、重复、半日停牌和错误证券均拒绝。事件源的 NOTICE_DATE 是
有效日标签，非可见时钟；提供商事件记录并不自动等于已核验交易所公告链。
独立覆盖报告可将这些事件与无成交日交叉核对，不会改写或填充原日线。
