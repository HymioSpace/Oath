# Feature — Journal（追加日志、状态存储与恢复）

状态：Implemented。运行事务与固定消息视图遵循 [Intake](feature-intake.md)。

## 背景与目标

Oath 长期运行，两条硬约束：**durable** 与 **event-driven**。影响后续行为的执行意图、
结果与状态迁移必须先落盘再对外生效；恢复时只重建状态，不重做外部副作用。

**输入本身不落这里。** 输入到达时在队列里、在持久 buffer 里，被 Brain 认领的那次事务才写
进来 —— journal 里没有「收到但没处理」这一档。

两条都压在一个模块上：**journal 是唯一状态来源**。broker、快照、索引、trace 视图都是从
它恢复出来的 derived 设施。

本 feature 定义：追加日志的**记录格式**（跨仓库 wire 契约，语言无关）、agent state 的
**存储记录格式**、journal 对上提供的 **API**，以及恢复正确性凭什么成立。

不包含：事件运行时、工具生命周期与终态、Supervisor、日志裁剪、多机高可用。

**JSONL 实现只保证单 scope 单进程。** 一个 scope 一个进程持有它的 writer，用 `data_root` 下
一把按 scope 命名的排他锁保证同一个 scope 不会被两个进程同时写。不假装支持多机写者。

## 前置依赖

无前置 feature，journal 是其他所有 feature 的地基。所有层都可以依赖 `core/journal/`，
journal 不反向依赖任何层。

`core/protocol/` 放契约（datatype + interface，纯数据无 I/O），`core/journal/` 放机制
（校验、幂等、游标、投影，业务无知），`impl/` 放存储后端，`core/utils/` 放编码工具。
判据：承诺进 protocol，手段进 utils。异常集中在 `core/protocol/error.py`。

## 总览

一句话：**写进去的记录 fsync 之后必可读；恢复按游标watermark切出「已知状态」与「待处理
事件」，重放是纯函数，不重做副作用。**

```mermaid
flowchart TD
    W[append: 校验 → 幂等 → canonical JSON] --> F[write + flush + fsync]
    F -->|成功| IDX[更新幂等索引，返回游标]
    F -->|失败| ERR[抛错，上层不 ACK]

    R[recover: 全量扫描 events + 读 state 尾部] --> C[watermark = state.cursor_end]
    C --> M[messages: cursor ≤ watermark的投影]
    C --> P[pending: watermark之后的记录]
    M --> X[调用方: fold(state, pending) → 最新 state<br/>纯函数，无副作用]
    P --> X
```

五条贯穿全文的规则：

1. **只追加，不改不删。** 失效的东西追加新记录说明它失效了。
2. **watermark是游标，不是时间戳。** 墙钟会回拨、会撞精度。`occur_dt` / `commit_dt` 只用于展示。
3. **`append` 返回成功 ⇒ 已 fsync。** 这是唯一的耐久性承诺，也是全部其他保证的根。
4. **journal 不解释业务语义。** fold 是调用方的纯函数，不调工具、不调 LLM。快照是优化。
5. **损坏不静默跳过。** 抛错。跳过的正是「谁在什么时候做了什么」。

不变量：

| # | 不变量 |
| --- | --- |
| 1 | 同一 `(user_id, session_id)` 内记录严格有序，顺序即 append 顺序 |
| 2 | 游标单调递增；`recover` / `read_range` 拒绝本实现不支持的 kind |
| 3 | 幂等键在 scope 内唯一；同键同指纹返回原记录，同键异指纹抛错 |
| 4 | `Recovery` 的 `state.cursor_end` 与 `messages` 对齐同一个watermark，不重复一份 |
| 5 | scope 校验先于任何文件路径拼接 |
| 6 | 记录内的 `user_id` / `session_id` 必须与所在文件代表的 scope 一致 |
| 7 | 未知的 `payload_schema_version` 抛错，不静默丢弃 |

## 接口与数据

字段名统一 snake_case。记录时间使用 RFC 3339 UTC 字符串；RuntimeStore 的 deadline
使用 Unix 秒，恢复等待后转换为 monotonic 计时，不能拿它代替日志 cursor。

### 记录：事件日志（events）

