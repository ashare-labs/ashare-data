# 查询行情、缓存与质量要求

## 查询与缓存

```python
from ashare_data import Client, QueryLimits

client = Client(cache="data/sina", cache_mode="prefer")
prices = client.get_price("600000.XSHG", frequency="daily", count=3)
print(prices, prices.attrs)
# 完全离线：使用已捕获记录，不自动刷新。
offline = Client(cache="data/sina", cache_mode="only")
```

默认来源为新浪公开 JSON；不会在失败时自动换源。支持沪市 60/68、深市 00/30 的规范证券代码，单批默认最多 10 只；频率为 daily/1d、1m/minute、5m。默认 OHLCV；money 仅分钟可请求。DataFrame 价格/金额使用 float，量为股，精确源字符串另保存在 source_rows；原价不推导复权、ST、限价或公司行动。

count 默认 20，范围 1～1000，与 start_date 互斥；end_date 默认本次北京时间，纯日期包含该标签日，未来时刻拒绝。无时区时间按 Asia/Shanghai 解释。源窗口最多 1023 行，没有任意历史分页；QueryLimits 可调整本地预算，不扩大来源能力。默认批量预算 100,000 观测行。

`prefer` 缓存 TTL 300 秒，`only` 可读过期缓存但不联网，`refresh` 显式刷新。普通查询按精确 URL 缓存；原文 SHA 对象与历史观测不可变，请求索引仅是当前指针。旧响应中未闭合的行不会随缓存年龄增长自动成为完成数据。

单次网络响应最多 2 MiB、超时 15 秒，按主机至少间隔 1 秒；无重试、跳转或访问限制绕过，不使用账号/密钥，不执行响应 JavaScript。网络、访问拒绝、限流、源 HTTP、schema、缺字段、无完成记录、缓存和覆盖错误分别保留；CLI DataError JSON 写 stderr、退出 2。

`allow_partial=True` 允许逐证券 ok/partial/unavailable/not_attempted，全部合法无数据时可返回带状态空表；身份冲突、损坏和非法值仍报错。访问拒绝/限流后停止后续证券；不能同时要求 complete/fresh/final/tradable。strict 只检查已返回标签之间的间隔，不能替代完整覆盖。

## 覆盖与可见性

| 操作 | 保证与限制 |
|---|---|
| `Client(coverage_contract=CoverageContract(data))` | 使用显式版本化来源标签、日历、session 和证券状态；没有内置真实新浪完备契约 |
| `client.coverage(security,start_date,end_date,frequency=...)` | 检查头尾、内部、整日、异常标签与未知事实；缺契约不联网返回 unknown |
| `require_complete=True` | 证明契约标签槽覆盖；未知 COVERAGE_UNKNOWN，缺口 COVERAGE_INCOMPLETE，不认证全部成交 |
| `require_fresh=True` | 源水位、返回子集和当前交易状态同时满足；旧窗口不能靠新 HTTP 或范围外 bar 背书 |
| `trading_status(security,as_of=...)` / `require_tradable=True` | 纯本地交易状态；不满足时在行情读取前拒绝，不证明数据完整/新鲜 |
| `require_final=True` | 当前没有最终发布/修订水位，BAR_NOT_FINAL；15:05 不是最终值证明 |
| `acquire_price(security,start_date,end_date,frequency=...)` | 显式采一个有界近期窗口，返回证据与 requirements_met；固定上下文/only 禁止 |

无 as_of 的直接日线使用 15:05 软件完成阈值，标记 unrestricted_research、closure_verified=False。完成截止取 query/request_started/observed 三时刻的最早值；旧缓存缺请求开始时间时使用 observed。源标签仍用于 end_date 过滤，不能把标签过滤解释为当时已知。

`client.at(as_of, visibility=...)` 固定本地观测和 query_snapshot_id，之后查询不联网，随后刷新不改变它。缺缓存明确报错；不允许固定时钟再被查询参数覆盖。

