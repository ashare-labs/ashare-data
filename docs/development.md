# 开发、构建与工具职责

本页面向维护者。安装与首个查询见 [README](../README.md)，升级已有消费者见 [迁移说明](migration.md)。生成命令必须使用调用方选择的新输出目录；仓库不保存真实数据库、公告原件、账户或冻结交付。

## 免费增量采集的交付与目录迁移

`free-updates` 与 `scripts/run_daily_update_registry.py` 提供已配置来源的采集、快照查询和按日续跑；`daily-update` 保留全市场日线/近期分钟队列与显式候选发布入口。财报官方目录的分页恢复位于 `financials.official_expansion`。这三组功能已共同集成，不要求运行时保留开发工作树；未配置的来源不会因此自动启用，采集不隐式发布正式市场库。

部署沿用仓库外的既有运行根。迁出临时任务目录时，保留原始响应、观察库、最新快照指针、检查点、冷却和请求预算，按原配置依赖复制固定运行环境。只变更可移植配置中的路径并重新绑定其哈希；不能改写不可变原始记录、快照 manifest 或包文件来继承验收。路径参与摘要的控制器检查点和请求预算目录需要明确迁移映射，防止搬目录造成同日重复请求。

切换前验证源/目标文件摘要、数据库行数与完整性、快照查询及解释器身份，并用原冻结入口预览。两个既有调度应在空闲时逐一切换到已验证的持久路径，保留原时刻、UID 和单实例标签，不同时保留两套自动采集。原始安装和旧配置保留作回退；后续实际运行产生新状态后，回退前须协调状态，不能直接覆盖成旧副本。自然运行与手动检查分别记录。迁移记录、精确路径和回退步骤保存在部署目录，不把个人路径、真实数据或运行环境提交到仓库。

## 目录与依赖

| 目录 | 职责 |
|---|---|
| core | 数据错误、时间、覆盖、查询预算、可移植路径 |
| market / reference | 行情与兼容查询；日历、状态、上市、前收及股票池 |
| corporate_actions / financials | 事件与股份来源；财报账本、版本和时点 |
| factors | 具名因子目录、研究定义、Alpha191 算子 |
| storage | 不可变 Store 发布/恢复；可变本地 SQLite 日线库 |
| product | 正式本地库目录、只读查询、共享清单选库与有界组合输入发布 |
| providers | 来源协议、HTTP、SDK 子进程、格式解析与解码资源 |
| observations / research | 实际时钟观察；固定快照、输入预检与条件研究 |
| cli | 安装后的 ashare-data 命令 |

CLI/根公开入口调用领域读取器，后者使用来源解析、存储和 core。因子可消费财务与显式市场输入，行情读取不隐式计算因子。Store 用延迟导入连接各读取器，不持有账户或撮合逻辑。

src 只包含可复用逻辑和必要资源，不在运行时依赖 docs、tests、scripts。scripts 是开发/采集/构建入口，tests 放确定性协议或合成夹具，examples 演示公开接口。docs 按读者任务保持 12–19 篇 Markdown；生成型表格由当前 API 输出，历史验收结果不进入指南。Python 使用 snake_case，文档及非 Python 脚本使用 kebab-case。供应商字段、公式编号和协议标识不得因内部模块改名而替换。

## 为什么保留 JavaScript

仓库实际保留 **两份开发 CJS 和一份随包 JS**。它们都服务源数据解码，没有 Web 前端、npm 项目或 Node 常驻服务。

| 文件与所有者 | 实际消费者 | 依赖及保留理由 |
|---|---|---|
| `src/ashare_data/providers/sina-decode.js`；AD providers | `daily_sources.decode_sina`，本地 Sina 日线维护 | 固定第三方解码实现由 Python `quickjs==1.19.4` 执行；与 `sina-decoder-notice.txt` 一起进入 wheel。使用 `local-data` extra，**不需要 Node** |
| `scripts/decode-factor-panel.cjs`；AD 因子输入工具 | `providers.factor_panel_inputs`、`factor_research_inputs` 校验其 derived JSON 与原文/decoder SHA | Node 标准库 fs/vm/crypto/path；按显式 scope 窗口生成因子输入。Date/binary64 的 JSON 字节是现有输入契约的一部分 |
| `scripts/decode-research-daily.cjs`；AD 日频研究工具 | `scripts/build_daily_research.py` 读取其投影，再由 DailyResearchView 验证固定绑定 | 同样仅需 Node 标准库；现有固定窗口配方，含首末源日期、拒绝覆盖及 `--verify` 字节复验，不能直接用因子脚本替换 |

