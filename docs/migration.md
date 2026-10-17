# 0.10 功能布局迁移

这是明确不兼容的开发版迁移，基于 `fd162770815900eb86502574a9b51dfbbbb4800d`。旧模块没有永久别名，候选尚未推送或发布。

## 迁移顺序

1. 保留冻结环境、账户、数据及回执。为新版建立独立虚拟环境和输出目录，不原地安装到旧运行包。
2. 更新下表的导入、类型、Store 方法、CLI 和子进程模块字符串。根 Client、Store、get_price、FinancialPIT、FactorResearchView、DailyResearchView 和 DailyExecutionView 保留公开入口。
3. 固定已验证的候选提交或 wheel 摘要，记录新的 owner/version/runtime_code_sha256/parser_sha256/profile_sha256/query_contract_sha256。新代码和资源不继承旧验收。
4. 在支持的格式上从保留原件生成新组件，不能覆盖旧输出或重写旧 pin 强行接受。SDK、公式来源等外部指纹仍指向原始证据字节。
5. 在下游验证序列化、实际调用链、缓存/恢复、时间、单位、缺失处理和执行门禁，再决定切换。

## 类型、方法和标识

| 旧名 | 当前名称 |
| --- | --- |
| D1Facts / D1View | FiniteFacts / FiniteFactsView |
| D1OwnerReceipt / D1PrevCloseCall | FactReceipt / PreviousCloseCall |
| D1FactClaim | FactClaim |
| BR1* 类型 | Conditional* 类型 |
| M2* 类型 | Portable* 类型 |
| M2AD08Call / M2ConsumedPrevious | AdjustedHistoryCall / ConsumedPreviousClose |
| Store.import_d1_facts / d1_facts | Store.import_fact_component / fact_component |
| Store.compose_d1 / d1 / d1_snapshots / recover_d1 | Store.compose_facts / facts / facts_snapshots / recover_facts |
| Store.*br1* / *m2* | Store.*conditional* / *portable* |
| CLI d1-* / br1-* / m2 | facts-* / conditional-* / portable |
| m2a 窗口 | two_day |
| ad.f1-small-pack.v1 | ad.financial-ledger.v1 |
| ad.f1* / ad.f2* / m2.* | ad.factor-panel* / ad.factor-research* / portable.* |

JoinQuant 来源字段使用明确因子名；ATR6 和 ATR14 独立列入目录，共 19 项。公式、缺失和可见性限制仍保留，不认证供应商逐值等价或历史 PIT。旧确认/观察 API 退出运行包，可复用算术保留在领域模块，通用比较位于 scripts。

## 发布树和生成链

历史实验 JSON、范围预算、验收 Markdown、旧源码冻结测试树和固定批次配方退出发布树；没有改名放入 archive，原字节仍可从 Git 历史查阅。当前契约、来源许可、配置和最小合成/协议夹具保留。真实采集、公告、数据库及账户在仓库外。

数据生成链为：显式范围 → scripts 采集/构建 → 仓库外原件与回执 → providers 解析和领域校验 → 新身份数据 → 下游适配。构建链为：pyproject + uv.lock → sdist/wheel → 独立安装 → 结构、资源、CLI 和离线行为检查。

## 跨仓边界

AQ 的冻结旧 AD 核心和另行固定的新读取端使用不同版本，不能原地替换后宣称兼容。冻结运行继续使用原版本；新版 AQ 采用映射和候选 pin，在新输出目录重跑自己负责的调用链与账户测试。协议假设和经济字段保留其定义，不靠盲目字符串替换推断迁移。

共同约定涉及功能命名、目录职责、显式依赖/路径和新交付身份。框架适配、账户、调度、UI 及原生执行准入由各仓负责。本次不部署、启动服务或修改其他仓库。

## 模块映射

