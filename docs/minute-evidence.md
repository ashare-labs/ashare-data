# 分钟证据与准入诊断

`MinuteEvidenceView(binding, expected_binding_sha256=...)` 只读固定来源包，复用历史市场的哈希、回执、路径、状态和限价校验。`seal_minute_evidence` 封存明确输入；原始响应、发行人证据及交付包由调用方另行保存。

公开查询包括 `state`、`bars`、`calendars`、`label_coverage`、`corporate_actions` 和 `admission_report`。先看字段 status、source_quality、缺失原因及 ready_for_execution；来源行本身不表示可交给引擎。

`visibility="posthoc"` 明示事后研究；received 要求 knowledge_at 不早于实际采集完成；verified 对未获证实的历史可见性拒绝。源分钟标签不推断 interval_start/end；未知边界时 `require_intervals=True` 拒绝。源量额单位声明与独立认证分开，源 preclose 不自动成为公司行动后的限价参考价。

状态未知、单位未确认、历史股票池/行动覆盖不足和时间边界未证实会保留在门禁报告。`export_backtest()` 当前仍拒绝 `MINUTE_GATES_BLOCKED`，软件测试通过不关闭这些事实缺口。旧一次性采集/续包脚本与验收计数不属于当前分发；当前运行结果需对新输入重新核验。
