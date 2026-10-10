# 上市事实：精确日期增量与旧年份兼容（0.8.2.dev2）

本次复用 `Store.import_listing_evidence` / `Store.listing_fact(snapshot_id)` / `view.get`，精确日期返回已有通用 `FactRecord` 与 `Evidence`，没有新增证券专属方法或类型。仍仅接纳两个固定审阅包，更新证据需要新的审阅策略；不是通用公告自动解析器。

| 固定包 | 支持字段 | 行为 |
|---|---|---|
| 旧年精度包 `08eb0578e608ed92d2ae8df9c2b7f72092016bdd63d13eaab26162da53d9f9e5` | initial_listing_year | 原有返回和owner_version=0.8.2.dev1、snapshot_id不变；精确日期仍LISTING_FACT_UNAVAILABLE |
| 新日期包 `f046562fc6db577ce0c201872655c0e91c7984bbb981c27bf75d4ece17685099` | initial_listing_date；派生initial_listing_year | 日期2018-06-11，精度day；年份从该日期派生，非另一份独立来源声明 |

两个包均仅300750.XSHE，消费查询日期仅2026-09-28/29/30。请求日期是研究上下文，事件本身发生于2018-06-11。三个消费日可读取该历史事实，不意味着这三日持续上市、可交易或整体资格完备。

| 公共接口 | 结果与约束 |
|---|---|
| `Store.import_listing_evidence(directory)` | 显式离线核对包/原文/回执/转换文件，再不可变发布；相同输入幂等 |
| `Store.listing_fact(snapshot_id)` | 每次公开读取核对全部引用；拒绝latest、缺失、损坏、未发布 |
| `view.get(security,on_date,field='initial_listing_date')` | 新包返回已有frozen FactRecord；`value`为ISO日期字符串，`value_json`保留原JSON序列化；source_fields返回独立副本 |
| `view.get(security,on_date,field='initial_listing_year')` | 保留原ListingYearFact；新包value=2018、actual_initial_listing_date为Python date、evidence_status=derived_from_initial_listing_date；旧包actual日期仍None |
| `view.get(security,on_date)` | 默认仍为年份，保持既有调用兼容 |
| `descriptor/lineage/evidence(name)` | typed范围、证据名称/页码/接收时间、原始bytes；新日期结果复用Evidence模型追溯PDF与发布时间元数据 |
| `view.validate()` | 新包VALID_LISTING_DATE_FACT_ONLY；旧包VALID_YEAR_FACT_ONLY；完整证券史及执行许可仍false |
| `view.require_eligible(...)` | 始终LISTING_ELIGIBILITY_UNKNOWN |
| `Store.listing_fact_snapshots()/recover_listing_facts()` | 新旧快照共存；显式恢复prepared，损坏prepared转aborted，不联网补证 |

## 时间与可见性契约

日期结果的 `fact.source_fields` 明确包含：

| 字段 | 值/语义 |
|---|---|
| event_date / valid_date | 2018-06-11，单次首次实际A股上市事件日期；不是持续可交易区间 |
| document_published_date | 2018-08-24，发行人官网元数据标注发布日期 |
| document_published_datetime_label | 原文2018-08-24 17:12:17 |
| document_published_timezone | None，来源未注明，不猜时区 |
| evidence_received_at | 2026-10-10T04:43:49.030165+00:00，本批必需证据中最后一个HTTP响应接收完成 |
| historical_available_at / historical_eligible | None |
| execution_permission | false |

PDF实际接收为2026-10-10T04:42:41.537331+00:00，元数据实际接收为2026-10-10T04:43:49.030165+00:00，两者分别在 `fact.evidence[*].retrieved_at` 和回执中保留。官网元数据infoId=2055的file路径精确对应同一PDF。第39页表中的2018-06-08是其引用的IPO公告披露日，不是本份半年报发布日期。HTTP Last-Modified、批准报送日、URL日期均不替代采集时间或历史可见性。

默认 `visibility='posthoc'`：事后研究，不提供历史系统已知声明；附knowledge_at必须显式改用其他模式。`received`要求带时区knowledge_at不早于上述证据接收完成时刻；它只核对本地采集时间边界，不证明当时系统已采用该事实或全市场首次可见。2018-06-11、2018-08-24以及2026策略窗口内的历史时钟均返回VISIBILITY_UNKNOWN。`verified`始终PIT_UNAVAILABLE，不因现在取得事后报告而证明2018上市当天系统已知。

## 证据与CLI

新包原文为发行人官网2018年半年度报告（3,627,659字节，SHA256 `819b9b215881a920a9b92c3af11528d50355a8194e4a5ec9cc62110d49885e05`）。第1页代码300750，第38页事后陈述，第39页上市日期表、第40页延续说明；元数据对应发布日期2018-08-24。原文、4张已核对关键页、全文提取、官网索引HTML及回执一起按hash固定。PDFKit提取出现CoreGraphics警告，但关键页已成功渲染核对，139页文本已提取。转换经作者核对，独立复核另列，不以hash代替语义审阅。

```sh
ashare-data --store /新的私有目录/store init
ashare-data --store /新的私有目录/store listing-import /已交接精确日期证据包目录
ashare-data --store /新的私有目录/store listing-query value \
  --snapshot <完整snapshot_id> --security 300750.XSHE --date 2026-09-30 \
  --field initial_listing_date
python examples/listing_fact.py --store /新的私有目录/store --evidence /已交接精确日期证据包目录
```

CLI仍用 `listing-import/query/snapshots/recover`。日期查询输出已有FactRecord的 `to_dict()` 结构：`value_json`为序列化日期，`raw_json`为公开来源/时间元数据；Python消费者可直接读 `fact.value` 和 `fact.source_fields`，无须读私有事实文件。非value查询拒绝多余字段参数，输入缺失/越界/不支持错误JSON退出2。成功读取/完整性校验退出0仅代表该事实可读，不代表执行准入。

仅新增12项小范围真实证据回归，保留旧76项年份回归及全部既有测试。独审用原始证据外置交接，软件包不再分发原始PDF；缺外置样本时明确skip，不用合成事实代替。没有增加采集、标的、其他事实字段、回测引擎或交易功能。
