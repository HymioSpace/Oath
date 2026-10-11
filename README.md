# Oath

持久化、事件驱动的 agent runtime。

core 提供机制：事件运行时、状态机、追加日志、工具生命周期、超时、权限判定、context 组装、
skill 加载、模型调用。扩展只提供业务内容和动作。日志是所有状态的唯一来源，崩溃后从这里恢复。

**状态**：日志存储（feature07）已实现，跑在 JSONL 上，验收套件 21 项全过。
事件运行时、工具系统、恢复和模型接入还没开始。

## 目录

| 目录 | 放什么 | 可缺省 |
| --- | --- | --- |
| [core/](core/) | 机制，含 JSONL 日志 | 否 |
| [extensions/](extensions/) | 业务 tool、业务 skill、额外 context 来源 | 是 |
| [biz/](biz/) | runtime 操作与查询 API | 否 |
| [spec/](spec/) | 设计文档 | 否 |

依赖方向一律指向 `core/`，反过来不成立。`bootstrap/`（装配与入口）等有代码时再建。

「可缺省」指删掉它 runtime 照样启动，而且照样能恢复 —— 因为日志在 `core/` 里。

## 怎么跑

```bash
uv sync
uv run ruff check .
uv run mypy
uv run pytest -q        # 默认跳过 slow
uv run pytest -m ""     # 含 slow
```

## 设计文档

- [整体架构](spec/overview.md)
- [feature 清单](spec/README.md)
- [日志模块](spec/feature07-journal.md)
- [扩展](spec/feature05-extensions.md)

Oath 服务于 [Hymio](https://github.com/HymioSpace/Hymio)。
[Nostalgia](https://github.com/HymioSpace/Nostalgia) 通过扩展提供个人 context。
