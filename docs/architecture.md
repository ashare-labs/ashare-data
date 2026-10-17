# 功能目录与依赖

Python 包只放可复用运行逻辑及它必须携带的资源。源码不依赖 docs、tests、scripts 或任何开发者的证据目录。

| 目录 | 职责与代表模块 |
| --- | --- |
| core | 数据错误、时间/覆盖约束、查询预算与可移植路径检查 |
| market | Client、行情解析、分钟会话、量额校核和显式兼容查询 |
| reference | 上市事实、源日历、停牌/ST、源前收、历史市场状态 |
| corporate_actions | 行动历史、证券/事件身份和来源归档 |
| financials | 报告版本、披露时点、单季/TTM 和精确账本运算 |
| factors | 来源库目录；joinquant 研究定义；Alpha191 定义与算子 |
| storage | 不可变 Store、发布/恢复及长期本地日线库 |
| providers | HTTP、BaoStock/TDX 子进程、JQ 固定观测、zzshare 及源格式解析 |
| observations | 显式当日/分阶段/盘中采集、回执、行动基线与重放 |
| research | 固定研究数据、输入预检、有限事实、条件投影及可移植组合 |
| cli | 安装后的 ashare-data 命令及功能子命令 |

主要调用关系：CLI/根公开 API → 领域读取器 → 提供者解析与存储 → core。因子可消费财务账本和显式市场输入；行情读取不自动计算因子。Store 为持久化门面，用延迟导入连接研究、提供者和发布逻辑。

包资源紧贴使用者：提供者 SDK 指纹、Sina 解码器及其声明在 providers；条件策略在 research；Alpha191 公式、来源映射和第三方许可在 factors/alpha191。资源用 importlib.resources 或模块相邻路径定位，安装到任意目录仍可读取。

scripts 是开发、显式采集配方、比较、构建和验包的唯一目录；tests 使用最小合成输入；examples 展示当前公开接口。docs 只保留当前指南、契约和迁移说明。实际采集、数据库、账户、交付清单和验收输出放在仓库外，由调用方管理。
