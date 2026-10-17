# 选择来源与读取固定捕获

默认 Client 只使用新浪。下列提供者必须显式选择，不自动回退、跨源拼接、补数或认证交易资格。来源权利见 [来源与许可](sources-and-licensing.md)，原文、回执和数据另行保存。

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

日线最多31自然日；分钟支持5m/15m/30m/60m、最多2自然日，没有1m。价格 Decimal、成交量整数股、金额元，原价与源前收分开。失败也保存 capture_id 与诊断，不能把失败当成功空集。证券资料支持 type=1/2/3、status=0/1；空/未知枚举拒绝，ipoDate/outDate 非空须严格日期且顺序合法，空日期保持未知，不生成无穷退市日。

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
