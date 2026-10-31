# 查询公司行动与历史股份

## 历史事件与股份

`CorporateHistoryView` 把公开原文、采集回执、公告逐页引文和标准化记录封存到独立目录。它补充已有 `DailyResearchView`、`HistoricalMarketView` 和独立事件查询接口，不修改复权公式、行情库或账户。开启时必须给外部固定的 binding SHA-256；每个引用文件均验证路径、长度和内容哈希。

采集范围由调用方通过 JSON 配置文件显式固定，运行前应确认证券、窗口和请求预算。示例范围不表示已完成来源采集或全市场覆盖。原文、回执、提取文本和数据包由使用者另行准备，不随软件分发。

### 读取

```python
from ashare_data import CorporateHistoryView

v = CorporateHistoryView("/path/to/package/binding.json",
                         expected_binding_sha256="<reviewed SHA-256>")
v.events("000651.XSHE", start="2022-04-01", end="2022-04-15",
         date_role="economic")
v.coverage("000651.XSHE", start="2020-01-01", end="2025-12-31")
v.share_history("601012.XSHG", start="2022-05-20", end="2022-06-17",
                source="sina_structure", include_prior=True)
v.shares_on("601012.XSHG", "2022-06-06", field="total_shares")
```

`events` 返回逐字段值、状态及每个原始观察的来源、定位和数值。金额/比例全部是十进制字符串，缺失为 `None`，不把空值变为零。只有明确引用公告并逐字段声明 `resolves` 的校正才能选定冲突值；其他冲突不给合成值。原始相异值和校正理由始终保留。

日期角色独立：`announcement_date` 实施公告标签、`plan_announcement_date` 预案标签、`record_date` 登记、`ex_date` 除权息、`payment_date` 现金发放、`share_credit_date` 送转到账、`new_shares_tradable_date` 上市、`price_adjustment_date` 交易参考价调整、配股缴款起止、代码转换的 `account_code_change_date` 与新代码交易 `effective_date`。日期不是盘中可用时点。`registration_date` 保留为显式登记事件日期，不自动等同股权登记日。

`date_role="any"` 包含披露日期；`economic` 排除纯披露标签，并保留登记到结算之间横跨查询窗口的权益。覆盖和 AQ 导出使用 `economic`，因此 2025 年提出、2026 年实施的计划不会被计入 2025 年账户收益。按派息日查询仍返回该事件全部可见来源的其他字段。查询必须在已声明证券与日期范围内。

未解决的结算日期冲突也保留可能存在的整个未结权益区间。例如登记 6/1、除权 6/2，派息候选为 6/4 与 6/8 时，`economic` 查询 6/3 或 6/5–6/7 都返回该事件和派息冲突，导出阻断；登记之前和所有可能结算都已结束之后可返回正常空区间。只按 `payment_date` 标签查询时匹配候选日期本身。明确解决冲突后仅使用选定日期，不用已被更正的旧日期延长区间。

公告日为空、源缺失标记或非法日期的已实施业务行仍保留；原始坏值在 `source_issues` 中，未知值为 `None`。全部经济日期未知时返回 `window_placement="unlocated"`；必需结算日期未知时，按已知登记/除权等日期约束可能窗口，返回 `unknown_required_date`，不会把未结算权益当成已确认空窗口。纯现金事件无需补不存在的股票到账日期。明确更正后的日期用于日期查询，已被替代的除权日不再生成第二次权益；原公告日期仍可分别查询。

#### 事件身份与更正

返回 `event_identity_version=2`，`event_id` 是不含可变经济日期的、不透明的权益键。优先使用审阅声明的 `event_identity`；否则锚定最初正式公告（更正链保留前件），最后才退到源记录定位。`identity_method` 说明采用的证据。提供稳定的发行人计划标识时，应从首次封存起为该权益所有版本维护同一 `event_identity`。缺乏正式公告的源定位可能因提供方重新排列表格而变化，不承诺跨任意采集包自动去重。

更正公告用 `supersedes:["<prior source_id>"]`、前件 `observation_id`，或 `supersedes:[{"source_id":"...","locator":"..."}]` 明确引用已封存的同证券、同类型公告。关联引用须有公告引文，实际变更的字段仍须逐项写 `resolves`；引用链本身不会自动覆盖所有字段。旧结构化审阅字段 `native_fields.corrects` / `supersedes_source_id` 继续支持，系统不从自由文本猜关联。`None` 加显式 `resolves` 表示撤回该字段，不回退到旧值。前件在查询范围外时仍保留，以免丢失身份和更正血缘。

