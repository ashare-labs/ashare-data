# 选择来源与读取固定捕获

默认 Client 只使用新浪。下列提供者必须显式选择，不自动回退、跨源拼接、补数或认证交易资格。来源权利见 [来源与许可](sources-and-licensing.md)，原文、回执和数据另行保存。

## CMES 免费更新候选

`free-updates` 保留已确认菜单的 198 个可尝试条目及原 Excel 行号。可选 SDK 使用
`free-updates` extra 的 AKShare 1.20.2；每个模块核对固定源码摘要，未安装的可选
JavaScript 引擎只阻塞需要它的条目。直接公开请求、源码来源和许可随包保存。

```sh
ashare-data free-updates catalog
ashare-data free-updates plan --rows 17 18 19 --context scope.json
ashare-data free-updates collect --root captures/cmes-free --rows 17 18 19 --context scope.json
ashare-data free-updates query --root captures/cmes-free --row 17 --limit 5
ashare-data free-updates report --root captures/cmes-free --output acceptance.json
python scripts/verify_free_updates.py --root captures/cmes-free --output captures/cmes-free/verification.json
```

`scope.json` 可以是 `{"stock_upper":"SH600000"}`，也可以是多个明确范围对象的列表。
显式范围只证明这些证券或板块的样本；缺少参数保留为待处理项，不当作全市场覆盖。
数据目录必须为空或已有本功能标记，拒绝生产 CURRENT/releases。命令不设置定时任务，
不发布生产数据。已实现并验证数量与每日部署数量分别输出。

每次采集保存原始响应、哈希、接收时点和版本摘要；空表、日期陈旧、校验失败分别记录。
请求有预算、超时、限速和持久冷却，401/403/429 不做认证绕过。相同观测重跑去重，
修订保留为新版本；`query --as-of` 根据实际接收时点读取，返回请求范围和来源，
不会把财报报告期、只有日期的公告日当成历史日内可见时刻。

验证脚本把真实捕获复制到临时隔离目录，模拟超时后恢复并重放相同结果，检查原文哈希、
去重、读回及已审核的字段单位和业务主键。模拟故障明确标为离线测试，不算新的真实请求；
未审核口径的条目继续待验收。源统计期、公告日期、当前快照观察日期分别保留。
腾讯日线候选的成交额为空，手数转换后的股数保持源精度限制，不跨源补值。

十大股东与十大流通股东使用同供应商的 `RPT_DMSK_HOLDERS`、`RPT_F10_EH_FREEHOLDERS`
报告，保留原生全部字段、证券市场、`END_DATE` 报告期和 `NOTICE_DATE` 公告日。
`UPDATE_DATE` 单列为源更新日，不能替代公告日。请求范围如
`{"stock_prefixed":"sh600000","report_date":"20260630"}`；逐页核对总数并拒绝串证券、
串报告期和重复业务键。持股数单位为股，两个比例字段分别是总股本、流通股本百分比。
部分个人股东缺少来源编号，原样保留；股东名称只用于同证券同报告期内去重，不作跨公司身份。
报告期内修订与回退均保留实际接收记录。全部当前股东列表通过不代表全市场或全部历史通过。

代码版本变化后，旧回执与验收仍绑定原版本，不通过改写摘要继承新版本验收。
跨版本交付表须逐行标明实现版本、证据目录和覆盖范围；固定运行环境不会自动采用工作树改动。

采集可使用 `--workers 2` 同时处理独立来源，同一来源族串行。`--max-jobs` 只计新尝试，
续跑会越过当日已成功的作业；默认每作业每日最多两次失败尝试，限速和错误回执在超时后仍保留。
子进程有独立截止时间，中断后先等旧作业截止，再按已保存范围继续。中断不记为数据源失败。

`run-scheduled --config schedule.json --dry-run` 只读预演一次更新，省略模式也默认预演。
显式 `--execute` 才采集；不安装定时器、不发布正式数据。配置示例：

```json
{"schema":"ad.free-update-schedule.v1","root":"observations",
 "rows":[137,196],"context":{},"workers":2,"timeout":60,
 "max_jobs":20,"attempts_per_day":2,"lookback_days":14,"overlap_days":7}
```

`root` 相对配置文件解析，已有正式目录被拒绝；行情/证券/板块范围须显式给出。
断更会按上次成功日期扩大重叠窗口，超过一年须先做带日期的分批回补。
初次完整历史及全目录覆盖是独立验收项，不能用该默认窗口推定已完成。
当前快照按实际接收日期保存，历史 `as-of` 参数不会把今天的成分伪装成历史成分。

新浪保险、运输和商品零售序列采用显式分页，结果附来源总量、页偏移、下一页和字段单位。
通过 context 的 `macro_offset` 选择续采页；单页成功不代表全部历史。
全国 CPI 环比/同比已显式映射到东财 `NATIONAL_SEQUENTIAL`/`NATIONAL_SAME`：
原候选和替换原因仍在清单中，统计月与发布日期分开，预测值和缺失公告日期不补造。
年度数据按年度统计期核对新旧，金十空实际值的预测行不用于提升新鲜度。

`scripts/report_free_update_coverage.py` 导出原始 198 行、当前样本范围、采集与最新数据日期、
逐项完整覆盖缺口和阻塞类别。401/403 只证明接口拒绝，不能据此认定用户需要购买权限。

宏观审核保留统计期、发布日、修订版本和原始单位。工业增加值的缺失单月值不以累计值填充；
GDP 单季度同比使用国家统计局发布表中的单季度不变价增长率，按官方目录发现最新季度，
不与年内累计同比或季调环比混用。官方序列本次修订的旧季度值带本次发布日期，预测值为空。
金十、东财来源映射变更记录在 `source_selection`，原 Excel 六列与行号保持不变。

上海金、银基准价按源毫秒时间戳的北京时间交易日对齐早盘与午盘，单边缺值保持空值。
价格单位分别为元/克、元/千克。旧 SDK 的前一日日期投影及未筛选的中债混合曲线保留审计，
默认查询排除；`query --include-superseded` 可显式读回，结果标记 `superseded_projection`。
中债国债条目只返回“中债国债收益率曲线”。人民币中间价按外管局定义分别保留每100外币的
人民币值与每100人民币的外币值，不把所有币种套入同一方向。

