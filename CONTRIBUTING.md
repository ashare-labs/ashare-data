# 贡献指南

## 目录与命名约定

源码位于 `src/ashare_data`，按 12 个业务领域组织子包：

`core`、`market`、`reference`、`corporate_actions`、`financials`、`factors`、`storage`、`providers`、`observations`、`research`、`product`、`cli`。

- **包根**：`src/ashare_data/__init__.py` 仅负责导出公共符号与版本号，包根下不放置散落实现文件。
- **命名规范**：Python 包、模块与脚本统一使用 `snake_case.py`；`docs/` 与 `scripts/` 下的非 Python 文件使用 `kebab-case`。不使用阶段代号（如 `f1`、`d1`、`br1`、`m2`、`round*`）命名文件或目录。
- **依赖方向**：`core` 仅放置基础异常、校验与通用模型，不反向依赖上层业务子包；产品代码不导入 `tests/`、`examples/` 或 `scripts/`。
- **数据与凭据隔离**：不在仓库中提交个人本机路径、API 凭据、真实行情归档或构建产物。

## 文档编写约定

- `README.md` 保持「项目简介、安装指南、快速开始、使用指南、限制与说明」五章结构。
- `docs/` 按功能域组织指南，优先说明接口用途、调用示例与参数/返回结构；全局限制统一归口在 `README.md` 与 `docs/development.md`，不在各篇重复堆砌免责声明。

## 本地验证流程

提交代码或文档变更前，在项目根目录依次运行：

```sh
uv run python scripts/check_structure.py
uv run ruff check src tests scripts examples
uv run pytest -q
```

如涉及打包资源、动态入口或依赖变更，构建 wheel 并运行隔离环境验证：

```sh
uv build
uv run python scripts/verify_wheel.py dist/ashare_data-0.10.0.dev98-py3-none-any.whl --full
```
