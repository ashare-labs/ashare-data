# 逐证券、日期、用途的研究输入盘点

Preflight把用户显式绑定的本地不可变版本转成有证据的研究值和缺口。没有采集、默认选源、换源、跨日历capture拼接、事实回填或执行授权。`research_values_complete=True`只表示下表要求的研究字段有来源值，停牌/ST为True或preclose为零也仍是已知来源值；不表示可以买卖、PIT成立或满足M2/INITIALIZE要求。

## Python和CLI契约

```python
from ashare_data import Store, ResearchInputRef, ResearchSecurityInputs

store = Store('/已有本地目录')
binding = ResearchSecurityInputs(
    '600000.XSHG',
    price=ResearchInputRef('baostock', '完整价格capture_id'),
    status=ResearchInputRef('baostock', '完整状态capture_id'),
    preclose=ResearchInputRef('baostock', '完整preclose_capture_id'),
)
result = store.preflight_research(
    inputs=[binding],
    calendar=ResearchInputRef('baostock', '完整日历capture_id'),
    trading_dates=['2026-09-29'],
    require_complete=False,
)
print(result.report_id, result.research_values_complete)
for row in result.rows:
    print(row.security, row.trade_date, row.role)
    for fact in row.facts:
        print(fact.field, fact.state, fact.value, fact.unit, fact.reason)
```

ID示例是占位符；实际必须为64位小写SHA256。`ResearchInputRef(kind, version_id, store_path=None)`的kind仅`research`、`baostock`；价格支持两种，其余引用仅支持baostock。可选store_path只定位显式本地store，省略使用调用Store；同一固定版本迁移目录后报告身份不变。不同数据源不能共享一个隐式兜底路径，每个绑定在请求和结果中清楚可见。

`ResearchSecurityInputs(security, price=None, status=None, preclose=None)`每个证券一份，未绑定项返回INPUT_NOT_BOUND。输入限1–10个唯一规范证券（六位ASCII代码.XSHG/.XSHE）；锚点为1–31个唯一date或精确YYYY-MM-DD，最早至最晚的自然日包络至多31天。拒绝datetime截断、重复、子类/其他类型和非bool严格标志。按锚点输入顺序、证券输入顺序、下表三用途顺序返回。

JSON请求格式与上面的构造一致：顶层必须且仅为 `inputs`、`calendar`、`trading_dates`。证券绑定字段为security及可选price/status/preclose；引用字段为kind/version_id及可选store_path。未知字段拒绝。

```shell
ashare-data --store /本地store research-preflight --request request.json
ashare-data --store /本地store research-preflight --request request.json --require-complete
```

Python JSON入口是 `Store.preflight_research_request(request, require_complete=False)`。成功输出JSON、退出0；错误使用既有DataError JSON、退出2。默认成功返回可以包含缺口，必须检查研究字段和执行边界；`--require-complete`只要求研究字段齐全，不要求或证明执行资格。`Store.capabilities()['research_preflight']`提供策略、边界和来源kind。

## 用途、日期和字段

策略版本 `research-input-preflight-1`。P/T/next都来自同一个固定源日历capture的 `get_calendar_links()`。锚点不是已知开市日、邻日超界或未知时，保留锚点日期及错误，前日和后继日期均为None；不猜任何一个邻日。邻接采用完整三元组，不能把部分成功包装成完整窗口。

| role（研究用途，非执行read_role） | 日期 | 要求的研究字段 |
| --- | --- | --- |
| prior_day_research | 源日历前一开市日P | 完整日历关系、calendar_open、close、suspended、is_st |
| anchor_day_research | 请求锚点T | 完整日历关系、calendar_open、open/high/low/close、volume、amount、source_preclose、suspended、is_st |
| successor_metadata | 源日历后一开市日next | 完整日历关系、calendar_open、suspended、is_st |

不产生后继行情字段。来源证据可以保留完整原始源行；这些研究用途不是隔离原始字段的权限沙箱。不存在INITIALIZE/DECIDE/结算读权限，也不构建新的owner window plan/profile。

