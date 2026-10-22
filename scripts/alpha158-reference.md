# Alpha158 官方参考与独立比较

本工具固定 Microsoft Qlib **v0.9.7**、提交 `da920b7f954f48ab1bb64117c976710de198373e`，用其官方表达式引擎计算合成数据。它为将来的 AD 实现提供可搬运对照；不导入 AD 因子计算代码，不训练模型，也不读取真实行情、CMES 或 Air 工作树。

默认定义为 **9 个 K 线特征 + 4 个当日价格比值 + 29 组 × 5 个滚动窗口 = 158**。直接调用 `Alpha158DL.get_feature_config()`，按返回次序保存名称与表达式，不手工重写公式。已比对 v0.9.8：该版本的 loader、handler、滚动 Cython 源码与 v0.9.7 相同，ops 差异位于注册逻辑；为与 Air 对齐，交付固定 v0.9.7。

## 先使用已有小型夹具

仓库根目录运行。比较和校验只需 Python 标准库，不需要安装 Qlib：

```sh
python3 scripts/alpha158_reference.py verify
alpha158_fixture=tests/fixtures/alpha158-reference
python3 scripts/alpha158_reference.py compare \
  --candidate "$alpha158_fixture/expected.csv.gz"
# 将候选实现结果另存为 candidate.csv，再用同一入口比较：
python3 scripts/alpha158_reference.py compare --candidate candidate.csv
```

退出码：0 通过；1 数值、NaN/无穷掩码或行键不一致；2 输入格式、版本或夹具校验错误。JSON 报告包含分类计数和最多 20 个差异示例。自比通过仅验证比较工具，不代表 AD 已实现或通过官方对照。

候选 CSV 必须使用 `instrument,datetime` 两个键列，随后是 `features.json` 的 **158 个有序名称**。行可以重排；缺行、多行、重复键、缺列、多列和列顺序变化均不通过。不要只提交有限值的交集。日期使用 `YYYY-MM-DD`；NaN 可写 `nan` 或空单元格，无穷必须显式写 `inf`/`-inf`。比较保留 NaN 掩码、无穷符号，正负零视为相等。

默认有限值规则为 `abs(actual-expected) <= 2e-7 + 2e-6 * abs(expected)`，适配官方 float32 结果的小量舍入误差。用 `--rtol 0 --atol 0` 可要求有限值完全相等；不要为了消除错误自动放宽容差。

## 输入合同与场景

[输入](../tests/fixtures/alpha158-reference/input.csv) 是原创确定性合成数据，只有 641 行；按日历补齐两个缺失日后输出 643 行 × 158 列。9 个合成证券覆盖普通波动、缺失记录/字段、零成交及恢复、常数、零价格、并列值、近常数、全缺失、三日短历史。

- 字段固定为 `instrument,datetime,open,high,low,close,volume,vwap`。OHLC 和 VWAP 使用同一种合成货币/股单位，volume 为股。VWAP 必须独立提供，缺失时保留 NaN；不能从 OHLC 代造。零成交样本的 VWAP 明确缺失。
- 所有数值先量化成 IEEE little-endian float32，再写成可精确往返的十进制输入和 Qlib `.bin` 文件。候选若要核对这个 oracle，须消费这些已量化的输入。输出也由官方 `LocalExpressionProvider` 转成 float32；这不是任意 float64 输入的高精度真值。
- 80 个工作日标签只是合成会话序列，不是中国交易所日历。标签代表 Asia/Shanghai 会话日期，存储为无时区 ISO 日期。窗口按这个共同日历计数，不按有效观测数压缩。
- `instruments.json` 明确每个证券的有效区间。`SYN_SHORT` 从全局第 77 个位置开始，只有三天；它之前没有历史。缺失的行在区间内占有日历槽，补 NaN，不前向填充。价格为 0 的场景是刻意的异常输入探针，不是可用于交易的有效行情样例。
- 没有复权处理、拆股、真实股票池或跨证券标准化。真实数据接入方需要另行定义统一价格、复权与数量口径，本参考不替代该合同。

## 官方窗口、归一化与缺失语义

默认滚动窗口为 `[5,10,20,30,60]`，均含当前会话。`MA60` 最多依赖 59 个过去会话，`ROC60` 和带一日变化的 60 日窗口最多需要 **60 个过去会话，即含当日 61 行上下文**。`features.json` 按官方表达式树逐列记录 lookback/lookahead；所有默认特征 lookahead 为 0。

官方 rolling 使用 `min_periods=1`，因此短历史并非全部 NaN；`MA60` 首个有效观测即可为 1，`ROC60` 则在第 61 个会话才有足够历史。标准差采用 pandas 样本标准差 `ddof=1`，第一条为 NaN。回归使用官方 Cython 实现；其缺失位置保留在时间坐标中。

官方内部已有价格/当期 close、volume 等比值归一化。例如 `ROC5` 是 `Ref(close,5)/close`，不是减 1 的收益率。默认 Alpha158 推理没有额外处理器。本包仅生成原始 feature，不执行 Fillna、ProcessInf、ZScoreNorm、训练期拟合或标签处理；带未来引用的默认训练标签不在包内。

