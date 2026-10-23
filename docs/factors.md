# 因子目录与研究计算

`ashare_data.factors.catalog` 是目录模块。JoinQuant 命名空间有 19 个明确字段，Alpha191 有 191 个公式编号。名称表示公式来源或研究定义，不保证供应商逐值相等。

```python
import ashare_data.factors.catalog as factor_catalog

print(factor_catalog.libraries())
print(factor_catalog.catalog("joinquant"))
```

需要直接导入函数时使用 `from ashare_data.factors.catalog import catalog, libraries`；包属性 `ashare_data.factors.catalog` 始终表示模块。

| 入口 | 字段/用途 |
| --- | --- |
| `ashare_data.factors.joinquant.compute_panel` | ROC120、Variance20、ATR6、ATR14、EMA5、VROC6、TVMA6、VOL5、circulating_market_cap、net_profit_to_total_operate_revenue_ttm、net_profit_growth_rate、operating_revenue_ttm、net_operate_cashflow_growth_rate |
| `FactorResearchView.from_file(...).query(...)` | BIAS5、CCI20、PSY、quick_ratio、debt_to_asset_ratio、net_operating_cash_flow_coverage |
| `FinancialLedger` / `financial_factor_candidate` | 明确报告版本、时点和字段的财务候选 |
| `net_profit_growth_candidate` / `share_metrics` | 明确滞后规则、股本口径、原始量价及数值转换模型 |
| `bounded_prediction` | TVMA6 原始元金额与显式精度/舍入假设 |
| `compare_reference` / `compare_composite_reference` / `summarize_composite_comparisons` | TVMA6 算术界限、来源区间、舍入后输出与汇总结论；位于 `ashare_data.factors.joinquant.amount_comparison`，并由 `ashare_data.factors.joinquant` 公开导出 |
| `ashare_data.factors.alpha191.Alpha191` | [Alpha191 输入、公式、缺失与排名约定](alpha191.md) |

计算必须显式提供 definition/mode，且所用交易日、证券、单位和证据来自调用者。价格、股数、人民币元、原始量、复权依据不可混用。短历史、停牌、未知状态、缺金额/股本或缺财报保持缺失或拒绝，不以零、删行或静默换源补齐。ATR6 与 ATR14 是独立目录条目。

`FactorResearchView` 用固定原件清单核对数据文件；输入只读。其发布日规则与 FinancialPIT 的严格时点认证不同，使用前检查返回的 historical_pit、jq_equivalent、source_files_verified 和缺失原因。计算成功不授予执行权限。

## 金额比较

先以显式原始元金额生成 `bounded_prediction`，再把参考值的规范 binary64 十六进制字符串传给比较函数。比较不读取历史报告，也不限制某个验收日期；不会修改输入预测或放宽原算术界限。

`compare_reference` 分别返回原始算术误差、来源金额区间与输出格点/区间诊断。`compare_composite_reference` 判断“来源区间经过舍入和 binary64 序列化”后的模型相容性，保留直接来源区间不相容的结果。`summarize_composite_comparisons` 分别汇总这些结论，拒绝空、重复目标或错误版本的比较记录；模型相容不表示来源精度已认证、JQ 等价或生产准入。

运行 `python examples/amount_comparison.py` 可离线查看合成边界例：参考值 `700000004.167` 不满足原始算术界限或直接来源区间，但满足舍入/序列化复合模型。该示例只使用程序内的六个整数金额。

## 输入与开发工具