追加日志，每行一条：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `user_id` | string | 是 | 第一层隔离。`^[A-Za-z0-9_-]{1,64}$` |
| `session_id` | string | 是 | 第二层隔离与串行化边界。同上字符集 |
| `id` | integer | 否 | 自增。**MySQL 实现必填；JSONL 实现不写** |
| `type` | string | 是 | 见下方 type 集合 |
| `payload` | object | 是 | 结构由 `type` + `payload_schema_version` 决定 |
| `payload_schema_version` | integer | 是 | 从 1 开始，同一 `type` 内单调递增 |
| `idempotency_key` | string | 是 | 在 `(user_id, session_id)` 内唯一 |
| `occur_dt` | string \| null | 否 | 业务发生时间。调用方提供，不保证单调 |
| `commit_dt` | string | 是 | 落库时间。由实现盖章，不接受调用方指定 |

`user_id` / `session_id` 在每行里冗余存在，即使文件已按 scope 分目录 —— 不变量 6 靠它
成立：分文件写错、文件被手工搬过、路径拼接出错，都会被挡住，而不是变成静默的越权读。

**行编码**必须固定：键按字典序、无多余空白、UTF-8、非 ASCII 不转义、`\n` 结尾。幂等指纹要哈希
payload，跨语言必须能生成逐字节一致的记录。

### type 集合（v1）

| type | payload | 来源 |
| --- | --- | --- |
| `user.message` | `{content: string, ...}` | 用户输入 |
| `assistant.message` | `{content, reasoning, tool_calls?}` | 模型返回的 assistant 轮 |
| `tool.call` | `{tool_call_id, invocation_id, tool, arguments}` | 发起一次工具调用 |
| `tool.result` | `{tool_call_id, invocation_id, outcome, ...}` | 这次调用的终态 |
| `system.agent_step` | `{step_id, phase}` | step 生命周期审计 |
| `runtime.commit` | `{version, reason, changes}` | scope 内原子状态与事实提交 |

闭合集合，加新成员按破坏性变更处理：先定 payload 字段，再升协议版本走双读。

`system.` 只是命名约定，不是特殊类别：和 `user.message` 一样是一种 type，没有「system
前缀免检」这条路。

`assistant.message` 的 `reasoning` 即使没有也写 `null`：字段存在但为 null 与字段不存在，
在跨版本读取时行为不同。

**payload schema 拒绝未知字段**，每种 type 一视同仁：拼错字段名当场被抓住，加字段先升
`payload_schema_version`。

### 记录：状态日志（state）

agent state 快照，**独立的追加日志**，不混在事件流里。落盘形态就是 `AgentState` 序列化，
加 `id` 与 `commit_dt` 两个记录层字段。`AgentState` 的成员**不是 `commit_state` 的入参**：

```python
@dataclass(frozen=True, slots=True)
class AgentState:
    scope: Scope  # 它属于谁
    cursor_start: Cursor | None  # 它覆盖的区间起点
    cursor_end: Cursor  # 它覆盖的区间终点
    payload: JsonObject  # 它是什么内容
    payload_schema_version: int = 1
```

理由是同一个：传参的话「提交的 scope」和「状态自己的 scope」、「声明的版本」和「内容的
实际版本」都能被写成两件事，而这两处写岔都不会报错，只会在恢复时悄悄给出另一份状态。

`payload` 是**全量 KV 快照**，不是 delta。`cursor_start` 一般留 `None`（表示「从日志开头
覆盖」）：字段先占位，将来改增量快照不用动 schema。

状态日志与事件日志分开的理由：恢复时状态日志只需读**尾部**（最后一条完整记录），事件
日志必须全量扫。混在一起会让快照参与每轮消息投影。

### 游标（cursor）

```json
{"kind": "line", "value": 128}
{"kind": "id",   "value": 9001}
```

一个游标 =「在某个 scope 的日志里定位到某条记录的位置」，`kind` 标明由哪种实现盖章：
`line` 是物理行号（1-based，`value=0` 是合法值，表示「第一条记录之前」，空日志的watermark
就是它），`id` 是自增 id。不同后端的游标不可互换，`kind` 的生成与解析归实现。

