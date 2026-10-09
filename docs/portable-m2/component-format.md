# 公开组件格式与来源配方

本页与 `Store.import_m2_sources` / `import_m2_component` 的严格字段解析器共同构成 v1 契约。无隐藏私有清单白名单。原件属于用户，不随源码/wheel分发。

## 最短导入：来源配方

`Store.import_m2_sources(kind=..., classification=..., documents=[...], claims=...)` 自动读取显式文件、计算 SHA256/字节数并封存；无需用户手工计算 hash。路径可为本机配置，保存的组件只含内容引用。市场配方示意（文件必须为合法取得的实际原文，不能用占位符凑数）：

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
report = store.validate_m2(price_id, facts_component_id=facts_id,
                          state_component_id=state_id,
                          window_ids=["m2a"], mode="conditional_research")
```

研究原始 manifest 和 dataset ID 被保留并重新校验；不会只抽价格而遗失研究来源身份。不能用当前新浪尾部样本填补2020年缺日。

## 字段全集

根配方：`kind`（market/facts）、`classification`（user_supplied_source/synthetic）、`documents`、`claims`（market必须null）。不得添加真实完整、已独审、PIT等自授保证。synthetic允许作为导入/拒绝测试，**阻断 M2 条件产品生产**。

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

## 事实与覆盖解释

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
- window_domains：以所选window ID为键，每窗含price_basis/registration/listing三项start/end；精确匹配公开WindowPlan几何。m2a为价基01-02～06、登记01-03～07、挂牌01-02～07；w1为01-02～16/01-03～17/01-02～17；w2为01-03～17/01-06～20/01-03～20（均2020）。
- checks：equity_events/listing_exceptions/rule_exceptions三项，各含status/rationale/evidence；status允许bounded_review_no_conflict_identified/unknown/conflict，后两者阻断。
- evidence：同上原件引用列表，不能只给一句“无事件”。
- jan20_rule：effective_date=2020-01-20、applies_to=commodity_futures_etf_only、ordinary_a_share_resale=T+1、evidence；保留特定修订依据。

覆盖解释及源文件的语义真实性需要数据层复核/独审，软件只能核声明、闭包及一致性；不会因用户写了“无冲突”而给 verified_absent、market_authenticity 或独审PASS。

## 可手动检查的组件目录

`component.json`为以上内容增加 `schema="m2.component.v1"/security="600000.XSHG"/origin=null`，每个artifact严格为`path/sha256/bytes`。
路径为目录内相对文件，拒绝越界。import时丢弃path，其余元数据及原始字节全部决定component ID。
origin为内部保留字段，用户配方/目录必须null；研究来源由`compose_m2(price_dataset_id=研究ID,...)`自动绑定其完整manifest，不能手填。

每组件最多64文档、原件32MiB；可移植产品导出最多256对象/64MiB。manifest最后写入，未完成导出不能导入；不覆盖已有目录。新Store导入导出产品不需要原组件数据库、原绝对路径或旧实验ZIP。

生成产品时可明确选择原窗子集；profile列表只含所选合法窗口，消费者不得复用其他窗口的profile/ack。

## CLI

`m2 import-sources --request recipe.json` 中路径相对当前工作目录；示例脚本会明确将配方相对路径解析到配方所在目录。
`import-component`请求directory；`component`请求component_id；validate/compose请求六项price_dataset_id/facts_component_id/calendar_component_id/state_component_id/window_ids/mode（两个可选ID在CLI明确写null）；report请求report_id；export请求dataset_id/directory。
validate打印完整报告，BLOCKED时exit2；compose阻断时exit2且错误details含report_id/gaps。结构/原件损坏返回明确DataError，不当成缺数据继续生产。

新v2产品的WindowPlan增加`owner_validation_dependencies`（security/raw_price_dates/state_dates/calendar/consumer_read_permission=false），供数据依赖检查；不是新的消费者角色。m2a/w1的校验锚点为2019-12-31，w2为2020-01-02。下游应保留完整Plan及其owner摘要；不把这个依赖表变成可调用的read_roles。

0.8.0.dev2 的数值范围、事件最小结构及错误行为详见[发布前校验契约](validation-fix.md)。
