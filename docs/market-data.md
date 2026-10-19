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
