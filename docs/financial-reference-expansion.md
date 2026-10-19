# 财务与交易参考扩采

这是独立的采集与交换层，尚未选择统一数据库引擎，也不修改正式部署清单。
目标是把免费公开源的可得观察扩展到已有历史证券范围，保留缺口，待统一 schema 审定后映射。
不能把供应商当下返回的历史值当作原始首次发布值；本入口不生成官方 FinancialLedger 包。

## 范围与复用

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

## 来源核实与权利边界

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

## 临时交换 schema

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

## 下游读取约束与异常归因

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
# 默认严格PIT：应返回 allowed=false，进程退出码2。
PYTHONPATH=src /Users/yinming/Workspace/ashare-local/.venv/bin/python -B \
  -m ashare_data.financials.expansion_access \
  --root /Users/yinming/Workspace/ashare-data-local/financial-reference/batches/20261019-public-v1 \
  --task sina_income-sh600176 --period 2025-09-30 --field net_profit \
  --as-of 2026-10-19T12:30:00+00:00

# 只有明确选择当前源研究、且查询在绑定证据捕获之后，才返回已复核字段。
PYTHONPATH=src /Users/yinming/Workspace/ashare-local/.venv/bin/python -B \
  -m ashare_data.financials.expansion_access \
  --root /Users/yinming/Workspace/ashare-data-local/financial-reference/batches/20261019-public-v1 \
  --task sina_income-sh600176 --period 2025-09-30 --field net_profit \
  --purpose source_research --as-of 2026-10-19T12:30:00+00:00
```

## 运行、断点与验收

使用正式安装环境的 Python，并通过独立工作树 `PYTHONPATH=src` 载入新增模块；不改正式安装包。

```sh
PYTHONPATH=src /Users/yinming/Workspace/ashare-local/.venv/bin/python -B \
  -m ashare_data.financials.expansion prepare \
  --root /Users/yinming/Workspace/ashare-data-local/financial-reference/batches/20261019-public-v1 \
  --daily /Users/yinming/Workspace/ashare-data-local/daily.sqlite3 \
  --start 2010-01-01 --end 2026-10-19

PYTHONPATH=src /Users/yinming/Workspace/ashare-local/.venv/bin/python -u -B \
  -m ashare_data.financials.expansion run \
  --root /Users/yinming/Workspace/ashare-data-local/financial-reference/batches/20261019-public-v1

PYTHONPATH=src /Users/yinming/Workspace/ashare-local/.venv/bin/python -B \
  -m ashare_data.financials.expansion audit \
  --root /Users/yinming/Workspace/ashare-data-local/financial-reference/batches/20261019-public-v1 \
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

## 下一阶段

完成各批后验收 3,426 个目标代码逐类覆盖、股本历史边界和深市月历，补公告索引与更正链，
再用已经可比且有完整期间输入的基础科目派生常用指标。官方全历史限价、完整停复牌/ST公告史、
两市官方全历史日历没有在本批取得，不根据价格、涨跌比例或缺行推导。
统一库尚待用户审定，批准后按确定键/区间契约接入，通过缺口验收再登记正式入口。

## 逐字段历史证据与当前查询范围（2026-10-19续作）

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
| `source_research` | 三表8个映射金额及参考事实；字段缺失/未核实未来日期等逐条限制 | 当前捕获快照，不能声称当时投资者已知 |
| `listed_security` | 上项加原代码上市生命周期过滤 | 不延长旧代码，不把发行人延续视为股票延续 |
| 现有官方ledger研究模式 | 已有18份报告、66条观察形成46个证券×字段×期间可选结果 | 官方索引日期次日的显式研究约定，无精确首发证明 |
| `historical_pit` | 有可信发布与版本绑定的具体字段可通过；目前正式扩采根没有这样的真实绑定 | 严格截止时刻选择，缺少证据逐条拒绝 |

现有官方小包只读验证了48个源文件；46个结果在研究模式可读，严格模式均为
`visibility_unproven`。其中仅000637和603169属于本次主板基线，300124/300630仍属于既有样本，
没有扩大本次范围。新增正例测试使用合成证据，不能算真实PIT覆盖。
现有官方ledger桥接支持5个合并YTD金额（营业收入、营业总收入、净利润、归母净利润、经营现金流），
资产、负债、期末现金余额尚不在该严格ledger字段集内。没有将日期补成午夜。

## 历史限价与参考观察独立批次

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
不使用新账号、积分、代理或付费来源；已核实Tushare历史限价有积分要求，因此没有调用。

```sh
PYTHONPATH=src /Users/yinming/Workspace/ashare-local/.venv/bin/python -B \
  -m ashare_data.financials.limit_expansion prepare \
  --root /Users/yinming/Workspace/ashare-data-local/financial-reference/batches/20261019-limits-v1 \
  --baseline-root /Users/yinming/Workspace/ashare-data-local/financial-reference/batches/20261019-public-v1
PYTHONPATH=src /Users/yinming/Workspace/ashare-local/.venv/bin/python -u -B \
  scripts/run_reference_expansion.py --provider zzshare-reference \
  --root /Users/yinming/Workspace/ashare-data-local/financial-reference/batches/20261019-limits-v1
```