国房景气指数已获国家统计局确认停止发布，清单保留为受阻及历史可读，例行计划跳过该项；
不自动用房地产投资、销售或其他指数替代。每个停发判断带官方说明、确认日和检查日。

北京碳市场、同花顺资金流以显式 `source_page` 分页，页码、下一页及源单位保留。
移动分页须回扫日期重叠区间；同花顺拒绝访问时停止。上交所 ETF 日报允许五日内寻找已发布
统计日；深交所当前份额按实际接收时点保存快照，未提供的份额统计日保持空，不以上市日替代。
CNBS 官方目录指向的单个下载 URL
已受限审核；urllib 方式的表格读取同样遵守证书、预算、冷却和原文捕获。

真实样本的离线验收另测试重叠批次、零重复写入与故障恢复。它不证明跨真实发布日期运行，
全目录版本化、跨午夜稳定的分片队列和单 writer 发布接入分别验收。定时器由固定版本的独立
配置管理，启用状态及自然触发记录以具体部署回执为准。

## baostock

安装 local-data extra 后使用固定 `baostock==0.9.4` 官方 SDK；AD 校验源码指纹，不捆绑 SDK 或读取凭据。每次单证券/一类业务、匿名单连接、独立子进程，无重试或换端点；与其他采集串行使用，本机锁不约束其他软件。

```python
from ashare_data import BaoStockSource, Store

store = Store.init("data/bao-new")  # 已有目录用 Store(path)
result = BaoStockSource().get_price(
    "600000.XSHG", store=store, start_date="2024-10-08", end_date="2024-10-09",
)
capture_id = result.report["capture_id"]
view = store.baostock(capture_id)
print(view.get_price().data)  # 固定版本，离线
```

| API | 边界 |
|---|---|
| `BaoStockSource(sdk_path=None,timeout=15)` | 当前环境 SDK 或显式官方源码目录 |
| get_price / get_security_info / get_trade_days | 显式采集并返回来源结果，不提升历史资格 |
| fetch(store,kind=...,...) | 保存原始请求、响应和回执，返回 capture_id |
| `Store.import_baostock_capture(directory)` | 离线验证 SDK/协议/请求/时钟/摘要，幂等导入；不执行输入目录代码 |
| `Store.baostock(capture_id)` | 完整 SHA 固定读取；descriptor/lineage/quality/coverage 保留证据及未知 |
| baostock_snapshots / recover_baostock | 本地列表/显式恢复，损坏 prepared 转 aborted，不重采 |

日线最多31自然日；分钟支持5m/15m/30m/60m、最多2自然日，没有1m。价格 Decimal、成交量整数股、金额元，原价与源前收分开。失败也保存 capture_id 与诊断，不能把失败当成功空集。证券资料保留原生 type 数字代码，1/2/3 之外的合法代码列入 `unmapped_basic_type_codes`，不据此开放行情查询；status 仍须为0/1。ipoDate/outDate 非空须严格日期且顺序合法，空日期保持未知，不生成无穷退市日。

底层 type14 分红、type16 复权因子允许字段/长度/终止符/CRC/请求身份/终页都验证的原生空段，记 complete_empty_unknown；并不证明无事件、完整历史或严格准入。type96 压缩帧外层完整性未知不因独立行解码而消失。实际接收、离线重放和历史可见时间分开。

## 知识时点

Bao 分钟原始17位标签保留毫秒，按 Asia/Shanghai 解释；bar_start/bar_end/available_at 未证仍未知。

| 操作 | 行为 |
|---|---|
| `view.get_price(end=...,end_inclusive=True)` | 按源标签≤上限；False 为严格<，参数须真实 bool |
| `view.at(as_of,visibility="source_label",inclusive=True)` | 固定标签研究上限；后续查询/lineage/coverage 取所有上限交集，不放宽 |
| `source.get_price(...,end=...)` | 起止日期仍控制整日捕获；end 只筛返回，非法上限在联网前拒绝 |
| received | 仍检查完整接收/存储可见性；现有 type96 的外层未知会拒绝，不回退标签模式 |
| verified | PIT_UNAVAILABLE |

上限须带时区、秒精度、最多6位小数的 datetime/ISO；纯日期、无时区及隐式截断拒绝。窗口前上限返回空表，窗口后返回现存行，均不推断市场不存在数据。selection 单独记录上限，原 query_complete 不随筛选升级；筛选 lineage 不泄出窗口外原行，审计完整回执须另开原视图。标签筛选不是恶意同进程策略沙箱。

状态、源前收、源日历与邻接日的 typed API 和错误见 [参考数据](reference-data.md)。CLI 使用 baostock-fetch、baostock-import、baostock-query；示例见 `examples/baostock_demo.py`、`examples/baostock_minute_demo.py`、`examples/baostock_cutoff.py`。

## tdx-分钟柱

`TDXBarSource(host=...,python_executable=...)` 使用调用方已有 eltdx 3.0.3 环境、明确 TDX 节点的 0x052d 原生1m/5m OHLC/量额。没有自动安装/探测节点/换源、拆5m为1m或重建 OHLC；离线 TDXBarView 不需要 SDK。

```python
from ashare_data import TDXBarSource, TDXBarView

source = TDXBarSource(host=chosen_node, python_executable=existing_sdk_python)
captured = source.fetch("000001.XSHE", directory="captures/new",
                        frequency="1m", count=800, max_pages=1)
view = TDXBarView("captures/new", expected_sha256=captured.manifest_sha256)
result = view.get_price(mode="retrospective")
```

单证券/节点/连接，间隔至少1秒；每页≤800条、每次≤32页/25,600条。start_offset 为0..65535协议偏移，不是日期；按实际行数推进，短页继续，空页/重复交叠/反向/权限/限流/错误停止。next_offset 不保证新的盘中窗口无缝。stop_reason 区分请求条数、本地页限、协议边界和来源错误，complete_history 始终 False。

