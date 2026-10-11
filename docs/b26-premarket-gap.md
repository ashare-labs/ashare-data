# B26 盘前参考输入检查（2026-10-11）

**当前结论：现有固定 AD 产品没有可供 AQ 在 2024-06-14 09:00 使用的独立盘前调整参考价。**
本工作树从 `d4df91033a742d9d13800c7daeadabde92388404` / `0.9.1.dev10` 建立。
本次只记录实际公开接口检查及数据请求；没有新增价格投影、修改行情、开放角色或发布版本。

已实际调用 `IndependentQueryView.read_adjusted`：binding 为
`/Users/yinming/Documents/Codex/2026-10-10/task-3/independent-query-delivery/binding.json`，
SHA256 `15861bfe56e337f001f4f6eac38601dcb6f051ab30fb652b566a6dc60a7e1313`；
产品 `B25-adjustment`，证券 `000001.XSHE`，`basis_date=2024-06-14`，`fq=pre`，
角色 `independent_retrospective_query_research`。既有产品覆盖前一日，允许检查候选，
不代表将其改名为 B26 估值输入。

返回的 6/13 调整 close 为 `10.08099999999999456355035744287866`，factor 为
`0.9334259259259254225509590224887651`。AD 没有执行价格舍入。
`source_publication_time=null`、`historical_pit=false`、`strategy_visible=false`，
提供商数据采集于 2026-10-10。源因子的有效日期不是 2024 年盘前到达时钟。
不能将该值自行量化到分，或把当日日线的后续字段倒填为 09:00 参考价。

公开契约分别保留以下界限：

- [独立查询](independent-queries.md) 的前复权仅使用源因子比，不隐式舍入，拒绝初始化和策略角色。
- [源前收](preclose.md) 的 `reference_price_eligible`、`historical_available_at` 仍未知；数值已知不等于适用性成立。
- 原公司行为事实支持公告日期、登记日、除息日、支付日及毛额；精确历史发布时间、日内到账顺序、完整其他行动排除仍未知。
- 现有 TDX 10:00 点价是事后点价研究；它不能提前成为盘前价格，也不是完整分时行情。

聚宽补证的 09:00/09:01 公开字段支持 AQ 需要这种角色，但补证只作为比较证据。
本次没有将补证导入 AD，没有请求或制造一个数值必须等于聚宽答案的产品。
[后续输入请求](b26-premarket-request.json) 中 `price` 和可见时钟证据均为空，状态为阻塞。
应先由独立来源或有独立依据的显式研究绑定满足该请求，再由 AQ 审查并接入。

实际请求、完整公共响应及回归日志位于本工作树 `.verification/b26/`，最终冻结副本位于
`/Users/yinming/Documents/Codex/2026-10-11/task-6/b26-closeout/ad/`。