| visibility | 行为 |
|---|---|
| verified（as_of 默认） | 要求历史发布与版本证据；当前源报 VISIBILITY_UNKNOWN |
| assumed（显式选择） | 按声明 bar_end 过滤；无映射时假设分钟结束标签、日线到 23:59:59.999999。可使用创建视图前已有的事后回填，不证明历史可见 |
| received | 仅选 observed_at≤as_of 的已收到版本，并执行闭合模型；received 不是源首次公布或最终版 |

assumed/received 的 point_in_time_verified 始终 False；CLOSED_PROVISIONAL 不等于 FINALIZED。固定上下文对当前名称和年度网页日历等无历史证明的接口明确拒绝。

### 多次观测、冲突与溯源

每份响应先独立做闭合与可见性筛选，再按同 endpoint/证券/频率的实际标签选择最新合格观测。精确 URL、大窗口都无优先权；新窗口缺某行不证明删除/撤回。最新同时间、同标签的完整 source_row 不同才产生 OBSERVATION_CONFLICT；只比较当前请求字段或 JSON 空白都不够。严格较新且唯一的合格值可消解旧冲突，固定视图创建后的刷新不能消解。

只要请求选中未消解标签就拒绝；其他流/范围外/不可见冲突不阻断。provenance.observation_conflicts 保留 unresolved/resolved/superseded_by_conflict、完整 source_row、原文/URL/观测时钟及 resolved_by。row_observations、response_observations 提供逐行/逐响应来源。多响应组合的单一 sha256/url/observation_id/request_started_at/completion_cutoff 为 None，completion_basis=per_observation；汇总 observed_at 不能替代逐行来源。

最多读取同流 128 份观测、100,000 原始行，固定上下文最多 10,000 个索引/日志文件，超限 BOUNDED_QUERY。无 received 合格版本时，晚到版本只可解释错误，不返回为历史行情。

### CoverageContract 格式

顶层为 schema_version=1或2、id、provider、frequency（daily/1m/5m）、trading_scope、evidence_kind（synthetic/observed）、evidence、label_semantics（verified/unverified）、calendar、statuses。

- calendar 每日明确 date/is_open/evidence/aware available_at/sessions。开市须有 session，休市无 session；session 为 id/aware start/end，左闭右开。mapping=end_labels 按周期生成槽；explicit 的 label/start/end 槽须无缝覆盖。日线范围取声明时段，标签仍为日期；不默认 240 根。
- v1 statuses 为 security/date/state（trading/suspended/unknown）/evidence/available_at。v2 使用 effective_start/effective_end 的带时区左闭右开区间，最多 366 天，不得重叠或混用 date。修订生成新契约；缺记录、空洞和未来可见事实保持 unknown。
- 槽跨状态切换且不能完整证明时 PARTIAL_BAR_STATUS，不能造半根或整根删掉；全槽已证停牌才排除。日线的声明活跃 session 状态须完整已知。
- `count=N` 从最近闭合槽逆查到 N 个有据可交易槽，只跳过已证休市/全槽停牌；未知、半根或证据下限不足就拒绝。找到 N 槽后的更早无关未知不阻断。报告包含 requested_count/count_evidence_start。start/end 查询仍检查整个闭区间标签范围。
- contract_id 是声明内容 hash，构造器检查重叠、重复、格式和映射，复制输入；不认证证据真实性。合成契约通过仍是 synthetic。grid_complete 不证明量额完整，trade_totals_verified 仍 False。

交易状态按左闭右开 session：开盘边界可交易，收盘边界不可交易但边界 bar 可已闭合。这是声明模型，不自动等于经纪商接单规则。

### 新鲜度与错误

freshness 分开记录 last_source_label/last_returned_label、标签年龄、response_observed_at/HTTP 年龄、source_watermark_status、selected_window_status、trading_status 和 admissible。日线标签年龄自 00:00 起，不是发布年龄。水位满足但休市/午休/停牌仍不准入；只含未来标签为 unknown/FUTURE_SOURCE_LABEL。