NaN 语义随算子而异：滚动统计可使用窗口中剩余有效值，比较运算的 NaN 可以变成 false，因而全缺失 close 的 CNTP 可为 0；不能统一把整行标成 NaN。Rank 使用平均并列名次/有效数；IdxMax/IdxMin 使用窗口内从 1 起的位置，取第一个极值，不能擅自改成“距今天数”或 nanargmax。缺失值和并列值的具体结果由官方输出固定。

只在官方公式原有位置加入 `1e-12`；没有统一除零保护。零成交量可能产生很大的 VMA/VSTD，零价格可产生无穷或 NaN，参考全部保留。Corr 与 Rsquare 的近常数判定还使用 `atol=2e-5`，因此输入缩放不保证在所有边界保持数值不变。

生成时会实际执行语义探针、带历史上下文的切片查询，以及修改第 65 个位置之后全部输入的未来扰动检查。这些检查证明本组小样本的行为；不等价于全市场、跨平台或候选实现验收。

## 在隔离环境重新生成

下列命令均从仓库根目录运行。Python 3.12.15 已在 mini 验证；安装环境使用完整官方依赖树，不安装 Qlib extras。系统 Python 不会被修改。只在首次没有源码目录时执行 clone：

```sh
mkdir -p .cache/alpha158
git clone --depth 1 --branch v0.9.7 https://github.com/microsoft/qlib.git .cache/alpha158/qlib
git -C .cache/alpha158/qlib rev-parse HEAD
# 必须为 da920b7f954f48ab1bb64117c976710de198373e
```

已有源码请复用，并确认其 HEAD。环境使用独立 [项目定义](alpha158-reference-environment/pyproject.toml) 与 [uv.lock](alpha158-reference-environment/uv.lock)，不改变根项目的依赖：

```sh
UV_CACHE_DIR="$PWD/.cache/uv" \
UV_PROJECT_ENVIRONMENT="$PWD/.venv-alpha158" \
UV_PYTHON_DOWNLOADS=never \
uv sync --locked --no-config --project scripts/alpha158-reference-environment --python 3.12.15

PYTHONDONTWRITEBYTECODE=1 MPLCONFIGDIR="$PWD/.cache/alpha158/matplotlib" \
.venv-alpha158/bin/python -B scripts/alpha158_reference.py generate \
  --qlib-source .cache/alpha158/qlib --output .cache/alpha158/reproduced

python3 scripts/alpha158_reference.py verify --bundle .cache/alpha158/reproduced
python3 scripts/alpha158_reference.py compare \
  --candidate .cache/alpha158/reproduced/expected.csv.gz --rtol 0 --atol 0
```

缺少指定 Python 时先准备一个独立 3.12 环境；上面的 `UV_PYTHON_DOWNLOADS=never` 会明确失败，不替换系统解释器。输出必须是**新目录**，生成器拒绝覆盖旧结果。生成时禁止 socket 网络访问，Qlib provider、二进制输入及本地实验目录均位于自动清理的临时目录，不启动 Redis、MLflow 或行情服务。安装阶段才需要访问 GitHub 和 PyPI。

生成器核对官方 checkout 提交、已安装包的 Git direct URL、八个关键源码文件与原始 Git 对象、许可原文，以及锁定的 NumPy/pandas/SciPy 版本。它只调用固定官方默认表达式，不接受外部公式。`manifest.json` 记录输入/输出哈希、源码哈希、生成器哈希和探针结果；`environment.json` 记录实际 Python、平台、包版本、许可证元数据和环境锁文件哈希。跨平台需重新生成并比较，不能把某个平台的实测结果直接宣称为另一个平台已通过。

## 来源、许可与接入边界

官方来源：[Alpha158 loader](https://github.com/microsoft/qlib/blob/da920b7f954f48ab1bb64117c976710de198373e/qlib/contrib/data/loader.py)、[handler 默认处理器](https://github.com/microsoft/qlib/blob/da920b7f954f48ab1bb64117c976710de198373e/qlib/contrib/data/handler.py)、[数值算子](https://github.com/microsoft/qlib/blob/da920b7f954f48ab1bb64117c976710de198373e/qlib/data/ops.py)、[Cython 回归](https://github.com/microsoft/qlib/blob/da920b7f954f48ab1bb64117c976710de198373e/qlib/data/_libs/rolling.pyx)。

Qlib 和衍生表达式快照遵循 MIT，夹具带有未经改写的 [Microsoft 许可原文](../tests/fixtures/alpha158-reference/qlib-license.txt)。依赖版本及来源锁在独立 uv.lock 中，许可证元数据在 environment.json；未打包第三方安装目录或虚拟环境。合成输入没有真实行情或第三方数据库内容。

Air 接手时只需让其实现读取相同输入、日历和证券区间，导出同列顺序结果，然后运行比较器。这里没有 AD 生产因子、因子看板或评价接入改动，也不包含 Air 未推送工作树的代码。仓库离线回归使用 `python scripts/run_tests.py -q tests/test_alpha158_reference.py`；运行环境与构建说明见 [开发指南](../docs/development.md)。