不足 count 默认 TDX_PARTIAL_CAPTURE，可显式 allow_partial=True 读已接受页；拒绝页原文保留但不进入结果。start/end/最多1000个 labels 只筛已捕获源标签，不额外翻页；分钟上限带时区且含端点。价格原整数毫元转 Decimal，volume 用协议股数并保留 volume_lots，金额用源独立字段，不乘价造额；上游编码精度边界保留。

新目录保存请求、逐页 bin payload、SDK 字段、源码/native二进制 hash、接收时钟、分页日志与 manifest。它不是完整 TCP 抓包；证券/偏移身份依赖请求和 SDK，不是服务端独立回显。读取重验摘要/行数/拼接/时序，不认证历史发布时间、状态、公司行动、闭合或成交能力。

## tdx-点价

`Store.import_tdx_points(TDXPointInput(**source_receipt),source_kind="local_tdx_history_capture")` 只导入已保存的 eltdx.minutes.history(include_raw=True) JSON。需证券、日期、绝对源路径/SHA、节点、真实捕获起止、decoder版本/提交/native二进制摘要；单日≤240点/1MiB。导入核对原始整数、差分累计价格、记录数及序号，测试另标 synthetic_fixture。

`store.tdx_points(id).read(security,at=...,role="retrospective_point_research")` 只取精确标签；不存在报 NO_EXACT_SOURCE_POINT，不取最近值。序号0→09:31、119→11:30、120→13:01、239→15:00；无09:30/13:00/午休点。源不回显证券/日期/逐点时间，依靠请求和映射。

price 为元/股；volume_lots 整数手，volume_shares=手×100 不恢复不足一手精度；零量 executable=False，其余 None。点价没有 OHLC/金额/盘口、历史到达或已认证收盘语义，historical_pit/strategy_visible 均 False。

## 分钟会话

`TDXMinuteSessionView([TDXSessionSource(security,directory,manifest_sha256),...],execution_days=...,history_days=...,model="tdx-full-session-retrospective.v1")` 只读固定 TDXBarView，最多10股、31个选定日、63自然日包络。各声明日恰好09:31–11:30和13:01–15:00共240标签；缺行/错频/错股/hash拒绝，历史日须早于首执行日。

| read purpose | 显式模型时钟 |
|---|---|
| opening_price | 09:30起，只给09:31条 open 单值代理；不泄 high/low/close/量额 |
| minute_history | 标签≤as_of；历史预热不成为执行事件 |
| valuation_only | 15:00起，只给真实15:00条 close；不用14:56替代 |
| closed_daily | 15:00起，close取末条，量汇总全部240条含零量，并附原行hash |
| closing_auction_matching | 显式角色，保留14:57边界与14:58–15:00，逐条到模型时刻才可读 |
| jq_daily_initial_capacity | 显式引擎事后容量政策，可提前取同日 volume；不含日 close，strategy_visible=False/non_pit=True |

