# 2010 年前的主板来源观察

早期扩展追加到同一个 `ashare-data/daily.sqlite3`，原 `raw/` 保留原文。
2000–2009、1992–1999、1990–1991 分段执行，候选由调用者提供的官方名单和代码沿革确定。
当前交易所名单的历史回溯仍缺部分证券阶段，例如 1992 年重组前的旧豫园。
候选并集、响应首末日期或多个源一致都不是全市场历史成员、完整日历或 PIT 认证。
该扩展提供可追溯的逐来源研究观察，不能直接标为完整可回测行情。

## 覆盖与缺口

以当前本地库审计输出为准。HTTP 来源行按 `(symbol,date,source)` 计数，唯一证券日按 `(symbol,date)` 去重；两者都不代表交易所已认证或全市场完整率。代理日历缺口与已证实漏采不同，未认证 BaoStock 声明不能消除缺口。

源失败、空响应和未发出任务分别保留。早期面值、价格、量单位、身份阶段、日历与公司行动仍可能缺证据；差异不能直接转换为缩放规则。异常原值隔离，缺日不补零，不跨源自动复权，查询保持 `strict_usable=false`。

## 读取

```sh
ashare-data local-daily query-early --root ~/.local/share/ashare-data \
  --symbol 600001.XSHG
ashare-data local-daily query-early --root ~/.local/share/ashare-data \
  --symbol 600001.XSHG --start 2000-01-04 --end 2000-01-07
ashare-data local-daily query-early --root ~/.local/share/ashare-data \
  --symbol 000002.XSHE --start 1991-06-08 --end 1991-06-08 --diagnostics
```

`query-early` 省略 `--start` 时使用 `1990-12-01`，省略 `--end` 时使用
`2009-12-31`，两端包含；这只是查询允许范围，不承诺边界当日存在行情。
只传一端时，另一端仍使用上述默认值，不跟随现代库的截止日。
显式日期按原值校验，范围外日期（例如 `--start 2010-01-01`）会报错。
现代 `query`、`query-alias` 及维护操作的默认起始日仍为 `2010-01-01`。

```python
from ashare_data.storage.local_daily import LocalDailyStore

store = LocalDailyStore("~/.local/share/ashare-data", readonly=True)
try:
    result = store.get_early_observations(
        "600601.XSHG", "1990-12-19", "1991-12-31", diagnostics=True
    )
finally:
    store.close()
```

每行保留供应商观察日期、来源、原文标识、上市阶段判断和单位约定。
默认展示独立的 HTTP 来源行，不自动选源、拼接字段、缩放价格或移动日期。
`--source` 可选择单源。`--diagnostics` 才展示 BaoStock 的未准入观察和 HTTP 异常行；
`validation_unknown` 及外层完整性未认证状态不会被行解码成功掩盖。
`strict_usable=false` 表示本扩展没有认证完整经济口径，不表示响应为空。
搜狐的负成交额等异常原值会单独保留，同一响应中的有效行继续入库；不把负数
改成零、取绝对值或当作缺失值。原标准采集入口仍按原来的整段严格校验处理。

现代 `query` / `query --mode compatible` 继续使用原有上市和日历范围。
早期观察日期不会加入两市共用的现代日历，周末记录也不会被研究查询删除。
`factors` 入口保留源版本。1900-01-01 的因子是基准占位，不是公司行动。
因子变化、送配与拆细可能不完整，本入口不生成自动复权或总收益序列。

## 采集与恢复

`python -m ashare_data.market.early_history` 提供 `prepare`、`http`、`plan-tencent`、`query`。
`prepare --evidence <已取证目录>` 使用已经归档的官方名单和候选边界，
不刷新现代元数据。`http --stage 2000-2009 --sources sina,sohu` 会优先回放
新浪全历史归档；`plan-tencent --stage ...` 按候选日历缺口及早期深圳制度风险
选择按年查询，再执行 `http --sources tencent`。成功空集保持 `empty_unknown`。
失败任务保留原因，不因重复执行自动无限重试。
连续三项 HTTP 服务错误或明确的 401/403/429 会停止该来源本段的后续提交，
未发出的任务标为 `deferred_source`，区别于成功空集和已发出但失败的请求。
需要针对旧的搜狐整段异常失败重放时，先执行 `retry-invalid-sohu --stage ...`；
它保留旧作业失败快照，只选择可用异常行隔离处理的失败，不重试身份错误。

任务写入既有 `jobs`，以 `early_jobs` 关联分段、请求来源和验证等级。
每个任务的行与完成状态事务提交；重复规划不增加同一任务，未完成任务可恢复。
新增观察若与已有同源同日行不同会拒绝覆盖。HTTP 行必须落在任务窗口且早于
2010-01-01。`STOP_EARLY` 可停止后续 HTTP 任务提交。

`scripts/collect_early_baostock.py --stage ... --output ...` 复用原匿名会话锁和
受限报文采集，使用 `archive_only=True`，不会调用原库的自动发布。
原文存入 `raw/early-baostock/`；全段完成后用 `--import-only` 导入
`early_diagnostics`。SDK/独立解码一致、终页出现，只能证实响应可解码与分页终止；
它们不能解决压缩 type-96 的外层校验问题。该扩展不把这些行写入 `bars`。
`--import-only` 应在有限采集尝试结束后执行：缺失报文和验证失败会登记为失败，
不会留成可被普通每日采集误接手的待执行作业。进行中的尝试使用 `.progress.json`，
最终完成文件只在采集进程退出前写出。

`scripts/report_early_history.py --output <新目录>` 生成分段/逐证券/逐来源行数、
64 早退覆盖、跨源冲突、量额隐含价格异常、日期边界和因子版本范围。
`proxy-gaps.csv` 使用新浪上海日历作选取和诊断代理；它不是深圳早年或 PT
特别转让阶段的应交易日清单。BaoStock 未认证的停牌/成交声明单独标记，
不用于消除缺口，不把缺日填零或补成停牌行。

本流程不修改原现代覆盖报告。新的最终报告、执行日志和前后校验应另行保存，
并报告原库增长、失败与未认证范围。源端收费、权限、限额和传输障碍须明确列出。
