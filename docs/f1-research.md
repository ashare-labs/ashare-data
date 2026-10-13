# 有限因子研究接口

本地开发版提供 `ashare_data.f1`，需要调用者合法取得并显式提供离线输入。计算结果保持研究候选状态；独立审查只确认下表、[FI02/SV 增量](f1-fi-sv-round14.md) 及下述批量验收的固定样本，不会把任意新输入自动认证为聚宽等价。

新增[十二槽批量研究接口](f1-batch-usability.md)：12股、59日本地面板，以及其中4日624格参考已完成[独立验收](f1-batch-reference-validation.md)。7槽通过本轮数值与共同可算样本排序门槛，3槽仅排序，2槽不足；ROC120完整候选池Top3仍改变，VROC6仅直接支持48个无窗内因子变更的普通窗口。该批次与下列旧精确兼容结论分别报告，不放宽旧阈值；[整合记录](f1-batch-integration.json)固定来源与独审证据。

| 内容 | 可用入口 | 已确认的范围 |
| --- | --- | --- |
| 合并净利润 TTM / 营业总收入 TTM（FI01） | `FinancialLedger`、`financial_factor_candidate`、`confirmation_prediction` | 300124.XSHE，2024-04-11 与 2024-04-29 收盘，两格原生 float64 为 0 ULP |
| 经营活动现金流净额 TTM 同比增长（FI04） | 同上，`slot="FI04"` | 300124.XSHE，2024-04-11 收盘，一格原生 float64 为 0 ULP |
| 独立成交额六日均值及条件比较（TVMA6） | `bounded_prediction`、`compare_composite_reference`、`summarize_composite_comparisons` | 600519.XSHG、300124.XSHE，2024-04-19 与 2024-04-29，组合模型相容 4/4；原严格检查与冻结点逐 bit 均为 0/4 |

FI02 的 `fi02_lagged_candidate` 和 SV 的 `sv_stage_candidate` 另见 [后续有限研究增量](f1-fi-sv-round14.md)：FI02/SV01 命名样本有限一致，SV02 原逐位失败与独立误差界内数值等价并列。原 `financial_factor_candidate` 仍只接受 FI01/FI04。

价格研究另见 [PV round11 固定归档接口](f1-pv-round11.md)：`pv_round11_candidate` 支持 Variance20、ATR6、ATR14 的有限候选计算，给定 JQ 公共价格时 3 个字段匹配，计 2 个原槽；新浪独立源仍为 0/4。`pv_round11_ema5_local_candidate` 仅复算已失败的 EMA5 固定路径。所有来源兼容性与生产认证仍不成立。

PV01/ROC120、PV05/VROC6、FI03 的既有计算现在也有显式研究入口，见下节；这三个旧固定归档入口仍保留原未对齐及移植待独审状态；新批量验收不回写旧证书。原十二槽完整认证仍未成立。原研究分支与历史审计材料继续保留，没有用软件交付替换其结论。

## 三个未对齐的研究入口

| 入口 | 实际计算与输入 | 保留的缺口 |
| --- | --- | --- |
| `roc120_native_candidate` | 校验原输入包哈希及来源文件；121 个正常交易点，原始收盘价乘当日有效新浪原生因子，按 Decimal50 算 lag120 百分变化，不先修价、不新增输出修约 | 24 个既有预测可重放；已存两股公共端点的 lag120/lag119 均不能复现命名 ROC。实际因子端点和输入视图未识别，`ADJ-INPUT-001` 未解 |
| `vroc6_fixed_snapshot_candidate` | 只接受原隆基 2012-06-19～06-27 六交易日 binding；交叉核对原量、因子、日历，按原 `half_even(V/F_sina)` 研究量算 lag5 百分变化 | 原独立试点 5/160 失败、首窗研究调整量 0/6、VROC 0/1；隆基事件规则反例保留。本六日演示没有查询或比较隆基 VROC 标签，量转换未获供应商认证 |
| `reported_operating_revenue_ttm` | 复用 `FinancialLedger` 的同一截止版本选择、完整四季和年度桥，返回合并营业收入原额、元单位及来源链；不混营业总收入、不取整 | 四个既有日期的原额可重放；2023 两格命名 TTM 与原报重建仍明显不符。内置物化组件版本及存储/处理路径未知 |

