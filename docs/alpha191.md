# Alpha191 输入、计算与显式定义

## 输入与计算

`ashare_data.factors.alpha191` 在本地 NumPy/pandas 上计算 GTJA Alpha191，不联网、不调用聚宽或 DolphinDB。默认公式版本为 `gtja191.public.v1`，显式版本 `gtja191.explicit.v1` 覆盖全部 191 条计算路径，选择与来源见[定义说明](alpha191.md#显式定义)。确定定义、输入和数值约定不等于供应商逐值相同或历史 PIT 认证。

### 两库统一目录

```python
from ashare_data.factors.catalog import libraries, catalog

print(libraries()[["namespace", "label", "entry_count", "api_field_count"]])
print(catalog("joinquant")[["qualified_name", "api_fields", "definition", "entrypoint"]])
print(catalog("alpha191")[["qualified_name", "availability", "dependencies", "note"]])
# catalog() 同时列出两库，以 (library, factor) 为唯一键。
```

| library / namespace | 系统名称 | 当前登记 |
| --- | --- | --- |
| `joinquant` | 聚宽主因子库（部分实现） | 19 个具名字段：13 个面板字段和 6 个研究字段，ATR6/ATR14 分列 |
| `alpha191` | Alpha191 | 全 191 编号；默认 public.v1 为 183 定义 + 8 阻断；显式 explicit.v1 为 191 条确定路径，依赖和解释见版本说明 |

目录的 `api_fields`、`definition`、`entrypoint` 指向对应计算契约，不在不同输入结构间隐式转换。`alpha191:alpha012` 对应 `Alpha191.compute([12])`；Alpha191 数值列名和公式编号保留。JoinQuant 字段及开发工具见[因子指南](factors.md)。

### 先运行

使用项目现有依赖环境，离线运行合成示例：

```sh
PYTHONPATH=src .venv/bin/python examples/alpha191_offline.py
```

合成示例演示接口，不是行情证据。真实使用时将合法取得的日线整理为以下形式，每个字段都是同一日期 × 证券矩阵：

```python
from ashare_data.factors.alpha191 import Alpha191, coverage

# frames = {"open": ..., "high": ..., "low": ..., "close": ...,
#           "volume": ..., "amount": ..., "vwap": ...}
# pool: 同样索引/列的 bool DataFrame；必须明确每一天的排名成员。
result = Alpha191(
    frames, universe=pool, adjustment="raw", input_id="my-frozen-dataset-id"
).compute([12, 35, 39, 81, 191])
print(result.values.tail())       # (date, security) 索引，alpha012 等列
print(result.status_labels().tail())
print(result.report)              # 每项依赖、可用格数、缺失原因统计
print(result.metadata["input_sha256"])
print(coverage())                 # 所有 191 项登记及定义
```

`compute()` 不传编号时返回全部 191 列；也接受 `"alpha012"`。不能计算的格子为 NaN，原因在 `status` 中；不会吞异常、用 0 补齐或用另一因子替代。`between(start, end)` 只截取输出，计算仍使用完整输入预热历史。原始输入会复制固定，调用者之后修改自己的 DataFrame 不影响既有对象。

### 实际覆盖

完整的[191 项覆盖表](alpha191.md#覆盖表)由 `coverage()` 生成，包含字段依赖、公式表达式、保守窗口长度、递推/事件历史需求、横截面排名标记及逐项说明。登记数量不能当成任意数据集的非缺失输出数量。

| 登记状态 | 数量 | 条件 |
| --- | ---: | --- |
| IMPLEMENTED | 178 | 根据覆盖表提供所需行情字段 |
| REQUIRES_BENCHMARK | 3 | 075、149、182，需要基准序列 |
| REQUIRES_EXTERNAL_FACTORS | 1 | 030，需要同日 MKT/SMB/HML |
| REQUIRES_EXPLICIT_SEED | 1 | 143，需要显式初值 |
| AMBIGUOUS_FORMULA | 8 | 036、054、127、146、165、166、181、183，保留编号并阻断计算 |

上表描述默认 public.v1，因此该版本有 **183 个已实现计算定义、8 个原文歧义状态**，不是“191 个无条件可用数值”。原文中的阻断原因：036 的 CORR/RANK 括号与参数位置无法唯一解释；054 的 STD、127 的 MEAN、181 的末尾 SUM 缺窗口；146 末尾 SMA 缺 m；165/183 的 SUMAC 和极值缺范围且括号不明确；166 分母缺运算符。这些阻断在默认版本中保留。完整计算可明确选择 [explicit.v1](alpha191.md#显式定义)，采用逐项记录的补全解释，并显式提供 146 的 m 参数；原文歧义本身没有被消除。

028 保留原文第二个分母的 `TSMAX(LOW,9)`；159 保留 `CLOSE-SUM(...)` 和重复的 `6*24` 权重；190 保留原文分子/分母不同的平方项。它们的数值可能不符合常见指标直觉，不自动改写成 KDJ、常见动量或标准偏度。v1 中的其它排版处理和上游修正见覆盖表 `note`。

### 输入契约

- 日期必须是升序、唯一、无时区的日标签 `DatetimeIndex`。必须提供研究窗口的**完整交易日网格**，缺行情的交易日放 NaN，不删除该行。接口无法从调用者删掉的日期推断缺口；不能把两个不连续窗口拼成连续交易日。证券列与所有输入字段顺序必须完全相同，乱序、重复、无穷值、负量额、非正价格或明显越界 OHLC 会报错。
- `universe` 必须明确每格 True/False，不允许未知。这个池既控制当天 RANK，也控制最终输出；提供固定股票列表只是研究池，不证明它是历史可投资池。入池前的已提供价格可用于非排名时间序列预热；历史排名只使用当日池。
- 价格单位为 **元/股**，成交量为**股**，成交额为**元**。只接受规范单位；手、万元等应由调用者显式换算。0 量/额可以输入；被拿来作分母或对数时为未定义，不替换极小数。价格不是自动复权的。
- `adjustment="raw"` 表示原价。复权数据使用 `adjustment="adjusted:<固定策略或快照ID>"`，调用者须让 OHLC 和 VWAP 处于同一复权尺度，量额仍是真实股数/元。只写 `qfq` 会被拒绝。固定的前复权快照仍可能包含事后公司行动，不能据此声称 PIT。
- **VWAP 必须是真实量额所定义的均价或可靠来源的均价**。API 不用 OHLC 均值、收盘价或另一个供应商的数据自动补造 VWAP；没有 VWAP 时相关因子报告缺依赖。原价下可由同源 `amount / volume` 显式计算；复权时应按同一价格因子变换 VWAP。070/095/132/144 直接用真实 amount，不反推成交额。
- `benchmark` 是与日期精确对齐、列为 `open/close` 的非空子集的 DataFrame；按变量名解释为基准开/收盘，不能使用股票池平均价代替。`factors` 同理，列为 `mkt/smb/hml`，收益单位为小数。没有任何自动横向拼接或向前/向后填充。

### 算子定义固定在 v1

| 算子 | v1 行为 |
| --- | --- |
| RANK (`R`) | 当日横截面升序、相同值取平均名次、除以当日有效成员数，范围 `(0,1]` |
| TSRANK | 同一证券最近 n 个交易日中末值的升序平均名次/n；与 RANK 不同 |
| 缺失排名 | 默认 `rank_policy="strict"`：池内任一操作数缺失则该次排名不完整；显式 `"available"` 才允许在有效样本中排名，结果哈希记录该选择 |
| 滚动算子 | 右闭、包含当天、必须满 n 个观察；窗口中缺值传播；不以未来记录补齐 |
| DELAY / DELTA | n 个网格交易日滞后 / 与该滞后值之差 |
| STD / COV | 样本标准差/协方差，`ddof=1`；CORR 是 Pearson，零方差未定义；中心化及有理数排名路径见[数值修复](alpha191.md#显式定义) |
| SMA(X,n,m) | 第一有效值作种子；`Y=m/n*X+(1-m/n)*前Y`，不使用 adjust=True；输入缺口输出 NaN 并终止该连续段，下一有效值重新播种，无隐藏跨缺口衰减 |
| WMA | 当前权重为 1，滞后 i 期为 `0.9**i`，在 n 期内归一化；不同于 DolphinDB 源码的线性权重 |
| DECAYLINEAR | 从最旧到最新为 `1..n`，归一化；不等同于 WMA |
| MIN/MAX | 两个序列表达式取逐格极值；公开公式中 `MIN/MAX(序列,窗口整数)` 的惯用窗口形式在登记中显式写为 TSMIN/TSMAX |
| HIGHDAY / LOWDAY | 极值距离当天的交易日数，当天为 0；并列取最近一次；LOWDAY 取最小值 |
| 条件表达式 | 未知比较仍是未知；只有被选分支的无效值影响结果，不把 NaN 比较当成 False |

严格池模式可能让嵌套排名因子长时间无值，例如一个成员的相关系数窗口为常数。请读取原因再决定是否显式选择可用样本模式；这会改变因子定义的实际横截面，不能静默切换。

SMA 种子、`ddof`、排名归一化/并列、MIN/MAX 的窗口惯例并非都由原研报充分指定；上述是**本库固定的数值约定**，不代表已证明 GTJA 或 JQ 内部采用相同实现。`full_window_rows` 是所有有限窗口分支的保守预热长度；条件分支可能更早有效。含 SMA、SELF 或筛选事件的定义还需要持续历史，不能仅保留该行数。

### Alpha30、143、149

030 在每只证券最近 60 个交易日，用 `RET=CLOSE/DELAY(CLOSE,1)-1` 对同日 MKT/SMB/HML 加截距做 OLS，取窗口最后一条残差；再对残差平方作 20 期几何 WMA。缺任一回归观察不丢行，设计矩阵秩不足返回未定义。首个完整结果需要 80 个收盘观察。外部三因子的组合口径、形成时间和 PIT 由输入者固定；本库不从最新市值或最新财报构造 SMB/HML。

143 必须传 `alpha143_seed=1.0` 或覆盖全部证券的初值字典。**第一输入日**输出初值，之后仅上涨日令 `SELF=涨幅*前SELF`，其它日保持；不是乘 `1+涨幅`。初值可以明确为 0，但不会默认 0。中途缺失将此证券后续状态置为未知，不擅自重启累计状态。

149 按 FILTER 的顺序先选基准下跌日，再取最近 **252 个下跌观察**，用带截距 OLS 算证券收益对基准收益的斜率。因此历史常长于 252 个交易日。未选中的已知日期不占观察数；筛选条件未知保留占位，选中收益缺失不删除，直至移出窗口。这一事件窗口选择在 v1 固定，不承诺等于使用 252 天自然窗口的其它实现。

### 分块和状态

```python
from ashare_data.factors.alpha191 import Alpha191Stream
stream = Alpha191Stream(ids=[12, 81, 143], adjustment="raw", alpha143_seed=1)
a = stream.append(first_frames, universe=first_pool)
b = stream.append(next_frames, universe=next_pool)
```

证券轴/字段集合必须保持一致，日期只能严格追加。`benchmark/factors` 如使用也逐块传入。流式接口保留已有完整历史，重算前缀后只返回新块，保证 SMA、143、149 和排名边界一致；这是有界研究实现，不是恒定内存/恒定耗时的实时系统。大量批量计算优先一次 `compute()` 并按输出日期切片。不能沿证券轴独立分块后拼接 RANK 结果。

`status` 使用紧凑 uint8 编码；`status_labels()` 转为文字：0 OK、1 WARMUP、2 MISSING_INPUT、3 UNDEFINED_MATH、4 OUT_OF_UNIVERSE、5 INCOMPLETE_RANK_POOL、6 AMBIGUOUS_FORMULA、7 MISSING_DEPENDENCY。同格多个原因时按传播规则保留一个阻断原因；它不是全部根因列表。`report` 和 `metadata["missing_dependencies"]` 汇总依赖缺失。

### 来源和许可

复用路线是**许可清晰的 DolphinDB 公式源码静态转换 + 研报逐项纠偏 + 自有 Python 算子**。源码固定于 [DolphinDBModules c285bf6](https://github.com/dolphindb/DolphinDBModules/blob/c285bf60208f5c261f6b242c7dbf24717ca71483/gtja191Alpha/1.0.0/src/gtja191Alpha.dos)，其 Apache-2.0 原文、归属和逐项公式映射分别随 wheel 保存在 `alpha191/LICENSE.dolphindb`、`NOTICE`、`sources.json`。未复制 GPL 或无许可证候选代码，未执行上游 DolphinDB 代码。

公式依据为 GTJA 2017《基于短周期价量特征的多因子选股体系》表 6（11–17 页）、算子附录（30–31 页）；本地阅读固定 [dbac760 镜像中的研报](https://github.com/wpwpwpwpwpwpwpwpwp/Alpha-101-GTJA-191/blob/dbac7600fae2c496aabc69a9007cc9c7db581985/国泰君安－基于短周期价量特征的多因子选股体系.pdf)，哈希见映射文件。未找到 GTJA 官方许可计算代码，不能称“官方开源实现”。研报、实际行情不随包分发。第三方适用许可不改变 ashare-data 自有源码的[许可状态](sources-and-licensing.md#许可与分发)。

### 验证与本地真实复放

公开测试覆盖全部编号/依赖、共同算子手算、20 个代表公式的独立标量计算（含 012/035/039 符号）、OLS/递推/筛选窗口、异常输入、缺失与池变更、证券重排、全历史与分块逐格一致、前缀因果性及未来数据扰动。真实多股材料及对应原题测试保留本地；公开测试只使用合成输入。

```sh
PYTHONPATH=src .venv/bin/python -m pytest -q tests/factors/test_alpha191.py
# 合法固定真实面板：必须提供你已核对的 SHA256
PYTHONPATH=src .venv/bin/python examples/alpha191_offline.py --panel panel.json --sha256 <hash>
```

示例读入的 JSON 格式为 `schema: "alpha191.panel.v1"`，含 `sessions`（ISO 日期数组）、`securities`、`universe`（bool 矩阵）、`fields`（各字段数值矩阵，缺值写 null）、`units`（price/volume/amount）、`adjustment`。可附 `benchmark`、`factors`（列名到数组）、`version`、`alpha143_seed`、`alpha146_m`、`rank_policy` 和 `provenance`；只读取，不联网。生产调用可直接传 DataFrame，无需此文件格式。

## 显式定义

`gtja191.explicit.v1` 为全部 191 个编号提供确定的计算路径。选择它表示采用本文的研究定义；**原研报的 8 处缺参/歧义仍然存在**，没有声称还原了 GTJA 或聚宽内部实现。默认的 `gtja191.public.v1` 继续保留这 8 项阻断，方便按原文保守复核。

```python
from ashare_data.factors.alpha191 import Alpha191, EXPLICIT_VERSION, coverage
from ashare_data.factors.catalog import catalog

result = Alpha191(
    frames, universe=pool, adjustment="raw",
    version=EXPLICIT_VERSION,
    benchmark=benchmark,      # 同日基准；149/181只需close，075/182另需open
    factors=factors,          # 同日mkt/smb/hml；不自动构造，不保证历史PIT
    alpha143_seed=1,
    alpha146_m=1,             # 调用者明确选择；不是库或研报默认值
).compute()
print(coverage(version=EXPLICIT_VERSION))
print(catalog("alpha191", alpha191_version=EXPLICIT_VERSION))
```

完整路径计数为：184 项行情定义、4 项基准依赖（075/149/181/182）、030 外部三因子依赖、143 显式初值、146 显式参数。缺输入的因子返回 `MISSING_DEPENDENCY`；预热、缺行情、零分母、零方差和排名池不完整仍保留原原因，不能用登记数量代替有效输出数量。`source_ambiguity` 对这 8 项始终为 True，`version` 和逐项 `note` 表明选用了哪个解释。

### 8 项逐项定义与依据

源码依据仍是固定的 [DolphinDB Apache-2.0 源码 c285bf6](https://github.com/dolphindb/DolphinDBModules/blob/c285bf60208f5c261f6b242c7dbf24717ca71483/gtja191Alpha/1.0.0/src/gtja191Alpha.dos)。仅静态阅读/转换，没有运行 DolphinDB，也没有复用其它无许可代码。补全选择和修改原因随包写入 `sources.json` 的 `explicit_definition`。其它 183 项公式文本、价格/量额/PIT/排名政策/平滑种子约定沿用 public.v1。

| 编号 | 显式定义 | 依据和限制 |
| --- | --- | --- |
| 036 | `R(SUM(CORR(R(V),R(VWAP),6),2))` | 固定源码将 6 放入 CORR、2 放入 SUM；研报括号无法直接执行。 |
| 054 | `-R(STD(ABS(C-O),10)+C-O+CORR(C,O,10))` | 固定源码补 STD 窗口 10。 |
| 127 | `SQRT(MEAN((100*(C-TSMAX(C,12))/TSMAX(C,12))²,12))` | 固定源码补外层 MEAN 窗口 12；数字参数的 MAX 明确采用本库滚动极值约定，不直接执行上游 `max`。 |
| 146 | 令 `r=C/DELAY(C,1)-1`，`b=SMA(r,61,2)`；结果为 `MEAN(r-b,20)*(r-b)/SMA(b²,60,m)` | 保留研报分子乘数，代数化简分母输入。`m=alpha146_m` 必须是调用者明确提供的 1..60 整数，无默认。上游漏乘数并将分母换成 MEAN，不能把它当成同一公式。 |
| 165 | `CUMRANGE(C-MEAN(C,48),48)/STD(C,48)` | 本库明确选择滚动累计差范围解释；原文没有足够参数来唯一选出它。具体路径见下。 |
| 166 | `-20*19^1.5*SUM(r-MEAN(r,20),20)/(19*18*SUM(MEAN(C/DELAY(C,1),20)²,20)^1.5)` | 固定源码用 MEAN 补缺运算符、补乘号。保留一次方分子；不是标准收益偏度。 |
| 181 | `SUM(r-MEAN(r,20)-(BC-MEAN(BC,20))²,20)/SUM((BC-MEAN(BC,20))³,20)` | 固定源码补最后 SUM 窗口 20。BC 是基准价格；不替换为基准收益，不改为常见协偏度。 |
| 183 | `CUMRANGE(C-MEAN(C,24),24)/STD(C,24)` | 与 165 同一解释，窗口为 24。 |

#### 165/183 的累计范围没有隐藏起点

设 n=48 或 24。先在每个历史时点计算 `z[t]=C[t]-mean(C[t-n+1:t+1])`。在输出 t 的最近 n 个 z 中，从最旧值开始重新累计：`s[k]=sum(z[t-n+1:t-n+1+k])`，k=1..n；`CUMRANGE=max(s)-min(s)`。不追加第零项 0，不沿证券轴取极值，不从数据集第一天永久累计，不把每个历史均值替换为输出 t 的同一个均值。最后**整个范围**除以当前 n 期样本标准差。

因此 165 需要 95 行收盘观察，183 需要 47 行；它们无跨窗口递推状态，常价时分母为零、输出未定义。缺行情会污染完整嵌套窗口，不能删掉该日后继续累计。

上游 165/183 实际调用 `rowMax/rowMin`，在日期×证券矩阵上取横截面极值；[DolphinDB 官方 rowMax 文档](https://docs.dolphindb.com/en/Functions/r/rowMax.html) 确认其逐行含义。本版本没有采纳这一路径。上述滚动路径是清楚、可复现的本库解释；保守研究可继续选择 public.v1 阻断，不能将两者的结果混为同一口径。

### 数值修复与版本溯源

两种公式版本均使用 `alpha191.python.v3` 统计内核，`metadata.engine_version` 参与输入计算哈希。公式版本相同、内核不同也不会复用旧哈希。公式版本、内核版本与输入身份共同决定计算身份，旧候选结果不能作为新运行结果。

普通 CORR/COV 先减去各自窗口的首个观测值，再除以各自最大绝对差，最后中心化归一后的值。均值、乘积及平方和采用 Neumaier 补偿求和；协方差最后用浮点尾数和二进制指数恢复尺度，避免先乘巨大/微小尺度。若有限极值相减溢出，先将窗口除以 2 并保留指数。基数不同但可表示差分相同的窗口走相同的运算路径，045 的平移同形相关系数因此保持并列。该处理位于共享算子，没有按因子编号分支。

恒定输入的协方差严格为 0；两点非恒定 Pearson 为精确 ±1，零方差 CORR 仍未定义。RANK/TSRANK 的有理数结果及其支持的线性算术、SUM/MEAN/DECAY/COV 保留整数分子分母；排名序列的 CORR 使用规范化整数矩，以免真并列被浮点伪差拆开。每个窗口独立决定精确路径，溢出保护也按格/窗口判断，不受未来成员数影响。原始数值求和采用补偿求和。

RANK 按实际数值排序，不添加统一容差或全局四舍五入。真实相差约 `1.3e-14` 的相关系数以及相差约 `8e-14` 的协方差在回归中仍分别排名。

误差界按验证域明确限定：直接算子用输入 binary64 的精确 Fraction 矩与 80 位 Decimal 平方根作参考；测试 CORR 的绝对误差不超过 `1e-14 + 4e-15*abs(reference)`，COV 不超过 `1e-14 + 4e-15*sqrt(var_x*var_y)`（样本方差）。协方差近零时不能用它本身作为相对误差分母。验证覆盖窗口 3/5/15/17/230、最高 `1e12` 的平移基数、仅相差一个浮点间距的近常数、互补的 `2**±700` 尺度及相减溢出场景。上述是可复放测试的接受界，**不是所有浮点输入的全域误差证明，也不参与排名**。输入转换前已丢失的微小变化无法恢复；归一化下溢、一般算术舍入及整数路径保护界之外仍有有限精度边界。不宣称任意代数恒等相关都能得到位级相同的输出。

公开回归使用独立 Fraction/Decimal 参考、原反例及因果性/分块检查。历史实验计数不作为当前安装版本的验收证据。

## 覆盖表

覆盖表由当前安装版本直接生成，不另存两份易过期的 CSV。`coverage()` 是 public.v1，`coverage(version=EXPLICIT_VERSION)` 是显式定义版本；它们的覆盖数不代表输入足够或市场语义已认证。

```sh
python -c 'import sys; from ashare_data.factors.alpha191 import coverage; coverage().to_csv(sys.stdout,index=False)'
python -c 'import sys; from ashare_data.factors.alpha191 import coverage, EXPLICIT_VERSION; coverage(version=EXPLICIT_VERSION).to_csv(sys.stdout,index=False)'
```

如需保存，显式重定向到本次研究输出目录，并记录 wheel、公式版本、内核版本与生成命令。
