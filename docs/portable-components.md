# 生产可移植研究组件

## 生产与发布

仅离线、有界、600000.XSHG 未复权日线、初始零持仓/旧权益、原 two_day/w1/w2 几何；不增加因子、交易系统或网络 fallback。

### 公共流程

| 接口 | 契约 |
|---|---|
| `Store.import_portable_sources(kind=..., documents=..., classification=..., claims=...)` | 直接从用户文件配方自动计算原件hash并导入，返回component_id；不用手工拼私有包 |
| `Store.import_portable_component(directory)` | 导入公开 `component.json` 和本地原件；支持 market/facts 两类，返回不可变 component_id。文件名仅输入配置，不参与规范身份；逐原件核 hash/size/来源类型。事实是带证据的结构化解释，不是软件验证过的市场真值 |
| `Store.portable_component(component_id)` | 返回 PortableDocument：类型、来源声明、对象引用、状态、单位、unknown；每次重读校验 |
| `Store.validate_portable(price_dataset_id, *, facts_component_id, calendar_component_id=None, state_component_id=None, window_ids, mode)` | 返回有内容身份 report_id 的 PortableDocument；不发布行情产品。完整列出缺日/字段/来源/单位/权益/规则/窗口冲突。可检查 `status=BLOCKED`，不把缺失当无事件 |
| `Store.compose_portable(...)` | 相同参数，成功返回 PortableDocument，至少含 dataset_id/manifest_sha256/profiles/windows/quality/report_id；阻断时抛 PORTABLE_COMPOSITION_BLOCKED，details 中带 report_id 和 gaps；报告可重开 |
| `Store.portable_report(report_id)` | 校验并重开确定的缺口/通过报告；本地保存时间不进身份 |
| `Store.export_portable(dataset_id, directory)` | 将新 v2 产品和最小原件闭包写到新空目录；拒绝覆盖，返回导出清单。实际数据输出私有，绝不塞入代码/wheel |
| `Store.import_portable(directory)` | 继续接收旧固定格式，新增公开 v2 导出格式；新 Store 重导入再验证全部原件和派生规则，不信任粘贴的 plan/receipt。相同代码/原件/选择保持身份；代码或源证据变化产生新身份/明确拒绝旧 pin |

`price_dataset_id` 可为已有 `Store.import_research(..., format='baostock_daily')` 产生的研究 dataset，或含 prices 的 market component。若其包含日历/状态，可省略两个可选 ID；否则必须显式提供。不同来源或重叠行不自动选择，冲突拒绝。

消费 API：portable_profiles/portable/descriptor/windows/assumptions/quality/use/envelope、原 PortableRead/PortableReadContext 和 codec、verify/revalidate。
新数据生成自己的 profile/plan/assumption 引用，不复制旧私有产品 hash，不带旧独审 PASS。

CLI 使用 `ashare-data --store DIR portable {import-component,component,validate,compose,report,export,...} --request FILE`，请求字段对应 Python 参数。
所有 queries 离线；采集不隐含在导入、validate、compose 或查询中。

### 范围及原有分工

三窗几何保持；暖启动 2020-01-02，最后终后继 2020-01-20。为核 01-02 source preclose 与 P raw close，需 **2019-12-31 原价锚点**及连至01-02的源日历；这是校验域，不是执行日/额外策略读取。新产品 WindowPlan 的 `owner_validation_dependencies` 逐项列出原价/状态日期和源日历区间，并明确 consumer_read_permission=false；原 read_roles 不增加。既有原文可包含2019-12-30等有界冗余，绝不据此开放角色。

two_day 执行01-03/06；w1执行01-03～16十日；w2执行01-06～17十日。P/T/next需源日历逐日证实，不能凭周末算法补日。
P-only T09 strategy、T15/日末 engine、terminal仅有界挂牌全部沿原契约。四项 owner 假设与三项 backend 假设不变。
source available_at、PIT、finality、市场真实性、事件完整性仍 unknown，不因导入成功或记录 HTTP200 升级。

