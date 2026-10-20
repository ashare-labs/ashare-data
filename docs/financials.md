# 查询财报版本、单季与 TTM

2026-10-20 后续授权已进入正式表实现。现有采集阶段的历史设计说明继续保留；
当前建表、隔离候选、共同发布和读取用法见 [财务和交易参考正式表](financial-tables.md)。
表实现复用现有 Parquet / DuckDB 发布体系；候选完成不等于共用release已经发布。

## 财务时点

`FinancialPIT` 在已有 `FinancialLedger` 上提供多证券、多字段查询。可按一个历史截止时点取当时符合可见性规则的 YTD 财报版本，也可转单季或计算 TTM；每个结果保留公告日期、来源页码、源文件 SHA256、修订关系及全部计算输入。查询实现不含证券硬编码，不计算或更改命名因子。

## 可调用接口

```python
from ashare_data import FinancialPIT
from ashare_data.financials.ledger import RESEARCH_MODE

view = FinancialPIT.from_file(
    "data/financial/pack.json",
    evidence_root="data/financial",
    expected_sha256=pack_sha256,  # 由本次独立输入清单提供
)
result = view.query(
    ["000637.XSHE", "300124.XSHE"],
    ["net_operate_cash_flow"],
    period="2024-12-31",
    as_of="2026-08-07",
    basis="TTM",
    mode=RESEARCH_MODE,
)
for row in result["rows"]:
    print(row["security"], row["status"], row["value"])
```

输入完整且可见时返回金额；缺少所需报告时返回 `missing`，不会补齐。若要查各股最新一期，省略 `period`。同一股多个字段的最新可见报告期必须一致，否则该股全部请求字段返回缺失，避免悄悄混用不同季度或回退旧期。

输入兼容既有 `ad.financial-ledger.v1`：`reports`、`observations`、`sources` 的结构不变。`FinancialPIT(pack)` 可用于调用者已装载的账本，结果标记 `source_files_verified=false`；`from_file` 必须逐项核验所有声明的证据字节，并检查报告 PDF、文本与索引均在源清单中。可用 `expected_sha256` 额外锁定整个转录包。校验哈希不证明源真实性或历史完整性。

| 参数 | 规则 |
| --- | --- |
| `securities`、`fields` | 单字符串或非空、不重复的字符串序列；输出按证券×字段保持矩形排列 |
| `period` | 可省略；指定时为标准自然季度末 `YYYY-MM-DD` |
| `as_of` | 带时区的 ISO 时刻，或日粒度模式下的标准日期；日期表示**上海该日开始时 00:00** |
| `mode` | 必须显式传 `DAILY_MODE`、`RESEARCH_MODE` 或 `"strict"`，无自动回退 |
| `basis` | `"YTD"`、`"quarter"`、`"TTM"`；默认 YTD |

首批支持合并报表、人民币元的五个累计流量字段：`operating_revenue`（营业收入）、`total_operating_revenue`（营业总收入）、`net_profit`（合并净利润）、`parent_net_profit`（归母净利润）、`net_operate_cash_flow`（经营现金流净额）。字段彼此不替代。资产负债表时点数、母公司单体报表、非人民币、非自然年口径尚未接入。

每行 `status` 为 `ok`、`missing` 或 `visibility_unproven`。后两类的 `value=None` 且附原因；格式/口径错误、未知字段、重复版本或歧义修订链抛 `AcceptanceError`。金额为十进制字符串；不以 0 代缺失，不转二进制浮点、不追加修约。

## 日期、版本与计算约定

`RESEARCH_MODE` 的全名为 `reported_date_next_day_incomplete_archive`。只有公告日期证据的报告，在公告日的**后一自然日上海 00:00**纳入；它不是交易日推算或实际发布时刻。例：2024-07-05 披露的普利财报，在 `as_of="2024-07-05"`、当日收盘和当日 23:59:59 都不可见，`2024-07-06T00:00:00+08:00` 才可见。

`DAILY_MODE`（`official_date_next_verified_session`）提供官方日期证据下的历史日粒度查询，具体入口见文末。官方日期、主体、版本、字段口径及已知更正有据时，无需秒级首次发布时钟，也无需证明不存在任何未知修订。公告当日不纳入；从后续已验证的本交易所交易日开始使用，结果以 `daily_pit_usable=true` 明确区分可用粒度。

原有 `strict` 是更强的精确时刻证明契约，要求带时区 cutoff，以及报告的精确 `published_at`、`available_at`、原始首次发布证明与完整修订证据；可用时刻为前两者的较晚值。它不满足不代表日粒度历史查询不可用。结果保留 `historical_pit_certified=false` 表示不声称无遗漏的历史档案认证，不能把这个布尔值当作全部PIT可用性的计数。

版本按原报告与明确替代链选择，而非按本次抓取时刻排序。修改 `captured_at` 不改变历史答案；较晚才采集的原报也不能覆盖已可用的更正。后续报告的比较列只在该后续报告自己的可见边界进入，不倒签到比较期结束日。日粒度下，同日原报/更正有明确替代证据即可在后续交易日选更正；没有替代关系的并列版本仍拒绝。

单季为本季累计减上季累计，Q1 直接取累计。TTM 要求连续四季完整，并校验四个单季之和等于 `本期累计 + 上年全年 - 上年同期累计`；年末 TTM 为全年值。所有项在**同一 cutoff**独立选最新可见版本，结果 `terms` 和 `quarters` 保留带正负号的原始输入。缺任一季度即缺失，不仅凭年度桥凑出数值。


## 财务与交易参考扩采

这是独立的采集与交换层，尚未选择统一数据库引擎，也不修改正式部署清单。
目标是把免费公开源的可得观察扩展到已有历史证券范围，保留缺口，待统一 schema 审定后映射。
不能把供应商当下返回的历史值当作原始首次发布值；本入口不生成官方 FinancialLedger 包。

### 范围与复用

2026-10-19 正式基线为 `~/Workspace/ashare-data-local/daily.sqlite3` 中 3,490 个历史代码。
其中 3,426 个代码与 2010-01-01～2026-10-19 窗口相交。64 个已知代码生命周期在窗外，
保留在 `universe.json` 并记录排除理由，不请求也不悄悄删出基线。
原中小板的历史板块边界沿用已有目录，不能把 2021-04-06 以前的记录称为当时主板证券。
退市及旧代码不因当前名单缺失而丢弃；换码必须经现有 `code_events` 明示，不跨代码拼接。

独立只读盘点已确认：日线有 3,425 股停牌/ST 日状态，复权有 3,490 股 53,136 条（含基准占位）；
这些数据与日历、上市事实、换码均原位引用，不重下、不复制。它们不是全历史官方公告认证。
现存行动/限价小包有自身绑定，不被本批替换。

第一批每股三大报表、分红配股、流通股本，共 17,130 项。总股本及其他股份类型已通过股本结构源
试采，另建 3,426 项批次，首12股中11股成功、1股超时待重试；页面含多年份数据，但不据此承诺
发行人全历史无遗漏。不能用流通股本冒充总股本。分项股份超过总股份等源冲突另加质量标志。
另建深交所官方月历202个月批次（2010-01～2026-10），已复用正式六个月原件；终止于10月19日的
目标窗不纳入该月尚未来到的日期。自然日由源逐日返回，重复、越界或未知标志拒绝；缺日时保留
其余已知源行并将任务标为 `partial_source`、列出缺失日期，不生成缺日的开闭市值。

### 来源核实与权利边界

