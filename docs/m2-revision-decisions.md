# M2 r2 实施前裁决（2026-10-09）

本文件先于产品实现冻结。范围仅600000、m2a/w1/w2日频条件研究，已有材料足够；新增网络0。
用户已授权完成四项修订后直接在独立分支实施，故不再停在设计审批。新产品仍须独审，数据方不授予后端执行许可。

| 独审项 | 裁决 |
|---|---|
| R1 | 每操作定义严格Query/EconomicResult白名单并带schema/operation。经济结果不含任何层级receipt/evidence/authorization/context/自摘要；嵌套listing也是纯经济payload。先规范query/result，再Evidence，再Authorization，最后运输M2Read。WindowPlan hash排除自身字段。黄金向量必须包含嵌套listing及generation变化。 |
| R2 | 新A-VIS同时覆盖P日末模型闭合→T09策略，以及T15和日末仅engine可见的事后T日线。query_end只是模型边界，非发布证明。consumer显式strategy/engine；T09读T价、strategy读T15、terminal价均拒绝。 |
| R3 | 原官方包manifest、事实/事件/规则原件、两份01-02已有公告、01-20具体修订和本次扩窗附录进入内容寻址闭包。新四项假设明确按窗opt-in，unknown/verified_absent=false不变；已知登记落域即便支付域外硬拒。 |
| R4 | 单一公开canonical codec：aware时刻UTC六微秒Z；十进制无指数去尾零；严格type(x) is int，lot=100接受而100.0/True/字符串拒绝；JSON重复key/非有限数拒绝。原始bytes hash不重编码；经济语义hash规范化。 |

发布身份不自引用：产品OwnerPin固定版本、运行时代码指纹、dataset/manifest、profile/query/parser/rules；
外部release receipt绑定commit/wheel与OwnerPin。wheel不能内嵌自己的SHA，commit/wheel不参与自身代码或数据hash。
WindowSpec→profile→OwnerPin→WindowPlan→Envelope；外部独审/后端binding不反向入owner pin。

严格D1、旧BR1、received/verified、分钟/paper不扩权。实现仅加新模块及Store/CLI入口；旧281908目录和所有原件不写。
原生值codec可以用真实已封存样本离线消费验证，但本轮不导入/启动RQ交易运行，不据此声明native AD08 observed。

原审阅：task-6/m2_data_design_review/报告_M2数据设计与扩窗样本独立审阅.md；细项R1～R4与G01～G11。
