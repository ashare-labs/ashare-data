# 真实日历日线观测前置

`python -m ashare_data.daily_observation` 每次在前台显式采集一次，不提供调度或交易执行许可。它使用已有新浪原价日线、SSE 年度休市日历和原 HTTP 收据。只支持当日上海时间 17:45 以后、1–3 只沪市证券、恰好120个已公布日历的开市日；17:45 是本入口观察窗口，不是新浪最终发布保证。跨年度日历覆盖不足、停牌缺行、最新日未到、修订或未知均阻断，不填值。

```sh
PYTHONPATH=src python -B -m ashare_data.daily_observation --root /独立观测目录 capture \
  --day 2026-10-12 --securities 600000.XSHG 600036.XSHG 601318.XSHG
PYTHONPATH=src python -B -m ashare_data.daily_observation --root /独立观测目录 read --capture 完整SHA256
PYTHONPATH=src python -B -m ashare_data.daily_observation --root /独立观测目录 recover
```

示例日期只有实际当日才允许；历史/未来日期在联网前拒绝。17:45前返回等待。采集失败的 attempt 保存错误与时刻；成功 packet、原文 blob 均不可变。重开核对原文、证券、120日网格及实际请求/接收时间。重复观测保留独立接收收据；重叠历史行改变则保存前后值、阻止新信号，没有自动“接受修订”。恢复只处理被杀进程的未完成 attempt，不联网；完整包已发布则确认原包，未完整则标为 interrupted，须显式重新采集。

该观测能证明本机在记录时刻拿到了这些版本，不能证明首发时刻、最终性、停牌/ST/限价或公司行动覆盖。`execution_permission=false` 永久保留。SSE日历不扩展为深市个股资格。此入口不修复或提升既有因子/F1验收状态，也不替换已有历史 research/execution 包。

测试 `tests/test_daily_observation.py` 使用合成网络/时钟，包括真实子进程 SIGKILL；不把这些测试算作真实跨日通过。真实观测、网络探测和原文应保留在仓库外，禁止发布到Git。
