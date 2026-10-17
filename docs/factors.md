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
