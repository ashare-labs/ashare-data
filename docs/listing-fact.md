# 300750首次上市年份：单字段契约（0.8.2.dev1）

唯一支持字段：`initial_listing_year=2018`，精度明确为year，证券仅300750.XSHE，查询日期仅2026-09-28/29/30。它是2018年度报告摘要中已完成首次A股上市的发行人陈述。首次上市公告书给出2018-06-11安排，但本次事后材料没有核实实际具体日期，因此`initial_listing_date`不支持，返回LISTING_FACT_UNAVAILABLE。

| 公共入口 | 结果与约束 |
|---|---|
| `Store.import_listing_evidence(directory)` | 显式离线验证固定审阅包、原始PDF、索引、回执和提取文本，再发布不可变snapshot_id；相同输入幂等 |
| `Store.listing_fact(snapshot_id)` | 每次公开读取重新核对全部引用；拒绝latest、缺失、损坏、未发布 |
| `view.get(security, on_date, field='initial_listing_year')` | frozen `ListingYearFact`：2018、precision=year、证据等级/出处/页码/真实接收时间，实际日期和历史资格为None |
| `view.descriptor()/lineage()/evidence(name)` | typed范围、来源、原始bytes；`to_dict()`生成独立副本，不要求消费者读私有事实文件 |
| `view.validate()` | VALID_YEAR_FACT_ONLY，完整证券史/执行许可仍false |
| `view.require_eligible(security,on_date)` | 始终LISTING_ELIGIBILITY_UNKNOWN，不用年份判断可交易 |
| `Store.listing_fact_snapshots()/recover_listing_facts()` | 显式列版本/恢复prepared；不联网修复损坏，损坏prepared转aborted |

查询默认posthoc事后研究。`visibility='received', knowledge_at=aware_datetime`仅在本批证据接收完成后允许；知识时钟为2026-09-30则拒绝VISIBILITY_UNKNOWN。`verified`始终PIT_UNAVAILABLE。公告索引时间不是历史available_at，HTTP Last-Modified不是上市实际发生日期。当前转换接收时刻2026-10-10，不倒填2018或策略窗口。

CLI：`--store DIR listing-import PACKAGE`；`listing-query {value,descriptor,lineage,validate} --snapshot ID`（value必须有`--security --date`，可选`--field --visibility --knowledge-at`）；`listing-snapshots`；`listing-recover`。输入缺失/不支持/越界错误为DataError JSON、退出2；有效年份读取退出0不代表执行准入。

固定包SHA256在模块中受owner策略约束。用户修改字段/证券/日期/证据或重算清单hash都会拒绝，更新材料需要新审阅策略和新版本。本版不是通用公告自动解析器：原文先由PDFKit离线提取、关键页人工核对，再确定窄字段转换，原文和转换文件共同按hash封存。该采纳是作者本轮事实核验，独审状态另列，不能以hash代替语义审阅。

范围内三日只表示消费者可请求本历史事实，不表示当日挂牌连续性、ST、停复牌、涨跌停、事件完整覆盖或真实可成交。本组件不改价格数据、不接入M2执行准入，price_context_dataset_id仅标明所针对的既有共同窗口，不证明事实与行情可组成完整执行产品。未实现F2或复权。

## 重放与审阅

```sh
ashare-data --store /新的私有目录/store init
ashare-data --store /新的私有目录/store listing-import /已交接固定证据包目录
ashare-data --store /新的私有目录/store listing-query value \
  --snapshot <完整snapshot_id> --security 300750.XSHE --date 2026-09-30
python examples/listing_fact.py --store /新的私有目录/store --evidence /已交接固定证据包目录
ASHARE_LISTING_EVIDENCE=/已交接固定证据包目录 python -m pytest tests/test_listing_fact.py
```

`descriptor.evidence_files`枚举可读取证据；`lineage`分别指向事后年度摘要与事前上市安排，并提供原文、回执、提取文本名称。原始PDF/索引/回执保留真实字节和接收时间。收包时间、公告披露时间、查询日期是不同字段。公开读取会重新校验整包；本原型只服务这一固定小样本，未做大库性能验收。

证据采集本轮共5次公开HTTP：2次索引、上市公告书、完整年报失败请求、年度摘要。完整年报返回约10MB，超过8MiB上限，未保存完整原文、未采纳；摘要PDFKit提取有CoreGraphics警告，但关键页2/7已成功渲染核对。网络取数已停止，不自动补资料。原始精确日期目标未完成，不能把“年份可用”写成“实际上市日已核实”。

验收包含真实原文导入、3日类型化读取、幂等与旧快照保全、所有11份证据逐一篡改拒绝、自签包拒绝、字段/证券/日期/时钟边界、无网络读取、共享写入锁、清单写入前后中断及损坏恢复拒绝、CLI一致性。完整source/wheel运行日志和固定身份由外置交接报告提供；源码包不携带真实证据，未提供时76项真实字段测试会明确skip。