| 来源 | 已核实能力 | 访问与保留边界 |
| --- | --- | --- |
| 新浪公开 `CompanyFinanceService.getFinanceReport2022`，`type=0`，`fzb/lrb/llb` | 报告期、原始科目 ID/名称/值、`rType`、币种、公告日期、供应商更新时间；完整性检查要求 `report_count`、`report_date`、`report_list` 一致 | 匿名可得；源未发布可核实的数值配额。本采集器单 worker，响应后至少等 2 秒，不使用账号、代理切换或付费接口 |
| 新浪分红配股页面 | 复用 `corporate_actions.sources.parse_sina_actions`；每10股明确转每1股，日期角色分别保留，单位/证券身份变更拒绝 | 页面说明免费提供合作方资料供信息参考，未授予无限请求或公开再分发权；仅本地研究归档 |
| 新浪 `StockService.getAmountBySymbol` | 复用 `parse_sina_float`，流通股份源观察 | 只有该源声明的股份类型；不合成总股本或逐日权益 |
| 新浪股本结构页 | 复用 `parse_sina_structure`，总股本及分类股份、源变动/报告日期与公告日期，万股转股且保留源精度 | 一页包含多个历史表；源冲突不改写、不日历化填充 |
| 深交所官方 `monthList` | 2010-01试采31个自然日成功；已有2023-10～2024-03六个月证据原哈希复用 | 匿名、单worker至少2秒；无数值配额证据，遇403停止，不修改网络路由；仅证明深市源日历 |
| 东方财富公开财务数据中心 | 单次核源返回报表科目、报告日和公告日期 | [免责声明](https://about.eastmoney.com/home/disclaimer)对内容复制/传播及行情复制设有权利限制；尚未核实本任务批量归档授权边界，第一批未纳入批量队列 |
| BaoStock | 官方知识库声明季频财务自 2007 年，覆盖部分公司和字段；现有日线状态已规模采集 | 当前正式环境没有 SDK；未安装新环境或增开连接。当前访问规则页未取得可解析数值限额，因此不宣称无限额，也不启动重复补采 |

源页面：[新浪报表](https://vip.stock.finance.sina.com.cn/corp/go.php/vFD_FinanceSummary/stockid/600600/displaytype/4.phtml?source=lrb)、
[新浪分红](https://vip.stock.finance.sina.com.cn/corp/go.php/vISSUE_ShareBonus/stockid/600600.phtml)、
[BaoStock 知识库](https://www.baostock.com/mainContent)、[访问规则入口](https://www.baostock.com/blacklist)。
当前 Sina 律师链接实际返回“页面没有找到”，robots 请求返回404；这些失败不作为授权凭据。
如后续需要商业分发、账号、积分、付费或新权限，停在该边界报告；本批不取得这些权利。

核源样本最早报告期：青岛啤酒利润表 1992-12-31，浦发银行资产负债表 1996-12-31，
600656 现金流表 1998-12-31。这是逐源实际返回边界，不是全市场覆盖承诺。
600656 源还返回退市后的报告，已用 `period_after_code_exit` 隔离这一含义，不能延长上市区间。

### 临时交换 schema

`manifest.json` 固定目标窗、证券基线和任务清单 SHA；`universe.json` 保存小规模证券事实及换码引用，
没有行情行；`tasks.json` 保存显式 URL。每任务一份压缩源响应、一份标准化 JSONL、一份回执。
公告全文、PDF 和额外大库不在第一批抓取范围。
目标窗是报告期/事件相关日期的筛选范围，不是历史信息可见截止时刻；跨日持续采集以每份
`observed_at` 表示真实接收时刻。窗口内已公告的未来实施事项可保留其全部日期，不能算已结算权益。

`ad.financial-source-report.v1` 一条对应证券×报表×报告期×源快照：

- `security/provider_security/statement/period_start/period_end/basis/scope/currency` 独立保存。
- `announcement_date` 和 `announcement_time_precision` 只表达源公告日期；午夜时间不可冒充精确发布时间。
  `published_at`、`historical_available_at` 保持空；供应商 `update_time` 仅保存在自己的 epoch 字段。
- `source_revision_id` 为源报告快照 hash，`version_kind` 明示不是官方修订号；
  `supersedes=null`，`first_publication_proven=false`，`revision_history_complete=false`。
  以后再抓到不同内容另开批次保留，不覆盖旧批次；不能据抓取时间推导修订先后。
- `fields` 保留源字段 ID、标题、顺序、原值、十进制字符串和缺失状态。相同标题不去重；
  `metrics` 仅映射已核对的基础科目，禁止跨科目替代；没有完整输入时不派生值。
- `unit_basis` 为源原币金额，不作缩放；映射金额在 CNY 源报告上标 CNY，未映射项目保留源语义，
  每股指标等不能笼统解释为金额。`response_sha256/source_locator/observed_at` 定位证据。

`ad.corporate-source-observation.v1` 保留现有解析器的逐字段日期、每股现金/比例、股份数量、
源精度、原字段和来源定位。股份的 `source_effective_date` 是源观察日；`valid_to=null`，
`carry_forward_authorized=false`，不猜补每日连续有效区间。未知经济日期记录仍保留为 unlocated。
`actions`、`nonimplemented` 和 `shares` 分别计数，不把预案或不分配记成已实施分红。

这些 schema 用于交换与审核，不是新永久表设计。接入统一库前需审定：证券稳定身份/代码区间，
报告与观察版本键，公告时间精度与历史可见规则，单位字典及交易参考有效区间。
现有 FinancialPIT 的严格/研究模式及官方源证据契约保持不变；本批源快照不会自动通过它。

### 下游读取约束与异常归因

规范化文件是保留源事实的交换档案，计算使用入口为
`ashare_data.financials.expansion_access.SnapshotReader`，命令行同名模块。
读取器验证批次身份、回执文件哈希、记录来源绑定，再按用途返回一个明确字段。
默认 `historical_pit` 要求该字段、该源版本的官方历史证据绑定；缺少绑定时只拒绝该条输入，不能通过手改快照里的认证布尔值升级为官方 PIT。
`source_audit` 可查看完整原件和限制，不暴露“可计算值”；`source_research` 只读当前已捕获快照，
`listed_security` 还要求报告期落在该原代码存续区间。公告发生在退市之后、但报告期在退出之前，
不会仅因公告较晚而判为退出期外报表。已有换码关系只是证据引用，不静默重写证券代码。

源公告日用上海日历日比较，不能用 UTC 截断制造未来日期。晚于请求日的日期、
晚于捕获日而未复核的源日期都会挡住计算；仅时间经过午夜不会解除未复核限制。
`usage-reviews.json` 被读取器实际消费，逐项绑定 `task_id + response_sha256 + source_revision_id +
source_locator`、明确字段和值、官方证据文件哈希与捕获时钟，不能套用到其他版本或字段。
若当前官方文件已直接核实该数值，则在证据捕获上界之后允许明确的当前研究读取；
这是“最迟此时已观察到”而非首次发布时间，不填充 `historical_available_at`，不解锁严格 PIT。
证据损坏、版本不符、字段未核证或查询早于证据捕获时刻，仍然拒绝。

2026-10-19 对固定核查范围的归因：

- 7 条源日期为10月20日的记录，分别来自600176和600318。官方三季报落款10月19日，
  巨潮索引日为10月20日，文件在19日已公开可得。2条是本期，5条是后来报表中的比较列；
  20个已映射金额均匹配当前官方表格。不能把这些日期当成2025年对应期间的首次披露日，
  也没有证据把所有比较列都称为更正。原日期、更新时钟和版本原样保留。
- 130 条股本相加异常集中在有B/H股的5个主体。源HTML原列即如此，金额换算和列对齐未出错；
  “流通A股”经常接近全部流通股份，分类不能直接相加。读取器考虑打印精度，避免舍入误报；
  对确有重叠的记录拒绝分类股份计算，仍保留总股份用于源研究。不通过减去H/B股编造A股。
- 1,218 条退出代码后报告属于34个原代码：393条对应已核证换码，147条属于长油退出后及
  重新上市的发行人历史（57条停留在退出至重上市间隔，90条在601975重上市之后），
  其余678条另在 `investigations/20261019-followup` 逐组核对官方终止上市公告、退市板主体和原始报告。全部保留；不能因此延长旧代码上市区间，
  也不能把换股吸收合并后的主体、子公司和新上市母公司直接拼在一起。

正式根 `investigations/20261019-anomalies/attribution.json` 给出分组及限制，
`classified-records.jsonl.gz` 保存1,355条精确异常定位，不复制完整报表。
官方证据只保存少量压缩源件与回执。运行中的采集器仍加载原代码；新增读取器不要求重启采集。

```sh
## 默认严格PIT：应返回 allowed=false，进程退出码2。
PYTHONPATH=src python -B \
  -m ashare_data.financials.expansion_access \
  --root $ASHARE_FORMAL_DATA_ROOT/financial-reference/batches/20261019-public-v1 \
  --task sina_income-sh600176 --period 2025-09-30 --field net_profit \
  --as-of 2026-10-19T12:30:00+00:00

## 只有明确选择当前源研究、且查询在绑定证据捕获之后，才返回已复核字段。
PYTHONPATH=src python -B \
  -m ashare_data.financials.expansion_access \
  --root $ASHARE_FORMAL_DATA_ROOT/financial-reference/batches/20261019-public-v1 \
  --task sina_income-sh600176 --period 2025-09-30 --field net_profit \
  --purpose source_research --as-of 2026-10-19T12:30:00+00:00
```

### 运行、断点与验收

使用正式安装环境的 Python，并通过独立工作树 `PYTHONPATH=src` 载入新增模块；不改正式安装包。

```sh
PYTHONPATH=src python -B \
  -m ashare_data.financials.expansion prepare \
  --root $ASHARE_FORMAL_DATA_ROOT/financial-reference/batches/20261019-public-v1 \
  --daily $ASHARE_FORMAL_DATA_ROOT/daily.sqlite3 \
  --start 2010-01-01 --end 2026-10-19

PYTHONPATH=src python -u -B \
  -m ashare_data.financials.expansion run \
  --root $ASHARE_FORMAL_DATA_ROOT/financial-reference/batches/20261019-public-v1

PYTHONPATH=src python -B \
  -m ashare_data.financials.expansion audit \
  --root $ASHARE_FORMAL_DATA_ROOT/financial-reference/batches/20261019-public-v1 \
  --verify --write
```

成功任务复用并验 hash；成功响应在解析中断后也复用。网络失败最多三次且有持久重试时刻；
429 至少冷却一小时并加大间隔，401/402/403 阻断该主机待处理，连续三次传输错误冷却半小时。
不同主机独立处理。`STOP` 或 TERM/INT 在当前请求结束后停止，advisory writer lock 防重入。
源成功后解析错误保留压缩原件与错误，不能当空集；修复后须离线重放，不重复下载。
所有临时原子文件在成功/失败路径清理，崩溃遗留 `.tmp` 由下次持锁启动回收。
`replay --root ...` 离线重放解析失败的已有响应，`--all-captured` 允许重建全部规范结果；不触发网络。
每份回执保留规范器与复用的公司行动解析器 SHA256。原始源响应继续固定保存。
`scripts/run_reference_expansion.py --root ... [--root ...]` 可在本机持续推进已准备批次，
按已持久化的冷却时刻恢复；没有安装服务器或系统调度。线程心跳创建被平台拒绝（仅支持本地线程），
因此不能宣称有定时智能跟进；采集进程状态和续采说明均保存在正式扩采目录。

每50次请求及退出时输出 `coverage.json` 与逐任务覆盖。`audit --verify` 核验已保存原件/规范文件。
外部 `audit --write` 与采集/重放共用写者锁，活跃采集中应省略 `--write`，避免覆盖进度文件。
总数、完成任务、空集未知、解析失败和待采分开；报告期缺口仅表示未观察到，不证明公司当时应披露。
`full_history_complete=false` 和 `historical_pit_certified=false` 不因批量任务完成而变 true。
正式日线库尾日是2026-10-14；财务抓取截至2026-10-19不意味着行情已更新。

### 下一阶段

完成各批后验收 3,426 个目标代码逐类覆盖、股本历史边界和深市月历，补公告索引与更正链，
再用已经可比且有完整期间输入的基础科目派生常用指标。官方全历史限价、完整停复牌/ST公告史、
两市官方全历史日历没有在本批取得，不根据价格、涨跌比例或缺行推导。
统一库尚待用户审定，批准后按确定键/区间契约接入，通过缺口验收再登记正式入口。

### 逐字段历史证据与当前查询范围（2026-10-19续作）

`expansion_history.bound_history` 消费正式根的可选 `historical-bindings.json`
（`ad.expansion-historical-bindings.v1`），准确绑定任务、字段、源响应hash、源版本hash和定位。
每个绑定引用带hash/字节数的官方 `ad.financial-ledger.v1` 以及单独的
`ad.official-report-release-proof.v1`；文件均须位于该正式资产根内。
官方严格选择器必须在请求截止时刻选中绑定的那一修订，金额、币种单位、期间、合并口径和证券一致。
还须有同时期发布留存或已核实历史档案、精确发布/可见时钟，以及覆盖截止时刻的修订清单。
当前页面的日期标签、当前抓到PDF、供应商布尔标志均不能代替这些证据。
无关字段证据不足不会否决有证据字段；较新的源快照不能借旧版本绑定穿越到历史。

| 入口 | 实际范围 | 时间含义 |
| --- | --- | --- |
| `source_audit` | 已取得的原始记录、缺失和异常 | 原始证据查看，无可计算值 |
| `source_research` | 三表8个映射金额及支持的股本字段；公司行动仍走事件专用契约；字段限制逐条执行 | 当前捕获快照，不能声称当时投资者已知 |
| `listed_security` | 上项加原代码上市生命周期过滤 | 不延长旧代码，不把发行人延续视为股票延续 |
| 现有官方ledger研究模式 | 已有18份报告、66条观察形成46个证券×字段×期间可选结果 | 官方索引日期次日的显式研究约定，无精确首发证明 |
| `historical_pit` | 有可信发布与版本绑定的具体字段可通过；目前正式扩采根没有这样的真实绑定 | 严格截止时刻选择，缺少证据逐条拒绝 |

现有官方小包只读验证了48个源文件；46个结果在研究模式可读，严格模式均为
`visibility_unproven`。其中仅000637和603169属于本次主板基线，300124/300630仍属于既有样本，
没有扩大本次范围。新增正例测试使用合成证据，不能算真实PIT覆盖。
现有官方ledger桥接支持5个合并YTD金额（营业收入、营业总收入、净利润、归母净利润、经营现金流），
资产、负债、期末现金余额尚不在该严格ledger字段集内。没有将日期补成午夜。

### 历史限价与参考观察独立批次

公开[zzshare源码与说明](https://github.com/zzquant/zzshare)声明日线自2005年，
单股每次最多1000行。现有 `providers.zzshare` 的严格数字/身份/计数解析复用到
`financials.limit_expansion`；不安装SDK或读取Token。源码的MIT仅为软件许可，
不声称取得数据商业再分发权。只进行匿名本地研究采集，数值日配额仍未知。

`20261019-limits-v1` 复用已冻结的3490代码小清单；3426个目标代码拆成18490个不重叠区间。
每段不超过1000自然日，响应上限1000行，限定IPO至退出日期前一日；无需猜补分页偏移。
当下目标2010年至今，不为2005声明额外扩窗。实际最早/最晚日期按任务回执记录。
标准化只保留 `high_limit/low_limit/is_paused/is_st/factor`，类型为
`ad.daily-reference-observation.v1`；每个数值带原数字token、状态和单位。
零限价不是无限制交易的证据，空集不证明没有行情，缺行不推导停牌。
不保存第二份规范化OHLCV；完整网络响应只保留一份压缩原件以可复核这些参考字段。
供应商限价的上游和生成方法未明，固定为 `source_claimed`，不作为交易所认证价或实盘权限。
ST/停牌仅作为第二来源观察，正式日线已有状态继续优先原位复用。

已试采沪深2010年起区间9项，5402条；一次超时和一次429进入持久冷却，一小时后最低10秒间隔。
沿用原批次引擎的写锁、最多三次尝试、访问阻断、停止文件、原件重放和临时文件清理。
Tushare仅使用用户已授权的现有账号权限；不新开账号、付费升级或增加代理。
接入及安全凭据配置见文末，实际鉴权和历史覆盖以独立源批次回执为准。

```sh
PYTHONPATH=src python -B \
  -m ashare_data.financials.limit_expansion prepare \
  --root $ASHARE_FORMAL_DATA_ROOT/financial-reference/batches/20261019-limits-v1 \
  --baseline-root $ASHARE_FORMAL_DATA_ROOT/financial-reference/batches/20261019-public-v1
PYTHONPATH=src python -u -B \
  scripts/run_reference_expansion.py --provider zzshare-reference \
  --root $ASHARE_FORMAL_DATA_ROOT/financial-reference/batches/20261019-limits-v1
```

### 退出后主体核实与当前读取验收

固定678条/30旧代码已逐组查阅官方终止上市PDF和退市板主体。29组630条的法律主体延续得到支持，
其中博元→亚美、西部资源迁址更名、美都能源→美都控股有公告或报告中的明确沿革；
津信科官网简介将全称写成证券简称，已以2026半年报正文确认其仍为天津卓朗信息科技股份有限公司。
商业城关键词曾命中世茂，已按完整法律名称排除，实际退市板代码为400215；
锦州港A/B分别保留400280/420280，不按发行人合并股份类型。
这些是发行人证据，不是630条全部金额或全部合并范围已验收。

葛洲坝48条仍为 `absorption_merger_verified_post_merger_financial_scope_unresolved`。
官方公告确认2021年被中国能建换股吸收合并，但尚不能证明供应商后续报表属于哪个法律主体、
是否为重组后的子公司口径；不改成601868、不延长600068的上市区间。

银鸽、天成、美都、津信科4份2026半年报的合并三表8个金额逐一复核，共12源记录、32字段。
证据和页码写入 `usage-reviews.json`（加上此前7记录20字段，共19记录52字段）；
仅证明当前对应版本金额吻合，不反推首次可见时钟，不让旧代码期间外记录进入上市期计算。
完整归因与原件索引位于 `investigations/20261019-followup/issuer-review.json`、
`classified-records.jsonl.gz`、`current-amount-review.json`。

新限价批次的读取入口是 `financials.limit_expansion.ReferenceReader`，每次只给一个证券日字段，
验证任务/原件/规范文件hash及捕获时钟。`source_audit`保留缺陷事实，`source_research`允许明确供应商观察，
`historical_pit`拒绝尚无发布历史证据的该条参考输入；缺日不返回0或推导为停牌。

```sh
PYTHONPATH=src python -B \
  -m ashare_data.financials.limit_expansion read \
  --root $ASHARE_FORMAL_DATA_ROOT/financial-reference/batches/20261019-limits-v1 \
  --symbol sh600000 --date 2010-01-04 --field high_limit \
  --as-of 2026-10-19T13:00:00+00:00
```

48项扩采/读取/历史证据回归测试通过，包括可用历史字段正例、无关未知字段、公布时刻边界、
版本不符、证据损坏、日期-only伪精确证据、金额不符、证券退出区间、缺日与限流/访问拒绝。
合成PIT正例不能计入实际已认证覆盖。现有采集进程不因读取器更新而重启。


### 官方公告批量索引和历史字段核验

本仓采集命令须用已批准的正式 Python 环境。示例中的
`ASHARE_FORMAL_DATA_ROOT` 应设为正式数据根（例如 `$HOME/Workspace/ashare-data-local`），
不能指向 Codex 任务目录。运行前检查对应 `writer.lock` 和进程；不重复启动健康进程。

`run_official_expansion.py` 从已冻结的扩采 `universe.json/tasks.json` 取得3426代码，
不重新扫描日线或分钟库。每个证券依次核实官方 orgId，再完整分页抓取定期报告四类、
更正标题、修订标题三个查询。查询总数、唯一公告数、每页主体必须一致；改变或歧义隔离。
同日排序跨页重复时，只补查受影响的单日；要求补查页完整、主体与日期一致，且合并后唯一数
等于原查询总数，否则继续隔离。修复解析器后可显式用 `--retry-parse-failed` 复用缓存重试，
重试前保留原失败状态和新解析器身份，已完成的证券不重新请求。
“这三个查询已完整分页”不等于“历史修订档案无遗漏”，撤回或另行分类的公告仍可能缺失。
离线重放会先校验并保留 `field-reviews.json` 绑定的原有派生文件，只重建尚未人工核验的
文档；已核验文件发生变化时立即拒绝重放。单列“利润表”等未注明合并范围的标题会终止
此前的合并表范围，不能沿用上一页的合并口径。

公告 epoch 按 `Asia/Shanghai` 转为官方日期标签。零点 epoch、PDF 创建时间、本次捕获时间
均不转为首次公开时钟。每条保留原 epoch、公告ID、URL、报告期、日期精度、原文hash、
捕获时间和解析器hash。当前索引中2010年公布的2009年年报保留为边界证据，不算2010年报表期。

首轮原文队列为明确的120代码范围，每个代码选最早、中间、最近的完整一季报报告期，
保留选中报告期所检索到的全部原文版本。这个队列用于验证历史字段与修订，而不是宣称完整财务入库。
正文只保留一次压缩PDF和必要页文字，不生成PNG档案；原始下载/解析临时目录成功失败均清理。
原文图片、非标准列序、单位不明确或缺少合并报表标题，保持候选缺口，不用摘要数字猜补。

```sh
PYTHONPATH=src python scripts/run_official_expansion.py prepare \
  --root "$ASHARE_FORMAL_DATA_ROOT/financial-reference/batches/official-batch" \
  --source-batch "$ASHARE_FORMAL_DATA_ROOT/financial-reference/batches/public-batch" \
  --document-codes 120
# 用系统 Swift 编译 scripts/extract-official-pdf.swift，给出输出二进制路径。
PYTHONPATH=src python -u scripts/run_official_expansion.py run \
  --root "$ASHARE_FORMAL_DATA_ROOT/financial-reference/batches/official-batch" \
  --extractor /path/to/approved/pdf-extractor
# 后续解析改进只重放缓存原件；不请求网络，不触碰已运行的其他采集器。
PYTHONPATH=src python scripts/run_official_expansion.py replay \
  --root "$ASHARE_FORMAL_DATA_ROOT/financial-reference/batches/official-batch" \
  --extractor /path/to/approved/pdf-extractor
```

请求回执按确定性键缓存。单线程至少3秒间隔；429遵守 Retry-After（秒或HTTP日期）且至少冷却1小时，
并降低速率；401/402/403停源，三个连续错误冷却30分钟，单请求最多三次。源拒绝、重试耗尽、
解析失败、原文成功但无可提取字段分开统计。不会用新批次、账号、代理或更高并发绕过限制。

`official_access.reviewed_ledger(root, review)` 将逐字段人工版面核验的原文版本接到已有
`FinancialLedger` 选择逻辑，直接验证既存压缩原件，无资产复制或数据库迁移。
`ad.official-field-review.v1` 必须绑定衍生文件hash、PDF hash、字段、金额、页码及显式替代关系；
更正关系另需已捕获公告回执。索引和PDF存在本身不算版面核验，也不证明整条历史修订链。
研究日期约定下可以按历史截止日查询原版/修订版；严格模式继续拒绝缺少首次公开时钟的真实记录。
不会把只有日期的研究记录标成严格PIT，合成正例也不计入真实覆盖。

已实证的一组历史版本是兰石重装2015年一季报：4月29日索引原报经营现金流净额
为-303,698,290.00元；6月6日更正公告及修订报为-86,886,718.84元，原因为经营/投资流出分类错误。
两个原文第30页及更正公告均已核对。日期研究约定下，2015-05-01和2015-06-07返回相应版本。
采用下述日粒度规则时，更正从2015-06-08起可用；不能用研究模式的自然日边界代替交易日边界。
这个结果来自实际历史原文，相关原件、字段复核和实测输出留在正式数据根的本批次内。

### 有证据边界的日粒度历史查询

```python
from ashare_data.financials.official_daily import reader
from ashare_data.financials.ledger import DAILY_MODE

# field_review 为现有逐字段复核JSON；reference_tasks显式选择已经完成的日线参考窗口。
view = reader(official_root, field_review,
              reference_root=reference_root, reference_tasks=reference_tasks)
result = view.query(securities, fields, period="2015-03-31",
                    as_of="2015-06-08", mode=DAILY_MODE)
```

入口核验原文/衍生文件hash、官方索引原始行中的证券与机构身份、公告日期和PDF链接。
索引须完成本次定期报告/更正/修订三个有界查询；衍生索引不能漏掉已捕获的源版本。
逐字段版面核验继续约束合并范围、报告期、本期列、币种和单位；更正必须有原文及适用范围。

交易日当前复用指定窗口中正成交量、未停牌的本交易所证券日线，保留供应商及源定位。
这是历史成交证据，**不冒充交易所完整官方日历**。只取公告日期之后最早已验证的交易日；
单证券停牌或缺日只会延后可用日，不能推断市场休市。可用日期的上海00:00为明确日粒度
策略边界，不填入真实 `published_at` 或伪装历史抓取时钟。后续可替换为审定的完整交易日历。

已知同报告期未核字段版本或范围未明的财务更正，只阻断其披露后的有关历史区间，
不能阻断更早已核验版本；未发现的修订是档案边界，不作为全体不可用的门槛。
实际例子：兰石重装2015Q1从4月30日起返回原报现金流，6月8日起返回修订值；
上海机场2010Q1净利润从5月4日起可用；皖通高速2010Q1营业收入从4月30日起按原文读取，
即使当前供应商快照金额不同也不会覆盖原报。未核验的字段、缺季度TTM和不明主体仍缺失。

### 错误回执与恢复节流

共享采集器保存实际HTTP状态、接口业务状态、响应字节数/hash、受控脱敏错误正文，
只保留 `Retry-After`、日期、内容类型/长度和数值限额头。不会保存Cookie、认证头、账号或令牌；
错误原始全文不另外落盘。不存在的等待头与格式无效的等待头分别记录。
`Retry-After` 大小写无关，支持秒数或HTTP日期，并保守处理服务端与本地时钟偏差；
503等非429响应的明确等待指示也会持久化，重启不能跳过。

zzshare公开说明只称匿名调用频率较低，没有可核实的数值配额。首次429后的10秒间隔
及至少一小时冷却均为本地策略。本次改进仅利用自然采集成功记录：连续至少120次成功且
至少30分钟无失败后，间隔最多降低10%，下限5秒；任何失败清空恢复窗口，429立即加倍间隔
且至少一小时退避，重复429增加退避。服务端更长等待优先。无额外探测请求、并发压测或绕限流。
新策略只在正常源批边界或已有冷却期间安全换版；不为加载代码打断健康采集进程。

### 已授权账号的Tushare接入

`scripts/run_tushare_reference.py` 使用官方HTTPS接口，不需要在正式Python中安装SDK。
仅复用用户已授权的账号；2000积分档可访问的接口不代表账号鉴权已经成功。
先准备独立批次，再在本机终端使用隐藏输入启动，不把token放进命令行、聊天或配置文件：

```sh
PYTHONPATH=src python -B scripts/run_tushare_reference.py \
  --root "$ASHARE_FORMAL_DATA_ROOT/financial-reference/batches/tushare-reference" \
  --baseline "$ASHARE_FORMAL_DATA_ROOT/financial-reference/batches/public-batch" \
  --prompt-token --detach
```

如已有安全注入的环境变量，可用 `--token-env TUSHARE_TOKEN` 代替 `--prompt-token`。
`--prepare-only` 不读取凭据或联网，只写不可变清单。隐藏输入必须在用户自己的终端完成；
默认凭据只留在工作进程内存；用户明确授权持久保存时，可使用下述限定服务的macOS钥匙串入口。
拒绝跳转和降级到HTTP，源响应若回显凭据则拒绝保存。
隐藏输入若不能关闭终端回显会直接失败；已有环境变量仅在本进程读取并移出其环境，避免传给子进程。
不搜索密钥存储、不新开权限、付费、升级或接受协议；权限错误停源并保留可恢复状态。
批次总根的 `STOP` 在当前请求保存完后生效，等待冷却时也检查；总根及各子批次写锁防止重复启动。

后台启动通过 `subprocess.Popen` 执行全新的Python解释器并建立新会话，不在多线程进程中调用
Python级 `os.fork()` 后继续采集，也不用preexec_fn。单次凭据经匿名管道传递，不放在argv或子进程环境。
已运行批次在询问凭据前被拒绝；子进程取得批次锁并写初始化回执后才回传启动确认。
启动确认明确标为 `authentication_unverified`，不是鉴权或数据成功证明。
握手超时且子进程仍在时保留进程、报告不确定状态，不能自动再启动或杀掉可能正在采集的进程。

用户批准持久配置后，编译 `scripts/tushare-keychain.swift` 为固定位置的本机helper，使用
`scripts/configure_tushare_keychain.py --helper HELPER --status-file BATCH_ROOT/keychain-status.json`
在用户终端隐藏输入。它仅新增服务 `ashare-data.tushare`、账户 `tushare` 的通用密码项，
已有项目直接保留，不能覆盖。也可由用户在钥匙串访问的登录钥匙串中手工创建同名项目。
不通过security命令行的密码参数、不写明文配置、不经聊天；macOS权限交互由用户在本机处理。
本批次的 `configure-tushare-keychain.command` 封装该一次性配置，保存后重试不要求再次输入token。
手工保存的项目如需本机helper访问许可，可由用户运行 `HELPER authorize` 触发系统授权；
只读取该项并丢弃值，输出仅含授权状态，不修改ACL或解锁其他项目。后台读取禁止弹窗。
采集使用 `--keychain --keychain-helper HELPER`；新解释器核对 `credential-policy.json` 中的服务、账户、
helper绝对路径及SHA256后读取。helper将秘密写入限定匿名管道，stdout只用于非秘密状态，状态查询不读取秘密。
钥匙串锁定/访问拒绝单独报告；不能为恢复访问绕过系统保护或偷偷改到明文存储。
旧不可变请求清单保留准备时的存储注释，后来用户批准的变更由父批次 `credential-policy.json` 和worker中的实际来源单独记录。

依据：[Python 3.12 fork约束](https://docs.python.org/3.12/library/os.html#os.fork)、
[subprocess进程/管道接口](https://docs.python.org/3.12/library/subprocess.html)、
[Apple Keychain查询](https://developer.apple.com/documentation/security/secitemcopymatching(_:_:))。

首阶段为沪深各年 `trade_cal`，再按实际返回交易日采 `stk_limit`。最新日作为普通首任务，
随后连续采全部历史日期；没有额外小批批准门槛。目标证券仍由原始历史主板基线及代码存续区间限定。
原响应仅一份gzip，规范数据只保留目标范围，官方日线/分钟数据库不受影响。

单线程响应后至少1秒，本地速率低于官方2000积分档一般200次/分钟；遇明确服务器等待指示
或流控会持久退避，权限问题停源。5800条上限按全响应判断，不能先过滤目标股票再判断截断。
触顶日期保留 `partial_source`，只为缺失目标证券生成单股单日补采；不猜测未记载的分页参数。
0、null、缺行都不等价于“无涨跌幅限制”或停牌；`up_limit/down_limit/pre_close`保留十进制源值及元/股单位。
返回成功、目标证券全出现、整个市场历史完整是不同结论；源实际最早日以已采覆盖清单为准。

在Tushare同范围产生可用数据前，既有zzshare进程保持运行。交接在原请求边界进行，
保留既有源分区，重新核对缺口后仅保留必要补采；不能继续双份全量请求，也不能把限价成功
扩大为停复牌/ST/复权因子全部已替代。新浪财务和巨潮原文继续提供互补字段和版本证据。
普通财务接口逐股保留 `ann_date/f_ann_date/report_type/update_flag`，各表日期参数分别核源；
不使用5000积分VIP、权限有冲突的 `share_float`、独立分钟权限或全因子库。

官方依据：[stk_limit](https://tushare.pro/document/2?doc_id=183)、
[trade_cal](https://tushare.pro/document/2?doc_id=26)、
[权限频次](https://tushare.pro/document/2?doc_id=290)、
[积分权限](https://tushare.pro/document/1?doc_id=108)、
[财务修订FAQ](https://tushare.pro/document/2?doc_id=122)。

### 财务、分红和因子的按字段补采

同一入口在限价及截断目标补采结束后，等待原公开源批次自然结束并安全回放失败缓存，
再冻结 `fundamentals/gap-plan.json`。等待期间凭据仍仅在进程内存；STOP和信号均可停止。
已有计划恢复时沿用原任务与回执，不重新生成全量请求。各接口继续共享限流/封禁状态，串行实采。

`scripts/plan_tushare_gaps.py --baseline PUBLIC_BATCH --root GAP_ROOT` 可先离线预览。
它只读本次扩采的回执及规范数据，以回执和代码哈希缓存紧凑字段清单，不扫描日线或分钟大表。
在途源任务标为 `pending_existing_source`；预览不能被当作已冻结或已采集清单。
三张基础报表只对现有合并口径、CNY金额的实际字段缺口补采type 1金额；已有金额仅取缺失版本元数据。
缺失报告期以其他已采基础报表的实际期间为候选，不把季度列表当作强制披露义务。
普通元数据响应若返回此前未见的期间，再据该回执生成一次对应期间的金额补采；新披露不会因旧库存没有该季而被丢弃。
type 4与5分别补已调整比较期、调整前合并报表；不把这些类型当作完整修订链或擅自确定supersedes关系。

| 接口 | 参数日期含义及边界 | 保留和缺口规则 |
| --- | --- | --- |
| `income`、`balancesheet`、`cashflow` | start/end按公告日期；period按报告期。资产负债表不传未记载的f_ann_date输入 | 限定合并type 1/4/5；每条返回版本、原序号和字段投影哈希分别保留 |
| `fina_indicator` | start/end按报告期；单次100行 | 仅基础输入仍缺失的常用指标；已有兼容基础科目先派生净利率、资产负债率，不扩全因子库 |
| `dividend` | 没有start/end参数；优先逐股ex_date事件 | 补已知事件缺失的付款、实施公告、基准股本等字段；仅现有分红记录为零时取单股源历史，并限定目标日期 |
| `adj_factor` | 逐股有界交易日期，单窗口最多366个自然日 | 通过显式已有覆盖清单仅采缺口尾段；不重取已有2010年至覆盖截止日的因子历史 |

财务amount原值保留十进制，未有足够金额单位证据时标记 `provider_native_amount_unit_unverified`，
对账仍列为单位待核，不进行猜测缩放。`ann_date/f_ann_date`只证明供应商返回日期；
不能仅凭股票+报告期把新日期绑定到另一来源的旧金额，也不自动进入历史PIT读取。
同一版本内、同币种同口径且分母非零时派生的两项比率标为当前源研究值；零分母保持未定义。

财务三表暂以100行为本地保守截断警戒，并非声称官方统一上限；触顶或has_more时二分公告日期。
指标按报告期二分，最多18层；单日或单期间仍触顶时留明确未解决缺口，不猜offset。
分红的2000行上限单列；`cash_div`税后、`cash_div_tax`税前，不互换；`base_share`万股精确转股。
已实施状态、除权日、登记日、税前现金及送转比率全部相符且匹配唯一，才可对账原事件；
多版本/多事件歧义保留，预案和未实施记录不转成已发生行动。
Tushare再投资因子与Sina hfq不是可直接衔接的同一序列；缺日不前填，不据复权因子反推行动事实。

因子覆盖通过 `--factor-inventory FACTOR_INVENTORY.json` 传入，schema为
`ad.existing-factor-inventory.v1`，绑定universe_sha256；codes逐股提供covered_through、basis和来源证据。
未提供或不含某股覆盖时明确延后，不能把库存未知当作从2010年重新全采的理由。
入口输出逐字段 `field-reconciliation.jsonl.gz` 及汇总；拆分子窗口按同一缺口合并对账，
保留已覆盖字段、研究派生值、单位待核、空值、截断、失败、主体隔离和事件歧义。
只写独立源分区和协调清单；正式统一库导入仍需审定结构及与日线分钟任务协调。

官方依据：[利润表](https://tushare.pro/document/2?doc_id=33)、
[资产负债表](https://tushare.pro/document/2?doc_id=36)、[现金流量表](https://tushare.pro/document/2?doc_id=44)、
[财务指标](https://tushare.pro/document/2?doc_id=79)、[分红](https://tushare.pro/document/2?doc_id=103)、
[复权因子](https://tushare.pro/document/2?doc_id=28)。文档中分红起始日的概述与示例不完全一致，
实际最早覆盖须以本批回执统计为准，不能由文档概述填补缺失。

### 运行中监督、缓存回放和来源交接

`scripts/run_tushare_reference.py --coalesce-dividends` 可在原采集器释放写锁后继续同一批次。
它保留冻结任务清单和已有成功/已捕获回执，把同一证券的多个待查询分红日期合并为一次字段并集请求。
`dividend-coalesced` 分区保存真实请求、完整返回版本及原任务映射；验证证券、字段、日期和原事件锚点后，
在原任务下写入明确标注 `source_reuse` 的本地投影回执。原始响应以硬链接共用，日期任务不伪造网络调用。
返回达到2,000行、声明 `has_more`、字段不足或事件未匹配时保留原日期请求。
日期、登记日、税前现金均匹配且其他非空锚点不冲突时，可复用该日期的查询结果；
源送转字段为null不会补成0，严格跨源事件配对仍返回未解决，并记录 `unresolved_event_bindings`。
同日多版本全部保留，范围外新事件也保留在独立源分区；不会通过选择最新一行消除歧义。
每50次源请求后的写锁边界应用复用结果，`reuse-summary.json` 区分已避免的日期请求、真实网络尝试及净减少量。
续跑先复用成功响应，保留失败尝试和回执快照；该模式保留失败临时文件，不自动清除既有临时文件。

`scripts/watch_reference_expansion.py --spec PLAN.json --root FOLLOWUP_ROOT` 只读取显式批次，
不读取凭据、不请求网络、不重启采集器。每30秒检查写锁和源状态；旧的supervisor或worker-exit
必须与当前progress的PID和时点相符，不能据旧结束文件改在途回执。
源自然结束后才重放 `parse_failed` 缓存并校验完整性；不会用 `--all-captured` 重写已审核派生物。
巨潮源遍历结束后逐份校验原始响应和已审字段，主体冲突、分页变化和缺页继续单列。

计划可指定Tushare与zzshare的交接。监督器只观察连续采集中的正常结果：Tushare工作进程活跃、
沪深日历源批结束、最早和最近的计划交易日均返回两市正上下限价且没有当前源封禁/冷却后，
写入带证据的zzshare停止文件；原请求保存后停止。既有用户STOP绝不覆盖或移除。
这证明历史和近期接口可用，不证明全历史完整；Tushare继续主批和截断补采。
原zzshare未采任务保留原状态，ST/停复牌/复权因子及缺失限价日期仍为待核缺口，
必须先复用正式日线与既有因子、形成明确缺口计划，才能恢复必要补采，不能重新跑双份全量。
监督器自己写入的交接STOP如果后来被移除，不自动重建；来源已经自然结束时不再发停止指令。

正式计划指定 `finish_policy: current_batch_only`：只监督该计划的原采集及本次Tushare工作进程。
顺序执行的批次可用after和owner_pid绑定依赖；现有进程冷却时仍算在运行，不能据旧PID退出文件误判。
所有本批次来源关闭且Tushare工作锁释放后写 `completion.json` 并退出；权限阻断、用户停止、异常退出、
未配置凭据分别记录未完成，不改成采集成功。此后不等待未来凭据、不装系统常驻服务。
本地隐藏输入入口可通过 `--followup-spec PLAN --followup-root ROOT` 唤起同一有限监督器，
子进程使用显式最小环境，不接收token；不重启原网络采集器。
计划可指定gap_preview，在原公开源完成回放后离线更新最终缺口预览，即使尚无Tushare凭据也可完成。

监督计划也可绑定真实历史查询的预期状态和值，在启动及安全回放后复核。
`official_daily.reader(..., correction_reviews=...)` 接受显式更正范围核查，
逐项绑定官方索引原响应、原PDF哈希、审核页和具体字段×报告期。
仅对明确不受该更正影响的字段解除泛标题造成的阻断，不能扩大到其他字段或其他公告。

上述接口适配及合成协议回归不代表鉴权实采成功。账户未配置时仅完成代码、源契约、库存及请求预览；
鉴权实采、实际金额单位证据、各源全历史覆盖、稳定主体/换码映射与审定数据库结构导入仍分别验收。
`suspend_d`、VIP以及权限冲突的股本接口不按当前积分擅自启用。

### 与统一行情发布的接入接口

统一行情现有 `ad-market-release.v1` 使用不可变Parquet对象、SQLite元数据和固定清单，
`MarketRelease.read/sql` 目前只接受 `bars_1d/bars_1m`。财务源采集不能把这两个事实表当作通用入口，
也不能通过改写已发布元数据绕过新release。`merge_market` 已确认来源修订与解析版本分离，
事实context绑定 `report_version/extraction_version/context_id`；尚未定义下表的正式DDL和发布接口。
以下是具体接入契约，表名是待协调的逻辑名，不表示已建表或已导入。

| 逻辑表/接口 | 已有字段与必须保持的键 | 接入端需明确的部分 |
| --- | --- | --- |
| `meta.source_assets` / `meta.ingest_batches` | 原响应SHA、字节数、源定位、请求批次、捕获时间、parser/mapping摘要 | 复用现有asset/batch ID分配，外部原件原位引用及恢复策略 |
| 既有身份registry / `reference.security_bindings` | issuer、instrument、来源主体/代码、关系角色、有效区间、审核证据、registry/release摘要 | 财报绑定issuer，证券查询经有证据的issuer–instrument关系；不按代码或名称推定公司连续性 |
| `financial.report_versions` | issuer、来源报告/投影身份、内容摘要、report_type、报告期、源版本 | 表示一版内容；官方原文与供应商投影独立，披露事件和解析运行另有身份 |
| 披露事件 / `financial.visibility` | 事件ID、issuer、渠道、原生公告ID、内容版本引用、公告日期/时刻及精度、证据状态 | 复用等价事件登记，保留一次内容的多次披露；捕获时钟只属于回执，不冒充公开可见时间 |
| `financial.extractions` / 既有批次登记 | 内容版本、解析运行/批次、parser_sha256、mapping_sha256、schema/输出摘要、抽取版本 | 复用data_batches和batch_inputs；重新解析不产生新的报告内容或披露事件 |
| `financial.contexts` | report_version、extraction_version、规范化context_id；issuer、合并口径、instant或duration、YTD/单季、币种、单位、维度 | context由版本化规则生成，原始上下文另外保留；可内联并以视图暴露，不必新建物理表 |
| `financial.observations` | context、原字段名/标题、canonical_field、source_locator、ordinal、原值、decimal、单位/换算证据、缺失状态 | 现有唯一键只防同一抽取输出重复；不能充当跨来源、跨修订的经济事实去重键 |
| 来源/修订关系行 | 稳定link_id、明确端点、关系类型、字段/期间/issuer范围、证据、审核与生效边界 | 优先复用等价关系登记；影响PIT或替代选择的关系必须能逐行引用、查验，不能只放JSON数组 |
| `reference.exchange_calendar` / `reference.price_limits` | 交易所/证券、日期、来源版本、开市标记/原上下限价 | 来源优先级和缺口合并；缺限价不推出无限价或停牌，保留原日线状态 |
| `corporate_action.events` / `reference.share_observations` / `reference.factor_observations` | 事件日期角色、税前/税后、股份基数、来源精度、因子basis | 明确事件配对键；Tushare与Sina因子分系列，禁止拼接或猜补 |
| `financial.derived_indicators` | 公式及版本、输入事实ID、报告期、单位、可见依据 | 仅已核实基础科目派生的常用指标；缺输入、口径冲突或单位未知逐项返回 |
| `meta.coverage` / 候选发布 | 目标证券/日期/字段、成功/空/失败/截断/未采、逐字段对账、父清单摘要 | 发布器如何登记新增逻辑表，原子提交/回滚与读者固定版本协议 |

接入前可继续通过 `SnapshotReader`、`ReferenceReader` 和 `official_daily.reader` 使用各自已支持的源，
沿用其逐字段、日期精度和已核查修订边界。新增Tushare规范记录保留独立源分区和字段对账，
不能仅因API成功就让旧读者把未支持schema作为已有能力。正式接入验证须覆盖原件→规范字段→
目标表→公共读者的键/值/单位/版本对应及缺口数量；旧行情release和现有账户测试应保持可重现。
只处理已完成或已明确封闭的批次；仍在写入的分区不能冒充不可变全范围交付。

### 公司、版本和事实身份的审阅约束

这些是上述接入接口的语义约束，尚未建立正式表，也不以“必须新增几张表”为设计目标。
来源资产及回执复用 `source_assets`，采集/解析运行及输入复用 `data_batches`、`batch_inputs`，
证券ID、代码和已核别名复用 `release_instruments`、`release_identifiers`、`source_aliases`。
当前这些证券登记不等于已经有完整issuer登记；若缺公司节点或带时间的发行关系，只在同一registry
内补最少的类型和关系能力，不能另造一套公司主数据或把instrument_id改称issuer_id。

报告内容直接归属issuer；公司与A股、H股、旧代码、新代码或其他instrument通过registry关系关联。
证券入口需要核对关系角色、有效区间、证据及查询可见边界，才可选择公司的报表。
同公司多证券不会复制财报，代码复用、借壳或同名也不会自动合并公司；绑定未定时保留来源主体，
证券计算入口拒绝未经核实的关联。现有按security读取的有界账本继续遵守原契约，不冒充已完成此迁移。

内容版本、披露事件和解析运行各有身份。一版内容可在多个渠道或日期披露；同一公告也可带多个附件。
重复抓取增加捕获回执，不增加内容版本或披露事件；同原文换解析器只增加解析运行/抽取版本。
内容身份不含抓取时钟或parser，披露事件保留原生ID、渠道、日期精度和证据，解析运行绑定输入内容、
parser/mapping/schema及输出摘要。供应商字段投影是独立源版本；缺官方公告ID时不能编造等价关系。

报告到原文/回执/披露事件的source links，以及更正到前后观察的revision links，须提供稳定可引用的关系行。
复用等价通用关系表或受同等约束的关系数据集即可，无需按财报供应商复制表；仅从未约束的JSON数组
临时展开不算完成这项要求。端点类型与存在性、重复关系、字段/期间/主体范围、替代时间与无环性必须验证。
`replaces`、范围未定更正和已核无关更正不能混为一种关系；未知范围继续阻断相应PIT选择。
跨SQLite与Parquet的引用由发布前校验器及固定清单检查，不宣称存在跨引擎SQL外键。

`(report_version_id, extraction_version_id, context_id, source_field, source_locator, source_ordinal)`
是**抽取输出唯一键**：它防止同一输出被重复导入，允许同一经济事项出现在多个原文、比较列和修订中。
issuer＋字段＋期间也不能单独去重。经济事实匹配还需合并口径、期间类型、YTD/单季、币种、单位、
维度和栏目角色，并受版本及披露关系约束；匹配候选组不是唯一性证明。已确认同义或替代关系通过关系行表达，
不删除原观察，不用“保留最新行”代替版本选择。

context由版本化规范规则确定生成，包含issuer绑定、报表类型、范围、期间类型及起止日、basis、栏目角色、
币种/单位状态和规范维度。维度键有稳定顺序、类型和重复键检查；未知、缺失及不适用显式区分。
同一规范输入产生同一context_id；同一复合context键出现不同属性必须拒绝。保留原始上下文文本，
不能从标题相似推定相同口径；内联列加受校验的contexts视图可满足此契约。

观察分别保存 `raw_value_text`、精确 `value_decimal_text`、原单位/规范单位及换算证据、
`canonical_value_decimal_text`、值状态与单位状态。原空字符串、缺字段、null、解析失败、零各自保留；
未知单位不抹掉可解析的源decimal，也不产生规范数值。不默认元、不转DOUBLE、不以0代缺失；
规范换算必须保留比例与精度并通过无损往返核对。派生常用指标仍只消费口径相容且有可见依据的输入。

## 失败缓存的离线恢复

`scripts/repair_source_cache.py prepare` 接收独立新 `--root`、原 `--financial-batch`、
`--official-batch`，以及显式重复的 `--financial-task` 和 `--official-symbol`。
只读取已有响应及其回执，校验请求、范围和摘要；不创建客户端、不下载、不改活动源批次。
财务原文采用同盘硬链接，规范结果可直接由 `SnapshotReader` 读取。
`--financial-task` 也接受 `sina_structure` 任务。股本页中带明确“万股”单位的负数
单元保留原字符串、表格位置和 `negative_source_share_count` 原因，该规范字段及其
精度为未知；同列其他有效字段、其他历史列继续解析。源页标为 `partial_source`，
`unparsed_records` 和逐列 `field_gaps` 明示缺口。既不取绝对值或置零，也不从
总股本、限售股、高管股或另一接口回填这一源字段。单位不明、表格错列和身份不符
仍然拒绝解析；此前成功的正常列输出保持不变。

`scripts/audit_share_structure.py --batch STRUCTURE --float-batch PUBLIC --output NEW_JSON`
只读诊断已捕获失败页，可用带时区的 `--captured-before` 固定某次检查范围。
按“总股本等于限售股”“股份分类扣减关系”“尚不能解释”分组，在各显示值半个
最小单位之和内区分舍入误差，逐项绑定原回执及响应摘要。已有流通股序列只按完全
相同日期作独立记录比较；同一供应商的两个接口不算独立供应商确认，算式吻合也不
证明高管股与限售股互斥。上市前记录仍受既有证券存续期和PIT读取约束。
`source_repair.recovered_announcements(root, security=...)` 按公告实际返回代码提供恢复索引，
同时返回每类查询的缺口。共享机构ID不证明上市历史连续；新旧代码不自动合并。

总数漂移或缺页只使对应查询不完整，合法的已捕获记录仍可使用。重复公告保留每次来源位置；
同日跨页重复可由现有主查询和日期查询的并集核对，只有稳定总数、连续页及去重后数量均吻合才闭合。
同ID内容冲突或不符主体的行只隔离受影响记录。恢复索引完整也不证明历史修订齐全或精确PIT可见性。

发布采用独立暂存目录和原子改名；`--retain-temporaries` 保留失败暂存和临时文件供审查，
默认失败自动清理。`source-receipts/` 封存被修复的完整原回执，接纳后仍可核验原始失败。
`repair-manifest.json` 绑定原批次、回执、原文、
解析器和产物摘要。`apply-idle` 仅在取得原批次独占写锁后幂等接纳；不完整官方索引留在恢复批次，
不以文件存在冒充原批次完成。现有有限跟进计划可在对应任务配置 `cache_repair`（`root` 与
`manifest_sha256`），等原采集自然结束后接纳完整修复并核验；不增加采集进程或常驻服务。

### 负股本单元的显式点值恢复

`scripts/review_share_structure.py prepare --root NEW_REVIEW --structure-batch STRUCTURE --float-batch PUBLIC`
只读已经捕获的股本页、流通股序列及冻结证券范围。清单逐字段区分上市前、代码存续期内和身份未确定；
源代码上市时间及已知换码事件用于此分类，不声称上市前同名主体或历史主板成员资格已经核定。
原表负值、规范字段未知及既有回执保持不变，新清单单独绑定两份回执、原响应和具体源观察。

部分新浪股本页同一列内的图表XML显式给出正的流通A股值。恢复只解析原HTML中的惰性属性，
不运行Flash或发起请求。param与embed必须一致，证券及日期链接必须吻合；其他图中分类须与
表格明确的万股单位及原值对应，全部图中分类之和须与总股本在显示舍入界内相符。
图中流通A股还必须与另一已采接口同日、唯一记录的值精确一致，才能建立点值恢复关系。
不能由总股本减限售股、高管股或A/H股混合分类计算替代值，也不推断缺失的零值或日期。

```python
from ashare_data.financials.share_resolution import ShareResolutionReader

reader = ShareResolutionReader("NEW_REVIEW")
value = reader.read("001226.XSHE", "2026-06-30", purpose="listed_security",
                    as_of="2026-10-20T00:00:00+00:00")
```

该入口显式返回已采流通股接口的值及图表核对证据；每次重验两份源回执、源观察、日期和图中证据。
`as_of` 必须晚于两次捕获且满足既有公告、代码存续期约束；不允许历史PIT、跨日期前填或隐式换源。
同一供应商的多种表示不等于独立交易所历史确认。没有同日记录、图中分类不能闭合或单位不明时，
保留未解决点；这些点的流通市值、换手率分母及流通权重仍不可由此恢复。其他有效字段按各自读取契约使用。
`purpose="source_audit"` 可查看原负值和恢复关系，但不授予计算使用权。此清单不是正式表或全历史覆盖声明。

覆盖报告必须同时列出目标窗全部负值、上市前、上市后已恢复、上市后未解决和身份未确定，
并区分字段数、不同证券日期数与不同证券数。例如本轮862个负值字段中，上市后583个＝恢复535个＋
未解决48个，另有上市前279个；本轮未恢复合计327个。原表负值字段仍全部保留为未知，
535仅表示经显式恢复入口可用的同日源点值，不能把48写成全体未解决总数或把原源覆盖改成全完整。
