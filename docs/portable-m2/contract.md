# 可移植 M2 生产契约 v1

仅离线、有界、600000.XSHG 未复权日线、初始零持仓/旧权益、原 m2a/w1/w2 几何；不增加因子、交易系统或网络 fallback。

## 公共流程

| 接口 | 契约 |
|---|---|
| `Store.import_m2_sources(kind=..., documents=..., classification=..., claims=...)` | 直接从用户文件配方自动计算原件hash并导入，返回component_id；不用手工拼私有包 |
| `Store.import_m2_component(directory)` | 导入公开 `component.json` 和本地原件；支持 market/facts 两类，返回不可变 component_id。文件名仅输入配置，不参与规范身份；逐原件核 hash/size/来源类型。事实是带证据的结构化解释，不是软件验证过的市场真值 |
| `Store.m2_component(component_id)` | 返回 M2Document：类型、来源声明、对象引用、状态、单位、unknown；每次重读校验 |
| `Store.validate_m2(price_dataset_id, *, facts_component_id, calendar_component_id=None, state_component_id=None, window_ids, mode)` | 返回有内容身份 report_id 的 M2Document；不发布行情产品。完整列出缺日/字段/来源/单位/权益/规则/窗口冲突。可检查 `status=BLOCKED`，不把缺失当无事件 |
| `Store.compose_m2(...)` | 相同参数，成功返回 M2Document，至少含 dataset_id/manifest_sha256/profiles/windows/quality/report_id；阻断时抛 M2_COMPOSITION_BLOCKED，details 中带 report_id 和 gaps；报告可重开 |
| `Store.m2_report(report_id)` | 校验并重开确定的缺口/通过报告；本地保存时间不进身份 |
| `Store.export_m2(dataset_id, directory)` | 将新 v2 产品和最小原件闭包写到新空目录；拒绝覆盖，返回导出清单。实际数据输出私有，绝不塞入代码/wheel |
| `Store.import_m2(directory)` | 继续接收旧固定格式，新增公开 v2 导出格式；新 Store 重导入再验证全部原件和派生规则，不信任粘贴的 plan/receipt。相同代码/原件/选择保持身份；代码或源证据变化产生新身份/明确拒绝旧 pin |

`price_dataset_id` 可为已有 `Store.import_research(..., format='baostock_daily')` 产生的研究 dataset，或含 prices 的 market component。若其包含日历/状态，可省略两个可选 ID；否则必须显式提供。不同来源或重叠行不自动选择，冲突拒绝。

消费 API 不变：m2_profiles/m2/descriptor/windows/assumptions/quality/use/envelope、原 M2Read/M2ReadContext 和 codec、verify/revalidate。
新数据生成自己的 profile/plan/assumption 引用，不复制旧私有产品 hash，不带旧独审 PASS。

CLI 使用 `ashare-data --store DIR m2 {import-component,component,validate,compose,report,export,...} --request FILE`，请求字段对应 Python 参数。
所有 queries 离线；采集不隐含在导入、validate、compose 或查询中。

## 范围及原有分工

三窗几何保持；暖启动 2020-01-02，最后终后继 2020-01-20。为核 01-02 source preclose 与 P raw close，需 **2019-12-31 原价锚点**及连至01-02的源日历；这是校验域，不是执行日/额外策略读取。新产品 WindowPlan 的 `owner_validation_dependencies` 逐项列出原价/状态日期和源日历区间，并明确 consumer_read_permission=false；原 read_roles 不增加。既有原文可包含2019-12-30等有界冗余，绝不据此开放角色。

m2a 执行01-03/06；w1执行01-03～16十日；w2执行01-06～17十日。P/T/next需源日历逐日证实，不能凭周末算法补日。
P-only T09 strategy、T15/日末 engine、terminal仅有界挂牌全部沿原契约。四项 owner 假设与三项 backend 假设不变。
source available_at、PIT、finality、市场真实性、事件完整性仍 unknown，不因导入成功或记录 HTTP200 升级。

owner 校验当前 context 一致性，不维护可信 Session 的调用历史；generation/cursor 不倒退、同 generation 不换 epoch 由 A宽检查。要求数据端单独拒绝所有“回退”与现有无状态接口不一致，由调用方维护。过期 Auth 的当前消费、跨run/request/window拒绝由 owner 保持。

## 来源与事实解释边界

首个支持的市场格式是已有 Bao 本地完整响应 JSON 和可验证 request/response/receipt/SDK capture；必须保留请求证券、频率、原价标记、原字段与 source kind。volume只接受声明为股的来源，amount元（可缺且不消费），不对未知单位猜转换。旧 JSON 无原始接收凭证时 observed_at=null、available_at=null；wire capture保留实际捕获时间但不称历史可得。

facts component 必须提供证券身份、初始上市日期、普通股规则、已知事件及覆盖审阅的来源原件、定位和限制。既有文件可由用户合法提供；新增或改变解释产生新 ID。允许条件模型下“未识别相关事件、完整性未知”的有限审阅；不接受仅空事件列表、缺审阅域、缺来源、未知关键事件日期、已知相关事件或特殊规则反证。
软件验证声明结构、原件引用闭包及有限政策一致性；不会仅凭 URL/文本存在证明引用真实或解释正确。用户提供的解释始终标为 `source_document_claim_unverified`，独审待办；不得声称 owner 已独立核实、PIT 或真实无事件。synthetic 输入明确标记并阻断本条件产品生产，测试样例不冒充真实事实。

这是结构化证据的公共交付格式，不是让 A宽填造事实。数据准备、来源权利与解释复核属于数据层；下游只消费 owner API。原件缺失时返回明确缺口，不把后端接通当成补齐来源。

完整字段、格式与CLI错误约定见 [component-format.md](component-format.md)，端到端公共流程见 `examples/portable_m2.py`。此文件先冻结的入口新增了等价便捷入口 import_m2_sources，不改变缺口和准入原则。
