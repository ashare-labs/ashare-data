# 0.4.0.dev2：固定视图的可变引用隔离

独审 S1 复现为：修改 Bao 查询结果 `.report.provenance[].records[].source_row` 或 `source_rows[]`，会改变同一 view 和先前 `.at()` 兄弟 view 的后续价格；修改 `records[].source_label` 还会改变行数与标签。dataset_id/行hash没有变，磁盘未损坏。该问题阻断稳定复用；重新打开能恢复不算修复。

修复把所有权放在公开返回边界：ResearchResult 构造时深复制整个报告，并单独复制 DataFrame 的值、索引/列数组、object 单元格和 attrs。`get_price` 只在这个边界交出数据，attrs 与 report 各有独立副本；`to_dict` 对 rows/report 整体递归复制。`.at()` 仍轻量共享内部记录，但内部记录不再由任何公开结果泄漏。

## 输入/输出审计

| 边界 | 审计与处理 |
|---|---|
| Store.import_research 的 paths、文件原文 | 路径不保留在版本内；bytes/JSON在调用内读取，查询由内容地址版本驱动；既有实现保持 |
| Client.save_research、缓存索引、覆盖契约 | 观测索引脱离缓存对象，原文为不可变 bytes；覆盖契约也显式深复制；后续修改 Client 结果或缓存不改已保存版本 |
| ResearchView 的 Store 输入 | 只保存已确认的根路径值，不保留调用者可改的 Store 句柄 |
| get_price 的 security/fields 输入 | 只用于当前选择，不写入内部固定记录；返回表示脱离输入列表；不承诺调用期间跨线程修改参数的行为 |
| ResearchView.descriptor | 整个对象深复制，含 scope、objects、guarantees、reopen、units |
| ResearchView.lineage | 整个谱系信封深复制，含records、observations、calendar、coverage_contract及扩展/旧元数据 |
| ResearchView.coverage/quality | 从内部值构造新容器；Sina覆盖每次使用新解码/契约对象；逐层修改测试保持 |
| ResearchView.get_price / ResearchResult.report | 修复整个嵌套报告的隔离，包含记录包装、source_row、源标签、逐行/hash字段与覆盖/冲突证据 |
| ResearchResult.data / data.attrs | 新表格和独立元数据；object单元格递归复制，索引与列底层数组显式复制 |
| ResearchResult(data, report) 公开构造器 | 不保留传入DataFrame、嵌套单元格、attrs或report；修改输入及输出互不影响 |
| ResearchResult.to_dict / 消费者序列化缓存 | rows和report递归脱离；消费者修改返回缓存不改变固定view或已有其他结果 |
| ResearchView.at | 共享仅内部可达数据，改查询时钟；测试同时覆盖既有/新建兄弟view和fresh reopen |
| Store目录、恢复、发布 | 返回新行字典/标量ID；未改发布/恢复/磁盘校验规则 |
| 旧 DataView | calendar/instruments/lineage/quality原已脱离；补构造器 loaded/manifest 深复制、公开属性副本和conflicts元数据副本 |
| DataError | 构造器details与as_dict均复制，避免错误输出反向影响输入或共享错误状态 |
| CoverageContract / 原Client / JQStyle | 契约原始输入已JSON复制，返回状态/网格/边界为新容器；Client从原文字节新解码，JQStyle依赖已隔离DataView；加入正控制 |

直接访问下划线私有字段、外部重写磁盘文件或并发修改调用参数不属于公开结果隔离保证。公开结果可由消费者自由编辑；编辑后的副本不再代表经过校验的原始数据，不自动重新认证或重算其hash。固定数据身份通过后续公开查询返回的原始内容/hash来校验。

## 验证方法

新增58项测试：逐路径清空公开嵌套dict/list、逐字段篡改价格/标签/hash/证券、DataFrame值/索引/列/attrs、序列化与pickle缓存、输入参数/Store句柄、CoverageContract、旧DataView及DataError。每轮比较原view、此前和此后创建的兄弟view、重新打开的同ID版本以及先前返回结果；逐行重算hash，并比较价格、行数、标签、覆盖和谱系。

性能控制使用2行与366行固定研究读取，保持Decimal及全结果一致；额外证明1000次`.at()`不重读磁盘或深复制整套数据。具体计时、修复前后独审72组、源码/wheel全套结果由本次交付证据记录；这不是生产延迟或大规模历史吞吐承诺。

Manifest/schema/parser/policy 不改，原两个真实研究dataset_id继续有效。解析、时点、覆盖/交易门禁和源选择规则不变。新浪日线请求money/amount的既有错误为 `UNSUPPORTED_FIELD`；支持的字段在个别源行缺值才是 `SOURCE_FIELD_MISSING`，公开研究契约已明确区分。
