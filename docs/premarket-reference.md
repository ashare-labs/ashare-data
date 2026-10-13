# 独立盘前参考价重建 v1

`PremarketReferenceView.read(security, as_of=..., role="premarket_reference_reconstruction")`
读取哈希绑定的新浪 KLC **单独编码 `prevclose`**，供有限窗口、显式选择的原生估值研究。
它不改动 `IndependentQueryView.read_adjusted`、通用 `pre_close` 或既有角色权限。

输入 schema 为 `ad.premarket-reference-binding.v1`，policy 为
`sina_native_prevclose_known_cash_research.v1`。调用方必须提供预期 binding SHA256。
每次调用核实解码文件、原始字节、解码器、原公告事实、主公告 PDF 和交易日历哈希；
检查证券、相邻登记/除息日、此前公告、无送转的现金事件，以及唯一的指定源字段。
源参考价必须以源 0.01 元单位表示，与原始前收减毛分红相差不足一个源价格单位。
此条件仅拒绝明显冲突，不产生参考价，不指定舍入方向。字段缺失时拒绝，不回退到公式。

输出只含 `reference_price`、`raw_previous_close`、毛分红及对应日期；不读取当日
OHLC/成交量/成交额，不使用后续累计因子或价格，不返回整份行情。源记录含更晚日期，
投影只选择指定两天；原始字节重新解码是离线证据核验，不是模拟时钟下获取整天行情。

`model_available_at` 冻结为绑定除息日 09:00（+08:00）。接口不授予任意 `as_of`，
`historical_available_at=null`，`historical_pit=false`，策略、撮合及当前报价权限均为 false。
“源字段适用于此 09:00 估值”是明确的历史研究重建假设；采集在 2026 年，不能证明
2024 年同一时刻的收件、发布或修订版本。这项限制与已得到的源参考价是两件事。

B26 的已冻结新浪原始 KLC 经原解码器重新解码，8,449 条记录逐条完全一致。
6/14 特殊记录直接编码参考整数 `1008`，precision code 为 `6`，缩放指数 `[2,2]`，
得到 `10.08`；同源 6/13 原始收盘 `10.8`。读取不执行取整或因子反推。
6/6 发行人实施公告记录每十股毛额 `7.1900`、登记日 6/13、除息及支付日 6/14。
仅使用此前主公告核对，不把 8 月中报作为当时可见输入。

可复核脚本：`scripts/build_b26_premarket_binding.py` 从原独立交付复制证据，
`scripts/verify_sina_premarket.cjs` 在无网络 VM 中重新解码并检查全部记录。
它们均不接收聚宽输入。实际证据在
`evidence/b26-research/`，包含 binding、原字节、
解码器、主公告、原生调用公共响应、回归日志及完整前后 diff。

相关回归：134 passed, 3 skipped；其中新接口 20 项。跳过的是既有前收测试的
外部真实样本，未标记为通过。版本仍为功能分支 `0.9.1.dev10`，测试 wheel 由 SHA256
区分，未发布或改动 v0.9.0。