共同登记日或共同计划标签仅提示可能重复，不足以自动合并。关联不明、引用循环、身份冲突或无前件的日期解决会记录 `identity_issues` 并阻断导出。两个明确不同的计划标识允许共享登记日。多次更正按可见的引用链选定字段，旧来源和冲突值均保留；公告次日假设下尚不可见的更正不会提前覆盖值。

### 股份口径与冲突

`share_history` 是 **来源观察序列**：总股本、流通 A/B/H、限售 A、优先股分别保留源标签与万股到股的明确换算、原始展示精度和变动原因。定期报告日期标为 `report_snapshot_date`，公告日独立；不是精确的逐日中登登记账本。优先股与其他分项不能自行加减成另一个股本口径。

`sina_float` 是另一个历史流通量接口，不取代 `sina_structure`。两者同日数值可能不同，部分网页 A/H 分类还出现分项合计大于总股本。接口给出 `cross_source_float_disagreement` / `reported_float_components_exceed_total`，不猜原因或改写原数。`shares_on` 对受影响字段返回 `status="source_conflict", value=None`；原数在 `source_value` 和 `rows` 中供诊断。显式查询 `field="total_shares"` 不会因为另一个字段的来源差异自动弃掉总股本观察，也不会把该总股本升级为已验证事实。

默认只取当日观察；没有时返回 missing。只有显式 `carry="last_observation"` 才前向携带，返回参考日期和 `carried_observation_model`；任何模式都保持 `effective_daily_state_verified=False` / `field_semantics_verified=False`。对已确认的旧代码边界拒绝继续携带。历史记录只包含窗口内记录以及每源一个前置基点。

### AQ 引擎消费

```python
schedule = v.export_aq(
    "000651.XSHE", start="2020-01-01", end="2025-12-31",
    tax_model="gross_cash_no_tax",  # 或 external_tax_ledger
    accept_source_scoped_coverage=True,
)
if not schedule["ready_for_declared_date_model"]:
    raise RuntimeError(schedule["issues"])
```

输出 schema 为 `ad.aq-corporate-actions.v1`，包含日期级事件、字段血缘、覆盖与阻断项。显式接受来源范围仅表示消费者接受该研究样本边界；`economic_result_certified`、`historical_pit`、`execution_permission`、`strategy_visible` 始终为 false。默认不接受范围，CLI `export-aq` 被阻断时退出码为 2。

覆盖门禁按表的语义角色检查分红送转（`distributions`）、配股（`rights_issues`）、股本变动（`capital_changes`），不限定供应商名称。既有 `sina_actions` / `sina_structure` 保留兼容映射；其他来源须在封存的 `source_coverage`，或 release 的 `meta.financial_coverage.coverage_json` 中提供 `corporate_coverage`：

```json
{
  "schema": "ad.corporate-coverage.v1",
  "start": "2024-01-01",
  "end": "2024-12-31",
  "tables": {
    "distributions": {"source_table": "captured/distributions", "state": "business_rows_observed", "row_count": 1},
    "rights_issues": {"source_table": "captured/rights", "state": "provider_no_rows_reported", "row_count": 0, "explicit_empty_marker": true},
    "capital_changes": {"source_table": "captured/capital", "state": "provider_no_rows_reported", "row_count": 0, "explicit_empty_marker": true}
  }
}
```

这是生产者的证据声明格式，不是任何供应商已覆盖的证明。每个角色须有源表定位、完整解析结果及业务行数；零行必须来自明确空表证据，不能由未下载或未解析推断。可由多个来源合计覆盖三个角色。查询窗口须包含在声明范围内，同源成功采集时间须晚于窗口末日；release 还校验批次范围、输入/映射行数、隔离行和未解析行。实际采集时间从绑定回执取得，不能自行补历史时钟。显式空表只允许来源范围内的空结果，`absence_proved` 仍为 false；事件的登记、除权、支付、股份到账和可卖日期仍按实际事件类型逐项验证。当前 CMES 日线/因子包没有此项公司行动覆盖声明。

