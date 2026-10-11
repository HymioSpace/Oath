# Oath 设计与实施记录

[整体架构](overview.md) 是项目入口；每个 feature 使用 `featureNN-short-name.md`，按编号递增，不复用已用编号。

| 编号 | 方案 | 状态 |
| --- | --- | --- |
| 01 | [统一事件运行时](feature01-event-runtime.md) | Draft |
| 02 | [工具系统与 timeout 终态](feature02-tool-system.md) | Draft |
| 03 | [追加日志、快照与恢复](feature03-durability-recovery.md) | Draft |
| 04 | [独立 Supervisor](feature04-supervisor.md) | Draft |
| 05 | [扩展](feature05-extensions.md) | Draft |
| 06 | [Runtime 管理 API](feature06-runtime-api.md) | Draft |
| 07 | [独立追加日志模块](feature07-journal.md) | Implementing |

## 工作流程

新增功能先从 [feature 模板](feature-template.md) 建方案，明确目标、接口、状态、不变量、失败恢复与验收，再实现。实现中更新决定与取舍；结束时记录实际文件、验证证据和剩余限制。修改已有功能就更新原方案，跨模块变更同步 overview 和受影响契约。

状态使用 Draft → Accepted → Implementing → Implemented；未验证前不能标 Implemented。
除 feature07 外，当前文档都还是设计草案。

共享开发约定保留在本目录；本地 AGENTS.md 按用户要求被 .gitignore 忽略。真实 secret、生产 trace 和私人 context 不作为示例入库。语言与工具链已定：Python 3.11+ / uv / pytest / ruff / mypy。数据库、broker、license 未定。