owner 校验当前 context 一致性，不维护可信 Session 的调用历史；generation/cursor 不倒退、同 generation 不换 epoch 由 A宽检查。要求数据端单独拒绝所有“回退”与现有无状态接口不一致，由调用方维护。过期 Auth 的当前消费、跨run/request/window拒绝由 owner 保持。

### 来源与事实解释边界

首个支持的市场格式是已有 Bao 本地完整响应 JSON 和可验证 request/response/receipt/SDK capture；必须保留请求证券、频率、原价标记、原字段与 source kind。volume只接受声明为股的来源，amount元（可缺且不消费），不对未知单位猜转换。旧 JSON 无原始接收凭证时 observed_at=null、available_at=null；wire capture保留实际捕获时间但不称历史可得。

facts component 必须提供证券身份、初始上市日期、普通股规则、已知事件及覆盖审阅的来源原件、定位和限制。既有文件可由用户合法提供；新增或改变解释产生新 ID。允许条件模型下“未识别相关事件、完整性未知”的有限审阅；不接受仅空事件列表、缺审阅域、缺来源、未知关键事件日期、已知相关事件或特殊规则反证。
软件验证声明结构、原件引用闭包及有限政策一致性；不会仅凭 URL/文本存在证明引用真实或解释正确。用户提供的解释始终标为 `source_document_claim_unverified`，独审待办；不得声称 owner 已独立核实、PIT 或真实无事件。synthetic 输入明确标记并阻断本条件产品生产，测试样例不冒充真实事实。

这是结构化证据的公共交付格式，不是让 A宽填造事实。数据准备、来源权利与解释复核属于数据层；下游只消费 owner API。原件缺失时返回明确缺口，不把后端接通当成补齐来源。