两个 CJS 都只执行 SHA256 为 `39a599c94dde4df1c2eb0882d4bff9560160cfd52ead0797cebb04b3122c2f52` 的外部纯 KLC decoder；该 decoder 由输入包提供，**不是**随 wheel 的 Sina 解码资源。捕获响应只提取严格匹配的编码载荷，不当作脚本运行；VM 禁止动态字符串/Wasm 代码生成、限制 5 秒，输出上限 20,000 行。不能放宽 SHA 或互换 decoder 以绕过失败。

开发 CJS 在 Node 22 验证，无 npm 安装步骤，通常 Python 使用者无需 Node。改成 Python 需先证明解码结果、Date/binary64 序列化及输出字节一致，再版本化生产链；仅删除扩展名不同的文件会断开当前消费者。

```sh
# FACTOR_ROOT、DECODER、SCOPE 都是显式选择的输入；derived 输出可能被重写。
node scripts/decode-factor-panel.cjs "$FACTOR_ROOT" "$DECODER" "$SCOPE"
# RESEARCH_ROOT 必须是新输出目录；已有 derived 文件会拒绝写入。
node scripts/decode-research-daily.cjs "$RESEARCH_ROOT"
# 对已有投影只做字节复验，不写数据。
node scripts/decode-research-daily.cjs "$RESEARCH_ROOT" --verify
```

日频配方的输入窗口固定为 2019-07-01～2020-05-31、2021-08-01～2022-06-30、2023-07-01～2024-04-30；它不承诺任意历史下载。因子配方读取 scope.input_windows。二者输入 securities 结构、输出字段和覆盖写入策略不同，暂不为了去重合并成含隐式模式的通用脚本。

因子采集仍是有界研究配方：panel 的财报日期固定2017-01-01～2024-03-31，research 固定2019-01-01～2024-03-31；scope 窗口不自动扩大财报范围。panel 直接运行即请求公开源，research 还需 --allow-public-network；输入和 output 必须显式选择。

## 其他工具与生成链

`capture_action_history.py` 的显式采集流程需要 curl。uv 用于锁定依赖、构建与隔离验包。这些外部命令不是普通 Python 读取 API 的依赖。

| 任务 | 入口与输入 → 输出 |
|---|---|
| 因子研究与复验 | `scripts/evaluate_factor.py`、测试辅助 `tests/support/compute_factor_research.py`、`tests/support/compare_factor_panels.py`、`tests/support/compare_factor_reference.py`、`tests/support/validate_factor_reference.py` |
| 公司行动 | `scripts/capture_action_history.py` → 人工核对事实 → `scripts/build_corporate_history.py`；[公司行动指南](corporate-actions.md) |
| 日频与历史状态 | `scripts/build_daily_research.py` / `scripts/build_daily_execution.py` / `scripts/build_historical_market.py`；固定来源和外部基线摘要 → 新绑定 |
| 本地库维护 | `scripts/collect_early_baostock.py`、`scripts/accept_local_daily.py`、`scripts/audit_local_daily.py`、`scripts/accept_daily_update.py` |
| 开发交付 | `tests/support/execution_delivery_refs.py` 检查交付指针；`scripts/check_structure.py`、`scripts/verify_wheel.py` 检查产品 |

生产脚本不是一般行情 API；保留它们的边界、必需参数及失败状态。新生成物写入新目录，旧 manifest、hash、失败记录不能被“整理”成新通过。命令具体选项以各 Python 脚本 `--help` 为准。

## 离线验证与发行

```sh
uv sync --locked --no-config --python 3.12
uv run --locked --no-config ruff check src tests scripts examples
uv run --locked --no-config python scripts/check_structure.py
uv run --locked --no-config pytest -q
node --check scripts/decode-factor-panel.cjs
node --check scripts/decode-research-daily.cjs
node --check src/ashare_data/providers/sina-decode.js
uv build --no-config
python scripts/verify_wheel.py dist/ashare_data-0.10.0.dev5-py3-none-any.whl --full
```

`verify_wheel.py --offline --cache DIR --full` 使用预备锁定依赖缓存，在无 src 的环境运行安装包及公开测试。还应解开 sdist，检查其文档/测试/示例/脚本能复验并重建 wheel。测试不应读取真实证据目录；需要私有输入的复放与免费源联网抽样另列结果，未做或不可用不得记为通过。

JS、SDK/policy JSON、Alpha191 来源与许可通过相邻路径或 importlib.resources 定位；wheel/sdist 必须保留。源码 `.py/.json/.js` 的路径和内容参与 runtime identity；纯注释和文件移动也可能改变身份，新候选必须记录新摘要。数据 schema、owner policy 和包版本分别维护，不能一起盲改。

Alpha158 对照使用 `scripts/alpha158_reference.py`（详见 [因子目录与评价](factors.md#qlib-alpha158)），生产计算专项为 `tests/factors/test_qlib_alpha158.py`。
