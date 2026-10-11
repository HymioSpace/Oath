# Feature 02 — 工具描述、执行与终态

状态：Draft；依赖 feature01、feature03。

## 工具注册

Tool descriptor 至少包含 `tool_id, version, description, input_schema, output_schema?, effects, default_timeout_ms, max_timeout_ms, replay_policy, cancellation, permissions`。

- `tool_id` 使用带命名空间的稳定名称，例如 `kit.hymio.ledger.expense.create`。
- `effects` 为 `read / write / external`，只帮助策略判断，不自动证明 replay safe。
- `replay_policy` 为 `never / idempotent / reconcilable`；默认 never。
- `cancellation` 为 `cooperative / process / remote / unsupported`。
- descriptor 版本和实现 digest 固定到 invocation；进行中的调用不换实现。

模型可见 tool alias、provider tool-call ID、内部 tool_id、invocation_id、attempt_id 的映射写入日志。不同模型的 ID 不直接当内部唯一键；命名冲突拒绝注册。映射和调用查询必须带 user/session scope；日志同时携带 run_id，禁止通过猜 invocation_id 跨用户读取。

## 调用契约

调用必须有 `invocation_id, attempt_id, tool_id, tool_version, arguments, timeout_ms, deadline_at, idempotency_key, user_id, session_id, run_id, step_id, owner_epoch`。`timeout_ms` 是执行命令必填项，由 runtime 根据 descriptor 默认值补齐并限制范围，无需模型自己猜值；零、负数、无限值和超过最大值都拒绝。

总 deadline 从 invocation 接受时计算，包含排队、执行、重试与结果收敛；进度不自动续期。重试不得重置总 deadline，新的 attempt 有独立上限。

```text
accepted → queued → running → succeeded | failed | timed_out | cancelled | interrupted
                 └───────────→ terminal（未执行也可到期/取消）
```

另设 `effect_outcome = applied | not_applied | unknown`，与 runtime 状态正交。超时的写工具通常 outcome 为 unknown；读取到外部确认结果可追加 reconciliation 记录，但不覆写原终态。

## 执行与 timeout

1. 提交 invocation、deadline 和 dispatch intent，再发到 worker。
2. Worker 校验 capability、工具版本、租约/派发 token；执行工具并发送 progress 或 completion candidate。
3. 独立 deadline service 扫描持久化 deadline，即使 worker 永久挂住也提交 timeout candidate；启动恢复必须补扫到期项。
4. Handler 以原子 compare-and-append 决定逻辑 invocation 的唯一终态，并在同一 commit 记录 tool terminal event 的 outbox intent。
5. Publisher 至少一次投递终态至 tool queue；重复投递无害，直到 handler durable ACK 才标记已消费。

完成与 timeout 竞态由存储端时间和提交顺序判定：超过 deadline 尚无已接受终态时 timeout 胜出，不信任 worker 自报完成时间。日志永久不可用时无法承诺即时写出终态；恢复存储后 deadline service 必须补齐，期间停止派发。

工具终态是“本次等待已结束”，不等于操作系统进程或远程副作用必然停止。发送取消、关闭连接、回收独立进程是 cleanup 工作，具有单独状态、deadline 与重试；不可清理的远端任务保留 unknown 并上报 Supervisor。

## 恢复与迟到结果

已终态 invocation 的重复 completion 不再影响 agent，仅追加必要的审计/对账记录。旧 attempt、取消 run 或非法 dispatch token 的结果也不进入 context。owner failover 后如要接纳仍存活 worker 的结果，必须先写 `attempt.adopted`，明确由新 epoch 认领；否则按 feature03 对账或中断，不能凭时间戳直接接受。

业务写工具默认不自动重试。仅当执行端支持稳定 idempotency_key，或能先查询外部状态证明可安全继续，才创建下一 attempt。同 invocation 各 attempt 共用业务幂等键；用户有意再次执行必须新建 invocation。

## 验收场景

- 永不返回/不可取消的工具仍让主会话收到唯一的 timeout 终态。
- 队列等待期间到期，不执行工具并返回 timed_out。
- worker 执行成功后、结果落盘前 crash：不会盲目重做业务写入。
- 完成/timeout/cancel 并发仅一个终态；迟到成功可对账，不能再次推进 run。
- descriptor 默认 deadline 和调用显式 deadline 都有持久化且有范围校验。
