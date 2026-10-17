# 当前日历观察与回执

观察模块提供显式的一次采集、核验和读取；不会自行调度、启动服务、写账户或发订单。

| 入口模块 | 用途 |
| --- | --- |
| `ashare_data.observations.daily` | 日线与日历的原始响应、完成时刻、固定 packet 和比较 |
| `ashare_data.observations.phases` | 准备/开盘阶段的证券身份、停牌/ST、会话参考价、限价与行动检查 |
| `ashare_data.observations.intraday` | 有限盘中观察、源时间及实际接收时间 |
| `ashare_data.observations.close_confirmation` | 收盘观察的证据核对 |
| `ashare_data.observations.intraday_refresh` | 刷新时钟与显式查询窗口 |
| `ashare_data.observations.response_replay` | 固定响应重放与当前实现的重新解释 |

先运行相应 `python -m <模块> --help` 查看完整参数。示例：

```sh
python -m ashare_data.observations.phases --root data/observations admission --day YYYY-MM-DD --conditional
python -m ashare_data.observations.phases --root data/observations opening --day YYYY-MM-DD --conditional
```

命令中的日期、输出目录必须来自本次计划。当前有限条件策略仍只支持代码声明的证券/窗口。依赖发行人行动基线的阶段，通过 `ASHARE_PILOT_EVIDENCE` 显式指定仓库外合法证据；缺失时阻断，没有内置真实公告样例或自动寻找本机目录。

准备阶段的参考价/限价是会话事实，实时停牌状态可未知；已知停牌始终否决。开盘阶段需重新取得当前日、实际阶段内的行情与时间证据，核对正开盘价、源停牌标志及准备值一致性。旧日/未来时间、身份错配、缺源、限价公式冲突和新行动反证均阻断。读取重算判断，不能只信报告内的 ready 字段。

日标签、供应商时间、HTTP 开始/完成时间、历史 available_at 是不同字段。事后收到的数据不证明过去已可见，当前源股票池不证明历史挂牌，观测价不证明成交。所有 packet 保留 `execution_permission=false`。AQ 必须独立管理账户、时钟、请求身份、实际调用链与 native 风控。

代码移动和版本改变产生新的代码指纹。旧冻结 packet、账户及其解释继续由原固定运行环境读取；新观察/重放输出写入新目录，记录新代码身份，不能覆盖旧包后宣称继承旧验收。
