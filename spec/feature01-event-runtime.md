# Feature 01 — 统一事件运行时

状态：Draft；依赖 feature03 的日志语义，早期可用内存适配器验证。

## 协议

每条事件包含 `schema_version, event_id, user_id, session_id, run_id?, step_id?, type, queue, producer, correlation_id, causation_id?, occurred_at, accepted_at, expires_at?, payload`。回调另含 `invocation_id / attempt_id` 或 model request ID。Journal 补充 `seq`；执行状态提交还含 `owner_epoch, state_version`。生产者不得自己决定 seq 或伪装系统事件。`run_id` 对已开始运行的事件必填；尚未分配 run 的输入先用 event_id 关联，启动时追加 input-to-run 绑定，保留原记录。

`(user_id, session_id, event_id)` 是稳定去重键。相同 ID + 相同 payload hash 返回原 receipt；相同 ID + 不同内容返回冲突。超大 payload 存不可变 blob，日志只保存引用、hash 和大小；blob 必须在引用提交前持久化。

`occurred_at` 是生产者声称的时间，`accepted_at` 由存储端产生。业务顺序由 seq 决定，不依赖客户端时钟。

| 队列 | 输入 | 调度原则 |
| --- | --- | --- |
| user | message、follow-up、steer 请求 | 同会话 FIFO，只有 handler 决定是否启动 run |
| tool | 工具进度和终态候选 | 尽快收集，所有调用结束后才能解除该 step 的 barrier |
| system | model completion、timeout、cancel、owner recovery | 控制事件优先；配置处理配额避免其它队列饥饿 |

同一队列保持接收顺序；跨队列允许优先级调整，实际消费顺序必须记录。不是靠 broker 自身顺序恢复运行。

## 处理步骤

1. Ingress 校验身份、schema、大小、去重，追加 `event.accepted` 后 ACK。
2. Handler 按 `(user_id, session_id)` 租约选择事件，将 event ID、消费决定、状态迁移与新 commands 放进同一个原子 state commit；验证 expected_state_version 和 owner epoch。其它模块追加观察日志不会改变 state_version。
3. 该 commit 后才推进投递 ACK。crash 后重新投递可通过已消费的 event ID 去重。
4. 需要 agent step 时，context builder 读取已提交前缀、memory 查询结果及工具目录；产出固定 context artifact、内容 hash 和版本引用。
5. 提交 `model.requested` intent，模型 worker 发起有限时请求。结果以 `model.completed / failed / timed_out` 回到 system queue；postprocess 校验 tool calls，持久化响应与命令。
6. 无工具调用则形成 final answer；有工具调用则等待本 step 所有逻辑 invocation 终态后，由事件触发下一 step。

Context retrieval 等外部 I/O 同样属于受 deadline 与 journal 管理的 effect，不能在重放时默默重新查询。首版对流式 token 按有界 chunk 提交后再呈现，最终响应另存；不能把已显示但未持久化的内容当可靠输出。

## 新消息与中断

运行中的普通新消息排队；steer 在下一个 step 边界应用并记录选择的输入集。interrupt 立即终止当前 run 的继续调度，持久化 cancel commands，再收敛所有工具调用。取消请求本身不是“外部操作已取消”。一个未确认停止的工具不能阻塞系统控制队列。

队列满时入站拒绝并返回可重试错误，不能先 ACK 再丢弃。已接收的用户输入永不静默过期：取消/到期写出明确处理结果；进度事件可以合并，终态不能丢失。

## 验收场景

- 同事件重复投递不会增加第二次模型/工具 intent。
- 工具结果乱序到达：barrier 只在全部逻辑调用终态后解除一次。
- 模型返回非法工具或参数：记录校验错误，不执行工具。
- 活动模型请求期间 interrupt 仍能被消费；晚到结果不能重启已取消 run。
- 进程在接受、消费或派发边界退出：已 ACK 输入仍可找到且可继续处理。

## 待决

具体公平调度权重、队列容量和 token chunk 大小通过负载验证后确定。