消费者须检查 `event_identity_version=2`，按 `entitlement_key` 建立一次权益，并按 `settlement_keys` 中的登记、现金、股票到账、可卖阶段分别去重；跨窗口或重复查询不得再次发放。`revision_observation_ids` 用于检测修订。已经结算的更正需要账户显式冲正/差额处理，不能只因键相同就忽略。旧日期式 ID 与 v2 ID 不可混用，迁移须对账。以上是 AD 接口契约，尚不是 AQ 原生账户去重或更正执行的实现；隆基股票到账与可卖分日也仍需 AQ 适配。

经济单位及职责：

| 类型/字段 | 含义 | 消费者处理 |
|---|---|---|
| `cash_per_share` | 每一股有权旧股的税前人民币现金 | 按登记日持仓建立应收，按独立选择的到账模型结算 |
| `bonus_share_ratio` / `capitalization_share_ratio` | 每一有权旧股的新增股数，保留送股/资本公积转增区别 | 自有账户处理整数/碎股、税务、可卖数量；到账不等于上市 |
| `rights_ratio` / `rights_price` | 每旧股可认购比例 / 每新股认购价 | 认购需要现金流出和行权决策，不是免费送股；当前导出阻断账户执行 |
| `total_shares_*` / 历史股本 | 发行人层面的股份观察 | 增发、回购注销、限售解禁不自动增加投资者持仓 |
| `ex_reference_cash_deduction` | 交易所参考价计算中的折算现金扣减 | 不可替代投资者现金分红；如格力 2022-04-08 为 0.936124 元，而持有人权益为 1 元 |

税费必须显式选择。`gross_cash_no_tax` 是零税的研究口径；`external_tax_ledger` 需要消费者另行处理股东类别、持有期限、递延扣税和取整。接口不承诺税后到账、不提供实际券商流水，也不替消费者选择手续费。中国结算代派之外的自行派发股东可能有不同到账安排。

单条观察与跨来源合并后的日期使用同一组顺序约束：登记不晚于除权、现金发放、股份到账或可售；除权不晚于现金发放或可售；认购起始不晚于终止。跨来源组合出现倒置时，`coverage` / `export_aq` 返回 `conflicting_consolidated_chronology` 及 `before` / `field`，ready=false。约束不把股份到账、除权和可售强制为同一天。

AQ 适配时将 `payment_date` 显式映射为其现金支付字段；`share_credit_date` 是单数命名，不能静默丢进旧复数字段。`stock_credit` 阶段增加已到账但可能尚不可卖的股份，`stock_tradable` 阶段只解除新增股的可卖限制，不能再次增加总股数。普通送转的总股份倍数为 `1 + bonus_share_ratio + capitalization_share_ratio`，配股和重整不得套用。跨窗口开始时已有未结权益须带入原登记基数，不能按当前持仓重建。AD 返回日期和独立阶段键；持仓、应收、估值、日内可用模型、恢复与重复输入处理仍由 AQ 原生引擎实现并验收。

用原价撮合，登记日建立权益，不使用复权因子反推现金/新股。000022 停牌期间的分红仍可产生权益，但不能把调整后的停牌占位价当成成交。000033 的 2016 法律除权日与 2017 恢复交易参考价调整日分开；重整账户分配规则尚未接入，明确阻断。601313 于 2018-02-23 转换账户代码、2 月 28 日新代码交易，持有人数量不因代码转换而变化；原代码股本查询自 2 月 23 日阻断，发行收购资产的新股不能作为所有股东的送股。

### 可见性

- `posthoc`：使用当前封存原文，拒绝同时传 `as_of`。
- `announced_next_day`：公告日期次日上海零时可见的明确假设；晚披露的回溯事实和更正不提前。没有公告标签的源记录不进入可见行，但返回 `visibility_gaps` 和来源定位，不能把空结果当成无事件。公告标签未知不自动阻断其他日期完整的 `posthoc` 导出。**不是历史 PIT**，因为源表修订链未被证明。
- `received`：按真实采集回执时点过滤；2026 年抓到的原文不能在 2021 年的 received 查询中出现。
- `verified`：直接返回 `PIT_UNAVAILABLE`，不会用公告日期冒充历史精确发布时间。

### 获取、扩展与封存

```bash
python scripts/capture_action_history.py /isolated/captures --scope /isolated/scope.json
python scripts/capture_action_history.py /isolated/captures --requests /isolated/detail-requests.json
python scripts/build_corporate_history.py /isolated/captures /isolated/curation.json /isolated/package --scope /isolated/scope.json
ashare-data corporate-history coverage --binding /isolated/package/binding.json --sha256 <reviewed-sha> --security 000651.XSHE --start 2020-01-01 --end 2025-12-31
```