| 旧模块 | 当前模块或处置 |
| --- | --- |
| `ashare_data` | `ashare_data` |
| `ashare_data._bao_receipt` | `ashare_data.providers.baostock_receipt` |
| `ashare_data._bao_receipt.capture` | `ashare_data.providers.baostock_receipt.capture` |
| `ashare_data._bao_receipt.protocol` | `ashare_data.providers.baostock_receipt.protocol` |
| `ashare_data._bao_receipt.timing` | `ashare_data.providers.baostock_receipt.timing` |
| `ashare_data.action_extension` | `ashare_data.observations.action_extensions` |
| `ashare_data.action_scope` | `ashare_data.observations.action_baseline` |
| `ashare_data.alpha191` | `ashare_data.factors.alpha191` |
| `ashare_data.alpha191._engine` | `ashare_data.factors.alpha191._engine` |
| `ashare_data.alpha191._formulas` | `ashare_data.factors.alpha191._formulas` |
| `ashare_data.baostock` | `ashare_data.providers.baostock` |
| `ashare_data.baostock_minutes` | `ashare_data.providers.minute_labels` |
| `ashare_data.baostock_worker` | `ashare_data.providers.baostock_worker` |
| `ashare_data.br1` | `ashare_data.research.conditional` |
| `ashare_data.br1_listing` | `ashare_data.research.conditional_listing` |
| `ashare_data.calendar_binding` | `ashare_data.reference.bounded_calendar` |
| `ashare_data.calendar_scope` | `ashare_data.observations.calendar_scope` |
| `ashare_data.cli` | `ashare_data.cli` |
| `ashare_data.close_confirmation` | `ashare_data.observations.close_confirmation` |
| `ashare_data.compat` | `ashare_data.providers.joinquant_style` |
| `ashare_data.contracts` | `ashare_data.core.coverage` |
| `ashare_data.corporate_history` | `ashare_data.corporate_actions.history` |
| `ashare_data.corporate_history_cli` | `ashare_data.cli.corporate_actions` |
| `ashare_data.corporate_history_identity` | `ashare_data.corporate_actions.identity` |
| `ashare_data.corporate_history_sources` | `ashare_data.corporate_actions.sources` |
| `ashare_data.d1` | `ashare_data.research.finite_facts` |
| `ashare_data.d1_types` | `ashare_data.research.fact_types` |
| `ashare_data.daily_execution` | `ashare_data.research.execution_inputs` |
| `ashare_data.daily_observation` | `ashare_data.observations.daily` |
| `ashare_data.daily_research` | `ashare_data.research.daily` |
| `ashare_data.daily_status` | `ashare_data.reference.daily_status` |
| `ashare_data.execution_orders` | `ashare_data.research.order_inputs` |
| `ashare_data.f1` | `ashare_data.factors.joinquant` |
| `ashare_data.f1_acceptance` | `ashare_data.financials.validation` |
| `ashare_data.f1_batch` | `ashare_data.factors.joinquant.panel` |
| `ashare_data.f1_batch_reference` | Removed historical validation; arithmetic/comparison extracted into financials, factors or scripts |
| `ashare_data.f1_batch_sources` | `ashare_data.providers.factor_panel_inputs` |
| `ashare_data.f1_confirmation` | Removed historical validation; arithmetic/comparison extracted into financials, factors or scripts |
| `ashare_data.f1_fi_sv_scoped` | `ashare_data.factors.joinquant.share_financial` |
| `ashare_data.f1_financial` | `ashare_data.financials.ledger` |
| `ashare_data.f1_followup` | Removed historical validation; arithmetic/comparison extracted into financials, factors or scripts |
| `ashare_data.f1_precision` | `ashare_data.factors.joinquant.precision` |
| `ashare_data.f1_price_volume` | `ashare_data.factors.joinquant.amount_inputs` |
| `ashare_data.f1_pv_round11` | Removed historical validation; arithmetic/comparison extracted into financials, factors or scripts |
| `ashare_data.f1_pv_semantic_contract` | `ashare_data.factors.joinquant.price_volume` |
| `ashare_data.f1_remaining` | `ashare_data.factors.joinquant.source_factors` |
| `ashare_data.f1_tvma6_bounded` | `ashare_data.factors.joinquant.amount` |
| `ashare_data.f1_tvma6_composite` | Removed historical validation; arithmetic/comparison extracted into financials, factors or scripts |
| `ashare_data.f1_vroc6_snapshot` | Removed historical validation; arithmetic/comparison extracted into financials, factors or scripts |
| `ashare_data.f2` | `ashare_data.factors.joinquant.research_view` |
| `ashare_data.f2_sources` | `ashare_data.providers.factor_research_inputs` |
| `ashare_data.factors` | `ashare_data.factors.catalog` |
| `ashare_data.financial_pit` | `ashare_data.financials.pit` |
| `ashare_data.historical_market` | `ashare_data.reference.historical_market` |
| `ashare_data.independent_queries` | `ashare_data.research.independent_queries` |
| `ashare_data.intraday_diagnostics` | `ashare_data.observations.intraday_diagnostics` |
| `ashare_data.intraday_observation` | `ashare_data.observations.intraday` |
| `ashare_data.intraday_refresh` | `ashare_data.observations.intraday_refresh` |
| `ashare_data.jq_observations` | `ashare_data.providers.joinquant_observations` |
| `ashare_data.jq_queries` | `ashare_data.providers.joinquant_queries` |
| `ashare_data.listing_fact` | `ashare_data.reference.listing` |
| `ashare_data.live` | `ashare_data.market.client` |
| `ashare_data.local_alias` | `ashare_data.market.alias` |
| `ashare_data.local_baostock` | `ashare_data.providers.baostock_collection` |
| `ashare_data.local_code_history` | `ashare_data.reference.code_history` |
| `ashare_data.local_compatible` | `ashare_data.market.compatible` |
| `ashare_data.local_daily` | `ashare_data.storage.local_daily` |
| `ashare_data.local_early` | `ashare_data.market.early_history` |
| `ashare_data.local_sources` | `ashare_data.providers.daily_sources` |
| `ashare_data.local_status_evidence` | `ashare_data.reference.status_evidence` |
| `ashare_data.m2` | `ashare_data.research.portable` |
| `ashare_data.m2_cli` | `ashare_data.cli.portable` |
| `ashare_data.m2_codec` | `ashare_data.research.portable_codec` |
| `ashare_data.m2_components` | `ashare_data.research.components` |
| `ashare_data.m2_compose` | `ashare_data.research.composition` |
| `ashare_data.m2_payloads` | `ashare_data.research.payloads` |
| `ashare_data.m2_source` | `ashare_data.research.portable_source` |
| `ashare_data.m2_types` | `ashare_data.research.portable_types` |
| `ashare_data.minute_evidence` | `ashare_data.market.minute_evidence` |
| `ashare_data.minute_session` | `ashare_data.market.minute_session` |
| `ashare_data.model` | `ashare_data.core.model` |
| `ashare_data.phase_observation` | `ashare_data.observations.phases` |
| `ashare_data.preclose` | `ashare_data.reference.previous_close` |
| `ashare_data.premarket_reference` | `ashare_data.reference.premarket` |
| `ashare_data.query` | `ashare_data.research.snapshots` |
| `ashare_data.query_policy` | `ashare_data.core.query_limits` |
| `ashare_data.reconciliation` | `ashare_data.market.turnover` |
| `ashare_data.recorded_market` | `ashare_data.market.recorded` |
| `ashare_data.research` | `ashare_data.research.datasets` |
| `ashare_data.research_preflight` | `ashare_data.research.preflight` |
| `ashare_data.response_replay` | `ashare_data.observations.response_replay` |
| `ashare_data.session_reference` | `ashare_data.providers.sse_snapshot` |
| `ashare_data.source_calendar` | `ashare_data.reference.calendar` |
| `ashare_data.storage` | `ashare_data.storage.immutable` |
| `ashare_data.tdx_bars` | `ashare_data.providers.tdx_bars` |
| `ashare_data.tdx_bars_core` | `ashare_data.providers.tdx_collection` |
| `ashare_data.tdx_bars_worker` | `ashare_data.providers.tdx_bars_worker` |
| `ashare_data.tdx_points` | `ashare_data.providers.tdx_points` |
| `ashare_data.transport` | `ashare_data.providers.http` |
| `ashare_data.turnover_diagnostics` | `ashare_data.market.turnover_diagnostics` |
| `ashare_data.visibility` | `ashare_data.core.visibility` |
| `ashare_data.zzshare` | `ashare_data.providers.zzshare` |
| `ashare_data.zzshare_cli` | `ashare_data.cli.zzshare` |
| `ashare_data.zzshare_types` | `ashare_data.providers.zzshare_types` |