require_fresh 可能报 STALE_SOURCE、STALE_SELECTED_WINDOW、MARKET_CLOSED、SESSION_BREAK、SUSPENDED、FRESHNESS_UNKNOWN；各 guarantee 不能相互替代。价格必须有限、正数、低于 1e17 且满足 OHLC；公开 float64 价格拒绝次正规/下溢输入，不舍入为零。非法行情不缓存。

CLI 的 `price` 支持 --as-of/--visibility/--coverage-contract/--require-complete/--require-fresh/--require-final/--require-tradable；`trading-status` 离线且仅 tradable 退出 0；`live-coverage`、`acquire-price`、`reconcile-day` 在要求未满足时退出 2。

## 量额诊断

`reconcile_turnover(rows,reference,...)` 对明确股/元的同日数据做精确 Decimal 对账；不以缺 amount 为零或 close×volume。`reconcile_sina_day(security,date,minute_body,five_minute_body,quote_body)` 只读调用方 bytes，无网络；CLI `reconcile-files` 读取每份最多 2 MiB 的文件。

原始分钟数组须非空且≤1023行，时间对齐、同股/同日、无重复、有限且字段合法；quote 必须对应同日，不能拿当前 quote 核对历史日期。一分钟到五分钟是显式标签分组模型，量额相加无容差、无扣尾字段或补行。差异则 failed，即使相等也因真实覆盖/区间未知保持 unknown，accepted 始终 False，historical available_at=None。

## 风格适配

原生 API 不是 jqdatasdk 的直接替代品；客户端签名相似不能证明服务端的停牌、复权和分钟语义相同。

| 参数/接口 | 当前原生 Client 行为 |
|---|---|
| fq | 默认且仅 None，原始价；pre/post 明确拒绝 |
| panel | 默认且仅 False，单股时间索引、多股 time/code 长表 |
| skip_paused / fill_paused / round | 默认且仅 False，不推断停牌、不填缺行、不主动舍入 |
| get_trade_days | Python date 列表，限上交所当前发布年度，不是任意历史日历 |
| get_security_info | 当前名称/代码/交易所，上市等未知字段为 None |
| get_all_securities | 未支持，不生成历史股票池 |

`ashare_data.providers.joinquant_style.JQStyle` 另提供绑定明确 Snapshot 的高级离线适配，返回带 snapshot_id 的字典，见 [固定快照](snapshots.md)。其用户声明的分钟契约不能直接套到新浪源；它不加载账号、策略或回测引擎。已有 JQ 固定响应的导入和限制见 [观察数据](observations.md)。

## Registered local minute queries

The installed product CLI and `LocalMinuteProduct` expose the same read-only
minute registry. The default source is `tdx`; HF requires an explicit registered
source, normally `neigezhu`. Unknown or unavailable sources never fall back.

```sh
ashare-data product --deployment /path/to/deployment.json minute-sources
ashare-data product --deployment /path/to/deployment.json minute \
  --source neigezhu --security 600000.XSHG \
  --start 2026-06-03T09:40:00+08:00 --end 2026-06-03T09:42:00+08:00
```

`python -m ashare_data.product` accepts the same arguments. The API is:

```python
from ashare_data.product import LocalMinuteProduct

owner = LocalMinuteProduct("/path/to/deployment.json", profile="product")
sources = owner.sources()
result = owner.prices("600000.XSHG", "2026-06-03T09:40:00+08:00",
                      "2026-06-03T09:42:00+08:00", source="neigezhu")
```

Use the existing `ashare-deployment.v1` manifest and a product profile. Register
read-only assets under `assets`, then add `minute_sources` to that profile:

```json
{
  "tdx": {
    "reader": "tdx-session", "asset": "minute-tdx",
    "content": {"sha256": "<externally reviewed session JSON SHA256>"},
    "coverage": "<actual securities and days>", "limits": ["<source limitations>"]
  },
  "neigezhu": {
    "reader": "neigezhu-native", "asset": "minute-hf-download",
    "manifest": {"path": "assets/minute/neigezhu/file_manifest.csv", "sha256": "<reviewed SHA256>"},
    "review": {"path": "assets/minute/neigezhu/quality_overlay.json", "sha256": "<reviewed SHA256>"},
    "coverage": {"basis": "sealed audit", "last_day_partial": true},
    "limits": ["<actual audit and remaining semantic limitations>"]
  }
}
```

Asset `kind` is `reference`. The HF asset retains its existing
`minute-hf-download` key and points to the single original library. Relative
asset/evidence paths are relative to `data_root`, which is relative to the
deployment file. The registry does not require a Codex directory, working
directory, manual `PYTHONPATH`, data copy, or new service configuration.

Discovery reports `registered_unopened`: coverage is the operator's registered
audit scope, not a fresh scan or a promise of every security/day. A malformed
source registration is reported separately and does not disable other sources.
The selected query checks its pinned content. HF requires both the sealed
manifest and the sealed quality overlay; neither is optional at this product
entry. Single-security calls retain the reader's 60-day / 15,000-row bounds.

The response contains `source`, `binding`, `rows`, and `report`. HF rows/reports
are the native reader's unchanged output, including exceptional numeric values,
physical row identity, diagnostics, review evidence, and separate execution and
fresh-valuation denials. TDX rows retain the session package's existing evidence
and time semantics. Empty windows remain empty. Discovery and queries neither
repair data nor initialize state nor grant matching/valuation permission.