**为什么不用 `commit_dt` 做watermark。** 「取 `commit_dt > state.commit_dt` 的记录」有三条
静默丢数据的路径：同一时间戳精度内的边界那条被漏掉或重复 fold；NTP 校正让墙钟倒退 →
后写的记录比快照还小 → 永久丢失；写快照与写事件是两次独立 append，中间崩溃 → 顺序不确定。

### API

```python
class Journal(ABC):
    def append(
        self, scope, type, payload, *, payload_schema_version, idempotency_key, occur_dt=None
    ) -> AppendResult: ...
    def commit_state(self, state: AgentState) -> StateRef: ...
    def recover(self, scope) -> Recovery: ...
    def read_range(self, scope, cursor, *, limit=None) -> Iterator[Record]: ...
```

`append` 返回 `AppendResult{record_id, idempotent_hit, cursor}`。`commit_state` 返回
`StateRef{covered_through, commit_dt}` —— `covered_through` 是快照覆盖到的事件日志位置，
不是状态日志里的行号：调用方要的是「从这儿往后才是新的」。`read_range` 从 `cursor` **之后**
（不含）向后顺序读原始记录，不重建状态，给 trace 视图、审计、Supervisor 扫描用。

`commit_state` **不要求幂等键**：快照自描述，重复提交只是多写一份。想知道上次提交成没成功，
读回 `recover()` 即可。

### 幂等语义

幂等键在 scope 内唯一。重复 `append` 时比较指纹（`type` + `payload_schema_version` +
`payload` 的 canonical JSON 的哈希）：

- 指纹**相同** → 认为是重放，返回原记录，`idempotent_hit=True`，不追加新行。
- 指纹**不同** → 抛 `IdempotencyKeyConflict`，不覆盖不追加。

第二分支是必要的：同一个键配不同内容是调用方的 bug。只按「键存在就返回原记录」处理，
重放路径会把 bug 变成静默的丢记录 —— 上层以为写进去了，拿到的其实是旧值。

JSONL 实现的幂等索引是打开时从日志重建的内存结构 —— **幂等必须在重启后依然成立**，否则
重放会写入重复记录。

### 恢复

`Recovery{state, messages, pending}`：

- `state` —— 最后一条完整状态记录产出的 `AgentState`。从未提交过快照时 `cursor_end` 为 0、
  `payload` 为空。
- `messages` —— 按游标顺序投影出的 LLM 消息数组，**只含 `cursor <= state.cursor_end` 的
  记录**。
- `pending` —— `state.cursor_end` 之后的全部记录，按游标升序。

`state` 与 `messages` 严格对齐**同一个** `cursor_end`：把截至快照的 state 和截至当前的
messages 配起来交给模型，会出现「模型记得 `tool.result`，但 state 里没有这次调用已经生效」
的错位。从未提交过快照时两者一起退回起点，也不能给出「state 是空的，但模型记得刚才说过
什么」。

**`messages` 是自洽的，但不是完整的对话**，它只到快照为止。要完整历史就**把全部记录
投影一次**：

```text
recovery = journal.recover(scope)
state    = fold(recovery.state, recovery.pending)   # 纯函数
messages = project_messages(all_records)           # 一次投影
journal.commit_state(state)                        # 下次 recover 更快
```

把 `recovery.messages` 与 `project_messages(recovery.pending)` 相加看着省事，实际是在快照
边界上把**一次调用和它的结果**劈成两半：前半段看到 call 看不到 result，槽位被判成
`unresolved`；后半段的结果配不上任何槽位，只能被丢掉。

**journal 不 fold pending。** fold 要知道「`tool.result` 的 outcome 如何改变 state」，那是
agent runtime 的业务语义。

### RuntimeStore 与消息视图

RuntimeStore 是 Journal 上的 scope 事务层，所有 runtime 组件共享同一个实例。
在短临界区复制状态、执行纯数据 change、计算 table/key 的变化，追加一条 runtime.commit；
fsync 成功后发布缓存。记录版本从 1 连续递增，缺失或乱序拒绝恢复。

