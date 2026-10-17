# 因子目录与研究计算

`from ashare_data.factors import catalog, libraries` 返回按来源区分的目录。JoinQuant 命名空间有 19 个明确字段，Alpha191 有 191 个公式编号。名称表示公式来源或研究定义，不保证供应商逐值相等。

| 入口 | 字段/用途 |
| --- | --- |
| `ashare_data.factors.joinquant.compute_panel` | ROC120、Variance20、ATR6、ATR14、EMA5、VROC6、TVMA6、VOL5、circulating_market_cap、net_profit_to_total_operate_revenue_ttm、net_profit_growth_rate、operating_revenue_ttm、net_operate_cashflow_growth_rate |
| `FactorResearchView.from_file(...).query(...)` | BIAS5、CCI20、PSY、quick_ratio、debt_to_asset_ratio、net_operating_cash_flow_coverage |
| `FinancialLedger` / `financial_factor_candidate` | 明确报告版本、时点和字段的财务候选 |
| `net_profit_growth_candidate` / `share_metrics` | 明确滞后规则、股本口径、原始量价及数值转换模型 |
| `bounded_prediction` | TVMA6 原始元金额与显式精度/舍入假设 |
| `ashare_data.factors.alpha191.Alpha191` | [Alpha191 输入、公式、缺失与排名约定](alpha191.md) |

计算必须显式提供 definition/mode，且所用交易日、证券、单位和证据来自调用者。价格、股数、人民币元、原始量、复权依据不可混用。短历史、停牌、未知状态、缺金额/股本或缺财报保持缺失或拒绝，不以零、删行或静默换源补齐。ATR6 与 ATR14 是独立目录条目。

`FactorResearchView` 用固定原件清单核对数据文件；输入只读。其发布日规则与 FinancialPIT 的严格时点认证不同，使用前检查返回的 historical_pit、jq_equivalent、source_files_verified 和缺失原因。计算成功不授予执行权限。

## 开发工具链

1. `capture_factor_panel.py` / `capture_factor_research.py` 读取显式 `--scope`，将公开源响应和回执写入调用方 `--evidence-root`。这些命令会联网，测试不会执行采集。
2. `decode-factor-panel.cjs` 接收归档根、固定 decoder 文件和 scope；核对解码器身份，保存派生文件。
3. `compute_factor_panel.py` / `compute_factor_research.py` 读取归档原件生成输入与面板。scope 旁的 `.sha256` 是调用方本次预先固定的摘要，不使用仓库内实验范围。
4. `verify_factor_panel.py --dataset ... --panel ... --output ...` 提供独立运算形式核对；`verify_factor_research.py` 核查显式源和报告锚点。
5. `compare_factor_reference.py --scope ... --local ... --reference ... --output ...` 只比较本地文件；scope 的 queries 指定 date/securities/fields，另给 thresholds、coverage_min、numeric_pass_min、minimum_panel_size、rank_rule。面板为 `{"rows":[{"security":...,"date":...,"field":...,"value":...}]}`，缺值显式 null。

比较既检查共同可用证券的数值与排名，也独立检查各侧可选股票池的 top/bottom 变化；完美的共同池相关性不能掩盖缺失候选造成的替换。工具不访问 JQ、不重写输入，不继承旧样本的通过率。最小用例见 `tests/test_factor_selection.py`。
