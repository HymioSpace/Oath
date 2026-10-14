# TODO

| 分类 | 事项 | 边界 |
| --- | --- | --- |
| 存储 | 事务快照 / 增量索引 / 裁剪 | 当前 RuntimeStore 全量 fold，事务复制 scope 状态；长会话需优化 |
| 存储 | 多进程 owner / fencing | JSONL 仅单进程 writer，不能共目录部署多个实例 |
| 工具 | 外部 operation 对账 | running 恢复暂为 outcome_unknown；查询或幂等续执行需明确工具契约 |
| 工具 | 可终止 worker | 当前线程 timeout 不终止外部动作，挂起线程持续占用物理槽位 |
| 上下文 | 压缩产物复用与精确计量 | 压缩按 scope 独立备产物；token 数是估算，未接 provider 计数器 |
| 技能 | 索引热加载与语义检索 | 索引只在启动时构建；find 是子串判定，不做语义召回 |
| LLM | provider fallback | 失败有限重试原请求；跨 provider 切换另定请求与计费语义 |
| 客户端 | 分页、历史增量与流式重试 | session.status 提供当前全量快照；delta 不可靠重放 |
| 接入 | 生产认证 / TLS / 限流 | 默认凭据映射和 AllowAllPolicy 仅演示，Hymio 装配实际策略 |
| Supervisor | 独立观察与恢复 | system.invoke 已可消费，尚无独立监督进程 |