ROC 既有算术和端点问题有独审背景；FI03 复用已审账本，原差异也曾被独立重现。PV05 目前只定位到作者测试。**本次新增接口统一返回 `integration_independent_review_completed=false`**，软件回归不是独审签署，也不增加命名因子通过数。各结果携带 `alignment_status`、已知反例和剩余缺口；JQ 等价、生产资格均为 false。PV05 单独标记原实现未独审，不能据其精确分数输出宣称对齐成功。

拥有本地封存材料时，可直接复放，例如：

```python
from pathlib import Path
import json
from ashare_data import f1

archive = Path(".local-evidence/f1-remaining")
roc = f1.roc120_native_candidate(
    archive / "pv01", "600000.XSHG", ["2020-01-13"],
    expected_inputs_sha256="3aecbda2b33f77952318ea4370561bdbe5650c599056300f6ab6db18508448b0",
    definition=f1.ROC120_RESEARCH_DEFINITION, role=f1.RESEARCH_ROLE,
)
volume = f1.vroc6_fixed_snapshot_candidate(
    archive / "pv05",
    expected_binding_sha256="b661e8fa8e377e8f23b4bf98ca527fa2bda3e91d30141da40d7865eb2fd8b613",
    definition=f1.VROC6_RESEARCH_DEFINITION, role=f1.VROC6_RESEARCH_ROLE,
)
pack = json.loads((archive / "fi03/financial-chain.json").read_text())["input_pack"]
f1.verify_pack_files(pack, archive / "fi03")
revenue = f1.reported_operating_revenue_ttm(
    f1.FinancialLedger(pack), "600519.XSHG", "2024-04-29T15:00:00+08:00",
    definition=f1.FI03_RESEARCH_DEFINITION, mode=f1.RESEARCH_MODE,
)
```

ROC 缺日、缺因子或预热不足返回失败行和原因，不能把 `value=None` 当零；格式、单位或证据哈希错误抛异常。PV05 不能换证券、窗口或 binding 来扩成通用 VROC6。FI03 缺季、同周期未连接的版本会拒绝；其来源校验由调用者在建账本前完成，日期级研究约定不证明严格 PIT。

此次只复制原 ROC 方法、原 PV05 有界读取/计算代码，复用现有财报与来源校验；没有新公式、行情框架、修约搜索或取数。历史核对材料留在本地忽略目录；[来源与复核状态](f1-remaining-research.json)固定证据清单。运行 `scripts/verify_f1_remaining.py` 可核对 24 个 ROC 值、六行 VROC 研究量和四个 FI03 原额及来源链；此验证器只准备可重现的软件复核证据，不签发独审证书。

## 财务输入与输出

```python
from pathlib import Path
from ashare_data import f1

# pack 为调用者提供的 ad.f1-small-pack.v1 账本。
f1.verify_pack_files(pack, Path("my-evidence"))
ledger = f1.FinancialLedger(pack)
raw = f1.financial_factor_candidate(
    ledger, "FI01", "300124.XSHE", "2024-04-11T15:00:00+08:00",
    mode=f1.RESEARCH_MODE,
)
prediction = f1.confirmation_prediction(
    ledger, {"slot": "FI01", "security": "300124.XSHE", "date": "2024-04-11"},
    exposed_regimes=[],  # 由调用者如实提供；空列表不证明从未曝光。
)
print(raw["value"], prediction["prediction"])
```

账本显式记录报告、合并口径、人民币元单位、YTD 期间、字段、原额、页码和版本替代关系。`verify_pack_files` 核对提供的文件哈希，不替代来源真伪或逐页转录审查。`FinancialLedger` 按发布时间和明确更正关系选版本；`available_at` 控制严格模式资格，迟到原报不会覆盖已可用更正，同日更正需要精确发布时间。抓取时刻不参与版本优先级。