完整字段、格式与CLI错误约定见 [component-format.md](portable-components.md#组件格式)，端到端公共流程见 `examples/portable_research.py`。便捷入口 import_portable_sources 与组件目录导入使用同一校验规则。

## 组件格式

本页与 `Store.import_portable_sources` / `import_portable_component` 的严格字段解析器共同构成 v1 契约。无隐藏私有清单白名单。原件属于用户，不随源码/wheel分发。

### 最短导入：来源配方

`Store.import_portable_sources(kind=..., classification=..., documents=[...], claims=...)` 自动读取显式文件、计算 SHA256/字节数并封存；无需用户手工计算 hash。路径可为本机配置，保存的组件只含内容引用。市场配方示意（文件必须为合法取得的实际原文，不能用占位符凑数）：

```json
{
  "kind": "market",
  "classification": "user_supplied_source",
  "claims": null,
  "documents": [
    {
      "name": "daily-response",
      "role": "prices",
      "format": "baostock_json",
      "units": {"price":"CNY/share","volume":"share","amount":"CNY"},
      "source_url": null,
      "observed_at": null,
      "artifacts": {"raw":{"path":"USER_DAILY_RESPONSE.json"}}
    }
  ]
}
```

此单文档**通常不足生产**，会缺日历、ST/preclose及事实。它不是可运行市场样本。价格JSON必须保留 `method/error_code/params/fields/rows`；请求字段与响应字段一致，code=sh.600000、frequency=d、adjustflag=3，逐行原值仍为字符串。

已有研究价格亦可直接使用：

```python
price_id = store.import_research(["daily.json"], format="baostock_daily", calendar_path="calendar.json")
report = store.validate_portable(price_id, facts_component_id=facts_id,
                          state_component_id=state_id,
                          window_ids=["two_day"], mode="conditional_research")
```

研究原始 manifest 和 dataset ID 被保留并重新校验；不会只抽价格而遗失研究来源身份。不能用当前新浪尾部样本填补2020年缺日。

### 字段全集

根配方：`kind`（market/facts）、`classification`（user_supplied_source/synthetic）、`documents`、`claims`（market必须null）。不得添加真实完整、已独审、PIT等自授保证。synthetic允许作为导入/拒绝测试，**阻断 PORTABLE 条件产品生产**。

每个 document 严格字段：

| 字段 | 类型/约束 |
|---|---|
| name | 非空逻辑字符串，组件内唯一，非本机路径 |
| role | market：prices/states/calendar；facts：evidence |
| format | market：baostock_json/baostock_capture；facts：official_document/review_record |
| units | prices=`{"price":"CNY/share","volume":"share","amount":"CNY"}`；states只含price；calendar/evidence为空对象 |
| source_url | null或无凭据的http(s) URL；official_document声明限定sse.com.cn/spdb.com.cn/cninfo.com.cn及其子域；域名不是真实性认证 |
| observed_at | null或aware ISO时间；旧JSON必须null，capture与原response_completed_at一致。物理捕获时序另外保留在lineage，不改available_at=null |
| artifacts | JSON/事实文档为raw；capture为request/response/receipt/sdk四项。来源配方每项仅path |

capture文件分别是原始request.bin、response.bin、原receipt JSON、SDK rows JSON。协议身份、原文字节hash、SDK行、receipt行必须全部相符；压缩外层完整性保持unverified。价格行需OHLCV/amount/adjustflag/tradestatus；state需date/code/preclose/isST/adjustflag。prices文档若本来含preclose/isST，可直接供state读取；不能推断缺列。

日历必须来自query_trade_dates，包含请求范围所有自然日及显式0/1状态。导入范围限2019-12-30～2020-01-20，最大32行/文档；生产只开放所选原窗口角色。冗余原文不扩权。重复日期来源拒绝，不静默去重、排序选优或换源。

### 事实与覆盖解释

facts配方的claims严格含 `facts/events/coverage`。

facts为记录列表，每条严格含：
`id/value/status/evidence`。status只接受`source_document_claim_unverified`或`unknown`；
`evidence`是非空`[{"document":"逻辑name","locator":"原文具体位置"}]`。

生产必须具备以下有来源的解释；类型必须精确匹配（100.0不是整数100）：

| id | 有限政策要求 |
|---|---|
| security_identity | 对象至少security=600000.XSHG、kind=ordinary_A_share |
| initial_listing_date | 1999-11-10 |
| buy_round_lot | 整数100 |
| price_tick | 字符串0.01 |
| normal_daily_limit_ratio | 字符串0.10 |
| same_day_resale | 布尔false |

这些政策值写在代码里不代表已提供证券事实。缺记录、unknown、无来源或矛盾仍阻断。

events保留有来源的已知记录，最少 `id/entitled_security/event_type/evidence` 及相应日期。
本有限模型支持已知类型cash_dividend（record_date/ex_date/pay_date均需明确）和convertible_priority_subscription（record_date/subscription_date/conversion_start均需明确）。已知类型不支持或关键日期未知，阻断；任何相关登记/除权/支付/生效/认购/转股/新股上市日期进入有限筛查域，阻断。支付在窗外不能掩盖窗内登记。空events本身不证明无事件。

coverage完整字段：

- start/end：解释筛查域YYYY-MM-DD；必须覆盖所选窗warmup～terminal。
- reviewed_at：本次解释实际审阅时刻，aware ISO；不是历史available_at或旧receipt。
- known_event_completeness固定unknown，verified_absent和historical_pit_verified固定false。
- window_domains：以所选window ID为键，每窗含price_basis/registration/listing三项start/end；精确匹配公开WindowPlan几何。two_day为价基01-02～06、登记01-03～07、挂牌01-02～07；w1为01-02～16/01-03～17/01-02～17；w2为01-03～17/01-06～20/01-03～20（均2020）。
- checks：equity_events/listing_exceptions/rule_exceptions三项，各含status/rationale/evidence；status允许bounded_review_no_conflict_identified/unknown/conflict，后两者阻断。
- evidence：同上原件引用列表，不能只给一句“无事件”。
- jan20_rule：effective_date=2020-01-20、applies_to=commodity_futures_etf_only、ordinary_a_share_resale=T+1、evidence；保留特定修订依据。

覆盖解释及源文件的语义真实性需要数据层复核/独审，软件只能核声明、闭包及一致性；不会因用户写了“无冲突”而给 verified_absent、market_authenticity 或独审PASS。

### 可手动检查的组件目录

`component.json`为以上内容增加 `schema="portable.component.v1"/security="600000.XSHG"/origin=null`，每个artifact严格为`path/sha256/bytes`。
路径为目录内相对文件，拒绝越界。import时丢弃path，其余元数据及原始字节全部决定component ID。
origin为内部保留字段，用户配方/目录必须null；研究来源由`compose_portable(price_dataset_id=研究ID,...)`自动绑定其完整manifest，不能手填。

每组件最多64文档、原件32MiB；可移植产品导出最多256对象/64MiB。manifest最后写入，未完成导出不能导入；不覆盖已有目录。新Store导入导出产品不需要原组件数据库、原绝对路径或旧实验ZIP。

生成产品时可明确选择原窗子集；profile列表只含所选合法窗口，消费者不得复用其他窗口的profile/ack。

### CLI

`portable import-sources --request recipe.json` 中路径相对当前工作目录；示例脚本会明确将配方相对路径解析到配方所在目录。
`import-component`请求directory；`component`请求component_id；validate/compose请求六项price_dataset_id/facts_component_id/calendar_component_id/state_component_id/window_ids/mode（两个可选ID在CLI明确写null）；report请求report_id；export请求dataset_id/directory。
validate打印完整报告，BLOCKED时exit2；compose阻断时exit2且错误details含report_id/gaps。结构/原件损坏返回明确DataError，不当成缺数据继续生产。

新v2产品的WindowPlan增加`owner_validation_dependencies`（security/raw_price_dates/state_dates/calendar/consumer_read_permission=false），供数据依赖检查；不是新的消费者角色。two_day/w1的校验锚点为2019-12-31，w2为2020-01-02。下游应保留完整Plan及其owner摘要；不把这个依赖表变成可调用的read_roles。



### 数值契约

价格、amount（存在时）、前收均使用消费端 `decimal_text`：十进制字符串最长48字符，禁止指数、空白、NaN/Infinity；绝对值小于 10^24，规范值最多8位小数。OHLC 为正且满足高低关系；amount 非负，空字符串保留缺失含义。前收必须为正，其实际生成的90%/110%价格限制也须通过同一编码器。

源 volume 为1至24位 ASCII 数字的非负整数字符串；有限的前导零可保留。长度检查先于整数转换，5000个零也拒绝；不修改 Python 全局整数转换限制。这是本版输入/序列化边界，不是对市场真实成交量的认证。校验与消费共用转换逻辑，保留原始文本与对象 hash，不修补或重写源字段。

市场数值非法时生成可重开的 `BLOCKED` 报告，`compose_portable` 报 `PORTABLE_COMPOSITION_BLOCKED`，不产生产品快照。组件格式或事件结构非法可在导入时提前返回 `DataError`。

### 事件身份契约

每条事件必须为对象，包含 `id`、`event_type` 和 `entitled_security`。ID/类型为1至128字符标识符：首字符为 ASCII 字母或数字，其余可含字母、数字、`_ . : -`。事件 ID 唯一。证券代码为六位数字加 `.XSHG` / `.XSHE` / `.XBSE`；null、空白、unknown 或错误类型不能解释成已知无关。已提供的关键日期须为 `YYYY-MM-DD` 字符串或 null。

先检查全部事件结构及身份，再按明确的证券代码过滤相关性。保留 `360003.XSHG` 优先股记录的既有排除行为；普通股相关事件仍须满足已支持类型、必要日期和窗口外日期限制。格式有效只代表明确的输入声明，不认证证券或事件真实存在。证据引用、事实完整性 unknown、`verified_absent=false` 与 PIT unknown 均保持原义。
