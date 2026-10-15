# 本地历史日线兼容补齐验收

2026-10-15，基于实际 Workspace main
`75bd88cc38b193d1f5ee6459a78ad26f75d0bb5e`，在独立分支
`codex/history-compatible-query` 完成。原库为 `~/Workspace/ashare-data-local`，
原库、Workspace main、既有环境及服务均未修改；未联网采集或推送。

以下先记录 `7edc029` 的初次验收；独立审阅提出的两项问题及正式修复后的最新结果见文末。

实现入口和返回契约见 [local-daily.md](local-daily.md#显式兼容补齐查询)。
`get_price(..., mode="compatible", alias_captures=[...])` 与
`ashare-data local-daily query --mode compatible --alias-capture ...` 已实际使用现有原文数据运行。
默认单源入口仍沿用原实现；本次不改变普通查询的既有字段、CLI 列表结构或原库覆盖统计。

## 实际结果

验收脚本 `scripts/accept_local_compatible.py` 逐项读取现有两份 gap dispositions 清单，
再调用公开查询接口；源码和已安装 wheel 各通过 **25,582** 项检查。
两次运行的 11,905 行逐日结果 CSV 完全相同。

| 验证范围 | 补齐前 | 兼容模式 | 结论 |
| --- | ---: | ---: | --- |
| 原生缺额清单 | 11,787 日无金额 | 11,784 日无金额 | 仅补入 601313 的三个已核验别名金额 |
| 明确来源停牌缺额 | 11,782 日 | 11,782 日仍为空 | 未补量额、未归零 |
| 002647 2025-03-12 | 停牌与新浪价量冲突 | 量额仍空，返回状态冲突与公告身份 | 重复前日量未作为成交依据 |
| 600656 2016-05-12 | 来源标交易、量额空 | 原标记保留，返回状态冲突与最后交易日推断 | 推断与明确停牌公告分开 |
| 原生缺日清单 | 118 日 | 118 日仍缺 | 每日有停牌证据及别名停牌观察；未制造交易行 |
| 601313 全旧代码有效区间 | 默认 Sina 1,368 行，0 行有金额 | 1,368 行，1,320 行有金额 | 48 行因兼容性不足继续为空；118 停牌缺日另列 |
| 600656 2016-05-10～11、优先 Sina | 2 行，0 行有金额 | 2 行都有金额 | 普通原代码来源间真实字段补齐 |
| 600000 2026-09、优先 Sina | 0 行 | 21 行 | 从库内其他来源真实整日回退 |

三个原生缺额日为 2013-05-27、2013-05-28、2015-10-22。
每个补入值均与既有 `alias-query-smoke.json` 的归档值逐值一致，带有
`provider_symbol=sh601360`、capture ID、公告哈希、代码事件身份及
`[2012-01-16, 2018-02-28)` 有效期。
2018-02-28 起查询旧代码不再返回行。
2015-12-03 的真实原代码/别名收盘价差异明确返回，阻止从该别名拼入字段。

多来源差异仍然存在，`has_conflicts` 包括未被采用的候选来源差异。
上表旧代码全区间金额覆盖低于“任意来源非空”的并集，是精确兼容规则的有意结果，
不能把可解释的保留空值视为已补齐，也不能把来源观察数量当作实际成交日数。

SQLite 使用 `mode=ro`、`PRAGMA query_only=ON` 和读事务。
查询前后抽取同一批原始行的汇总哈希相同，`total_changes=0`，主数据库大小与 mtime 均未改变。
脚本禁用网络连接，所有输出写入工作树 `.local-evidence/compatible/`，未复制数据仓。

## 回归与安装包

- 全量回归：**2,963 passed**，75.48 秒。
- 相关四组本地查询/别名/验收测试：**85 passed**，其中 30 个新兼容查询用例。
- `ruff check src tests scripts` 和 `git diff --check` 均通过。
- wheel 在工作树 `.local-evidence/package-env/` 内安装；第三方依赖从既有环境只读复用。
  原环境包没有更新或替换。
- Python 全清单重放和 CLI 六类实际案例均从 `/tmp` 执行，清除 `PYTHONPATH`；
  确认模块来自新环境的 `site-packages/ashare_data`，未从源码树导入。
- CLI 覆盖三个金额日期、两种状态冲突、公告转载停牌缺日、旧代码失效边界；
  默认模式仍输出列表，兼容模式输出带 `metadata` 的对象。
- wheel 无本地库、真实报价 CSV、原始归档或验收数据载荷。

新增反例还包括不同价格/成交量/ST/昨收、供给金额互相冲突、异常单位、NaN/Infinity、
未知/复权来源、伪装成别名的库内 source 标签、损坏公告、归档重复、非日历日、
上市区间外日期及未知零量；这些用例验证不补错值及结构化错误输出。

## 复跑

在安装了当前代码及测试依赖的环境中，从本仓库执行：

```sh
python -m pytest -q
ruff check src tests scripts
python scripts/accept_local_compatible.py \
  --root ~/Workspace/ashare-data-local \
  --alias-capture ~/Workspace/ashare-data-local/raw/bao-alias-sh601360-old-period \
  --output .local-evidence/compatible/replay.json
```

安装包验收先执行 `python -m hatchling build -t wheel`，将 wheel 用
`pip install --no-deps <wheel>` 安装到独立环境，再从 `/tmp` 使用该环境的 Python
运行同一个脚本的绝对路径，并把输出指向工作树。
`package_file` 必须指向独立环境的 `site-packages`。
只读库必须保留已有报告、归档和发行人公告；缺少必需输入时脚本会失败，不重采或造数据。

本次本地证据文件（不随源码/wheel 发布）：

| 文件，均在 `.local-evidence/compatible/` | 内容 |
| --- | --- |
| `acceptance-final.json` / `.csv` | 源码验收、输入报告哈希、逐日结果、来源和数据库不变证明 |
| `wheel-acceptance.json` / `.csv` | 安装包重放同一完整清单 |
| `package-smoke.py` / `.json` | 可重跑的安装包 CLI 案例和模块实际路径 |
| `full-tests-final.log` | 完整测试日志 |
| `manifest.json` | 上述文件哈希及源码/wheel 结果一致检查 |

逐日 CSV SHA-256：`27d741c316389fd3bb03f33f3b6aa94abd998aa8caee3776a78c17aa4021e972`。
验证 wheel SHA-256：`52c749795dc400c85b98b35df23e4eea96eddcf2be9df126beb619423b8def90`。

本次验证覆盖指定真实缺口清单、上述额外区间和完整代码回归；没有声称重新审计全库三千多万条原文。
原代码行情按既有入库适配器的单位和未复权契约读取，别名报文与相关公告原文每次查询重新核验。
历史 PIT、所有未来代码变更及其他供应商别名格式不在本次新增保证范围内。

## 独立审阅后的正式修正

在 `7edc029` 上重跑审阅者的 32 个独立反例，确实复现 **5 failed / 27 passed**。
两项问题已在本分支正式修复，没有直接套用隔离实验补丁：

1. 基础行有正成交量而金额供给行成交量为空时，允许用基础成交量校验金额。
   仍须全部共享字段精确一致，并通过组合后的最高/最低价、量额范围校验。
   供给方非空成交量冲突、多个金额供给方有分歧、停牌、异常金额都继续保留空值。
2. 抽出共用的来源身份校验，未知单位/复权来源及未经验证别名不参与状态判定。
   这些观察仍保留在 `observations` 和拒绝诊断中。身份可信的来源即使 OHLC 损坏，
   其明确停牌标记仍可阻断补齐；`status_conflict.observation_ids` 也只引用实际参与判定的来源。

新增 **16 项正式回归**，覆盖上述正例、组合边界、供给分歧、四个不可信状态反例和
“可信停牌但 OHLC 损坏”保护。修复后的结果：

| 验证 | 结果 |
| --- | --- |
| 完整仓库测试 | **2,979 passed**，70.25 秒 |
| 四组相关正式测试 + 审阅者 32 项独立测试 | 源码与新 wheel 分别 **133 passed** |
| 实际缺口清单 | 源码与新 wheel 分别 **25,582 checks passed** |
| 真实逐日结果 | 与初次交付完全一致；11,787 缺额日仅补三日，118 个停牌缺日不变 |
| 安装后真实 CLI 入口 | 三个金额日期、两种状态冲突、停牌缺日与旧代码到期边界通过 |
| SQLite / WAL / SHM | 安装包验收前后大小和 mtime 全部一致；`total_changes=0` |
| 静态检查 | `ruff check src tests scripts`、`git diff --check` 通过 |

新 wheel 在独立的 `.local-evidence/review-fixes/package-env/` 中安装，从 `/tmp`
清除 `PYTHONPATH` 后执行。97 个 Python 文件与修复源码逐字节一致，原安装环境未替换。
新 wheel SHA-256：`4f206d61a7e4557ed35ab46e6c3ae59f7fe8959a88dadea45b36779c88e528fe`。
真实 CSV SHA-256 仍为 `27d741c316389fd3bb03f33f3b6aa94abd998aa8caee3776a78c17aa4021e972`。

修复证据保存在 `.local-evidence/review-fixes/`：

- `independent-before.log`：五个失败反例的修复前复现。
- `related-tests.log`、`wheel-tests.log`、`full-tests.log`：正式与独立回归。
- `source-acceptance.json/.csv`、`wheel-acceptance.json/.csv`：真实清单重放。
- `package-smoke.py/.json`：安装包公开 CLI 验证。
- `manifest.json`：新旧真实结果、源码/wheel 一致性及文件不变证明。

复跑真实清单仍使用上文同一 `accept_local_compatible.py` 命令；将 Python 切换为新环境即可
复跑安装包验收。正式回归已进入仓库测试集，无需依赖审阅目录才能防止这两项问题再发生。