phase 分类仍 unverified，零量不删除或证明停牌。源 bar_start/bar_end/available_at 保持 null；普通日数据 replay_available_at 不早于15:00，容量另列 model_access_at。export 是引擎完整输入，不是策略历史快照。`examples/tdx_minute_session.py CONFIG OUTPUT` 拒绝覆盖已有输出；[分钟证据门禁](historical-market.md#分钟证据) 保持独立。

## zzshare

所有采集、导入、读取、恢复默认关闭，必须 enabled=True/enable_research=True；capabilities 可不启用查看。没有并入 Client 默认路由。

| API | 结果 |
|---|---|
| `ZzshareSource(enabled=True).fetch(store,security=...,start_date=...,end_date=...)` | 单证券、模式0、1～31自然日，一次匿名 HTTP，成功/失败都封存 capture |
| `Store.import_zzshare_capture(directory,enable_research=True)` | 校验 business.raw/receipt 请求和摘要，幂等导入 |
| `Store.zzshare(id,enable_research=True)` | 固定完整 SHA；get_daily/get_price 返回相同 typed 结果，descriptor/coverage/quality/lineage 为副本 |
| zzshare_snapshots / recover_zzshare | 显式启用的列表/恢复；prepared→published，旧发布不重写 |

strict=True 拒绝缺自然日/未知必需字段，False 可看部分数据；已知 OHLC 矛盾即使 partial 也拒绝。缺日不推断休市/停牌。数字保留原 JSON token 与 Decimal，不经 float64；未知字段也参与根深度≤64及有限 Decimal 检查。源 claimed limits 不是计算/认证价界，rule_derived_limits=None；因子只留原字段，无复权/as_of/分钟接口。

无重试或重定向；拒绝、限流（保存 Retry-After）、连接/截断/API/schema/身份错误明确区别。新捕获保留去敏感头的有序 response_header_pairs，任何重复 Content-Length 或长度不符拒绝；旧字典回执标记重复头不可知，不伪造证据。发布成功仅是封存，失败 capture 的 daily 查询仍抛原错误。

当前 projection-2 与 research-1 清单分开：同原件保持同 capture_id，复现还需固定 wheel。historical_available_at=None，PIT/完整覆盖/事件覆盖/执行许可均 False。CLI 为 zzshare-import/fetch/query/snapshots/recover --enable-research，最小示例 `examples/zzshare.py`。

### Free-source frozen queues

`ashare-data free-updates prepare-queue --config queue.json` is read-only. Add `--save`
to persist a content-addressed plan inside the isolated observation store.
`ashare-data free-updates run-queue --queue <saved-json> --execute` processes the bounded
job budget and resumes exact job IDs across days. Successful jobs are skipped;
untried jobs precede retries; failed-attempt limits use the actual Asia/Shanghai
execution day. Source cooldowns and the single store writer still apply. No
timer or production release is installed.

The config schema is `ad.free-update-queue-config.v1`, with `root`, explicit
`rows`, `as_of`, `lookback_days`, and either `context` or `context_file` plus
`context_sha256`. A context file is an `ad.free-update-universe.v1` manifest
containing `contexts`, `scope`, `source_receipts`, `observed_at`, and
`coverage_limitations`. A changed manifest or implementation needs a new queue;
old queues and evidence remain intact. Completion means every frozen job was
received, not that a supplied manifest proves the entire market or history.
Every CMES catalog row now carries a separate `collection_goal`; the six
original spreadsheet columns remain unchanged. Discontinued indicators retain
their menu identity and official evidence and are excluded from all plans.

Security event adapters preserve raw security codes and source dates, read all
reported pages within an explicit bounded window, and reconcile source counts.
Dividends retain amounts per 10 shares. Block trades retain multiplicity of
identical source dimensions and rounded source volume/amount precision; their
occurrence labels are not exchange trade IDs. LHB arithmetic exceptions remain
visible quality gaps and cannot earn core acceptance. SooZhu source product
specifications and per-head/per-kg units are persisted separately; its recent
rolling window does not provide unlimited historical recovery.

### Macro history coverage acceptance

Reviewed direct macro reports accept explicit `--source-start`, `--source-end`
and `--page-size` on `free-updates plan/collect`. Source date filters use the
reviewed report date field (LPR uses `TRADE_DATE`). Every page must agree on the
source count; empty or repeated pages, ignored date filters and conflicting
periods fail. Industry indicators carry the exact returned ID and name. Exact
duplicate industry projections are counted explicitly before collapse; original
responses remain immutable. Unknown units or a narrower insurance definition do
not earn core acceptance.

`scripts/verify_free_update_history.py --root <isolated-store> --rows 156 170 174 176
--as-of YYYY-MM-DD --seed-end YYYY-MM-DD --delta-start YYYY-MM-DD --output proof.json`
previews a five-stage trial. Add `--execute` to collect seed history, an unchanged
seed repeat, an overlapping later date window, full source history, and a second
unchanged full repeat. A trial uses a fresh child store with stage-specific
checkpoints, so restarting skips completed requests. The overlap and new rows
must reconcile exactly with the full source and with received-by queries. Source
revisions during the trial leave it unverified instead of hiding differences.

Coverage reports revalidate raw response hashes, exact request URLs, pagination,
database links, semantic checks and current implementation identity. A report's
`coverage_verified` is separate from sample `implementation_verified` and from
`daily_deployment`. Historical window extension uses real HTTP requests but is
not proof of a future release or an operating daily timer. For the small monthly
reports, a daily full-history read is bounded and also catches older revisions;
content deduplication avoids manufacturing daily statistical observations.

### Isolated daily staging and publisher handoff

`free-updates prepare-handoff --root <observation-store> --rows ...` previews a
bundle from current, accepted, complete macro captures. `--execute` atomically
saves it under that store's `outbox/`; `--previous <bundle-id>` compares snapshots
by business key. New periods, revised values and reversions are upserts. Removed
source periods require review; no historical deletion is generated. Bundles keep
native source units and their own history bounds, not a claim of CMES equivalence.
Manifest, snapshot, delta, source receipts and compressed bodies are hash checked.

The schedule config may set `prepare_handoff: true`. After a successful bounded
collection, it resumes the last prepared bundle checkpoint automatically. Failed
jobs give a nonzero CLI exit and prevent export. The checkpoint acknowledges only
local preparation, never production publication. A launchd plist can be rendered
with `free_update_handoff.render_launch_agent`; rendering does not load or enable
it. Use a pinned, verified installed wheel, and confirm the host clock's timezone.

`free-updates preview-intake --bundle <bundle-dir> --coordination <writer-json>`
only reads the bundle and existing writer's status. Production still needs an
explicit native-macro consumer in the existing writer, unit/namespace review and
its normal locked, expected-parent atomic release. These commands never write
the formal CURRENT pointer or start a second production publisher.

News capture preserves provider IDs and timezone-aware publication times. THS
epoch conversion is independent of the host timezone; latest-page retention is
reported explicitly. Latest news pages alone are not ready for complete daily
news delivery until paging/cursor and missed-interval coverage are verified.

### 原生宏观数据的公共发布与查询

`native-macro prepare` 将已验交接包重放原始 HTTP 响应，核对统计期、单位、业务键和全部可见分页，再在发布根以外准备不可变候选。候选绑定父 release ID、manifest SHA 和消费实现身份；父版本或代码改变必须重新准备。宏观序列有独立的 `eastmoney_datacenter` 来源和原生 series 身份，不分配股票 instrument ID，也不填入 CMES 股票行情或财务标准字段。

消费 API 位于 `storage.native_macro_release`。现有控制器在本批发布和读回完成、没有 `.work/import.json` 或 `prepared.json`、已经持有 driver 和 writer 句柄时，才能通过 `controller_boundary`、`compose` 和 `ReleaseWriter.publish(..., native_macro_boundary=permit, expected_parent_sha256=...)` 追加。根 writer 锁空闲不授予权限。没有可直接发布宏观数据的 CLI；消费者和定时器的部署需单独协调。实际分类目录沿用 `data/<schema>/<table>`、`metadata`、`releases`、`.work`。

公共查询为 `MarketRelease(...).native_macro()`，也可使用 `ashare-data native-macro query/point`。CLI 必须显式提供 release 和 manifest SHA。查询指定原表行号、原字段及统计期；`received_by` 只表示本地实际接收时点，不能当作首次公告可见时间。源修订及改回旧值各保留版本，旧 release 和已有行情/证券身份保持不变。无变化交接只追加采集证据和计数，不重复写宏观事实行。

`scripts/verify_native_macro_release.py` 在新的隔离验证目录中，将明确提供的两份真实采集交接包从候选、公共发布、原子指针恢复验证到 CLI 查询。宿主发布及行情哨兵是合成夹具，宏观数据和回执来自输入包；它不联网，也不操作正式根或加载定时器。

新增的月度原始表契约保留新浪四张表的原始字段、单位和统计月，遇字段或单位变化即拒收；同花顺人民币存款、贷款、股票筹资保留网页原始表头。存款页的“新增”表头实际对应余额，贷款页的“累计”列是余额；映射会明确标注，不改成当月或年内新增。当前月筹资是截至采集时的累计观察，缺值不补零。新浪黄金数量为万盎司，东方财富黄金储备为亿美元金额，两条序列分别保留。

估值采集按来源证券身份和交易日保留全部原生字段，核对所选证券分页总数及流通A股的股数×收盘价与流通市值；负估值倍数保留，TTM与静态口径不混合，不把源修订值解释为历史当时可见值。全市场覆盖仍需目录验收。新闻联播按明确播出日目录核对全部文章ID和文字稿，任何缺篇都失败；播出日不是单篇精确发布时间。搜猪全国出栏均价只声明来源实际返回的近期窗口，不因页面标题“今年以来”推定全年覆盖。


日期审计先保留完整原始响应，再按字段角色检查日期，不截断或改写异常年份。通用实际统计期校验拒收未来日期；已有明确隔离规则的股票筹资与现货投影可保留有效行，同时在 `coverage.quarantined_rows` 保存异常原行、原因并核对总数。已核实的交易日历、发行申购日程、未结束周期上界以及带原生 A/E 标记的盈利预测分别标明日期角色；预告不被计为已发生事实。新浪现货原始行先保留再检查，避免 SDK 的 `dropna` 隐藏源年月错误；未明历史涨跌幅单位保留原值及说明。

盈利预测读取当前未筛选公司列表，用原生证券键排序并核对分页总数与证券唯一数；评级数量有大量并列，不能靠去重补救跨页漏股。每家公司逐个保留 `YEARn`、`YEAR_MARKn` 和 `EPSn`；A 为实际、E 为预测，缺值不填零。只有来源当前覆盖的公司构成该快照范围，不能推成全市场历史预测。

巨潮公告先从官方股票目录绑定代码和机构 ID，逐页核对 `totalAnnouncement`、`hasMore` 与实际条数。POST 回执同时绑定请求正文 SHA，避免相同 URL 的不同页、代码或日期窗口混淆。保留公告 ID、原始毫秒时间及上海时区日期；源午夜时间只有公告日精度，首次上网时刻未知。经完整响应证明的空窗口记为 `empty_source_window`，可断点跳过且不算独立真实样本通过。多证券验收要求每个请求窗口均验证，至少一个窗口有真实公告。

Shibor 保留全部八期限：利率为年化百分率，变动为 BP，按相邻日原始利率检查尺度。海关进出口当月和年内累计原始金额单位为千美元：源网页以 `zoom=-5` 转成亿美元显示，不能把显示表头直接赋给原始数值。`TRADE_BALANCE_100M_USD` 显式按 `(EXIT_BASE - IMPORT_BASE) / 100000` 计算当月亿美元贸易差额；同比、环比仍为百分比。此前版本的原始金额单位说明错误，原始数值未被改写，旧快照的单位标签不能继续使用。成品油历史调价单位为元/吨，各地区牌号价格及涨跌为元/升；使用字段名区分 92/95 号及前价，不按返回列顺序重命名。来源当前完整窗口不保证已包含每个官方最新调价事件，启用前须核实发布时效。

离线包验证可显式指定 `verify_wheel.py --wheelhouse <directory>`。只选择当前平台兼容且与 `uv.lock` 文件名、大小、SHA、包元数据和导出哈希都一致的 wheel，再以该文件唯一匹配的锁定哈希绑定本地 URI；`--require-hashes` 始终保留。不会删除原缓存、放宽哈希或使用未锁定依赖。

技术样本验收、来源返回窗口、按原发布频率的新鲜度、日常启用及正式发布分别记录在 `acceptance_dimensions`，统一天数阈值仅是年龄筛查。油价即使实现测试及完整来源窗口通过，仍明确标作“实现测试通过但新鲜度未验”；官方不调价窗口不能当作漏失的新价格记录。LPR 应分别检查 1 年、5 年实际非空列的起止期，辅助利率列的早期记录不是 LPR 历史。盈利预测同时统计至少一项预测 EPS 可用及全部预测 EPS 缺失的证券数。

巨潮公司概况和 IPO 日程按原生字段名重新解析，保留所有原始字段并核对 `count/total`、请求代码、POST 方法及正文哈希。注册资本是万元，成立日和上市日不代表资料更新时间；近三年 IPO 窗口不是全部发行历史。`F042N` 网上申购上限单位尚未证实，明确保留缺口，不擅自换算。可转债原生表的 `ONLINE_GENERAL_AAU` 是千元，与 `ISSUE_PRICE` 发行价区分；缺失行情不填面值。未来合同到期日、公布发行日程与实际观察日期分开，未核实的退市迁移或分离债身份阻止整批通过，不静默过滤。

`prepare_free_update_universe.py --kind stock-directory` 只从已有哈希校验的巨潮公告目录回执冻结全部 A 股目录身份；保留同一机构的旧、新证券代码，列出排除的 B 股和 CDR。目录缺少现役上市状态，不能称为当前全市场名单；据此生成的全目录采集计划仍需逐任务执行和验收。

房价与地区碳行情由 `free_update_regional_prices` 读取命名字段及完整分页/图表。房价同比、环比是基准 100 的指数，固定基期分别保留，缺失定基值不补写；湖北碳图表的空白日期仍入原始证据，未解释的 `zx` 不当作行情价格。来源窗口验收与最新官方发布对账分开，第二次真实读取零新增不等于已观察跨发布周期。

深圳融资融券报告的官方汇总适配器为 `szse_margin_summary`，需显式提供 `margin_date`（YYYYMMDD 或 ISO 日期）。它只接收官方 `1837_xxpl/tab1` 同日总量表的六个字段，保留亿元、亿股/亿份和两位小数，不恢复不存在的元级精度，不把基金份额统一写成股票股数。证券明细和完整历史单独验收；源返回空表不能自行认定休市。旧金十历史仍保留来源身份，不和新精度的值隐式合并。

新浪分钟适配器保留原生时间文本、周期及代码，严格校验 OHLC、重复标签和量。夜盘归属、区间起止和实际交割合约身份未确认时，`trading_date`、`timestamp` 保持空值；日线验证不能代替分钟语义验证。最近窗口的全部返回数据不等于完整历史。

`scripts/audit_futures_minute_alignment.py` 只重放已有回执，比较完整分钟区间的左右标签假设，按 Decimal 对账 OHLC、成交量及末持仓。未结束日期、窗口截断及含缺失分钟的区间单列；不补分钟、不修改原值，也不将样本吻合视为供应商时间定义。

搜猪省级排行榜用 `soozhu_province_rank` 读取原页面 `provincerank` 响应，保留源展示次序和缺失省份。地图接口的无序数据不再冒充排行榜。价格为页面所示元/公斤；原始变化文本保留，不标为百分数。页面更新时间由浏览器时钟生成，不能推成数据发布日期；快照沿用实际接收时间。来源当前列表覆盖不意味着全国各省齐全或历史排名完整。

`scripts/run_verified_free_updates.py --runtime-manifest <frozen.json> --boundaries <bounds.json> --output <reports>` 提供冻结入口的手动预览；加 `--execute` 运行一次。执行前检查 UID、全部包文件与配置哈希，通过独立安装解释器的 `-I` 模式调用采集器。成功后只从哈希已验证的完整交接快照导出逐行 CSV，单位和实际接收时间写入 `result.json`；失败返回非零状态并保留日志，不把旧导出当本次成功。该脚本不加载定时器、不处理权限弹窗、不正式发布。

对已核实完整来源窗口的条目，可用 `free-updates run-manual --config <profile.json> --execute`。配置采用 `ad.free-update-manual.v1`，显式列出 `rows`、`root` 和必要的 `context`，仅接受已审阅条目；房价必须提供两个城市参数均为“全部”。同一轮按来源节流和检查点续跑；18项、单并发、75秒/任务、每天最多2次尝试为上限。每项重新解析真实回执，核对日期、单位、业务键、总数及数据库读回，再独立生成不可变的本机快照和CSV。某项失败不会将其旧数据导出为新成功，也不阻止其他项读取；整体有失败时退出码为2。

边界记录的 `mode: manual-source-windows` 让冻结入口使用上述手动模式，不依赖或加载 plist。`free-updates manual-query --snapshot <snapshot-directory> --offset 0 --limit 1000` 返回固定快照的当前页及 `next_offset`；整个翻页过程固定同一快照ID，后来的修订不会混入旧页面。历史查询仍使用 `free-updates query`。来源全历史声明下旧键消失会拒收；近期滚动窗口移出旧键时只记录窗口变化，不删除历史，两个窗口不重叠时报告可能漏采。原始接收时间、统计期和正式发布日期各自保留；下一次真实发布尚未观察不能写成已观察。

`scripts/prepare_futures_universe.py` 按来源导航逐节点冻结期货目录和原始回执，断点续传须校验原响应哈希与节点身份。它不把供应商目录当交易所全部上市品种。只有 `futures-product-contracts.json` 已核报价单位、合约量纲的主连可以形成试采任务；未核品种留在目录，不猜单位。周、月以日线派生并重读当前未结束周期，主连与交割合约、换月和复权仍须分别验收。

本机验收只保留一个最新入口，由 `scripts/merge_free_update_acceptance.py` 合并显式逐行证据，再经 `scripts/build-free-update-acceptance.mjs` 更新原布局 XLSX，最后由 `scripts/export_free_update_acceptance.py` 导出 CSV/JSON 并原子更新 `latest.json`。XLSX 脚本使用文档运行环境提供的 `@oai/artifact-tool`，不属于 Python 采集器依赖。入口校验原表的 198 行身份、原字段和版本绑定；历史台账与原始回执保留。以上命令不上传文件、不加载调度、不修改正式数据根。

涨跌停池按原生命名字段读取，核对数量、证券身份及封板时钟；北交所 `920` 号段的来源市场值也是 `0`，不能由该值直接推定深市。统计天数 `days` 与涨停次数 `ct` 分列保存，炸板池允许两项均为零。历史日期请求曾返回不同 `qdate`，因此有效交易日保持未知：不一致的响应拒收，相等也不自动通过时间语义验收。流通市值高于总市值的源差值全部保留并列为缺口，不设置放宽阈值凑通过。

主营构成按发行人、报告期、分类类型和构成项保留原字段。金额为元，比例为小数比例，页面乘100显示百分数；缺失成本、利润、毛利率不补值。收入、成本和利润关系不一致时保留缺口，尤其不能把保险公司的行业口径猜成通用公式。报告期不是公告可见日，未提供公告时间的响应只能从首次实际接收时点可见。读取一个发行人当前返回的全部行，不等于该发行人全历史或全市场完整覆盖。

股票、ETF、指数的指定名单使用 `scripts/run_security_update_batch.py --profile profile.json` 预览；加 `--execute` 才采集。`ad.security-update-batch.v1` 配置明确给出 `root`、`rows`、`instruments`（`asset_type`、新浪带市场前缀的 `symbol`）、`max_jobs` 和 `lookback_days`。名单最多 10000 个身份，每轮至多 120 个独立请求，默认单并发、75 秒超时、每天两次失败尝试；`--max-jobs` 只能缩小配置预算。配置冻结后不原地改名单，扩大名单需新配置和新的隔离观察根。重复运行按当日检查点续跑，不重新请求已完成任务；在根目录创建 `STOP` 可在任务边界暂停，移除后继续。当前行 60/61 共享股票原生日线请求，不重复采集包装条目。

`scripts/query_security_update_batch.py --result <result_path> --row 61 --date YYYY-MM-DD` 按交易日跨证券读取，`--row 60 --symbol sh600000` 按股票读取；分钟、因子和日历也支持对应的原生日期。`--offset`、`--limit` 逐页返回，必须固定不可变的 `result_path`，不使用会变化的 `batch-latest.json`。日期无记录明确返回零条，范围尚未采集则报错。查询不联网，也不把后来快照混进此前页面。

每个证券、周期独立保存可翻页的 JSON/CSV 快照，`batch-latest.json` 列出成功、尚未尝试与失败任务，退出码 2 表示仍有未完成项。当天成功请求重复运行不会盘中刷新；下一自然日会重新取得滚动窗口，重叠键比较新增与修订，窗口外历史保留。盘中捕获的当日日线仍会变化，不当作收盘定值。新浪分钟最多 800 根/证券/周期；间隔过长可能漏出源窗口，系统明确记录缺口，不凭空补齐。股票日线保留原生未复权价和数量，前后复权因子单列为新浪口径；交易日历只交付 2010 年至采集日已返回日期，不声称未来官方日程。

财报复用 `scripts/run_legacy_financial_scope.py --profile profile.json --execute`。`ad.legacy-financial-scope.v1` 指定哈希绑定的 `source_universe`、`source_universe_sha256`、证券 `symbols`、报告起点 `start`、隔离 `root` 和 `max_jobs`；每家公司分别请求资产负债表、利润表、现金流量表。日期分批保存，已完成任务续跑零请求，跨日保留拒绝与冷却状态；未知原字段及原单位保留，公告日和报告期分开，不制造精确首次披露时刻。配置支持较大名单不代表已经实测全部公司。两个批量入口均不安装定时器，不调用正式发布器，不修改现有日常配置。

可转债指定名单也使用该批量入口，`asset_type: convertible_bond`，行38–42及54；每个身份必须带目录名称和证据。配置的 `tdx` 对象明确 `host`、`sdk_python`、`sdk_sha256`，只调用已有且按全部源码/原生库哈希固定的 eltdx 3.0.3。每次单节点、单连接、单页最多800根，2秒间隔、22秒子进程上限；无节点探测、自动安装或失败换源。原始wire与SDK解码值同时保存并离线重解析；价格使用原整数毫元，量额保持原生字段，不套用股票手/股换算。

行205提供明确8字段的最新财务快照：总/流通股本、EPS、每股净资产、总资产、净资产、营业收入和净利润。按已有SDK文档把万股/千元分别转换为股/元，保留原浮点值及精度限制。源更新日单列，报告期和公告日未知保持空；不将此快照当作完整历史财务指标或推定归母口径。主营构成的占比映射为百分数；收入、成本、毛利的字段含义已标明，但未证金额尺度仍显示原生单位。


ST 名单按完整来源分页保存。沪深 A/B 股分别标识，未确定北交所或新三板归属的原始路由身份单列；观察日期用于逐日留存，资格生效日期保持未知。可转债发行目录保留全部当前可见原字段、旧代码和转板代码，依据源市场标识区分身份。旧类债券不自动认作普通可转债；部分缺失条款或未明扩展字段逐条标注。DELIST_DATE 在部分记录等于到期日且存在更早赎回日期，因此保留为源生命周期参考值，不推定上市状态。

涨停、跌停和炸板池以实际接收时点保存当前名单。请求日期、来源 qdate、有效交易日各有独立字段；有效交易日未证实保持空。价格按原生毫元换算，连板数、统计天数及涨停次数分别保留，市值加总差异逐条可查。菜单68、69、70共享三个来源任务；每天重跑从检查点读取，历史窗口不会被当前快照删除。

期货日周月与五种分钟周期支持同一手动入口，配置显式提供 future_main、future_contract。日周月限一年请求窗口，周月从原生日线聚合，未结束周期标明可修订。分钟返回供应商符号、原始时间标签、OHLC及原生数量；价格单位来自已核品种契约，数量统计口径未证实则保留说明。夜盘交易日、UTC时间、标签起止未确认时保持空，数据仍可按供应商标签查询和增量留存。分钟窗口之外的旧记录继续保留。连续合约使用独立来源标识，CMES换月/复权衔接需要另行核对。

当前申万一级、二级成分也复用公开行业页，分别核对申万1级/2级列、标题行业代码及证券身份，未知纳入时间含义不猜补。邮电表保留源累计/非累计分区和每列单位；源声明期数与实际返回期数不一致时单列缺口。航运表按源最新1000期留存，HRCI/BHMI缺值不阻断已有BDI等指数。企业景气的指数值可查，源同比字段含义未确认时仍保留原值；股票交易月表的高低指数可查，未证实的股本量额单位不换算。

日常调度保留单实例和来源冷却；按实际 HTTP 回执身份核算请求，离线重解析不算新请求。支持每日、每周、每月任务，完成检查点以任务频率续跑。涨跌停来源 qdate 与请求日不一致时，不用请求日期覆盖源日期。

`scripts/run_daily_update_registry.py --profile registry.json` 校验冻结版本并预览，加 `--execute` 才执行。`ad.daily-update-registry.v2` 通过显式 `workers` 并行处理独立来源，同一来源族或同一观察库只运行一个子任务；`child_jobs` 批量推进既有队列，证券及财报的批量值仍受其冻结配置约束。可以用 `workers: 8`、`child_jobs: 4`、`between_chunks_seconds: 0` 起步，按机器和实测吞吐调整。`seconds_per_run` 是整轮恢复边界，子任务超时应覆盖整批已有单任务超时，不再对四个任务沿用一个任务的短期限。

v2 使用 `budget_scope: observation_store`，`http_requests_per_day` 和 `response_bytes_per_day` 分别作用于每个观察库，不是供应商限额，也不是所有来源共用的总配额。当前冻结采集器接受每库最多 1000 请求、1 GiB 响应；每次请求先原子预留、结束后结算，异常中断保留预留量。一个库到达自设边界时，其他库及离线派生继续。共享冷却、HTTP 拒绝及 Retry-After 仍按来源传播，不能因换库获得重新请求权限。使用量历史每轮每日核对一次；报告中的预算账包含尚未结算的预留，不等同于收到的有效数据量。

不同来源的独立条目应分成配置组，避免一个来源的冷却暂停同组其他来源。拆分时保留原观察库、范围、上下文和采集器，核对拆分前后任务身份集合完全相同；同一条目必需的多个来源不拆开验收。对子配置可显式设置每天最多两次尝试，重试仍使用原次数记录与冷却，不清空失败历史。子程序进度写入 stderr，stdout 只输出一个完整结果对象，避免财报等旧入口的进度日志破坏调度返回值。

升级时固定新入口、配置和包哈希，沿用根目录 `registry.lock` 与 `STOP`，新控制器检查点写入 `dispatches/<配置摘要>`，请求账本按观察库存储。保留旧冻结版本和状态，单实例切换后再核定时结果。创建根目录 `STOP` 会停止派发并等已启动子任务完成；移除后从检查点继续。`protected_sources` 和 `protected_observation_roots` 暂停与另一既有采集器共用的来源或观察库，其余任务在保护时段仍可工作。手动性能测验、只读查询比较和自然定时运行分别记录。

保险当前入口只选源明确标注“全国”的记录，保留原保险保费与寿险/财产险等原字段及万元单位；不把重复的省名猜成计划单列市，也不把寿险收入替作全部原保险。运输按方式、统计月份和累计/非累计建键，百分数可用；各方式数量与周转量的尺度未逐一核实，保留原标签及原值而不统一换算。存款准备金率按公告日和生效日复合建键，保留同一公告的分次生效，附注中的适用对象不推成全机构统一准备金率。

现货商品入口保留新浪 BDI、钢坯和澳大利亚粉矿（61.5%）原生数值；页面未明示物理单位，因此不猜测元/吨、美元/吨，也不把历史混合尺度的涨跌幅统一换算。错误年份的原行在 `coverage.quarantined_rows` 中保留，数量与有效日期行对账；有效日期可以继续留存。螺纹钢期现入口从同一来源产品页核对 `productId=11`、`RBSH00` 和元人民币/吨，未推定具体期货交割月或换月规则。

上金所日行情使用金十公开日报告，合约别名保持原样，空身份原行单独留证。Au99.99、Au99.95、Pt99.95 报价单位元/克，Ag(T+D) 为元/千克；其他尚未核明的尺度只提供原值，不换算成交量额或涨跌幅，也不把日行情当成上海金、银基准定盘价。同花顺领涨行业榜从两页原表读取行业及领涨股链接代码；资金为亿元，价格为元，观察日由实际接收时间记录，页面没有独立交易日时不推定。公开页面拒绝访问时停止该源，禁止执行挑战脚本。

海关旧金额单位的影响范围包括原始查询注记和已经导出的快照：当前通用查询在保留原数值、哈希、接收时间的同时明确附上千美元单位；旧错误单位快照及发布契约由当前查询器拒绝，须从原回执生成新身份。旧证据不重写。同比、环比百分比及外汇储备等其他报告的亿美元口径不随之更改。

央行社融增量入口直接读取官方当年表，保留人民币亿元单位、月度增量、未发布月份空位与实际接收时点。统计月份不冒充公告日期。油价入口发现最新发改委政策公告及上海车用汽柴油价格表：全国调整额为元/吨，上海最高零售价分别保留元/吨和元/升；优先取公告明确的实际执行调整额，区分理论涨幅，不猜转换密度，不将上海价格称为全国各省或实际加油站成交价。公告日期与生效时刻分列，24时转次日零时。

`python scripts/repair_customs_snapshot.py OLD_SNAPSHOT ISOLATED_REPLACEMENTS` 为退休的错误海关单位快照生成新身份。原生数值和原接收时刻保留，四项金额的注记改为千美元，贸易差额按明确公式派生；新清单绑定原清单与记录哈希。旧证据不可变，当前读者继续拒绝旧错误标签。修复可安全重复，不联网、不写正式数据根。

可转债基本资料通过公开HTTP入口独立执行，不要求本机通达信SDK；可转债K线、原生财务与盘口仍各自保留SDK运行条件。F10公司资料、主营构成和估值属于现有HTTP入口，不应归入Python3.13后台权限组。

宏观包装条目可通过 `ad.cached-macro.v1` 从已采 CPI、PPI、PMI、货币供应表生成；每次重解析原始回执并保存来源运行身份，查询显示原接收时刻，处理时间不冒充新采集。纯缓存任务不消耗网络预算，也不因上游冷却而停止本地处理。龙虎榜明细通过 `ad.billboard-queue.v1` 读取已采当日目录，按实际交易日及证券代码生成机构和买卖营业部任务，单任务执行，保留未完成日期续跑；多个上榜原因不重复发起相同请求。相同交易日已完成的队列直接复用。

日常注册表支持 `not_before_local`，新闻联播与宏观缓存安排在原宏观采集之后的晚间时段。深圳融资融券汇总按观察日向前最多八个明确日历日期查首个已公布报告；空表只表示该日期尚无可用报告，不推定休市、零余额或停发，保留旧有效快照。缺少 `txtDate` 的默认接口返回无逐行日期历史集合，不能用来判断最新日期。

原生通达信任务可在同一已批准的 Python 3.12 解释器下使用官方 eltdx 3.0.3 ABI3 wheel。运行配置绑定 SDK 全部 Python 与本机二进制文件哈希、解释器和固定服务器；每次连接也受请求及响应字节预算约束。五档仅为采样时的当前快照，原生财务保持源报告期与接收时间，不推定公告时刻。

日常任务可用 `depends_on: ["profile-id"]` 声明前置采集。派发器检查引用和循环，并要求前置任务在本次应运行日期完成后才启动后续任务；不能用昨天的目录满足今天的依赖。龙虎榜明细依赖当天总表，避免先运行后延迟到下一轮。

东财板块日线使用 `free_update_board_bars` 按已核实的原生 `BK` 代码直接请求原行情端点，目录发现与已有代码更新分开。手动行76配置给出 `em_concept`、`em_industry` 及最近日期窗口；两个类别分别留证。OHLC为指数点，成交量/额保留原生f56/f57并明确未转换尺度。响应身份、日期窗口、OHLC和重复键必须一致；目录暂时断连不能推断行情端点不可用。

第二批19项保存在独立 `supplemental-menu.json`，与首批198项身份互斥。`scripts/run_supplemental_updates.py --source-index index.json --root isolated-output` 读取已验证快照列表，生成独立不可变快照及 `latest.json`，不联网、不安装调度、不增加首批完成数。输入索引的 `manual_snapshot_queries` 每项至少包含 `function` 与 `snapshot`；引用快照的内容哈希、来源语义与记录数重新检查。索引为显式输入，需要在上游采集完成后刷新，不能将重复读取同一索引称为新日更新。

第二批字段子集包括票息、PE TTM/PB MRQ、公司资料、分红送转配股、题材名单及两个美国财政部长期平均利率序列；缺字段和供应商分类差异随快照返回。技术指标为不复权收盘价SMA5/10/20及相邻有值交易日收益率，窗口不足返回空值。重叠输入先按证券/交易日及实际接收时间合并，较新观察保留修订；同接收时刻的冲突或混合复权基准拒绝。当前指数成分只连接其源名单日期当日或之后的行情，列出尚未采到行情的成员，不倒填历史成分。
