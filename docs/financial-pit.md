# 财务 PIT 查询与真实公告验收

`FinancialPIT` 在已有 `FinancialLedger` 上提供多证券、多字段查询。可按一个历史截止时点取当时符合可见性规则的 YTD 财报版本，也可转单季或计算 TTM；每个结果保留公告日期、来源页码、源文件 SHA256、修订关系及全部计算输入。查询实现不含证券硬编码，不计算或更改命名因子。

## 可调用接口

```python
from ashare_data import FinancialPIT
from ashare_data.f1_financial import RESEARCH_MODE

view = FinancialPIT.from_file(
    ".local-evidence/financial-pit/pack.json",
    evidence_root=".local-evidence/financial-pit",
    # 正式消费时可传入 docs/financial-pit-evidence.json 的 pack_sha256
    # expected_sha256="...",
)
result = view.query(
    ["000637.XSHE", "300124.XSHE"],
    ["net_operate_cash_flow"],
    period="2024-12-31",
    as_of="2026-08-07",
    basis="TTM",
    mode=RESEARCH_MODE,
)
for row in result["rows"]:
    print(row["security"], row["status"], row["value"])
```

输入完整且可见时返回金额；缺少所需报告时返回 `missing`，不会补齐。若要查各股最新一期，省略 `period`。同一股多个字段的最新可见报告期必须一致，否则该股全部请求字段返回缺失，避免悄悄混用不同季度或回退旧期。

输入兼容既有 `ad.f1-small-pack.v1`：`reports`、`observations`、`sources` 的结构不变。`FinancialPIT(pack)` 可用于调用者已装载的账本，结果标记 `source_files_verified=false`；`from_file` 必须逐项核验所有声明的证据字节，并检查报告 PDF、文本与索引均在源清单中。可用 `expected_sha256` 额外锁定整个转录包。校验哈希不证明源真实性或历史完整性。

| 参数 | 规则 |
| --- | --- |
| `securities`、`fields` | 单字符串或非空、不重复的字符串序列；输出按证券×字段保持矩形排列 |
| `period` | 可省略；指定时为标准自然季度末 `YYYY-MM-DD` |
| `as_of` | 带时区的 ISO 时刻，或研究模式下的标准日期；日期表示**上海该日开始时 00:00** |
| `mode` | 必须显式传 `RESEARCH_MODE` 或 `"strict"`，无自动回退 |
| `basis` | `"YTD"`、`"quarter"`、`"TTM"`；默认 YTD |

首批支持合并报表、人民币元的五个累计流量字段：`operating_revenue`（营业收入）、`total_operating_revenue`（营业总收入）、`net_profit`（合并净利润）、`parent_net_profit`（归母净利润）、`net_operate_cash_flow`（经营现金流净额）。字段彼此不替代。资产负债表时点数、母公司单体报表、非人民币、非自然年口径尚未接入。

每行 `status` 为 `ok`、`missing` 或 `visibility_unproven`。后两类的 `value=None` 且附原因；格式/口径错误、未知字段、重复版本或歧义修订链抛 `AcceptanceError`。金额为十进制字符串；不以 0 代缺失，不转二进制浮点、不追加修约。

## 日期、版本与计算约定

`RESEARCH_MODE` 的全名为 `reported_date_next_day_incomplete_archive`。只有公告日期证据的报告，在公告日的**后一自然日上海 00:00**纳入；它不是交易日推算或实际发布时刻。例：2024-07-05 披露的普利财报，在 `as_of="2024-07-05"`、当日收盘和当日 23:59:59 都不可见，`2024-07-06T00:00:00+08:00` 才可见。

严格模式要求带时区 cutoff，以及报告的精确 `published_at`、`available_at`、原始首次发布证明与完整修订证据；可用时刻为前两者的较晚值。本批官方索引虽部分带毫秒数字，尚未认证其代表原始首次发布时间，故全部保留日期精度。对本批已公布资料请求严格模式得到 `visibility_unproven`；已知未来资料仍先排除，不能污染更早查询。结果始终保留 `historical_pit_certified=false`，输入声明和算术检查不签发认证。

版本按原报告与明确替代链选择，而非按本次抓取时刻排序。修改 `captured_at` 不改变历史答案；较晚才采集的原报也不能覆盖已可用的更正。后续报告的比较列只在该后续报告自己的可见边界进入，不倒签到比较期结束日。同日多个版本如果没有精确发布顺序，继承原账本的拒绝规则。

