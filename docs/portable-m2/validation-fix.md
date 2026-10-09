# 0.8.0.dev2：发布前数值及事件身份校验

本候选修复独审 PM2-R1 / PM2-R2。`tests/test_regression_gates.py` 逐字节保留独审的六项反例；文件开头的“currently 6 failures on e72f713”描述旧候选结果。另加边界测试，覆盖非有限数值、指数/空白、精度与范围、整数位数、未知证券、事件字段类型与重复 ID。

## 数值契约

价格、amount（存在时）、前收均使用消费端 `decimal_text`：十进制字符串最长48字符，禁止指数、空白、NaN/Infinity；绝对值小于 10^24，规范值最多8位小数。OHLC 为正且满足高低关系；amount 非负，空字符串保留缺失含义。前收必须为正，其实际生成的90%/110%价格限制也须通过同一编码器。

源 volume 为1至24位 ASCII 数字的非负整数字符串；有限的前导零可保留。长度检查先于整数转换，5000个零也拒绝；不修改 Python 全局整数转换限制。这是本版输入/序列化边界，不是对市场真实成交量的认证。校验与消费共用转换逻辑，保留原始文本与对象 hash，不修补或重写源字段。

市场数值非法时生成可重开的 `BLOCKED` 报告，`compose_m2` 报 `M2_COMPOSITION_BLOCKED`，不产生产品快照。组件格式或事件结构非法可在导入时提前返回 `DataError`。

## 事件身份契约

每条事件必须为对象，包含 `id`、`event_type` 和 `entitled_security`。ID/类型为1至128字符标识符：首字符为 ASCII 字母或数字，其余可含字母、数字、`_ . : -`。事件 ID 唯一。证券代码为六位数字加 `.XSHG` / `.XSHE` / `.XBSE`；null、空白、unknown 或错误类型不能解释成已知无关。已提供的关键日期须为 `YYYY-MM-DD` 字符串或 null。

先检查全部事件结构及身份，再按明确的证券代码过滤相关性。保留 `360003.XSHG` 优先股记录的既有排除行为；普通股相关事件仍须满足已支持类型、必要日期和窗口外日期限制。格式有效只代表明确的输入声明，不认证证券或事件真实存在。证据引用、事实完整性 unknown、`verified_absent=false` 与 PIT unknown 均保持原义。

## 复验与准入

设置 `ASHARE_M2_COMPONENTS` 为合法私有组件目录、`ASHARE_M2_BUNDLE` 为旧格式兼容夹具后运行完整 tests；生产只使用公共配方/组件及其原件，不依赖旧实验包。版本、parser/runtime、dataset/profile/plan/assumption/envelope 身份必须重算，旧 pin 不可套用。

本修复不新增事实，不采集网络数据，不授予 A宽 execution。作者复验与独立准入分开；新候选仍须独审后才能更新生产 pin。
