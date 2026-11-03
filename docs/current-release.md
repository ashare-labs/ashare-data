# 跟随正式市场发布

部署的 `market_release` 支持两种选择。冻结研究保持 `asset`、正整数
`release_id` 和 `manifest_sha256`。日常产品可使用：

```json
{"asset": "published-cmes", "mode": "current"}
```

该资产的 kind 为 `market_release`，path 指向正式发布根。current 模式不能
同时声明固定 ID 或 SHA。解析只读取 CURRENT；真正打开读器时验证其不可变
manifest 链和 metadata。CURRENT 缺失、坏清单、对象损坏均报错，不回退旧源。
不会读取临时批次、扫描候选目录或启动下载。

每次构造 `LocalDailyProduct.from_deployment()` 或 `LocalMinuteProduct()` 固定
一个发布快照。当前对象的多次调用、日线及参考信息保持同一版本。后续新对象
看到新 CURRENT；保存 receipt 的 root、release_id、manifest_sha256 可重开旧版。
AQ 每个市场操作构造新读器，研究覆盖/发布的参考绑定来自同一个日线读器。
冻结研究与已有结算账户不因此重新计算。

`market coverage` 从已提交的 manifest 汇总表、文件、行数、日期和标准读器状态。
默认价格查询只支持 1d/1m；其他已存储周期、集合竞价及竞价详情不因此获得
标准查询或执行认证。tick 未发布时明确返回 `not_published`。下载数量不等于
发布覆盖。股票名称、上市区间、复权因子缺失均不得按经验补齐；交易日历覆盖
到未来不代表存在未来行情。`ashare_data.Client` 及顶层在线 `get_price` 仍为
原来的在线接口；本设置只改变正式本地产品及使用该发布快照的 AQ 接口。

对于超大不可变对象，可显式设置 `ASHARE_MARKET_VERIFY_CACHE` 为源库外的
绝对路径。缓存目录必须是当前用户私有目录；首次仍验证完整 SHA256。
只有路径、预期哈希、大小、mtime_ns、ctime_ns、inode、device 全部相同时
才复用成功记录。变化会重新完整验证；验证中变化或哈希不匹配会拒绝。
缓存使用进程锁防止重复扫描和原子记录，不锁发布器，也不写原文件。
缓存与部署配置属于同一受信任本机边界，不用于抵御可同时修改数据和缓存
的本机主动攻击。首次新 metadata 的完整验证可能耗时，新版本不会被静默
替换为旧版；可在正式切换前用最终读器预热。删除缓存只会导致重新校验。
