# Market coverage repair and original observations

Immutable Parquet manifests, frozen SQLite metadata and DuckDB readers remain the market storage contract. Every reader fixes one release. Old releases retain their original objects. Accounts continue through the independent retained runtime.

Canonical daily reads combine series 1 and series 5 on disjoint security days. Series 1 preserves exact decimals; series 5 preserves original binary64 values. The logical SQL view uses tagged numeric structs to prevent implicit loss. Canonical minute reads combine HF series 2 and free-stockdb series 4 on disjoint whole security days. TDX series 3 is explicit. No intraday splicing or guessed timestamp shift is permitted.

Explicit Sina/Sohu/Tencent/Eastmoney/BaoStock research queries read the original SQLite source pinned by the release, retaining the prior provider filters and values. Main-series priority must not erase explicit source observations. Source files, WAL, fingerprints and hashes must match. A changed original fails closed. Before modifying a referenced original, retain its consistent snapshot or explicitly retire that release's original-source reproducibility.

Explicit HF queries retain original rows and use restrictions. Six structurally valid price-conflict observations return to canonical facts while preserving execution/fresh_valuation=deny. Nonpositive and structurally invalid OHLC remain excluded from canonical facts. Frozen manifests and review overlays reference the retained market objects.

Early Sohu observations require corroborated whole OHLC, share quantities within original lot precision, and compatible amount/volume envelopes. Original ingestion already converted lots to shares and ten-thousand CNY to CNY; this repair does not scale twice. Free-stockdb raw and adjusted APIs are distinct. Supplements retain original binary64 values and exact integral reported share quantities. Pre-2010 daily supplements additionally require per-record independent corroboration. Unsupported observations retain exact source locators and quarantine reasons.

## Resume and publication

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


## Verified R2 scale and retained limits

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

PYTHONPATH=src "$PYTHON" scripts/build_market_increment.py --root "$RUN"
PYTHONPATH=src "$PYTHON" scripts/prepare_market_increment.py --root "$RUN"
PYTHONPATH=src "$PYTHON" -m ashare_data.product.release_cli \
  --root "$MARKET" --release-id "$RELEASE" apply --plan "$RUN/build/replacement-plan.json"
```

采集器使用独占锁、逐请求原文和原子回执。每个主机顺序请求、间隔至少一秒；401/403/429 停止该主机，不自动绕过。每轮预算 8 GiB，并保留 150 GiB 可用磁盘。已有成功请求不重抓，错误重试绕过当天 HTTP 响应缓存，旧尝试仍保留。进程中断后从回执重建小型任务日志。

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