采集、固定解码、计算、比较的分工、命令和依赖见 [开发指南](development.md#其他工具与生成链)。比较既检查共同证券的数值/排名，也检查两侧可选股票池的 top/bottom 变化；共同池相关性不能掩盖缺失候选造成的替换。最小用例见 `tests/test_factor_selection.py`。

## 单因子效果评价

安装 `ashare-data[factor-evaluation]`，或开发时执行
`uv sync --locked --no-config --python 3.12 --extra factor-evaluation`。
这是纯本地诊断，不取数、不生成因子、不下单；账户回测仍交给既有 A宽。
`ashare_data.factors.evaluation` 的普通导入不加载可选分析库；调用时检查固定版本。

```python
from ashare_data.factors.evaluation import EvaluationConfig, evaluate

config = EvaluationConfig(
    input_id="caller-frozen-input-sha256",
    universe_label="调用方已冻结的股票池及选股理由",
    universe_mode="fixed_sample",  # 或 point_in_time；调用方负责证明历史成员可见性
    price_basis="已冻结同口径复权收盘价及其来源身份",
    horizons=(1, 5, 10), quantiles=5, lag=1,
    direction=1, min_assets=5, cost_bps=(0, 5, 10, 20), rolling_window=20,
    # start_date/end_date 可显式限定信号窗口；价格仍保留后续标签缓冲。
)
report = evaluate(values, prices, membership=membership, config=config)
```

`values` 是一个因子的 DataFrame；`prices` 是同口径收盘价格；`membership` 是当日已知的布尔池成员。
三者必须有完全一致且已排序的证券列、真实交易日 DatetimeIndex（无时区的午夜日期标签）。
保留完整交易日网格和未来标签缓冲，不能把跨年不连续的多个样本窗口拼成连续交易日。
未计算/无效因子状态先转换成 NaN；证券代码、重复日期、乱序、未知池成员、无穷值、非正价格均拒绝。
不自动复权、重采样、填充或静默换源。`price_basis` 必须说明 raw 或实际复权口径；raw 收益会受公司行动影响，
不等同于持有期总回报。价格与因子口径一致由输入所有者负责，纯数值矩阵无法验证来源时点。

默认信号窗口为首末存在有限因子值的日期，保留中间的全缺失日期；全缺失时使用整个输入网格。
要区分预热、标签缓冲与全缺失的信号首尾，显式提供在网格上的 `start_date/end_date`。
当日收盘后已知因子，标签为 `P[t+lag+h]/P[t+lag]-1`，lag 至少 1，h 按提供的交易日计数。
默认是下一交易日收盘至之后 h 个交易日收盘；不是次日开盘，也不保证撮合或可交易性。
持有区间含首尾任一天缺价格，标签即缺失；尾部缺缓冲单列原因。未来交易状态不参与信号排序。
调用者须保证因子输入本身不含未来财报或未来成分信息，冻结文件的哈希不能认证历史 PIT。

| 指标 | 定义与缺失口径 |
| --- | --- |
| `ic` / `rank_ic` | 每日横截面的 Pearson / Spearman 相关；每个预测期独立使用观察到的因子-收益对。少于 `min_assets`、恒定因子或恒定收益返回 null，同时提供 n。 |
| `quantile_membership` | 仅按信号日有效因子和成员分组；先乘显式 direction，再用 Alphalens qcut。高值为 Qmax。不按证券代码强行拆 ties；无法生成全部组时披露失败日期。 |
| `quantile_returns` | 每日每组等权原始 h 期简单收益；`selected_n` 是信号时成员数，`n` 是有完整标签数。两者不等则收益 null，不将未来存活股重新等权。 |
| `spread` | 原始 Qmax−Q1。Alphalens 0.5.0 默认按 100% 总敞口除以二，本适配明确乘二恢复一单位多头和一单位空头的诊断差。不是融券策略、账户净值或可成交利润。 |
| `turnover` | Alphalens 的“本日新进入该组的证券数/本日组证券数”，比较相邻提供的交易日。首日或前一日分组缺失则 null，不跨缺口比较；不是权重换手或成交量。 |
| `stability` | 按信号日期的月度均值和 trailing `rolling_window` 个交易日均值。滚动窗口不足或任一指标缺值时该滚动均值 null。该统计仅在相应未来标签成熟后可知，不能当作当日实时指标。 |
| `summary` | 各期限的日均值、样本标准差、有效日期数、正值占比、未年化 mean/std。标准差不超过 `8*float64_eps*max(1,max_abs_value)` 时比值 null。没有假设重叠标签独立的 t 值、年化收益或夏普。 |

`cost_sensitivity` 使用独立等权持有期 cohort：每个多头组固定入场名义本金支付一进一出，
扣 `2*单边bps/10000`；诊断双边头尾差扣 `4*单边bps/10000`。
`top_mean` 和 `bottom_mean` 分别是两组各自做多的成本后均值，`spread_mean` 是多高组、空低组的成本后差，
不等于两组净均值相减。三者使用头尾都有完整标签的相同日期，返回 n。
假设费用按入场固定本金计、无净额抵消，无借券费和持有期权重漂移；这是敏感性情景，非税费/冲击模型。
不同期限每日 cohort 重叠，不将它们串乘成策略净值。

输出是 `ad.factor-evaluation.v1` 的 JSON 安全字典（未定义值为 null），包含 config、engine、summary、
coverage、上述指标数组、assumptions 和 warnings。`engine.input_sha256` 绑定三个完整数值矩阵与轴，
同时记录适配器和依赖版本；`input_id` 绑定调用者自己的来源身份。coverage 列出实际证券、每日成员/因子/分组数、
每期限观察对及缺失原因。固定样本明确提示幸存者/事后选股偏差；point_in_time 只是调用方声明，不自动认证。
未模拟 T+1、涨跌停、停牌、整手、流动性和融券可得性。

离线复放：`scripts/evaluate_factor.py --input INPUT --sha256 SHA256 --output NEW_REPORT`。
INPUT 的 schema 为 `ad.factor-evaluation-input.v1`，含 `dates`、`securities`、三个二维数组
`values/prices/membership`、config 及可选 provenance。源文件只读、SHA 不符拒绝、输出文件已存在拒绝覆盖。
输入/报告存仓外；不得把真实文件当单元测试写入目标。数学边界测试见 `tests/test_factor_evaluation.py`。

### 依赖选择与许可

选用 [Alphalens Reloaded 0.5.0](https://pypi.org/project/alphalens-reloaded/0.5.0/)，
[源码/许可](https://github.com/stefan-jansen/alphalens-reloaded) 为 Apache-2.0，作为外部可选依赖，未复制其源码。
直接依赖 numpy、pandas、pytz、tzdata、scipy、statsmodels、matplotlib、seaborn、IPython、empyrical-reloaded；
实际传递依赖固定在 uv.lock。上游 import 会加载其绘图模块，但本入口仅调用 utils/performance 的数值函数，
不生成 tear sheet、不联网、不要求 Zipline。利用其分位数、RankIC、组均值、头尾差、换手，Pearson 使用 scipy。
自行实现的部分限于严格输入/时点对齐、缺失覆盖、JSON 输出及显式成本公式。

[jqfactor_analyzer](https://github.com/JoinQuant/jqfactor_analyzer) 也支持给 FactorAnalyzer 直接提供本地
pandas 因子与 prices；便捷 analyze_factor 路径另接 DataApi。其 PyPI 1.1.0 额外依赖 jqdatasdk、pyarrow、
SQLAlchemy、fastcache、cached_property、six，未选择安装。仓库 LICENSE 标 MIT，setup.py 元数据标 Apache v2，
这里保留该实际差异，不推断新的授权。Alphalens 的本地面板接口和已有 pandas/numpy 边界更直接。


## Qlib Alpha158

`ashare_data.factors.qlib_alpha158.Alpha158` computes the **official default**
158 engineered features, not a count of effective strategies. Discovery uses
`ashare_data.factors.catalog.catalog("qlib_alpha158")`; identifiers such as
`qlib_alpha158:ROC5` are separate from GTJA `alpha191:alpha158`.
Qlib v0.9.7 is pinned to `da920b7f954f48ab1bb64117c976710de198373e`.
The packaged `features.json` records exact official order, expressions and
lookback/lookahead; `qlib-license.txt` retains the Microsoft MIT license.
The [official reference and comparator](../scripts/alpha158-reference.md) are
independent test tooling. Production does not depend on Qlib or a model framework.

```python
from ashare_data.factors.qlib_alpha158 import Alpha158
result = Alpha158(
    data, universe=membership, adjustment="raw",
    volume_basis="actual share volume",
    vwap_basis="explicit same-row CNY amount / share volume; zero volume is NaN",
    input_id="immutable-input-id",
).compute(["ROC5", "MA60", "VWAP0"])
values = result.between("2024-07-01", "2024-08-30").values
```

`data` maps open/high/low/close/volume/vwap (optional unused amount) to
numeric date × security DataFrames, exactly aligned to explicit Boolean
`universe`. Dates are unique increasing timezone-naive midnight session labels.
No resampling, collection, sorting, fill, price adjustment or VWAP inference occurs.
Absent fields give `MISSING_DEPENDENCY`; NaN observations remain part of the grid.
Price units are CNY/share and volume is shares; adjusted inputs require an
`adjusted:<immutable snapshot/policy>` identity and same-basis OHLC/VWAP.
Caller must describe actual `volume_basis` and `vwap_basis`. This implementation
does not promise equality with an unspecified vendor's adjusted price/volume
feed. Existing raw CMES amount/share-volume VWAP is a declared daily ratio, not
certification of Qlib vendor VWAP, tick VWAP or total-return adjustment.

Inputs are quantized to float32 like official local binary storage; intermediate
operator dtypes follow Qlib and final values are float32. Infinite inputs and
float32 input overflow are rejected; zero prices are permitted for explicit edge
probes. Outputs preserve NaN and signed infinity. `status_labels()` distinguishes
`QLIB_NAN`, `POSITIVE_INFINITY`, `NEGATIVE_INFINITY`, missing dependencies and
membership exclusions. `QLIB_NAN` may mean missing observation, insufficient
history or undefined arithmetic; it does not assert a single inferred cause.
Finite CNTP can be zero even when all close inputs are NaN, as official comparisons
return false. Do not replace these values using Alpha191's missingness policy.

Rolling windows are 5/10/20/30/60, include today and use `min_periods=1`.
`full_window_rows` means available context, **not** a compulsory warmup mask:
MA60 can be available on day one; ROC60 needs 61 rows. Std uses ddof=1; Rank is
the average tied percentile; IdxMax/Min uses the first one-based position and
upstream NaN behavior. Corr/Rsquare preserve Qlib's absolute 2e-5 degeneracy rule.
Only the expressions' original epsilon constants are used. No fitted processor,
label, training, cross-security rank or model is added. Membership masks output
only and does not erase calculation history. Optional `instrument_ranges` names
every security's inclusive lifetime; missing rows within a lifetime still count
as sessions. Compute over all needed history, then slice via `between`; metadata
records the history start because floating-point recurrences depend on it.

AQ reuses frozen source registration, bounded workers and the existing evaluator.
JSON/plots expose nonfinite values as null with their explicit status (and `inf`
text for signed infinities); evaluator input uses only finite OK values. Raw
feature exploration and adjusted evaluation-return prices remain separately
identified in provenance. Package tests compare every official synthetic row and
column, including IEEE masks, without selecting only a finite intersection.