The existing daily product catalogue, source selection and publication contract
are unchanged. Minute reads do not require daily reference coverage. See
[native semantics](research-data.md#native-neigezhu-minute-research) before constructing bounded research inputs.

## Market coverage repair and original observations

Immutable Parquet manifests, frozen SQLite metadata and DuckDB readers remain the market storage contract. Every reader fixes one release. Old releases retain their original objects. Accounts continue through the independent retained runtime.

Canonical daily reads combine series 1 and series 5 on disjoint security days. Series 1 preserves exact decimals; series 5 preserves original binary64 values. The logical SQL view uses tagged numeric structs to prevent implicit loss. Canonical minute reads combine HF series 2 and free-stockdb series 4 on disjoint whole security days. TDX series 3 is explicit. No intraday splicing or guessed timestamp shift is permitted.

Explicit Sina/Sohu/Tencent/Eastmoney/BaoStock research queries read the original SQLite source pinned by the release, retaining the prior provider filters and values. Main-series priority must not erase explicit source observations. Source files, WAL, fingerprints and hashes must match. A changed original fails closed. Before modifying a referenced original, retain its consistent snapshot or explicitly retire that release's original-source reproducibility.

Explicit HF queries retain original rows and use restrictions. Six structurally valid price-conflict observations return to canonical facts while preserving execution/fresh_valuation=deny. Nonpositive and structurally invalid OHLC remain excluded from canonical facts. Frozen manifests and review overlays reference the retained market objects.

Early Sohu observations require corroborated whole OHLC, share quantities within original lot precision, and compatible amount/volume envelopes. Original ingestion already converted lots to shares and ten-thousand CNY to CNY; this repair does not scale twice. Free-stockdb raw and adjusted APIs are distinct. Supplements retain original binary64 values and exact integral reported share quantities. Pre-2010 daily supplements additionally require per-record independent corroboration. Unsupported observations retain exact source locators and quarantine reasons.

### Resume and publication

Use the registered market, audit and deployment paths. Complete early daily processing before free-stockdb, then validate and publish. Only staged objects and the checkpoint change during the first two steps. Restart validates the frozen source/rule version and skips complete batches.

```sh
python scripts/repair_market_coverage.py --root MARKET --audit AUDIT --entry ENTRY
python scripts/repair_stockdb_coverage.py --root MARKET --audit AUDIT --entry ENTRY --old-audit ORIGINAL_STOCKDB_AUDIT --tooling MSGPACK_TOOLING
python scripts/audit_coverage_repair.py --root MARKET --audit AUDIT
python scripts/finalize_coverage_repair.py --root MARKET --audit AUDIT --entry ENTRY
python scripts/finalize_coverage_repair.py --root MARKET --audit AUDIT --entry ENTRY --publish
```

The independent input audit checks unique original keys and reconciles each exclusive disposition, recording exact source/year/reason scopes. The finalizer also requires original explicit-source query restoration evidence from the retained acceptance run: all 31,384,115 daily provider-query records, plus all 30,581 previously excluded HF original observations, compared by keys and values while preserving use restrictions.

These are R2 repair scripts for the sealed R1 source inventory, not arbitrary ingestion commands. Publication verifies all changed keys and values in bounded instrument groups, retained old rows and batches, disjoint supplemental days, unique target keys, exhaustive input dispositions, metadata references and source versions. CURRENT changes atomically. Consumers separately pin the new release and manifest SHA in deployment.json. Rollback restores the prior consumer entry and paired runtime; it never deletes objects.

Market backup/restore retains the release chain and its data, metadata and audit objects. Explicit native-source research queries additionally require the pinned original assets; the market backup does not duplicate those originals or the independent account ledgers. A consumer rollback must restore the paired runtime and a Web checkout matching the prior catalog commit before restarting foreground services.

New dates without an evidenced calendar remain queryable with unknown expected coverage. Observed dates are not invented calendar sessions. Uncorroborated early observations, minute interval endpoints, the HF partial final day, rounded reported quantities and strict historical market PIT remain explicit limitations.

All repair audit artifacts, runtimes, test outputs and worktrees are retained in controlled Workspace paths. Preserve originals and every published object. This repair imports no financial samples, starts no persistent service and replays no account orders.


### Verified R2 scale and retained limits

Release 2 has manifest SHA256 `ffd319cef54f6ba850cd0e3678f9b85aec2e0e8e9a85310072eb5d5ae6a1d7fc`.
All 4,085,305,933 registered input observations are accounted for: 3,549,641,333 adopted, 13,058 equal duplicates, 535,064,901 resolved but unused, 586,641 quarantined, and zero format rejects. These are input dispositions; adopted rows equal the two logical tables' unique keys.

| Logical facts / series | Rows | Observed date range |
| --- | ---: | --- |
| Daily selected source, series 1 | 14,164,397 | 1990-12-19–2026-10-14 |
| Daily free-stockdb supplement, series 5 | 4,125,453 | 2000-01-04–2026-10-16 |
| HF minute labels, series 2 | 3,475,793,906 | 2010-01-04–2026-08-07 |
| Free-stockdb minute supplement, series 4 | 55,556,617 | 2026-04-13–2026-10-16 |
| Explicit TDX minute series 3 | 960 | 2026-10-09–2026-10-12 |

Daily facts contain 18,289,850 keys for 5,870 observed stable identities; minute facts contain 3,531,351,483 keys for 5,821. All 10,736,431 R1 daily keys and their values are preserved. Across the original early SQLite universe, 3,544,135 of 3,654,381 observed unique security days are now represented. The remaining 110,246 dates affect 747 identities; `000508.XSHE` still has no admitted daily facts. Exact residual dates, including wholly absent identities, are retained in the local coverage audit, rather than omitted from its denominator.

The frozen source versions contain 38,736,814 multi-provider SQLite daily inputs and 570,743,678 free-stockdb daily/minute inputs. They are not all distinct canonical securities/times, nor is their difference automatically same-value duplication. Explicit original daily source queries retain all 31,384,115 previously queryable records. All 30,581 previously excluded HF native observations remain queryable with their restrictions; six structurally valid conflicts also return to canonical facts with execution and fresh valuation denied.

The latest HF date still ends at 10:21 local time. Supplements cover separate security days and extend observations through 2026-10-16; they do not establish that every expected day or minute is complete. Source labels and rounded reported quantities retain their limitations. Missing historical listing evidence produces unknown expected coverage, even when the general calendar contains dates. A source identity labelled BJ does not certify a Beijing exchange listing throughout that identity's historical observations.

The R2 manifest references 61,456 verified objects totaling 48,216,091,613 bytes, excluding retained older-version-only objects and raw originals. The asset budget remains 200 GiB with 150 GiB free-space reserve. A 301,519,212-byte real-instrument recovery exercise verified old/new values, repeated restore, restrictions, and relocation of native audit evidence. No full-market repair copy was created. Formal market runtime is paired AD 0.10.0.dev11, AQ 0.12.0.dev13 and Web 0.3.8; the account runtime remains separate. Actual economic backtest results for 2020-02-03–07 and 2024-01-02–05 match the original-source baseline, including values, money, orders, trades, signals and restart results. These runs remain price-only diagnostics, not total-return certification.

Selecting `free_stockdb` for daily queries reads only series 5 admitted to the pinned release. It does not query the legacy SQLite provider table or claim to expose all raw free-stockdb observations. The default combines series 1 and 5 without overlapping securities/dates; missing listing or calendar evidence remains unknown for either view.

## 固定发布的近期行情增量

正式行情由一个不可变文件清单选择。增量沿用证券身份、来源优先级和冻结日历；旧日线 SQLite、HF、free-stockdb 原件及既有 release 保留。模拟账户不迁移，不随行情入口升级运行环境。

### 执行顺序

下列脚本在 ashare-data 源码目录运行，Python 必须安装当前配对版本和 DuckDB 1.4.3。每一轮使用独立的受控 `RUN` 目录，固定 `MARKET`、`RELEASE` 与不超过七天的日期窗口。`ACTIVE_SINCE` 选择父发布最近仍有日线观察的证券；它不代表完整上市证券范围。

```sh
PYTHONPATH=src "$PYTHON" scripts/collect_market_increment.py \
  --market "$MARKET" --release-id "$RELEASE" --root "$RUN" \
  --start "$START" --end "$END" --active-since "$ACTIVE_SINCE"

# 只在上一采集进程结束后续跑失败请求；成功回执跳过。
PYTHONPATH=src "$PYTHON" scripts/collect_market_increment.py \
  --market "$MARKET" --release-id "$RELEASE" --root "$RUN" \
  --start "$START" --end "$END" --active-since "$ACTIVE_SINCE" --retry-errors

# 若来源收盘数据在采集途中更新，只补刷仍落后于截止日的搜狐证券。
PYTHONPATH=src "$PYTHON" scripts/collect_market_increment.py \
  --market "$MARKET" --release-id "$RELEASE" --root "$RUN" \
  --start "$START" --end "$END" --active-since "$ACTIVE_SINCE" --refresh-sohu

PYTHONPATH=src "$PYTHON" scripts/build_market_increment.py --root "$RUN"
PYTHONPATH=src "$PYTHON" scripts/prepare_market_increment.py --root "$RUN"
PYTHONPATH=src "$PYTHON" -m ashare_data.product.release_cli \
  --root "$MARKET" --release-id "$RELEASE" apply --plan "$RUN/build/replacement-plan.json"
```

采集器使用独占锁、逐请求原文和原子回执。每个主机顺序请求、间隔至少一秒；401/403/429 停止该主机，不自动绕过。每轮预算 8 GiB，并保留 150 GiB 可用磁盘。普通续跑不重抓成功请求；显式搜狐截止日刷新每次最多 20 个代码，保留整份 HTTP 原文和逐证券块位置。 已有成功行情回执的代码可组批；无成功行情证据的代码单独请求，避免无数据成员触发整批来源错误。错误重试绕过当天 HTTP 响应缓存，旧尝试仍保留。进程中断后从回执重建小型任务日志。

构建固定回执清单、解析器和规则哈希。输入改变时必须使用 `--build-name` 新建阶段，原阶段不覆盖。按证券提交检查点，只重建受影响的完整证券/系列文件，没有整库复制。每条捕获行情有采用、同值重复、已裁决未用、未决隔离、格式拒绝五种互斥去向。新值与目标按键和值核验；未变化的旧行保留批次。

`prepare` 固定源别名、解析规则、批次、原文清单、逐行去向、小型显式来源观察库及摘要。固定方案重复应用返回 `already_applied`，无额外 release。父发布不同且不是该方案已成功发布时拒绝切换。新数据通过后，部署清单必须显式固定新 release 和 manifest hash；只生成目录不算接入完成。

### 来源与数值

日线继续使用 baostock、eastmoney、sina、sohu、tencent 的既定优先顺序。已有 free-stockdb 补缺日保留。更高优先级后来到达时可替换较低来源的整条日线；同优先级的变化值隔离，待证据裁决。不得把腾讯缺失的成交额拼接成另一来源的值。

搜狐原始成交量按手乘 100、成交额按万元乘 10000；来源报告的取整性质保留。腾讯原始成交量按手乘 100，未提供成交额记 NULL。近期新浪分钟为独立系列 6：保留来源标签，Asia/Shanghai 转为同一 UTC 时刻，无标签平移；股数须整数，价格与金额只有在十进制无损回转后才存 DECIMAL(38,12)。原始响应保留。

默认分钟由发布固定的系列 2（HF）、4（free-stockdb）、6（新浪）按互斥证券日组成；TDX 3 仍显式选择。新浪只补已存 HF/free-stockdb 最后日期之后的证券日，不在同一证券日拼接不同来源。新浪已发布标签可同值去重、补新标签；变化值隔离。1023 标签上限截断的首日不能据此建立新的完整证券日。

显式日线来源查询读取本地 release 固定的原始 SQLite 加近期不可变观察库。观察库按固定清单排列，不扫描变化目录；原始来源不同意见可查看，但不会改变正式优先级。

### 能力边界和验收

新浪的最近 1023 标签不能补齐更早的历史缺口。新观察日期超出冻结日历、上市区间或预期覆盖证据时，状态保持 unknown；缺行、零量不能自动认作停牌。分钟端点、公司行动和严格历史 PIT 未认证，研究读取不授予交易执行权限。

验收检查本轮全部输入、修改的键和值、发布幂等、CLI/API/UI 和 A宽的固定版本读取。未改变的大库文件沿用父 manifest 的完全相同描述，实际选中读取时验证内容。本轮不执行新的全市场扫描或全量恢复演练。失败输出、旧发布及原件均保留；磁盘不足时停止新写入并报告，不自动清理。

每轮 `build/increment-summary.json` 给出真实新增键数、实际修改行、逐日采用量、来源错误、输入去向和覆盖限制。正式 manifest 的 `increment.summary` 引用该摘要的不可变对象。


### 保留原件的有界日历补齐

`calendar_release` 只读取固定发布内的新浪日历响应回执及对应原件，并与保留的
深交所完整月历逐日核对。原件已经含有的日期不会再发网络请求。单次只允许当前月
至多 31 个自然日的追加，不由行情存在反推开市，也不改变证券状态或认证可交易性。
原有沪深共用日历语义保持明确；不能据此认证北京市场或逐证券停复牌。

```sh
PYTHONPATH=src python -B scripts/prepare_calendar_extension.py \
  --market-root "$MARKET" --release-id "$PARENT" --manifest-sha256 "$PARENT_SHA" \
  --output "$CALENDAR_OUTPUT" --end "$END" --sina-raw "$SINA_RAW" \
  --sina-response-id "$SINA_RESPONSE_ID" --szse-root "$SZSE_CLOSED_BATCH"
```

输出只含一份小型元数据候选和固定计划。验证比较所有元数据表的精确增删差异：
原行必须完整保留，只允许推导出的日历、批次、来源与标记行增加。可以与单位证据
在同一发布提交。旧版仍读取旧日历；财务参考表自己的日历覆盖不随市场日历隐式扩大。
准备和失败产物留在指定目录，正式默认通过 deployment 中明确的 release/hash 切换。
