# F2 独审问题修复（r2）

修复基于初版`437fec6fcceff0c10a2f96441b6d37792b73c562`，接收task-16独审的
`F2-REVIEW-001`（P2）和`F2-REVIEW-002`（P3）。初版公共API为需修改，原2730项回归和
冻结面板通过不能覆盖该缺陷。本文件记录作者修复与复验，待父任务安排独立续验。

## PIT独有证券公共查询

初版要求证券首先存在于行情/财务快照`dataset.items`，导致只存在于传入FinancialPIT的
证券被提前判为`SECURITY_INPUT_MISSING`。现将显式PIT现金利润比先路由到账本，
只对行情及资产负债表快照分支检查快照证券存在性。未改金融账本、因子公式、数据选取时点或阈值。

固定快照中没有的证券可以由显式 FinancialPIT 账本提供现金流和归母净利润。该路径无需添加空占位证券；真实金额和计算结果仅保存在本地。
混合字段仍返回完整矩形：该证券行情/资产负债表字段缺失；有快照但缺PIT的证券，其现金利润比
仍缺失，不回退到快照。直接构造、哈希文件加载和无快照证券三条路径均有回归。

## 延期首次披露表述更正

普利制药该期报告是2024-07-05延期首次披露，`replaces_in_this_pack`均为null。
原预约日2024-04-29、法定截止日04-30、董事会日06-28及披露当日07-05均不释放该期数值，
研究模式按次日07-06纳入。该证据验证延期披露边界，不能验证原值被更正值替换。

初版`docs/f2-research.md`、测试注释及外部`F2-DELIVERY-AND-REFERENCE-BUDGET.md`
第52行把此事误称“真实更正”。当前源码文档和用例已纠正；初版封存交接文件保留作为历史记录，
本节明确撤回其中“真实更正档案验证”的表述。F2现金利润比的真实修订链验收仍未完成，
本轮未获取或拼接新的财报输入，也未增加历史可见性声明。

## 本次作者复验

| 验证 | 实际结果 |
| --- | --- |
| 原样复制独审P2反例，修复前 | 1 failed；`missing / SECURITY_INPUT_MISSING` |
| 独审48个用例，修复后源码 | 48 passed（与原来47 passed/1 failed分开保留） |
| 本仓F2接口回归 | 30 passed；不计为额外独立来源覆盖 |
| 全库回归 | 2733 passed、241 skipped；跳过项不计通过 |
| Ruff与diff | 通过 |
| 原面板和算术oracle重放 | 3096格；预测SHA不变 |
| 双行情源与排序 | 1548格、129截面；结果不变，仍共用新浪因子 |
| 实际安装wheel | 仓库外导入；3096格及PIT独有证券12格混合查询通过 |
| 安装wheel上的原独审用例 | 48 passed |

原冻结范围、输入投影、3096格预测、263份证据清单及初版交付归档不改。
新回归日志和原样复制的审核用例放在单独`f2-r2`证据目录，重放只写其`replay/f2-batch`副本。
`docs/f2-batch-results.json`继续表示初版结果，不能当作r2公共API独审结果；本轮结构化结果见
`f2-review-fixes.json`。

新wheel SHA256：`931bc6a13bb3dac143053ac97284136ff6385392bac113352cbadacfda2d088d`。
不变预测SHA256：`0c0c91f09a4e8db50d8276c1536e2a3b657f47b4059719a921d013302b70c04c`。

解开新证据包后，以`R2`指向`f2-r2`目录：

```sh
PYTHONPATH=src python -m pytest -q -p no:cacheprovider tests/test_f2.py \
  "$R2/reviewer-tests/test_review_edges.py" "$R2/reviewer-tests/test_pit_adapter_regression.py"
PYTHONPATH=src python scripts/run_f2_batch.py --evidence-root "$R2/replay/f2-batch"
python scripts/check_f2_batch.py --evidence-root "$R2/replay/f2-batch" --anchors <本地原报锚点JSON>
```

安装 wheel 后的真实原题验证器及其财报转录保存在本地验证材料，不随公开源码分发。

## 不变限制与查询状态

六项聚宽数值、排名及选股等价未验证；快照财务不是历史PIT认证，适配器使用日期级不完整档案。
新额度已获授权：最多4次、252格，矩阵72+36+72+72；查询仍为0，旧43停用，无自动重试、
付费、积分扣除或新增权限授权。浏览器未释放，本轮完全离线，无主目录写入或push/release/deploy。
