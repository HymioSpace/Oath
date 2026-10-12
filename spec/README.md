# Oath 设计与实施记录

[整体架构](overview.md) 是项目入口；每个 feature 使用 `feature-<short-name>.md`，
不使用编号前缀，按创建顺序排列。

| 方案 | 状态 |
| --- | --- |
| [Journal：追加日志、状态存储与恢复](feature-journal.md) | Implementing |

已废弃的 `feature01`–`feature08` 编号方案及其文档已删除，不复用。

journal 模块本身已实现并验证，但整体仍是 Implementing：`Recovery.pending`
的消费方（agent runtime 的 fold）还不存在，「恢复后继续干活」这条完整链路
尚未跑通过。

## 工作流程

新增功能先从 [feature 模板](feature-template.md) 建方案，明确目标、接口、状态、不变量、失败恢复与验收，再实现。实现中更新决定与取舍；结束时记录实际文件、验证证据和剩余限制。修改已有功能就更新原方案，跨模块变更同步 overview 和受影响契约。

状态使用 Draft → Accepted → Implementing → Implemented；未验证前不能标 Implemented。
当前 `core/` 下只有 journal 模块有实现，其余目录仍为空。

共享开发约定保留在本目录；本地 AGENTS.md 按用户要求被 .gitignore 忽略。真实 secret、生产 trace 和私人 context 不作为示例入库。语言与工具链已定：Python 3.11+ / uv / pytest / ruff / mypy。数据库、broker、license 未定。
