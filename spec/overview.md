# Oath 架构

Oath 是 personal agent runtime，三个核心特性：事件驱动、持久化、独立监督。

当前架构与 schema 以 [持续运行的 Personal Agent 基座](feature-distributed-personal-agent.md) 为准。
隔离身份仅 `user_id`；用户没有永久线程，WS 断线不终止 Agent。

| 模块 | 约束 |
| --- | --- |
| core/consumer.py、core/intake | 队列独立判断唤醒；持久 buffer 只由 Intake 认领 |
| core/scheduling | 用户级调度、租约、有界 Worker 池；每用户最多一个有效 Brain 执行者 |
| core/agent | 固定输入 → context → LLM → 后处理，不自行消费队列 |
| core/tool | 幂等 batch / invocation、审批、超时与不可变终态 |
| core/queue | RabbitMQ 交付、confirm、手动 ACK、事务 Outbox relay |
| core/storage | SQLite schema、原子提交、版本检查、派生事件 |
| core/outbound | 持久事件的设备游标与离线补发顺序 |
| core/supervisor | 定期检查持久待办并发送 system.invoke；空输入不调用模型 |
| biz | trace、CLI 等 HTTP 业务能力 |
| bootstrap | 配置、依赖装配、进程入口和部署生命周期 |
| extensions、extensions-local | 公开 / 私有工具、技能和 CLI；私有目录不进 Git |

数据协议在 core/protocol；数据库、消息中间件、模型、工具执行环境通过接口隔离。
历史 feature 文档保留设计背景；其中 session、文件 store、Redis、每用户进程设计已被本次架构替代。
