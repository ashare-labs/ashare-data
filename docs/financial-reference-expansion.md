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
目标窗不纳入该月尚未来到的日期。完整自然日由源逐日返回，缺日、重复或未知标志直接拒绝。

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
总数、完成任务、空集未知、解析失败和待采分开；报告期缺口仅表示未观察到，不证明公司当时应披露。
`full_history_complete=false` 和 `historical_pit_certified=false` 不因批量任务完成而变 true。
正式日线库尾日是2026-10-14；财务抓取截至2026-10-19不意味着行情已更新。

## 下一阶段

完成各批后验收 3,426 个目标代码逐类覆盖、股本历史边界和深市月历，补公告索引与更正链，
再用已经可比且有完整期间输入的基础科目派生常用指标。官方全历史限价、完整停复牌/ST公告史、
两市官方全历史日历没有在本批取得，不根据价格、涨跌比例或缺行推导。
统一库尚待用户审定，批准后按确定键/区间契约接入，通过缺口验收再登记正式入口。
