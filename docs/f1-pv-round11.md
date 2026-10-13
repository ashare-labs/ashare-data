# 固定归档的价格因子研究候选

本增量提供 `ashare_data.f1.pv_round11_candidate`，仅计算经独审的 round11 本地归档，目标为 300124.XSHE、2024-04-19。必须显式指定定义、来源模式和字段；输入、日历、正常交易状态、复权锚点及派生包都受冻结文件清单绑定。归档缺失或改动即失败，不联网补齐。

| 字段 | 原槽 | 给定 JQ 公共价格的条件检查 | 新浪独立源检查 |
| --- | --- | --- | --- |
| Variance20 | PV02 | 固定目标逐 bit 匹配 | 不匹配 |
| ATR6 | PV03 | 固定目标逐 bit 匹配 | 不匹配 |
| ATR14 | PV03 | 固定目标逐 bit 匹配 | 不匹配 |
| EMA5 | PV04 | 原固定等价路径失败 | 不匹配 |

条件支持为 3 个字段、2 个原槽；独立源为 0/4，新增独立 JQ 对齐槽数为 0。此次只有 1 个新的确认目标；早先万科样本参与了规则选择，是开发证据。`ADJ-INPUT-001` 仍阻塞来源兼容性。可计算不等于已对齐，所有 `jq_equivalent`、`strategy_visible`、`production_eligible` 和供应商生成规则认证标志均为 false。

```python
from pathlib import Path
from ashare_data import f1

archive = Path(".local-evidence/f1-pv-round11/bundle/pv-semantic-confirmation-round11")
result = f1.pv_round11_candidate(
    archive,
    definition=f1.PV_RESEARCH_DEFINITION,
    mode="independent_source",
    fields=["Variance20", "ATR6", "ATR14"],
)
print(result["calculation"]["rows"])
print(result["empirical_review"]["fields"])
```

`reference_semantics` 读取已封存的 JQ 公共后复权价 binary64 输入，只讨论给定这些价格后的条件算术。`independent_source` 读取原始十进制新浪价，乘当日有效的新浪原生后复权因子。两种模式均先在价格层取 Q2 half-even、最后在因子层取 Q6 half-even。这是固定候选定义，不是已证明的供应商内部流程；独立模式不用 JQ 反推比例，也不在两种来源之间自动回退。

Variance20 使用 21 个收盘价形成 20 个简单收益率，按样本方差 `ddof=1` 乘 250。ATR 使用 6/14 个 TR 的简单平均，包含前一交易日收盘价。正常交易日必须显式存在；不补齐、不跳过缺日、不推测停牌语义。

EMA5 的原路径使用恰好 60 个收盘价、首收盘价种子、`alpha=1/3`，除以当日收盘价再取 Q6。该路径在确认目标失败，保留原结果；局部种子分析没有覆盖未知供应商起始状态。需要复算时只能显式调用失败候选入口：

```python
failed_candidate = f1.pv_round11_ema5_local_candidate(
    archive,
    definition=f1.PV_RESEARCH_DEFINITION,
    mode="reference_semantics",
    fields=["EMA5"],
)
print(failed_candidate["empirical_review"]["fields"])
```

每个输出同时保留条件检查、独立源检查和当前模式的逐字段状态；ATR6/ATR14 按 PV03 计一次。入口不认证任意新数据或全市场。底层 `f1_pv_semantic_contract.evaluate` 保持冻结算术供研究复用，但其自描述哈希检查不替代归档真实性校验，不能单独作为已审来源接口。

代码来自冻结 `523b84a8cffef3ba8bc20b642277cb1b21850978` 及其候选依赖 `e1a6d23dca5c26656939fdb7f3871f339eab44d5`。计算引擎和原合成测试逐字节不变。新增层只绑定实际归档、选择字段并呈现有限状态，没有调整容差、公式、历史失败或使用后续未审归因。逐文件来源、独审摘要与稳定证据哈希见 [机器记录](f1-pv-round11.json)。

完整证据和独审材料留在忽略目录 `.local-evidence/f1-pv-round11/`，不随 Git、wheel 或源码包分发。拥有本地证据时运行：

```sh
.venv/bin/python scripts/verify_f1_pv_round11_integration.py
.venv/bin/python -m pytest -q tests/test_f1_pv_semantic_contract.py tests/test_f1_pv_round11.py
```

离线验证同时复放原时间顺序、来源重建和失败记录，并比较新入口与冻结引擎的完整计算结果。没有新增行情请求或留出样本。此前 FI/SV/TVMA6 范围保持不变。此轮未集成的 FI03、ROC 及 PV05 后续研究入口见[当前能力说明](f1-research.md)，其数值缺口和 PV05 原失败保留。