`financial_factor_candidate` 返回未量化 Decimal50 比率、四季或八季链及年度桥；FI01 不用归母净利润替代合并净利润，FI04 不用当期 YTD 增长替代 TTM 增长。低层算术对部分负基期仍能返回研究值，不能据此宣称供应商边界一致。

`confirmation_prediction` 保留已审实验契约：只接受 FI01/FI04、正 TTM 分母和精确人民币分输入，季度链须与有理数年度桥一致；只在最终比率取 `nearest6_nonmidpoint` 六位候选，再转 float64。精确中点、已曝光财报状态以及无法区别最近舍入与截断的样本被拒绝。它是有限确认实验的预测入口，不是通用生产因子函数。

`RESEARCH_MODE` 为 `reported_date_next_day_incomplete_archive`：披露日期后一自然日上海时间 00:00 才可见。日期级归档没有完整历史修订史、原始历史字节及供应商入库时钟，因此不能通过严格 PIT。三格支持所封存样本和六位候选，不证明全市场算法、普遍舍入政策、非正分母、中点、缺失或有符号零语义。旧的读取前独审证书继续为 false；后来独审单独登记，不倒签。

## TVMA6 条件模型

```python
from ashare_data import f1

prediction = f1.bounded_prediction(item, "2024-04-19", role=f1.RESEARCH_ROLE)
comparison = f1.compare_composite_reference(
    prediction, reference_float_hex=observed_value.hex(),
)
summary = f1.summarize_composite_comparisons([comparison])
print(summary["composite_model"]["all_compatible"])
print(summary["original_v1_checks"])
```

`item` 必须含显式排序的市场交易日、`security`、`source_family="Sina"`、`currency="CNY"`、`raw_price_adjustment="none"`、`amount_unit="CNY"`、`amount_policy="native"`，以及带 `date`、整数元 `amount`、`trading_state="observed_trade"` 的原始行。窗口包含当天及前五个明确交易日；缺日、预热不足、非正金额、非正常交易或超过安全整数边界均拒绝，不补齐、不压缩窗口。成交额不依赖价格、复权因子、成交量或 `postAmt`。

模型显式假设每笔整数元成交额对应真值在 ±0.5 元内，最后仅一次按 0.001 元 half-even 修约，再序列化为 binary64。两项均为条件假设，不是供应商保证。新版组合判定检查目标是否属于这一修约及序列化结果集合；未修约目标与原均值区间的距离只作诊断，不再额外相与。

使用 `composite_model.all_compatible` 判断条件模型；`original_v1_checks` 保留历史 `bounded_consistency_met`、严格检查和逐 bit 结论。相容不证明来源误差上界、唯一修约规则、停牌语义、历史 PIT 或生产准入，所有相关认证标志维持 false。

## 来源、复放与维护

来源为冻结提交 `2205508f1cfa95b8457fe5941f62595e58568d92`（财务）与 `de9ea907388c757ff2ccbe17945c43e605a25c01`（TVMA6）。仅提取已验实现、必要共享依赖和合成回归，未合并作者当前 HEAD。逐文件映射、集成哈希、有限状态与证据摘要见 [机器记录](f1-integration.json)。

完整交付包、原始返回、财报、独审报告及文件清单只在本地忽略目录 `.local-evidence/f1-integration/`，不进入 Git、wheel 或源码发行包。机器记录绑定清单 SHA256；清单逐项绑定相对路径、大小和哈希。拥有这些本地材料时，在仓库根目录运行：

```sh
.venv/bin/python scripts/verify_f1_integration.py
.venv/bin/python -m pytest -q tests/test_f1_scoped.py tests/test_f1_financial_versions.py tests/test_f1_tvma6_bounded.py tests/test_f1_tvma6_composite.py
```

验证器只读本地材料，检查三格完整财务预测与返回、四格 TVMA6 预测及所有旧判定、新版组合汇总；缺失或改动证据会失败，不联网获取或补造。公开测试使用合成输入，不需要私有材料，也不将测试数量当作外部对齐证据。