默认每证券抓新浪分红配股、全历史股本、历史流通量、东方财富分红四类免费公开源。第二步只规划窗口内已实施送转的单次明细。纯现金的派息日期仍应补正式实施公告，混合“红利/配股起始日（送、转股到账日）”标签只在明确送转事件中投影股份到账，不当成现金派息证明。

附加请求 JSON 为数组，每项含 `id/kind/security/url`，可带 `form/referer`。`kind="issuer_pdf"` 用于正式公告；请求仅允许脚本列出的公开 HTTPS 域名。每次响应保存 body、headers、开始/接收时钟、HTTP 状态、字节数和 SHA-256。相同请求复跑先校验并复用；失败可显式追加重试，401/402/403/429 则停止相应主机，不自动重试、扣分、登录或修改网络配置。更新成功响应应新建采集目录，不覆盖旧版本。

`curation.json` schema 为 `ad.corporate-history-curation.v1`；`actions` 每项引用成功的 `issuer_pdf` 请求 `source_id`、相对 `document_text`、`security/type/fields`、`locator/source_status/currency/cash_basis` 和 `citations:[{page,quote}]`。文本页分隔符为 `\n--- PAGE N ---\n`。可用 `supporting_documents:[{source_id,document_text}]` 及 citation 的 `source_id` 引用更正/补充公告。公告事实需要审阅；引文校验保证引用位置与封存文本相符，不代替人工事实判断。`resolves` 只允许公告引文支持的逐字段解决理由。`event_classifications` 须引用公告并给出其公告日期；`security_boundaries` 须引用代码转换公告。

离线构建不写共享数据库，所有输入按内容寻址封存；重复构建固定输出须逐字节一致，否则拒绝覆盖。HTTP 错误原文及“200 但非 PDF”均保留为获取失败，不作为无事件证据。单次原文上限 15 MB，标准化包总量上限 256 MiB，东方财富若返回多页则明确拒绝不完整结果；扩大范围时必须分批并验证分页，不能截取当成全量。

解析失败保留原文、回执及 `source_coverage.status="parse_failed"`、`parse_error`，行数为未知而非零，`absence_proved=False`；包仍可打开、检查血缘与查询其他证券。部分未知业务行保留在 `unparsed_records`，相应证券导出返回 `source_parse_incomplete`。完全空且没有上述显式空表覆盖证据的来源返回 `source_has_no_locatable_history`；已有可定位历史的正常无事件子区间可返回 `empty_source_scoped_interval`，仍不证明全类型无事件。有引文的公告可补足无效源字段，原错误诊断不会删除。

### 尚待完成

发行人全部增发、回购、限售变更及退市重整不具备完整公告事件登记；目前历史股本记录保留这些变动的来源原因，不能断言已覆盖所有变动。精确每日股份、A/H 分类冲突、全部历史公告修订链、真实账户盘中到账、配股行权和重整/代码转换账户执行均未完成。

历史涨跌停参考属于独立的 AD 执行输入链：先按交易所/板块/ST/上市及退市阶段取官方历史规则和当日价限原值，以具体代码与日期绑定证据，逐事件与原价/参考前收盘核验，再交给 `DailyExecutionView`/AQ 撮合层。此接口没有提供认证涨跌停值，也没有把既有模型或复权因子提升为官方数据。

## 价格与事件

独立便携证据包现在可通过 [HistoricalMarketView](historical-market.md) 查询已取得的现金
分红、送股和转增事件；其覆盖、日期适用范围、单位和日内时点不足分别返回。它没有启用
下述旧 snapshot 存储入口，也没有实现复权因子或全市场行动覆盖。

当前 `snapshot.corporate_actions(...)` 和 `snapshot.adjustment_factors(...)` 都抛出
`DataError(code="UNSUPPORTED")`；`bars(adjustment=...)` 只接受 None。
未导入数据不能返回空列表来暗示“没有公司行动”，更不能伪造 factor=1。
当前可用的行动查询、历史股份、事件身份和导出约定见[公司行动历史](corporate-actions.md#历史事件与股份)。通用 snapshot 的复权入口继续拒绝 UNSUPPORTED，不能把有限源事件和因子观察当作全市场自动复权能力。
