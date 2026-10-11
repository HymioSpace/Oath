# Feature 03 — 追加日志、快照与恢复

状态：Draft。目标是先固定恢复语义，再逐步实现 HA。

## 持久化模型

独立 Journal 模块为每个 `(user_id, session_id)` 提供有序、不可原位修改的 commit stream，协议见 [feature07](feature07-journal.md)。一次 commit 含 `commit_id, user_id, session_id, first_seq, records[], schema_version, checksum`；执行状态 commit 另含 `owner_epoch, state_version`。所有 records 原子可见或完全不可见。已启动运行的记录必须有 run_id；会话级记录可显式为 null。

Journal API 提供 `append(scope, records, commit_id)`、`commitState(scope, expected_state_version, epoch, records, commit_id)`、`read(scope, after_seq, limit)`、`subscribe(scope, cursor)`、`head(scope)`。其中 scope 必须包含 user_id 和 session_id。恢复设施另提供带相同 scope 的 snapshot 与 lease ports。相同 scope/commit_id 内容相同返回既有 receipt，内容不同为 corruption。状态提交的租约校验、state_version 校验及追加在存储端同一原子边界完成；普通模块追加记录不需要争抢 agent owner。

Broker 不能与数据库做本地原子事务，因此以 journal 中的 intent/outbox 记录为派发事实；publisher 可重复发送，以稳定 effect ID 去重。queue cursor 和 projection checkpoint 也持久化；重建视图不需要重新执行 effect。

纳入日志：入站消息、消费决定、context artifact 引用、模型配置及请求/返回、工具参数与结果、调度与取消、权限决定、deadline、owner 切换、配置/扩展版本、恢复与 Supervisor 动作。每次可观察的状态改变都留下记录；进程的每个 CPU 指令不是记录单位。

日志不得含明文 credential；只存 secret 引用。敏感 context/blob 用访问控制与加密管理；未来删除通过 tombstone/受控 blob 清理或密钥销毁完成，保留审计引用，不能把 append-only 当作永久保留个人内容的理由。

## Effect 协议

```text
durable intent → dispatch → external execution → durable result → durable continuation
```

模型请求也属于 effect。已持久化的模型结果重放时直接使用；未持久化的请求可能重复计费/得到不同答案，记为新 attempt，不能声称完全确定性重新执行。

| 恢复时观察到的状态 | 处理 |
| --- | --- |
| 已有终态 | 不重新执行；补发尚未 ACK 的终态/后继 commands |
| intent 未派发 | 验证 deadline、配置、权限后派发 |
| 已派发，worker 是否启动未知 | 当成可能执行过；适用下面的 replay 分类 |
| safe/idempotent 工具，外部幂等仍有效 | 使用相同业务幂等键，有界重试 |
| reconcilable 工具 | 先查询外部 operation ID；结果已确定则补记录，否则按策略等待/中断 |
| 非幂等且结果未知 | `interrupted + unknown`，停止自动重做，保留待对账项 |
| deadline 已过 | 产生 timeout 终态并启动 cleanup，不续期 |
| 依赖扩展或版本缺失 | 记录 blocked；到期仍收敛为终态，不替换实现继续 |

## 快照

快照包含 `user_id, session_id, through_seq, state_version, schema_version, reducer_version, checksum`，以及会话/run/step 状态、队列消费记录、去重信息、未结束 invocation/attempt、deadline、barrier、pending outbox、扩展配置版本引用。快照是 committed prefix 的状态，不能含未提交数据。through_seq 绑定完整日志前缀，state_version 绑定执行状态；构建快照时使用一致视图，不能分别读取不同时刻的两个值。

恢复：获取新的 fencing epoch → 载入最新有效快照 → 顺序重放尾部记录 → 对账 in-flight effects → 重建到期任务和 pending delivery → 恢复接收。快照损坏回退上一份或完整日志；中间日志损坏停止该 session 并报告，不能跳行猜状态。

快照不授权删除历史。压缩与日志保留另立 feature，必须覆盖 lagging consumer、去重窗口、审计与恢复要求；v0.1 不做日志截断。

## HA 与旧事件

共享存储的 lease/fencing 防止旧 owner 继续写。每次状态追加和派发资格检查都验证 epoch；无法验证 lease 时暂停派发。Fencing 无法撤销已经到达外部服务的请求，因此仍需工具幂等/对账。

失效事件不删除原始记录：追加 `event.ignored` 和 `duplicate / old_attempt / unadopted_epoch / expired / run_closed` 原因。逻辑调用必须独立产生合法终态，不能靠丢弃结果让 barrier 永远等待。

## 实现阶段与验收

1. 内存状态机与 durable 单机适配器：在 intent 前后、dispatch 前后、result 前后、continuation 前后注入 crash，验证同日志可恢复同状态。
2. 快照加日志尾：结果必须与完整重放一致，损坏快照可回退。
3. 共享存储、多 owner 抢占：失去租约的 writer 不能提交，新 owner 不重复已完成工具。
4. 分别证明 process crash、host/power loss 的存储保证；没有证明的故障类型不写入 HA 声明。

## 参考与适用范围

参考 [Pi durable 上游 README](https://github.com/earendil-works/pi/blob/main/packages/durable/README.md) 的提交、恢复及工具 replay 分节：先持久化执行意图，按工具 replay 安全性处理未完成调用。该来源标为 experimental；尚未确认用户所说“Pi 2.0”对应的准确版本，不据此声明版本对齐。

Oath 自行定义三队列事件入口、唯一工具终态、fencing、deadline service 和独立 Supervisor。这是本项目的设计选择，不将搜索到的 Pi fork 设计当成上游保证。正式实现前应固定参考 commit。
