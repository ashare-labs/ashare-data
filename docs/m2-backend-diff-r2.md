# 给A宽dev9的精确公开字段差异

对照5c867f2的`docs/m2/data-design-diff.md`及独审缓存/选窗修订。产品最终接口以[m2-api.md](m2-api.md)和固定wheel为准。
无须变更后端模板/账户/两阶段责任，也不要求把后端内部hash改成owner hash。

1. **OwnerPin包身份**：运行时返回version/runtime_code_sha256和原计划七类data/profile/query/parser/rules身份；commit/tree/wheel放外部release receipt，避免自hash。后端DataBinding同时保留二者及owner envelope hash。
2. **Consumer**：增加显式run_id；ReadContext增加epoch及consumer(strategy/engine)。其他字段沿原提议。window_id仍owner字符串；WindowPlan带owner/spec/query/源角色，后端ID另存不覆盖。`use`绑定selected_window，在原文加载前拒绝别窗context。
3. **运输类型**：统一不可变M2Read，含query/result/sources/evidence/authorization五个M2Document；每个to_dict复制。前收`.value`为Decimal，完整value_basis/scoped_ratio在result；call在query，P行refs在sources。不要将此类型简化后重签owner hash。
4. **Evidence/Auth**：沿r1分离，revalidate仍只返回新授权；`read.with_authorization(auth)`组合后必须verify。query/result/sources/Evidence全部稳定，唯当前授权变化；同价异行、假设变更、错窗/数据/profile/request/策略/run绑定拒绝。native_call_id由后端添加，owner不签它。
5. **Canonical**：显式公开typed codecs+严格白名单；generic canonical不推断字符串类型。经济价格字段先decimal规范；raw字符串/原文另放sources，单独hash进Evidence。Context UTC固定六微秒。整数拒100.0/bool/数字字符串。JSON CLI与Python同向量。
6. **Native codec**：午夜Python datetime及pandas.Timestamp明确转上海date；Float64数组/标量按Decimal(str(float))精确比；拒float32/Decimal假装原生值。codec消费报告native_call_observed=false。后端需另观察真实调用并与决策一起持久化。

这些是owner公开字段，不是要求后端采用相同内部DTO名字。对接前先核release receipt、安装包code fingerprint和独审；
本产品所有envelope的backend_execution_authorized仍false。初始两个十日窗只是可显式条件读取，不证明十日原生运行已完成。
