# Feature — Agent Runtime

状态：Implemented（单进程执行）；输入与审批规则见 [Intake](feature-intake.md)。

## 总览

**一个 step 固定一次输入和模型请求。Brain 串行执行 step，三个 consumer 与工具 worker 独立运行。**

```mermaid
flowchart LR
    C[Consumer 请求 wake] --> Claim[原子认领三个 buffer]
    Claim --> Request[保存请求与 tool 槽位]
    Request --> LLM[模型调用]
    LLM --> Save[保存完整响应]
    Save --> Batch[幂等登记工具 batch]
    Batch --> Done[完成本步，通知 consumer 重查]
    Save -->|无工具| Done
```

贯穿全文的规则：输入认领不可撤回；请求重试不混入新消息；已保存响应不重新生成；
执行结束只完成本步 input IDs；只有 consumer 决定是否发起下一步。

## 1 执行身份与依赖

`user_id + session_id` 是隔离和串行化边界。新用户输入开始新 run；工具后续结果保留原调用的
run_id，但可以进入当前 run 的模型上下文。没有用户输入时，system invoke 可启动首个 run。

- `step_id` 在认领事务中生成并保存，恢复沿用。
- 一次响应的 batch_id 等于 step_id；invocation_id 为 `<step_id>-<index>`。
- 模型 tool_call_id 原样透传，只要求同一响应内唯一。

Brain 共用 RuntimeStore、ToolSystem、EventQueue、AgentEventSink 与 AgentConfig。
模型 adapter 实现 ModelProvider；业务工具不能直接执行模型 step。

## 2 唤醒与认领

`wake(scope, trigger, event_ids)` 只登记唤醒并返回。每个 session 一个 Brain worker；
忙时合并触发，consumer 继续接收事件。执行空闲后，触发 IDs 至少有一个仍在 buffer 才认领。
已经被别的 wake 收走的触发不产生空模型请求。

认领在一个 scope 事务内完成：

1. 如果已有 active step，恢复它；失败或暂停态不越过。
2. 选取三个 buffer 全部 `buffered` 输入，按持久化 receipt 序列排列。
3. 关闭未呈现 batch 的全部 tool response 槽位，保存真实结果或 reminder。
4. 保存完整 provider-neutral 请求、step/run 身份、输入 IDs 和状态。
5. 输入变为 claimed，batch 的 presented_by_step 固定。

context 组装为纯数据操作，当前与认领同事务完成。锁内不执行网络、压缩模型或工具。
认领提交后通知各 consumer 重查，避免 presented 变化后遗漏后到结果。

## 3 Context 与请求

`core/context/intake.py` 负责执行视图；`Journal.messages` 读取已提交的 conversation 视图。
查询视图可能停在 assistant 的工具声明；发给模型前必须经 Intake 关闭所有响应槽位。

原 tool 槽位一旦进入 step.input 就不改变。晚到结果追加转义后的 user-role 通知，保留
batch、invocation、event 与 outcome；等待批准是视图 reminder，不是工具终态。
完整序列校验禁止孤立 tool response、未闭合调用和重复 ID。

模型请求保存 messages、工具定义、model、temperature、max_output_tokens、extra_params。
重试与恢复读取同一份请求；新输入留在 buffer。凭据和 transport 配置由部署提供，不写请求快照。
ContextManager 另提供 journal 查询 / compressor 接口；执行链暂不接入压缩，需要保护固定槽位后再实现。

## 4 模型与后处理

调用模型前先持久化 attempts。完整响应校验、保存成功之后才能派发工具。
每个工具的 batch 与 invocation 身份从已保存 step 推导；重复派发登记只返回原调用。
模型响应已经保存时，恢复只补后处理，不再调用模型。

step.completed 把 assistant 消息追加到 conversation，只把该 step 的 input IDs 标为 done，
释放 active_step 并通知 consumer。模型不再声明工具、没有新输入时停驻。

assistant.delta 为在线展示，带 step_id、channel、seq；不参与恢复。完整 assistant.message 已保存
才推送。网络重试可能重新流式输出；客户端应以最终消息 / session.status 替换草稿。

## 5 失败与恢复

模型调用有限重试，失败记录随原 step 保留。LLM attempt 数达到上限后标为 failed，通知 UI；
新的用户输入、工具结果继续 buffer，但不能越过失败 step。`step.retry` 显式重试原 step，
重置预算与重试计数，重复同一控制命令不重复重置。

后处理和调度异常也有限重试。达到上限后保存 failed；连故障记录都无法写入时，进程内暂停并报错，
不继续忙循环。存储恢复或重启后仍以可靠记录为准。

`max_run_steps` 和 `max_run_seconds` 限制连续 step；耗尽时保留认领输入并暂停，显式 retry 继续。
provider 自身必须有有限 I/O timeout；Brain 不能安全强杀任意第三方 Python 调用。

取消先由 system consumer 保存目标 run_id 并阻止工具取得执行资格。
模型 HTTP 请求可以仍在返回途中；响应回来后不再派发该 run 的工具。
执行已开始的外部动作无法保证撤销，交工具系统报告不确定性。

## 6 边界与跨仓库影响

- JSONL + RuntimeStore 只支持单进程 writer，不提供多机 owner lease / fencing。
- 未保存的模型响应可能重试并重复计费；未保存响应中的工具不会执行。
- 输入、conversation 与工具结果当前保留在日志；压缩、大结果 blob、裁剪和分页另行实现。
- Hymio 客户端处理 step.status 与 session.status；失败后提供显式 retry，不能用新消息偷偷跳过失败步。
- Nostalgia 若读取工具事实，应读取真实终态，不能将 reminder 当成执行结果。
