# TODO

| 分类 | 事项 | 边界 |
| --- | --- | --- |
| 存储 | 事务快照 / 增量索引 / 裁剪 | 当前 RuntimeStore 全量 fold，事务复制 scope 状态；长会话需优化 |
| 存储 | 多进程 owner / fencing | JSONL 仅单进程 writer，不能共目录部署多个实例 |
| 工具 | 外部 operation 对账 | running 恢复暂为 outcome_unknown；查询或幂等续执行需明确工具契约 |
| 工具 | 可终止 worker | 线程 timeout 不终止外部动作，挂起线程持续占用物理槽位；bash 命令由执行环境自己连进程组一起杀，与线程执行是两回事 |
| 上下文 | 压缩产物复用与精确计量 | 压缩按 scope 独立备产物；token 数是估算，未接 provider 计数器 |
| 技能 | 索引热加载与语义检索 | 索引只在启动时构建；find 是子串判定，不做语义召回 |
| LLM | provider fallback | 失败有限重试原请求；跨 provider 切换另定请求与计费语义 |
| 客户端 | 分页、历史增量与流式重试 | session.status 提供当前全量快照；delta 不可靠重放 |
| 接入 | 生产认证 / TLS / 限流 | 默认凭据映射和 AllowAllPolicy 仅演示，Hymio 装配实际策略 |
| 接入 | 按 scope 出 credential | bash 子进程里的 `OATH_CLI_TOKEN` 靠反查配置里的凭据映射，认不出的 session 拿到空 token，CLI 请求会 401 |
| 执行 | 沙箱执行环境 | `ExecutionEnvironment` 只有宿主机实现；换成沙箱只需换这一个类，bash 与 CLI 都不感知。宿主机实现只做了「不把服务端凭证传进命令」这一条，靠固定白名单，没有按 session 收敛文件与网络的权限 |
| 执行 | 工具权限与命令任意性 | AllowAllPolicy 放行任意 bash 命令，等于给了 agent 宿主机 shell；越权要靠策略层与执行环境共同约束 |
| Trace | OTLP 导出与语义化检索 | span 形状已按 OpenTelemetry 定；当前只落文件，无 exporter，按属性检索未做 |
| Supervisor | 独立观察与恢复 | system.invoke 已可消费，尚无独立监督进程 |