表为 inputs、batches、calls、steps、meta、conversation、controls、compactions。修改保存 key 的完整新值，
不允许删除 key；输入完成通过 phase=done 表达。changes 是事务 delta，不是 AgentState 快照。
审批裁定与 queued、工具终态与 claim、请求草稿各自属于同一条提交记录；最终模型请求另以
step.request 提交。
reason 如 step.input / tool.terminal 表示提交原因；这些不再额外追加另一份裸事件作为恢复依据。

append 报错时丢弃缓存，下一次重新读取，处理 fsync 已成功但返回失败的歧义。
恢复只 fold 已提交的 delta，不执行模型或工具。当前扫描所有 runtime.commit，不使用事务快照加速。
Journal.scopes 枚举所有会话。一个 agent 进程只恢复自己那个 scope —— 进程数由 manager 按
session 表决定，不靠扫目录决定。

有 runtime conversation 的日志，messages 投影返回截止 cursor 最近的固定视图：
原 tool 槽位不回填，晚到真实结果以 user role 追加。它是查询视图，末尾可仍是工具声明；
下一次 Brain 请求必须经 Intake 关闭槽位。没有 runtime conversation 的独立事件日志，
project 仍可将 user / assistant / tool.call + tool.result 转为查询消息，不能代替 step 的请求快照。

### 错误

领域不变量违反用异常；状态字段留给 tool_result / inbound_event 层面的失败。

| 异常 | 触发条件 |
| --- | --- |
| `ScopeError` | `user_id` / `session_id` 不符合字符集 |
| `IdempotencyKeyConflict` | 幂等键命中但指纹不同 |
| `PayloadVersionError` | 未知 `payload_schema_version` |
| `PayloadSchemaError` | payload 不符合该 type + version 的 schema |
| `CursorError` | 游标 kind 不受支持，或位置越界 |
| `CorruptRecordError` | 记录结构损坏、payload 不合 schema、记录 scope 与文件 scope 不一致 |

`CorruptRecordError` 有一条例外：**文件最后一行可能被截断** —— 那是崩溃时正在写的那
一行，属正常情况，读取时丢弃并可选地把文件截断到这里。**中间**的损坏不豁免。

## 实现方案

**JSONL 布局** 按 scope 分目录，而不是一个全局文件：

```text
<root>/<user_id>/<session_id>/events.jsonl
<root>/<user_id>/<session_id>/state.jsonl
```

隔离落在文件系统层面：删一个 session 就是删一个目录，并发写互不干扰，不需要读侧过滤
scope；单个 session 可独立归档、单独限额，代价是至少两个文件。`user_id` / `session_id`
**必须先校验再拼路径**（不变量 5）：未校验就拼路径，等于把目录穿越的口子开在文件系统上。

**崩溃一致性**：`append` 返回成功 ⇒ 已 fsync，重启必可读。fsync 失败或磁盘满 ⇒ 把文件
截回这一批之前的长度再抛错、上层不 ACK —— 截回的长度是**字节数**，按字符算会把已经
fsync 过的上一条记录一起截掉。崩溃落在 `write()` 之后、`fsync` 之前 ⇒ 留下截断尾行，
恢复时丢弃。崩溃落在「写事件」与「写快照」之间 ⇒ 事件多出来落在 `pending` 里被重新
处理，这是安全的，因为 fold 是纯函数。

## 失败与恢复

已知缺口：

- **恢复是 O(总记录数) 的全量扫描。** 快照省的是 state 的重算，不省日志扫描。
- **JSONL 只支持单进程单写者。** 多进程同时写一个 session 的文件会交错写 —— 不加文件锁，
  不支持的场景不假装支持。关闭 Journal 后旧 worker 不可重新打开写入。
- **幂等索引常驻内存**，与事件条数线性相关。
- **不做日志裁剪**，`events.jsonl` 无限增长。

## 跨仓库协议变更

`core/protocol` 的字段与 type 枚举定稿后，Hymio 与客户端侧需要各自同步一份读取实现。
字段只增不删，删改走新 `payload_schema_version` + reader 双读；`type` 加新成员、
`CursorKind` 加新值都是破坏性变更。

## 待决事项

- MySQL 实现的具体表结构与事务边界；消费者要不要显式声明自己认识的 type（目前是不报错）；消息裁剪 / token 预算策略。
