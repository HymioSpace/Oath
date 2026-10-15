# Oath

持久化、事件驱动的 agent runtime。

core 提供机制：事件运行时、状态机、追加日志、工具生命周期、超时、权限判定、context 组装、
skill 加载、模型调用、trace 记录。扩展只提供业务内容和动作。日志是所有状态的唯一来源，
崩溃后从这里恢复；trace 是派生的，不参与恢复。

**状态**：三 consumer、工具批次、审批执行、durable drain、三级压缩、技能索引、trace、
bash 执行环境与 CLI 代理已实现。
当前为单进程 JSONL runtime；语义和恢复边界见 [Intake](spec/feature-intake.md)。

## 目录

| 目录 | 放什么 | 可缺省 |
| --- | --- | --- |
| [core/](core/) | 机制，含 JSONL 日志 | 否 |
| `extensions/` | 业务 tool、业务 skill、额外 context 来源 | 是 |
| `biz/` | runtime 对外暴露的功能，不是进程 | 否 |
| `spec/` | 设计文档 | 否 |

`core/` 内部：

| 目录 | 放什么 |
| --- | --- |
| `core/protocol/datatype/` | 数据类型：`Scope` / `Message` / `AgentState` / `Cursor` + journal 专属的记录格式与值对象 |
| `core/protocol/interface/` | 接口，按模块分文件（`journal.Journal`） |
| `core/protocol/error.py` | 契约层的异常 |
| `core/utils/` | 编码、时间戳与 token 估算，不构成契约 |
| `core/journal/` | 机制：校验、幂等语义、游标运算、messages 投影 |
| `core/journal/impl/` | 存储后端（v1 只有 JSONL） |
| `core/trace/impl/` | trace 存储后端（v1 只有 JSONL） |
| `core/shell/` | bash 工具与执行环境接口，宿主机实现在 `impl/` |
| `biz/trace/` | 按 trace_id 取回一条 trace（`api.py`） |
| `biz/cli/` | 业务 CLI 的命令表与路由 |
| `extensions/cli/` | 业务 CLI 的实现，纯函数 |

数据类型和接口是跨仓库契约，进 `core/protocol/`，依赖方向单向指向它；编码只是
手段，进 `core/utils/`。消费方从 `core.protocol.datatype`、
`core.protocol.error`、`core.protocol.interface` 三个包入口 import，不碰子模块，
也不走 `core.protocol` 这一层——两个 `__init__.py` 都不重导出符号。
`bootstrap/` 负责配置读取、模块装配与运行入口，并监听两个 server：WebSocket 收
command，HTTP（`bootstrap/biz_server.py`）供 biz 只读查询与 CLI 代理。

「可缺省」指删掉它 runtime 照样启动，而且照样能恢复 —— 因为日志在 `core/` 里。

## 怎么跑

```bash
uv sync
uv run ruff check .
uv run mypy .
uv run pytest tests/ -q        # 默认跳过 slow
uv run pytest tests/ -m ""     # 含 slow（真实进程 SIGKILL）
```

## 设计文档

- [整体架构](spec/overview.md)
- [feature 模板](spec/feature-template.md)
- [Journal：追加日志、状态存储与恢复](spec/feature-journal.md)
- [Intake：三个 consumer、工具批次与 Brain 唤醒](spec/feature-intake.md)
- [Intake 场景与崩溃推演](spec/intake-cases.md)
- [Trace：一次 ReAct 循环的 span](spec/feature-trace.md)
- [Bash 执行环境与 CLI 代理](spec/feature-bash-cli.md)
- [user_context：客户端带来的会话上下文](spec/feature-user-context.md)

Oath 服务于 [Hymio](https://github.com/HymioSpace/Hymio)。
[Nostalgia](https://github.com/HymioSpace/Nostalgia) 通过扩展提供个人 context。
