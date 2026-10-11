# Feature 07 — 独立追加日志模块

状态：Draft；由 core/journal 实现，feature03 负责在其上完成快照与恢复。

## 目标与边界

提供所有模块可随时使用的统一记录接口：agent、tool worker、事件入口、Supervisor、biz、extensions 都能直接追加自身记录。独立指职责、接口和依赖独立，首版可作为库嵌入进程，不要求另部署一套日志服务。

Journal 不识别具体 LLM/数据库/Kit，也不替调用者决定工具是否成功。具体业务状态迁移由 core 状态机验证后提交；已有日志永不修改。

## 隔离与 trace

每条运行日志必须包含 user_id、session_id；已开始运行还必须含 run_id。会话创建、运行前输入等 session 级记录可用 run_id=null。run 创建时追加输入到 run 的绑定，不回填旧日志；查询 run trace 时关联这些输入记录。

同一 run 在暂停/恢复和进程重启后保留 ID；另一次运行使用新 ID。跨 session 的工作通过 correlation_id 建立可授权的引用，不将 run_id 当全局访问令牌。一个用户同名 session 与另一个用户的 session 完全隔离。

## 记录结构

```text
record_id, schema_version,
user_id, session_id, run_id?,
category, type, producer,
correlation_id?, causation_id?,
occurred_at, committed_at, seq,
payload | payload_ref
```

producer 是模块/版本标识；seq 和 committed_at 由 Journal 分配。payload_ref 必须带 hash、大小及同 scope 的可授权引用；真实 secret 不进入 payload。producer/type 的访问权由绑定 writer capability 约束。

| category | 谁写 | 如何消费 |
| --- | --- | --- |
| event | ingress、工具 worker、模型 worker、系统控制服务 | 通过事件校验后追加，提交即具备持久化待投递语义；由 dispatcher 路由至逻辑队列 |
| state | 当前 session owner | 原子记录消费、状态迁移、effect intent、终态与后继 commands；用于恢复 |
| observation | 各模块，包括 Supervisor、biz 与扩展 | trace、诊断、耗时和决策证据；不自动驱动 LLM 或改变执行状态 |

事件派发状态通过 durable receipt 维护。直接 append 事件必须经过该类型的身份/schema 校验；不能只记录一行“tool succeeded”就视为已产生合法终态。读取日志的一方不能把它自己写入的记录再读回来处理，否则会形成回环。

## 接口草案

```text
writer = journal.bind(principal, user_id, session_id, producer, allowed_types)
writer.append(records, commit_id) -> receipt
stateWriter.commitState(records, commit_id, expected_state_version, owner_epoch) -> receipt
journal.read(scope, after_seq, limit, filters) -> page
journal.subscribe(scope, after_seq, filters) -> committed batches
journal.head(scope) -> {seq, state_version}
```

append 允许多个模块并发，持久化成功后才返回 receipt，整批记录原子提交并分配连续 seq。相同 scope/commit_id/内容返回原 receipt；同键不同内容拒绝。记录大小、批次大小和类型白名单由部署限制，满载显式背压，不静默丢记录。

commitState 仅允许当前 owner；检查 epoch 和 expected_state_version 后原子写入并推进 state_version。普通 observation/event 追加只推进 seq，不改变执行状态版本，因此不因高频 trace 写入让状态提交不断冲突。State owner 消费哪些输入必须显式记录，不能仅以当前日志尾部推测已消费范围。

trace 和事件全局有序范围只到 user/session，不要求跨所有用户共用一个全局序列。按 run_id/type 过滤分页时，cursor 保存已扫描到的 seq（包括被过滤记录），避免空页无法推进；cursor 绑定 principal、scope 和 filters。客户端重连可收到重复记录，按 record_id 去重。

## 失败与恢复

不能持久化则返回失败，影响执行的模块暂停后续 effect。日志写成功但 receipt 丢失时，用原 commit_id 重试；不重新生成 record ID。已提交输入可重新投递，状态 commit 中的消费标记防止重复推进。

诊断记录不能代替执行恢复事实。Reducer 按 category/type 读取完整前缀，忽略 observation 对执行状态的影响；需要恢复的业务决定必须在 state commit 中表达。快照包含 through_seq 和 state_version，前者表示扫描边界，后者表示执行状态版本。

## 实施与验收

先实现 scope-bound writer、结构校验、原子批次追加、幂等与 read；再接入事件投递、state commit fencing、订阅及 run trace 查询。存储引擎与 API 的网络传输方式待选。

- 多模块同时写，记录完整、序号唯一且 trace 保留各自 producer/run。
- 相同 session_id 的两个 user 无法相互读取、订阅或写入。
- 高频 observation 追加不造成 state_version 冲突；两个 state owner 的旧 epoch 被拒绝。
- 同 commit 重试不重复写；进程在 ACK 前后退出，不丢已确认记录。
- session 级输入可以从 run trace 找到；恢复不更换 run_id。
- 扩展不能伪造 core 终态；普通 observation 不触发 agent 下一步。

## 实施记录

状态：核心已实现并验证。事件投递、快照与恢复（feature01 / feature03）未开始。

### 存储布局

```
<root>/<base64(user_id)>/<base64(session_id)>/journal.jsonl
<root>/blobs/<hash 前两位>/<hash>            内容寻址的大 payload
```

一行一次提交，每行一个 JSON 对象，`kind` 是 `commit` 或 `lease`。

### 实际改动

`core/journal/jsonl.py` 是唯一实现，只用标准库。

一行一次 commit，不是一行一条记录。批次全有或全无因此等于「这一行要么完整要么不存在」；
最后一行只写了一半，重开时被丢掉。若一行一条记录，批次原子性要靠额外的 begin/end 标记
重新拼回来，而一批写到一半时无法判断该保留几条。

每个 scope 一个文件，进程持 `flock(LOCK_EX)` 到 close，单写者由操作系统保证。
所以不声明 `multi_process`。

### 验证证据

conformance 套件 21 项，0 跳过。

套件之外用真实子进程补了四项 durability 证据：

| 测试 | 证明 |
| --- | --- |
| `test_sigkill_preserves_acked_commits` | 外部 SIGKILL 后，逐条核对每个拿到过 receipt 的记录都还在，seq 连续无空洞 |
| `test_half_written_last_line_is_dropped_on_reopen` | 只写一半的那行不算一次提交；重开时丢掉，日志还能继续写 |
| `test_second_writer_is_rejected` | 单写者是事实不是约定 |
| `test_batch_cut_in_half_leaves_nothing_visible` | 一批记录写到一半进程死掉，这批一条都不出现 |

`checksum_ignores_storage_fields` 用「同一批内容写到两个 seq 起点不同的 scope，checksum
必须相同」来验证。注入 bug 试过：把 scope 混进 checksum，这条会失败。

### 已知限制

- `commits` / `dedup` 两个索引在内存里，随日志长度增长；重开时全量重扫重建。
- 每次 append 都 `fsync`，正确优先于吞吐。
- 单写者。多进程 owner failover 要等 SQLite adapter（`sqlite3` 也是标准库，
  所以在 core/journal/ 下再加一个实现即可）。
- 没有 subscribe。轮询交付留给事件运行时（feature01）需要时再写，那时才有真实调用方
  和测试。
- blob store 是同目录下的内容寻址文件，没有清理机制。
