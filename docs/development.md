# 开发验证、来源边界与许可

本篇说明 `ashare-data` 的架构分层、开发与打包验证脚本、受保护的内部资源锚点、第三方数据源边界及软件许可状态。

## 包结构与模块分层

`src/ashare_data` 下设 12 个功能子包：

| 子包 | 核心职责 |
| --- | --- |
| `core` | 领域模型、数据异常（`DataError`）、时间与标识符校验、查询策略与缓存协议 |
| `market` | 在线行情客户端 `Client`、统一查询路由与分钟/日线量额对账诊断 |
| `reference` | 交易日历、逐日停牌/ST 状态、源前收、盘前参考价、上市事实与历史市场状态视图 |
| `corporate_actions` | 分红送转配股历史台账（`CorporateHistoryView`）与事件范围校验 |
| `financials` | 三大财务报表与分红存储（`LocalFinancialStore`）、官方披露日对齐、分类台账与跨源补采修复 |
| `factors` | 因子目录（`catalog`）、聚宽 19 因子（`joinquant/`）、GTJA Alpha191（`alpha191/`）、Qlib Alpha158（`qlib_alpha158/`）与 Alphalens 评价（`evaluation/`） |
| `storage` | 不可变快照存储（`Store` / `DataView`）、Parquet/DuckDB 读写与 `native_macro_release` |
| `providers` | 新浪、BaoStock（`baostock_receipt/`）、TDX（`tdx/`）、聚宽输入（`joinquant/`）、zzshare、CMES 因子及免费公开源增量采集（`free_updates/`） |
| `observations` | 盘前准备、开盘、盘中刷新、收盘确认与响应重放模块 |
| `research` | `ResearchView`、`IndependentQueryView`、日频研究/执行视图与 Portable 组件生产消费实现 |
| `product` | `LocalDailyStore`、`MarketRelease`、`LocalMinuteProduct`、`NeigezhuMinuteView` 与发布 CLI |
| `cli` | `ashare-data` 命令行总入口及 `local_daily`、`free_updates`、`native_macro` 子命令 |

---

## 日常开发与打包验证

### 结构、静态检查与单元测试

```sh
uv run python scripts/check_structure.py
uv run ruff check src tests scripts examples
uv run pytest -q
```

`scripts/check_structure.py` 自动检查：
1. 12 个功能子包完整性与包根整洁度；
2. Python 文件 `snake_case` 与 `docs/`、`scripts/` 下非 Python 文件 `kebab-case` 命名规范（拦截残留阶段代号）；
3. 内部 `ashare_data.*` 导入目标存在性；
4. CLI `--help` 文本中的文档锚点、Markdown 内部链接及被引用的项目文件路径存在性；
5. `research/conditional-profile.json` 与 `research/conditional-listing-policy.json` 的 SHA-256 常量同步状态。

### 发行包（Wheel）隔离验证

`scripts/verify_wheel.py` 在不含 `src/` 源码树的临时虚拟环境中按 `uv.lock` 哈希安装依赖与构建好的 wheel，校验打包资源、源码指纹、因子目录及全部测试：

```sh
uv build
uv run python scripts/verify_wheel.py dist/ashare_data-0.10.0.dev98-py3-none-any.whl --full
```

如需在离线环境复用本地已缓存的平台 wheel，可附加 `--offline --cache <uv-cache-dir> --wheelhouse <wheel-dir>`（脚本仍强制校验每个本地 wheel 与 `uv.lock` 的字节大小和 SHA-256）。

### 辅助脚本与内置资源锚点

- **新浪历史解码脚本**：`scripts/decode-research-daily.cjs`、`scripts/decode-factor-panel.cjs` 与内置 `src/ashare_data/providers/sina-decode.js` 共同提供新浪压缩 KLC 数据解码。
- **采集与维护脚本**：`scripts/run_daily_update_registry.py`（多源日常增量与第二批 19 项注册表调度）、`scripts/run_tushare_reference.py`（Tushare 财务与分红参考采集）、`scripts/build_daily_execution.py`（构建日频执行数据包）。
- **随包分发的静态资源**：
  - `src/ashare_data/providers/`：`baostock-sdk.json`、`sina-decode.js`、`sina-decoder-notice.txt`及 `free_updates/` 下的 `cmes-free-menu.json`、`supplemental-menu.json`、`futures-product-contracts.json`；
  - `src/ashare_data/factors/alpha191/`：`sources.json`、`NOTICE`、`LICENSE.dolphindb`；
  - `src/ashare_data/factors/qlib_alpha158/`：`features.json`、`qlib-license.txt`；
  - `src/ashare_data/research/`：`conditional-profile.json`、`conditional-listing-policy.json`。

---

## 数据来源边界与软件许可

### 第三方数据源说明

- **新浪财经**：默认在线行情源（`quotes.sina.cn` / `hq.sinajs.cn`），提供近期日线与 1m/5m 行情；成交量单位为股，分钟成交额单位为元。本地日线适配器内置从 AKShare 提取的新浪历史压缩数据解码器，其 MIT 许可声明保存在 `src/ashare_data/providers/sina-decoder-notice.txt`。
- **BaoStock**：通过 `local-data` extra 安装官方 PyPI `baostock==0.9.4`（wheel SHA-256 `0bf71c6069ab5890ff3596632f9d6a5c11ab2cfaf8`）；本库仅校验 SDK 指纹，不捆绑 SDK 代码。
- **通达信（TDX）与 zzshare**：均由调用方显式配置并启用，不在默认 `Client` 中自动路由或回退。
- **数据使用合规**：公开可查询或免注册接口受各自平台服务条款约束；软件代码本身不替使用者授予第三方行情或公告原文的商业转售或公开再分发权利。

### 软件与第三方公式许可

- **项目自有代码**：当前版本（`0.10.0.dev98`）尚未指定开源许可证，保留权利人所有权利；未经权利人明确授权，不得将自有源码用于商业分发或再授权。
- **Alpha191 公式材料**：`src/ashare_data/factors/alpha191/_formulas.py` 转换自 DolphinDB `gtja191Alpha` 模块（提交 `c285bf60208f5c261f6b242c7dbf24717ca71483`），适用 Apache-2.0 许可（原文见 `src/ashare_data/factors/alpha191/LICENSE.dolphindb` 与 `NOTICE`）。
- **Qlib Alpha158 特征定义**：`src/ashare_data/factors/qlib_alpha158/features.json` 遵循 MIT 许可（原文见 `src/ashare_data/factors/qlib_alpha158/qlib-license.txt`）。
