# Oath 整体架构

状态：Draft v0.1；这是实现前的设计基线，不代表能力已实现。

## 定位与边界

Oath 是长期运行的 agent runtime。core 负责执行语义、持久化、调度、工具生命周期；extensions 提供业务 tool、业务 skill 和额外 context 来源；bootstrap 负责装配。biz 提供客户端可用的操作与查询 API。账号、订阅、渠道采集和 Kit 业务逻辑属于 Hymio。

三个设计原则：event-driven、durable、独立 Supervisor（运行监督与恢复）。Supervisor 负责监督 runtime 进程并在异常时发出可审计的恢复命令，它不是用户审批工具的权限系统；权限策略仍在每次工具执行前检查。

## 目录

```text
core/            机制：事件、状态机、agent step、工具系统、恢复
core/journal/    独立追加日志模块，供所有模块统一写入和读取
extensions/      业务扩展：业务 tool、业务 skill、额外 context 来源
bootstrap/       装配与入口：读配置、实例化、注入 CoreRuntime
biz/             runtime 操作、状态和 trace 查询
spec/            整体架构、feature 实现方案、决策与验收条件
```

依赖方向一律指向 `core/`；core 不反向依赖任何一层。

`extensions/` 可缺省 —— 判据是**删掉它，runtime 仍能启动**，只是没有业务能力。
core 自带落盘 journal，所以这条判据对 durability 没有任何让步。只有 `bootstrap/` 必需。

core 不禁止第三方库：需要什么就 import 什么，装在 `pyproject.toml` 里。

除 `core/` 外，其余目录现在都是空的，等有对应代码再填。

## 运行链路

```mermaid
flowchart TD
    Sources[User / Tool workers / System] --> Inbox[Durable ingress]
    Inbox --> Log[Append-only journal]
    Log --> Queues[User / Tool / System logical queues]
    Queues --> Handler[Unified event handler]
    Handler --> State[State reducer]
    State --> Step[Context build + LLM request + postprocess]
    Step --> Intent[Persist commands / effect intents]
    Intent --> Dispatch[Effect dispatcher]
    Dispatch --> Workers[Model and tool workers]
    Workers --> Inbox
    Log --> Supervisor[Independent Supervisor]
    Supervisor --> Inbox
    Log --> Views[Runtime / trace projections]
    Views --> Biz[Management API]
```

队列是持久化事件的投递视图，不要求一开始部署三个消息中间件。唯一业务状态来源是 journal；broker、快照、索引都是可恢复的派生设施。事件决定下一步，不以显式的 `while true → model → tools` 驱动 agent；队列消费器内部使用循环不违背这一约束。

## 执行单元

- `user_id`：第一层隔离；`session_id`：用户内第二层隔离与串行化边界。所有资源以 `(user_id, session_id)` 定位，同名 session 不得跨用户混用。
- `run_id`：session 内每次运行的稳定标识，用于关联输入、模型请求、工具、日志与 trace。恢复同一次运行保留 run_id；新运行分配新 run_id，重试用 attempt_id 区分。run 不是第三层权限隔离。
- `step_id`：一次固定 context / 配置下的模型请求及后处理。
- `invocation_id`：一次逻辑工具调用；`attempt_id`：其中一次实际尝试。
- `epoch`：会话 owner 的 fencing 代数；`seq`：会话所有日志记录的严格递增序号；`state_version`：仅执行状态提交递增的版本，用于并发控制。

当前不引入部署实例身份作为隔离维度。队列、工具映射、幂等键、快照、lease、查询 cursor 和 blob 引用都继承 user/session scope；user_id 来自认证主体或授权委托，不接受调用方任意伪造。

同一 `(user_id, session_id)` 仅一个有效执行状态 owner，但多个模块可以同时向独立 Journal 追加输入、结果与观察记录；Journal 负责原子写入和排序。可以同时有多个工具 worker；不同 session 并发。初期同 session 只允许一个活动 run，后来的用户消息默认排队，显式 steer / interrupt 才改变当前 run。主状态由 reducer 从已提交事件重建；所有时间、随机 ID、模型返回及外部查询结果都必须作为持久化输入，重放不重新生成。

## 独立 Journal 模块

[core/journal](../core/journal/) 提供统一的追加与读取接口，agent、工具、队列、Supervisor、biz 和扩展都能在自身流程中写记录，无需经主 agent 转发。Journal 是模块边界，首版不要求独立服务部署。输入记录触发处理，状态提交驱动恢复，观察记录用于 trace；三者共用日志但写权限和消费语义不同。详见 [feature07](feature07-journal.md)。

## 三个核心约束

1. **事件驱动**：用户输入、模型返回、工具返回、timeout、cancel、恢复命令统一进入事件处理。不是每条事件都调用 LLM；进度、重复或过期结果只更新审计/视图。
2. **Durable**：影响后续行为的输入、决定、effect intent、结果与状态迁移追加持久化。先落盘再 ACK / 派发 / 呈现；持久化失败时停止接收新工作。重放只重建状态，不重做外部副作用。
3. **Supervisor**：独立进程订阅日志、租约和心跳，产生可审计恢复命令。规则优先，未来可用 agent 提议优化；修改 prompt / 架构必须经过版本化评估与发布流程。

这些约束不提供跨第三方系统的 exactly-once。对于执行过但结果丢失的调用，必须允许 `outcome_unknown`，不可一律重试。

## 实施顺序

先完成内存模型与持久化适配器的同一套契约，再做单机 crash recovery；随后独立 Supervisor、biz 查询与多进程 owner failover。HA 依赖具备原子追加和 fencing 能力的共享存储，不能把单机 JSONL 宣称为多机高可用。

详见 [feature 清单](README.md)。语言、存储引擎、模型 SDK、broker、快照频率、租约时长均为待决事项；接口约束先确定。
