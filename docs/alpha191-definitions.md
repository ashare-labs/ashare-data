# Alpha191 显式定义版本

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

## 8 项逐项定义与依据

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

### 165/183 的累计范围没有隐藏起点

设 n=48 或 24。先在每个历史时点计算 `z[t]=C[t]-mean(C[t-n+1:t+1])`。在输出 t 的最近 n 个 z 中，从最旧值开始重新累计：`s[k]=sum(z[t-n+1:t-n+1+k])`，k=1..n；`CUMRANGE=max(s)-min(s)`。不追加第零项 0，不沿证券轴取极值，不从数据集第一天永久累计，不把每个历史均值替换为输出 t 的同一个均值。最后**整个范围**除以当前 n 期样本标准差。

因此 165 需要 95 行收盘观察，183 需要 47 行；它们无跨窗口递推状态，常价时分母为零、输出未定义。缺行情会污染完整嵌套窗口，不能删掉该日后继续累计。

上游 165/183 实际调用 `rowMax/rowMin`，在日期×证券矩阵上取横截面极值；[DolphinDB 官方 rowMax 文档](https://docs.dolphindb.com/en/Functions/r/rowMax.html) 确认其逐行含义。本版本没有采纳这一路径。上述滚动路径是清楚、可复现的本库解释；保守研究可继续选择 public.v1 阻断，不能将两者的结果混为同一口径。

## 数值修复与版本溯源

两种公式版本均使用 `alpha191.python.v3` 统计内核，`metadata.engine_version` 参与输入计算哈希。公式版本相同、内核不同也不会复用旧哈希。公式版本、内核版本与输入身份共同决定计算身份，旧候选结果不能作为新运行结果。

普通 CORR/COV 先减去各自窗口的首个观测值，再除以各自最大绝对差，最后中心化归一后的值。均值、乘积及平方和采用 Neumaier 补偿求和；协方差最后用浮点尾数和二进制指数恢复尺度，避免先乘巨大/微小尺度。若有限极值相减溢出，先将窗口除以 2 并保留指数。基数不同但可表示差分相同的窗口走相同的运算路径，045 的平移同形相关系数因此保持并列。该处理位于共享算子，没有按因子编号分支。

恒定输入的协方差严格为 0；两点非恒定 Pearson 为精确 ±1，零方差 CORR 仍未定义。RANK/TSRANK 的有理数结果及其支持的线性算术、SUM/MEAN/DECAY/COV 保留整数分子分母；排名序列的 CORR 使用规范化整数矩，以免真并列被浮点伪差拆开。每个窗口独立决定精确路径，溢出保护也按格/窗口判断，不受未来成员数影响。原始数值求和采用补偿求和。

RANK 按实际数值排序，不添加统一容差或全局四舍五入。真实相差约 `1.3e-14` 的相关系数以及相差约 `8e-14` 的协方差在回归中仍分别排名。

误差界按验证域明确限定：直接算子用输入 binary64 的精确 Fraction 矩与 80 位 Decimal 平方根作参考；测试 CORR 的绝对误差不超过 `1e-14 + 4e-15*abs(reference)`，COV 不超过 `1e-14 + 4e-15*sqrt(var_x*var_y)`（样本方差）。协方差近零时不能用它本身作为相对误差分母。验证覆盖窗口 3/5/15/17/230、最高 `1e12` 的平移基数、仅相差一个浮点间距的近常数、互补的 `2**±700` 尺度及相减溢出场景。上述是可复放测试的接受界，**不是所有浮点输入的全域误差证明，也不参与排名**。输入转换前已丢失的微小变化无法恢复；归一化下溢、一般算术舍入及整数路径保护界之外仍有有限精度边界。不宣称任意代数恒等相关都能得到位级相同的输出。

公开回归使用独立 Fraction/Decimal 参考、原反例及因果性/分块检查；当前结果由本次测试生成。历史实验计数不作为此版本的验收证据。
