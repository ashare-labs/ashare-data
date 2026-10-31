# 因子库与因子评价

本篇涵盖统一因子目录（`ashare_data.factors.catalog`）、聚宽 19 项命名研究因子、国泰君安 Alpha191（`gtja191`）、Qlib Alpha158（`qlib_alpha158`）以及基于 Alphalens 的离线因子评价接口。

## 统一因子目录检索

因子库按命名空间划分为三个来源库，合计 **368 项** 因子定义（19 + 191 + 158）：

```python
from ashare_data.factors.catalog import catalog, libraries

# 查看三大因子库概览
print(libraries())

# 查询全部或指定命名空间的因子定义表
all_factors = catalog()
alpha_factors = catalog("alpha191")
qlib_factors = catalog("qlib_alpha158")
```

| 命名空间（`library`） | 条目数 | 默认定义版本 | 核心入口 |
| --- | --- | --- | --- |
| `joinquant` | 19 | `jq.factor_research.v1` / `jq.batch_research.v1` | `ashare_data.factors.joinquant.compute_panel` / `FactorResearchView.query` |
| `alpha191` | 191 | `gtja191.public.v1`（可选 `gtja191.explicit.v1`） | `ashare_data.factors.alpha191.Alpha191.compute` |
| `qlib_alpha158` | 158 | `qlib.alpha158.v0.9.7` | `ashare_data.factors.qlib_alpha158.Alpha158.compute` |

---

## 聚宽 19 项命名研究因子（`joinquant`）

覆盖常用量价、动量、波动、换手与财务增长率研究因子（如 `ATR6`、`ATR14`、`TVMA6`、`VROC6`、市值与净利润增长率等）：

```python
from ashare_data import FactorResearchView
from ashare_data.factors.joinquant import compute_panel

# 1. 基于批量输入面板（ad.factor-panel-batch-inputs.v1）计算
panel_result = compute_panel(panel_inputs)

# 2. 基于独立研究快照（ad.factor-research-independent-inputs.v1）按证券与时点查询
view = FactorResearchView("inputs.json", expected_sha256="<64位SHA256>")
res = view.query(factor="ATR14", securities=["000001.XSHE"], as_of="2024-06-14")
```

- 财务类与股本市值类因子严格核对报告期链条（如同比所需的连续季度报告）与披露日可见性；复权价格输入须显式绑定同快照的复权因子。

---

## 国泰君安 Alpha191（`ashare_data.factors.alpha191`）

提供离线、版本化的 GTJA Alpha191 截面与时序公式计算，支持全量面板计算（`Alpha191`）与流式增量更新（`Alpha191Stream`）。

```python
from ashare_data.factors.alpha191 import Alpha191, EXPLICIT_VERSION

engine = Alpha191(
    data={"open": open_df, "high": high_df, "low": low_df, "close": close_df, "volume": vol_df, "vwap": vwap_df, "amount": amt_df},
    universe=universe_bool_df,
    adjustment="adjusted:v1",
    benchmark={"open": bm_open, "close": bm_close},
    factors={"MKT": mkt_s, "SMB": smb_s, "HML": hml_s},
    alpha143_seed=1.0,
    alpha146_m=2.0,
    version=EXPLICIT_VERSION,
    rank_policy="available",
)
result = engine.compute()  # 或传入子集如 [1, 14, 146]
print(result.values)
print(result.status_labels())
```

### 版本与公式规范

- **两个公式注册表版本**：
  - `gtja191.public.v1`（默认）：对照国泰君安 2017 年研报《基于短周期价量特征的多因子选股体系》表 6 与 DolphinDB `gtja191Alpha`（固定提交 `c285bf60...`，Apache-2.0，见同目录 `LICENSE.dolphindb` 与 `NOTICE`），对研报印刷存在窗口缺失或算子歧义的 **8 项因子**（`36, 54, 127, 146, 165, 166, 181, 183`）标记状态为 `AMBIGUOUS_FORMULA` 并返回 `NaN`。
  - `gtja191.explicit.v1`：为上述 8 项因子提供带明确假设与来源依据的可计算定义（其中 `Alpha146` 需调用方显式提供 `alpha146_m` 参数，`Alpha165`/`183` 采用窗口内前缀累计偏差极差除以标准差的定义，逐项说明存放在 `sources.json`）。
- **状态码矩阵（`result.status`）**：每个单元格独立标注 `OK`、`EXCLUDED_UNIVERSE`、`MISSING_INPUT`、`WARMUP_INSUFFICIENT`、`MISSING_DEPENDENCY`、`DOMAIN_INVALID`（如除零、负对数、零方差相关系数）或 `AMBIGUOUS_FORMULA`。
- **离线面板格式（`alpha191.panel.v1`）**：支持从带 SHA-256 校验的 JSON 面板文件加载输入，示例见 `examples/alpha191_offline.py`。

---

## Qlib Alpha158（`ashare_data.factors.qlib_alpha158`）

实现 Microsoft Qlib 0.9.7 规范的 158 项工程特征（涵盖 KBar 形态、价格窗口与滚动 `ROC`/`MA`/`STD`/`BETA`/`RSQR`/`RESI`/`MAX`/`MIN`/`QTLU`/`QTLD`/`RANK`/`RSV`/`IMAX`/`IMIN`/`IMXD`/`CORR`/`CORD`/`CNTP`/`CNTN`/`CNTD`/`SUMP`/`SUMN`/`SUMD`/`VMA`/`VSTD`/`WVMA`/`VSUMP`/`VSUMN`/`VSUMD`）：

```python
from ashare_data.factors.qlib_alpha158 import Alpha158

res158 = Alpha158(
    data={"open": open_df, "high": high_df, "low": low_df, "close": close_df, "volume": vol_df, "vwap": vwap_df},
    universe=universe_bool_df,
    adjustment="adjusted:v1",
).compute()
```

- 仅执行官方特征公式计算，不包含数据归一化处理器（Processor fit）或模型训练；非有限结果在序列化时转为 JSON `null` 并标注 `QLIB_NAN` 或无穷状态。

---

## 因子离线评价（`ashare_data.factors.evaluation`）

安装 `factor-evaluation` extra（`alphalens-reloaded==0.5.0`）后，`evaluate` 对因子面板与远期收益进行横截面统计评价：

```python
from ashare_data.factors.evaluation import evaluate

eval_report = evaluate(
    factor_values=factor_df,
    prices=adjusted_close_df,
    horizons=[1, 5, 10, 20],
    quantiles=5,
    lag=1,
    direction=1,
    min_assets=10,
    cost_bps=[0, 10, 20],
    rolling_window=20,
)
```

- **收益口径**：远期收益按 `P[t + lag + h] / P[t + lag] - 1` 计算（`lag >= 1` 避免当日信号收盘价立即成交的前视偏差）。
- **输出（`ad.factor-evaluation.v1`）**：包含各持有期的 IC / RankIC 均值、标准差、t 统计量、分层平均收益、多空收益差（Spread）、月度与滚动稳定性、相邻分位数换手率及不同单边交易成本（`cost_bps`）下的净收益敏感性。
