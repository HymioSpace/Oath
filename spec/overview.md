# Oath 架构

Oath 是 personal agent runtime，三个核心特性：事件驱动、持久化、独立监督。

应用可禁用内置 Bash，接入显式执行目标工具；设备业务留在应用层，见
[可替换执行工具](feature-execution-targets.md)。
工具授权由应用策略回答 `allow / ask / deny`，Oath 持久化 action digest、审批与终态，见
[工具授权](feature-tool-authorization.md)。

当前架构与 schema 以 [持续运行的 Personal Agent 基座](feature-distributed-personal-agent.md) 为准。
所有权归 `user_id`；主 Agent 与有界子 Agent 分别执行，见 [异步委派](feature-subagents.md)。
用户没有永久线程，WS 断线不终止 Agent。

| 模块 | 约束 |
| --- | --- |
| core/consumer.py、core/intake | 队列独立判断唤醒；持久 buffer 只由 Intake 认领 |
| core/scheduling | 用户级调度、租约、有界 Worker 池；每个执行上下文最多一个有效 Brain 执行者 |
| core/agent | 固定输入 → context → LLM → 后处理，不自行消费队列 |
| core/tool | 幂等 batch / invocation、审批、超时与不可变终态 |
| core/queue | RabbitMQ 交付、confirm、手动 ACK、事务 Outbox relay |
| core/storage | SQLite schema、原子提交、版本检查、派生事件 |
| core/outbound | 持久事件的设备游标与离线补发顺序 |
| core/supervisor | 定期检查持久待办并发送 system.invoke；空输入不调用模型 |
| biz | trace、CLI 等 HTTP 业务能力 |
| Vault | 模型密钥存储；Worker 按 `oath.toml` 中的别名读取 |
| bootstrap | 配置、依赖装配、进程入口和部署生命周期 |
| oath 命令 | 安装后启动 / 管理 Docker，并装配外部 skill、tool、CLI 目录 |
| extensions 与外部目录 | 内置及应用自定义的工具、技能和 CLI；按类型只读挂载 |

数据协议在 core/protocol；数据库、消息中间件、模型、工具执行环境通过接口隔离。
历史 feature 文档保留设计背景，其中的 session、scope、文件 store、Redis 和每用户进程方案不再是当前实现。
阅读实现或新增功能时，先以 [持续运行的 Personal Agent 基座](feature-distributed-personal-agent.md) 为准，再看对应模块的历史 feature。

应用身份由外部创建；默认受信接入，可通过启动参数启用 remote/static 认证。Gateway 与 Biz 共用验证器，鉴权失效不取消后台执行。见 [鉴权](feature-authentication.md)。

轻量客户端状态与模型缓存指标见 [状态与性能观测](feature-runtime-status-metrics.md)。

应用 Markdown 指令与持久工具续行预算见 [Prompt 与工具预算](feature-prompt-and-tool-budget.md)。

See [model selection](feature-model-selection.md) for per-user model choice and immutable step requests.

Native image references and private application resolution: [image input](feature-native-images.md).

Versioned incremental service transitions: [state patches](feature-state-patches.md).

Event XML and per-user context capacities: [context events and window policy](feature-context-events-window.md).

Late tool-result delivery, projected reads and stage timing: [runtime latency](feature-runtime-latency-results.md).
