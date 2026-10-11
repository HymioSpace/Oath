# Feature 04 — 独立 Supervisor

状态：Draft；依赖 durable journal、runtime commands 和工具终态协议。

## 职责

Supervisor 作为独立进程读取主 runtime 的已提交日志、租约、持久化 deadline 与心跳投影。独立进程用于隔离主 agent 卡死；多机故障恢复还需不同 host 的部署和共享存储。

它检查“有工作但长期无进展”“owner lease 失效”“工具 deadline 已过”“派发结果迟迟未确认”等状态。心跳只说明进程活着，不说明业务在推进；使用语义进展 seq、deadline 和 phase 区分正常长任务与停滞。

## 契约与动作

Observation 包含 `user_id, session_id, run_id?, through_seq, state_version, current_epoch, observed_at, signal, evidence_refs`。
Decision 包含 `decision_id, user_id, session_id, run_id?, policy_version, evidence_refs, action, reason, expected_epoch, expected_state_version, cooldown_key`。

允许的有界动作：请求状态 reconcile、补扫 deadline、重投递已记录 command、在 lease 合法变更后恢复 session、暂停 run、隔离失效 extension。Supervisor 不直接写主 agent 的内存/投影，也不自己调用业务工具；通过 system queue 提交命令，由 core 重新校验是否仍适用。

自己的 observation cursor、决策和派发意图通过 Journal 的受限 writer 持久化到目标 user/session stream；作为独立日志生产者，不依赖主 agent 进程代写。订阅时过滤自身 observation 类型以免循环记录；执行恢复动作仍通过 core command/state commit。多个 Supervisor 以 decision key 去重并使用租约；同一故障有最大恢复次数、cooldown 与退避。过期决策被拒绝并记录，不覆盖已恢复的新状态。

## 分工

Deadline service 保证工具到期终态，Supervisor 是其异常时的兜底，二者可重复提交同一到期候选但不能创建多个终态。外部进程管理器负责重启 Supervisor；避免主 agent 和 Supervisor 无限制相互重启。

存储故障时停止写副作用、保持服务不可用告警；恢复监督不能绕过持久化失败。预算超限、反复失败或不可判定的外部副作用应暂停并暴露给客户端。

## 未来：基于 trace 的改进

先记录失败模式、成本、延迟和恢复率，再由独立分析器提出 prompt/架构候选。候选经过离线 replay/eval、版本化发布、灰度和回滚。生产 Supervisor 默认无权自动改代码、schema 或权限；“自进化”不等于在活动 run 中热改执行语义。

## 验收

- 主进程卡死时 Supervisor 仍可发现停滞；合法长工具不会因普通心跳间隔被误杀。
- 同一故障两个 Supervisor 同时决策，只接受一次有效动作。
- 恢复命令晚到时 expected_epoch/state_version 不匹配，拒绝执行。
- 连续恢复失败触发有限次暂停，不形成重启风暴。
