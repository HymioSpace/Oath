# TODO

| 分类 | 事项 | 边界 |
| --- | --- | --- |
| 存储 | 事务快照 / 增量索引 / 裁剪 | 当前 RuntimeStore 全量 fold，事务复制 scope 状态；长会话需优化 |
| 存储 | 多机 owner / fencing | `ScopeLock` 是 flock，只挡同一台机器上的第二个 writer；两台机器各认领同一个 scope 没有任何机制 |
| 存储 | manager 重启后重建 session 表 | session 表只在内存里，manager 进程重启后已存在的 scope 不会自愈；落盘重建需先定「进程组怎么认领」 |
| 存储 | data_root 的清理策略 | journal、buffer、traces、details 只增不减，没有裁剪或保留期 |
| 部署 | 单机容量 | 上限是 `max_sessions` 个 scope，每个 scope 两个进程；跨机共享一个 scope 的机制后续建设 |
| 中间件 | 队列不可用时的行为 | consumer 对异常免疫，线程不会退出；但没有降级路径：`user.message` 提交时直接回 `internal_error`，输入进不了队列。错误码也还是通用的，没区分「队列不可用」 |
| 进程 | agent 崩了没人拉起 | supervisor 只投周期唤醒，manager 只拉起和回收，都不重启。回收读连接数，而探不到的 agent 不算「0 条」，那个 scope 会一直空着直到 biz 退出 |
| 进程 | 崩过的 scope 没法接着用 | journal 还在磁盘上，但 session 表只在内存里，没有拿它重建 agent 的路径。同一 session 重连只能等进程活着 |
| 中间件 | 队列长期不可用时看不出来 | 队列挂了 supervisor 退避重投、consumer 退避重试，进程都还在，但没有任何地方显示「这个 scope 已经聋了一段时间」 |
| 进程 | **用户离开不等于工作结束** | 现在回收只看 WS 连接数，客户端一断开就开始计宽限期，一个正在跑工具的 step 会在宽限期后被停掉。**这是错的**：连接状态只说明「有人在看」，不说明「有没有事要做」。有在飞 step、buffer 里还有待认领输入、工具还在跑的时候，agent 必须继续把这件事做完 —— 回收的判据应该是「工作做完了没有」，不是「有没有人在看」。这是本设计里优先级最高的一条未实现项 |
| 进程 | 线程级的存活检测 | 只测了 consumer 对异常免疫。Brain 线程与工具线程退出没有同样的保护，进程仍然会回心跳 |
| 进程 | 孤儿进程组 | biz 被 `kill -9` 之后，上一轮的 agent 与 supervisor 只能靠 pidfile 里的 pgid 收回；pidfile 被覆盖就再也认不出那一组 |
| 工具 | 外部 operation 对账 | running 恢复暂为 outcome_unknown；查询或幂等续执行需明确工具契约 |
| 工具 | 可终止 worker | 线程 timeout 不终止外部动作，挂起线程持续占用物理槽位；bash 命令由执行环境自己连进程组一起杀，与线程执行是两回事 |
| 上下文 | 压缩产物复用与精确计量 | 压缩按 scope 独立备产物；token 数是估算，未接 provider 计数器 |
| 扩展 | 扩展热加载 | 索引与命令表在 agent 进程启动时构建，改了要等那些 agent 进程重启才生效 |
| 技能 | 索引热加载与语义检索 | 索引同上，只在 agent 进程启动时构建；find 是子串判定，不做语义召回 |
| LLM | provider fallback | 失败有限重试原请求；跨 provider 切换另定请求与计费语义 |
| 客户端 | 分页、历史增量与流式重试 | session.status 提供当前全量快照；delta 不可靠重放 |
| 接入 | 生产认证 / TLS / 限流 | 默认凭据映射和 AllowAllPolicy 仅演示，Hymio 装配实际策略 |
| 接入 | session 凭证由谁签发 | 现在由 manager 签发并直接下发给 client。Hymio 若要自己管身份，要替换签发与 `SessionAuthenticator`，不能退回一个全局 token |
| 执行 | 沙箱执行环境 | `ExecutionEnvironment` 只有宿主机实现；换成沙箱只需换这一个类，bash 与 CLI 都不感知。宿主机实现只做了「不把服务端凭证传进命令」这一条，靠固定白名单，没有按 session 收敛文件与网络的权限 |
| 执行 | 工具权限与命令任意性 | AllowAllPolicy 放行任意 bash 命令，等于给了 agent 宿主机 shell；越权要靠策略层与执行环境共同约束 |
| Trace | OTLP 导出与语义化检索 | span 形状已按 OpenTelemetry 定；当前只落文件，无 exporter，按属性检索未做 |
| Supervisor | 基于 journal 的观察策略 | 目前只有周期唤醒；按记录内容生成 system event 的策略还没做 |
