# 贡献约定

适用于本仓库的新代码和整理工作；跨仓采用相同目录职责、命名和交付身份原则，具体框架约束留在对应仓库。

- 按实际功能组织模块，避免用任务编号、批次、验收轮次或个人目录命名。行业正式名、供应商字段及公式编号（如 Alpha191）按其语义保留。
- Python 文件/包/函数使用 snake_case，类使用 PascalCase，常量使用 UPPER_SNAKE_CASE；非 Python 脚本和文档文件使用 kebab-case。根 README 固定为项目简介、安装指南、快速开始、使用指南、限制与说明五个主章节。
- src 只放运行逻辑与必要资源；scripts 是开发、采集配方、构建、检查的唯一入口目录；tests 放最小可复放夹具；examples 只展示当前接口；docs 按读者任务保留 12–19 篇当前指南、契约和迁移说明；不保存可由 API 输出的覆盖 CSV。旧实验与验收输出退出发布树，通过 Git 历史查阅，不另建 archive 改名保留。
- 路径来自显式参数或通用用户数据目录，不写开发者绝对路径。真实数据库、原始采集、公告、账户和冻结交付存放在仓库外；禁止凭据、个人路径及原文进入提交。不要把任何真实文件当测试写入目标。
- 明确依赖、单位、时区、缺失与来源；禁止静默换源、补值或把合成夹具当真实证据。必要资源与模块同目录，用包资源定位，并保留第三方来源与许可。
- 接口改名同步更新导入、CLI、子进程字符串、示例、文档、生成链和下游映射。0.10 迁移不提供旧名字兼容别名；数据格式兼容必须显式说明。代码、schema 或资源变化生成新身份，不改旧账户/回执来继承旧验收。
- 提交前运行下面的离线检查；行为变化补充能捕捉回归的最小测试。不要为可逆文案修改堆测试。记录实际命令、结果与限制，不把旧实验计数当本次证明。

```sh
uv sync --locked --no-config --python 3.12
uv run --locked --no-config ruff check src tests scripts examples
uv run --locked --no-config python scripts/check_structure.py
uv run --locked --no-config pytest -q
uv build --no-config
python scripts/verify_wheel.py dist/ashare_data-0.10.0.dev5-py3-none-any.whl --full
```

构建工具的来源目录和安装验证输出目录分开。wheel 需验证包资源、CLI、公开读取与数值行为；sdist 解包后同样运行检查并能重建 wheel。`verify_wheel.py --offline --cache DIR` 可显式使用预备缓存，不得把缺依赖或跳过联网测试写成已完成采集验收。

跨仓提交先交付明确接口映射和候选提交；下游在独立环境改 pin、重跑自己负责的调用链和账户测试。只在各仓验证完成后讨论发布，不能自动部署、替换冻结环境或刷新生产数据。

工具职责、Node/QuickJS 的不同用途及生成命令见 [开发指南](docs/development.md)。供应商字段标识（例如 Eastmoney 的 f1/f2）是外部协议，内部命名整理不得改变；请求构造需用离线协议测试保护。