每个用途另外显式列出未支持项：historical_eligibility、listing_status、official_price_limits、rule_version、corporate_action_coverage、adjusted_prev_close、historical_pit、finality、owner_execution_plan，状态均unsupported、值None。它表示本接口未提供这些事实，不代表其他版本从未有过该数据或现实事件不存在。上市年份不代替逐日资格；源preclose不代替实际P日close、调整前收或无事件证明。

## 类型、单位、质量和身份

返回frozen `ResearchPreflightResult`、`ResearchInputRow`、`ResearchInputFact`；rows/facts为元组。`request`、`sources`、`fact.evidence`、`fact.value`与`to_dict()`都返回独立副本。每行带security/anchor_date/trade_date/role/row_sha256；每个字段有state/value/unit/price_basis/source_kind/version_id/evidence/reason。

- 值状态：available为已有源值；unknown为缺绑定、缺日期、未请求或源空/非法枚举等；unsupported为本接口未实现的事实。未知不用0、False或“无事件”填充。
- 价格和源preclose是Decimal的精确十进制字符串，volume为整数股，amount为十进制元。原字段和原串保留在证据。前日close和OHLC是raw_unadjusted；源preclose保持source_reference_adjustflag_3，不舍入或计算替代。
- 每个已取得值带固定版本ID、原行/响应hash、收到时间、原字段和原public quality；preclose附源定义。calendar_relation带完整自然日链。response_received_at可能只是最后收到字节，quality里的完成/完整性未知仍保留；historical_available_at=None。
- report_id绑定规范请求、固定来源描述符、逐用途值/缺口与策略；row_sha256绑定完整用途行。路径、运行时刻、缓存age不进入身份。ID是结果内容摘要，**没有持久化发布新snapshot**，复现须固定wheel、请求与全部源版本。
- 所有结果network_used=False、historical_pit=False、execution_permission=False，execution_admission=BLOCKED_NO_OWNER_PLAN。sources保存现有公共descriptor/quality，不将source_claim_unverified、合成或协议完整性未知提升为市场真实性。

实现只消费Store/view公共接口：research.get_price/descriptor/quality、baostock.get_price/get_status/get_preclose/get_calendar/get_calendar_links。未直接读取私有事实文件或存储表，没有对AQ私有实现的依赖。

## 错误与严格模式

| 错误 | 行为 |
| --- | --- |
| PREFLIGHT_ARGUMENT | 输入类型/规范证券/日期边界/引用kind或ID/JSON字段不合法，查询前拒绝 |
| PREFLIGHT_BINDING_MISMATCH | 绑定证券、频率或价基与用途不符，整体拒绝 |
| PREFLIGHT_SOURCE_MISMATCH | 公共返回的价格行或证据不匹配请求，整体拒绝 |
| PREFLIGHT_INCOMPLETE | 严格模式下所需研究字段有缺口；details保留完整同一报告 |
| INTEGRITY、BAOSTOCK_NOT_PUBLISHED、RESEARCH_NOT_PUBLISHED、QUERY_KIND_MISMATCH、SOURCE_REQUEST_FAILED等 | 破坏、未发布、错种类、失败源或畸形源输入整体拒绝，不降为正常缺口 |

合法来源中的COVERAGE_INCOMPLETE、SOURCE_FIELD_MISSING、UNSUPPORTED_FIELD、CACHE_MISS转成对应字段unknown并保留错误；日历未知/闭市/越界/边界不足转成calendar_relation缺口。日状态与preclose沿用其精确未知原因，false和0不误判缺失。不会吞掉未知异常后返回成功。

路径错误契约：`store_path` 的空白、NUL 或本地不可编码字符串返回 `PREFLIGHT_ARGUMENT`；有效字符串在系统分辨或访问目录时发生的错误（包括超长路径）返回 `PREFLIGHT_SOURCE_IO_ERROR`。库抛出 `DataError`，CLI 返回相同 code、退出 2；两者都不降格为空数据或缺口。既有 `STORE_NOT_FOUND`、`INTEGRITY` 等数据错误原样传播。