单季为本季累计减上季累计，Q1 直接取累计。TTM 要求连续四季完整，并校验四个单季之和等于 `本期累计 + 上年全年 - 上年同期累计`；年末 TTM 为全年值。所有项在**同一 cutoff**独立选最新可见版本，结果 `terms` 和 `quarters` 保留带正负号的原始输入。缺任一季度即缺失，不仅凭年度桥凑出数值。

## 本批实证范围

固定转录包包含 **4 公司、18 份报告/更正、66 个版本化原始值**；48 个本地源文件共 46,197,398 字节。18 份指包含数值的报告与更正，另有 2 份延期说明 PDF。每条原始值有 1-based 物理 PDF 页码，所有金额逐页核对；关键表格另外渲染核对单位及原值/更正值列。

| 公司 | 有界覆盖 | 验收用途 |
| --- | --- | --- |
| 300124 汇川技术 | 2022Q3～2024Q1 的 7 份报告；32 个收入、净利润、现金流值，含若干更早比较期 | 四季 TTM/年度桥、新报告边界、后期比较列版本；这些比较列数值相同，未宣称发生数值更正 |
| 000637 茂化实华 | 2024Q1/H1/Q3/FY 原报及 2026-08-06 更正；8 个现金流值 | 四个报告期的真实数值更正，单季与全年 TTM 随版本同步变化 |
| 603169 兰石重装 | 2025Q1/H1/Q3 原报及 2026-04-23 更正；18 个营业收入、净利润、归母净利润值 | 同一更正公告跨报告期、跨字段；利润由正转负；不足四季明确缺失 |
| 300630 普利制药 | 2023FY、2024Q1 实际均于 2024-07-05 披露；8 个值与两份延期说明 | 原预约日/法定截止日/董事会日均不释放数值，实际披露次日才纳入 |

更正公告可以改变同一报告期的金额和单季/TTM 链。日期级研究约定在公告次日释放新版本；具体原报转录和预期结果保存在本地验证材料。

原报与更正公告分别绑定自己的版本；不以更正公告中的“更正前”列倒签原始可见性。

普利的截止日事实来自公司逾期公告；查询时只使用实际财报公告日期。2024-06-28 的董事会签署日不是发布日。[逾期公告](https://static.cninfo.com.cn/finalpage/2024-05-01/1219966316.PDF) · [年报](https://static.cninfo.com.cn/finalpage/2024-07-05/1220543547.PDF) · [一季报](https://static.cninfo.com.cn/finalpage/2024-07-05/1220543574.PDF)

## 复放、打包与生产限制

公开源码只含合成测试；真实财报转录、固定预期结果、原始 PDF/索引及原题验证器均保存在忽略的本地验证目录，不进入 wheel/sdist。安装软件不会联网取财报；调用者可按同一包结构装载自己合法取得的来源。

```sh
.venv/bin/python -m pytest -q tests/test_financial_pit.py
.venv/bin/python examples/financial_pit.py \
  --pack .local-evidence/financial-pit/pack.json \
  --evidence-root .local-evidence/financial-pit \
  --security 000637.XSHE --field net_operate_cash_flow \
  --period 2024-12-31 --as-of 2026-08-07 --basis TTM \
  --mode reported_date_next_day_incomplete_archive
```

本地原题覆盖公告前后、修订前后、延迟披露、单季/TTM 及未来数据扰动。该有界验证没有扩展为全市场历史可见性认证。

当前来源是现在抓到的官方附件和索引。原报告附件与更正前数值已对应，但没有每份附件的历史网页存档证明，也未穷尽中间修订。发现茂化实华“2023年报”当前附件（公告 ID `1221364107`）制作日期晚于索引日期，已从本批原报输入排除。因此本次交付是**可复放的日期边界查询和真实样本验证**，不是全市场任意时刻严格 PIT。转录字段有限、历史缺口和未认证时刻必须继续向调用者显式暴露。

完整机器清单见 [financial-pit-evidence.json](financial-pit-evidence.json)。冻结提交 `75842ab47821c402e7e79976d4f912fb2df3ecb9` 已完成独立有界研究验收，未发现实质阻断；完整报告与证据绑定见 [本地整合记录](financial-pit-integration.json)。原交付机器记录的 `independent_review_completed=false` 作为生成时状态保留，不倒改原证书。既有因子门槛、旧失败与批量研究适用范围不变。
