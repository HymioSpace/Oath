# Feature — Journal（追加日志、状态存储与恢复）

状态：Implemented

编号约定：本方案不使用 `NN-` 前缀。后续 feature 沿用同一命名风格（`feature-<short-name>.md`），
已废弃的旧编号 01–08 不复用。

## 背景与目标

Oath 是长期运行的 agent runtime，两个最硬的约束是 **durable** 与 **event-driven**：
影响后续行为的输入、决定、effect intent、结果与状态迁移必须先落盘再 ACK；
恢复时只重建状态，不重做外部副作用。

这两条都压在一个模块上：**journal 是唯一状态来源**。broker、快照、索引、trace 视图都是
可从它恢复的派生设施。agent、工具、队列、Supervisor、biz、扩展都在自己的流程里直接写
journal，不必经主 agent 转发。

本 feature 定义三件事：

1. 追加日志（append-only log）的**记录格式** —— 跨仓库的 wire 契约，语言无关。
2. agent state 的**存储记录格式**。
3. journal 对上提供的 **API**（append / commit_state / recovery / read_range），
   以及恢复正确性**凭什么成立**。

**不包含**：事件运行时（消费记录、驱动 agent step）、工具生命周期与终态、
Supervisor、biz 查询 API、日志裁剪与压缩、多机高可用。这些是后续 feature。

**明确不做**：JSONL 实现只保证单机单进程。不要把它称为高可用。

## 前置依赖与影响范围

- 无前置 feature。journal 是其他所有 feature 的地基。
- 依赖方向：所有层都可以依赖 `core/journal/`；journal 不反向依赖任何层。
- 跨仓库影响：`Hymio`（biz 查询、Kit）、`Nostalgia`（context 契约）、客户端 trace 视图
  都会读 journal 记录。**记录字段与 type 枚举属于跨仓库协议**，改动是破坏性变更，
  需要通过 `payload_schema_version` 双读过渡。

## 目录与边界

```text
core/protocol/          契约层：只放 datatype 和 interface，纯数据无 I/O
  datatype/             数据类型
    scope.py            Scope（隔离键）与其字符集校验
    message.py          Message（对话历史里的一条消息）
    agent_state.py      AgentState（agent 的可恢复状态 + 它覆盖的游标区间）
    cursor.py           Cursor（在 scope 内定位一条记录）
    journal.py          journal 专属：落盘记录格式 / 接口返回值
  interface/            接口，按模块分文件
    journal.py          Journal
  error.py              契约层的全部异常
core/utils/             与 protocol 同级的工具层，不构成任何契约
  encoding.py           JsonValue / JsonObject / canonical_json / 时间戳
core/journal/           机制层：校验、幂等语义、游标运算、messages 投影。业务无知。
core/journal/impl/      存储后端。v1 只有 file_journal.py（JSONL，标准库）。
```

判据：数据类型和接口是承诺，进 `core/protocol/`；编码只是手段，进 `core/utils/`。
`datatype/` 顶层放跨模块通用的类型（`scope` / `message` / `agent_state` / `cursor`），
journal 专属的集中在 `journal.py`。
异常集中在一个 `error.py`，消费方一把 `except JournalError` 接住全部失败。

`core/journal/__init__.py` 与 `core/protocol/__init__.py` 都**不重导出任何符号** ——
多一条 import 路径就多一种拿法。契约层的 import 走 `core.protocol.datatype` /
`core.protocol.error` / `core.protocol.interface` 三个包入口，不碰子模块。

MySQL 等实现将来落在 `core/journal/impl/`，替换 v1 时 `core/protocol` 与 `core/journal`
不动。**不预先写第二份实现**——只有一个实现和一个使用者时，多写一层抽象是负债；
分目录是为了将来替换时改动面收敛，不是为了现在就支持多后端。

## 接口与数据

### 命名约定

字段名统一 snake_case。`user_id` / `session_id` 用下划线分隔（不用 `userid`），
与项目内其余 Python 标识符一致。`occur_dt` / `commit_dt` 沿用「dt 表示时刻」的写法，
时间值一律 RFC 3339 UTC 字符串（如 `2026-10-12T08:31:04.123456Z`），不用裸 epoch 数值
——epoch 数值丢失可读性，且浮点秒在跨语言比较里有精度陷阱。

### 记录：事件日志（events）

追加日志，每行一条，字段如下：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `user_id` | string | 是 | 第一层隔离。`^[A-Za-z0-9_-]{1,64}$` |
| `session_id` | string | 是 | 第二层隔离与串行化边界。同上字符集 |
| `id` | integer | 否 | 自增。**MySQL 实现必填；JSONL 实现不写**（见「游标」） |
| `type` | string | 是 | 见下方 type 集合 |
| `payload` | object | 是 | 结构由 `type` + `payload_schema_version` 决定 |
| `payload_schema_version` | integer | 是 | 从 1 开始，同一 `type` 内单调递增 |
| `idempotency_key` | string | 是 | 在 `(user_id, session_id)` 内唯一 |
| `occur_dt` | string \| null | 否 | 业务发生时间。调用方提供，不保证单调 |
| `commit_dt` | string | 是 | 落库时间。由 journal 实现盖章，不接受调用方指定 |

`user_id` / `session_id` 在每行里冗余存在，即使文件已经按 scope 分目录。理由是
不变量 8：读的时候校验「记录里的 scope == 文件路径代表的 scope」，不一致就报错。
这是一道冗余防线——分文件写错了、文件被手工搬过、并发下路径拼接出错，
都会被挡住，而不是变成静默的越权读。

**行编码**：每行是 canonical JSON——键按字典序、无多余空白、UTF-8 编码、非 ASCII 字符
不转义、`\n` 结尾。跨语言要能生成逐字节一致的记录（幂等指纹要哈希 payload），
所以编码必须钉死。

### type 集合（v1）

| type | payload 内容 | 来源 |
| --- | --- | --- |
| `user.message` | `{content: string, ...}` | 用户输入 |
| `assistant.message` | `{content, reasoning?, tool_calls?}` | 模型返回的 assistant 轮 |
| `tool.call` | `{invocation_id, tool, arguments}` | 发起一次逻辑工具调用 |
| `tool.result` | `{invocation_id, attempt_id, outcome, ...}` | 一次 attempt 的终态 |
| `system.agent_step` | `{step_id, phase}` | step 生命周期审计 |

`system.*` 是开放族：新增 `system.<name>` 不需要升协议版本、不需要改 journal 代码，
只在消费者侧注册处理。`user.*` / `assistant.*` / `tool.*` 是 v1 的闭合集合，
要加新成员按破坏性变更处理。

**`assistant.message` 的 payload 至少包含 `reasoning`**，即使本次没有也写 `null`——
字段存在但为 null 与字段不存在，在跨版本读取时行为不同，显式写出来更省事。
`reasoning` 的内容是模型的思维链，原样落盘不加工。它是否在重放时回传给模型，
由模型供应商策略决定（部分 provider 拒绝接受历史 reasoning）——这是 provider 适配层的事，
journal 只负责存得下。

### 记录：状态日志（state）

agent state 快照，**独立的追加日志**，不混在事件流里。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `user_id` / `session_id` | string | 是 | 同事件记录 |
| `id` | integer | 否 | 自增，同上可选 |
| `commit_dt` | string | 是 | 落库时间，由实现盖章 |
| `payload_schema_version` | integer | 是 | payload KV 的版本，从 1 开始 |
| `payload` | object | 是 | agent state 的 KV 快照 |
| `cursor_start` | cursor \| null | 是 | 本快照覆盖的日志区间起点 |
| `cursor_end` | cursor | 是 | 本快照覆盖的日志区间终点 |

`payload` 是**全量 KV 快照**，不是 delta。

`cursor_start` 取自 `AgentState.cursor_start`，v1 调用方一般留 `None`
（表示「从日志开头覆盖」）。字段先占位：将来若改增量快照，schema 不用动，
语义变的是 `cursor_start` 不再为 null。`cursor_end` 取自 `AgentState.cursor_end`。

状态日志与事件日志分开的理由：恢复时状态日志只需读**尾部**（最后一条完整记录），
不需要全量扫描；事件日志则必须全量扫。混在一起会让快照参与每轮消息投影。

### 游标（cursor）

```json
{"kind": "line", "value": 128}
{"kind": "id",   "value": 9001}
```

一个游标 = 「在某个 scope 的日志里，定位到某条记录的位置」。`kind` 标明由哪种实现盖章：

- `line` —— JSONL 实现使用。物理行号（1-based，每行都是一条记录）。`value=0` 是合法值，
  表示「第一条记录之前」，空日志的水位线就是它。
- `id` —— MySQL 实现使用。该 scope 内的自增 `id`。

不同后端的游标不可互换。`core/journal/cursor.py` 提供 kind 校验与解析（`check_kind` /
`parse_cursor`），支持的 kind 集合由实现自己给出——JSONL 只支持 `line`，它是实现的
私有常量。kind 的生成与解析归实现。

**游标单调递增，`recovery` 拒绝 kind 不受支持的游标。**

### 为什么不用 commit_dt 做水位线

一个直觉方案是：state 记 `commit_dt`，恢复时取 `commit_dt > state.commit_dt` 的记录。
这个方案有三个会**静默丢数据**的路径：

1. 两条记录落在同一时间戳精度内 → 边界那条被漏掉或重复折叠。
2. NTP 校正让墙钟倒退 → 后写的记录 `commit_dt` 小于快照的 `commit_dt` → 永久丢失。
3. 「写快照」和「写事件」是两次独立 append，中间崩溃 → 两者相对顺序不确定。

所以水位线是 `cursor_end`，`commit_dt` 退回**纯审计字段**：可用于展示、按时间粗排、
排查问题，但任何正确性判断都不依赖它。同理 `occur_dt` 由调用方提供，也不可信。

### API

```python
class Journal(Protocol):
    def append(
        self, scope, type, payload, *, payload_schema_version, idempotency_key, occur_dt=None
    ) -> AppendResult: ...
    def commit_state(self, scope, state: AgentState, *, payload_schema_version) -> StateRef: ...
    def recover(self, scope) -> Recovery: ...
    def read_range(self, scope, cursor, *, limit=None) -> Iterator[Record]: ...
```

`append` 返回 `AppendResult{record_id, idempotent_hit, cursor}`。
`commit_state` 返回 `StateRef{covered_through, commit_dt}`——`covered_through` 是这份快照
覆盖到的事件日志位置（= `state.cursor_end`），不是状态日志里的行号。调用方要的
是「从这儿往后才是新的」，不是「快照写在第几行」。
`read_range` 从 `cursor` **之后**（不含该位置）向后顺序读原始记录，不重建状态——
给 trace 视图、审计、Supervisor 扫描用。`limit` 不给则读到日志末尾。

`commit_state` 收 `AgentState` 而不是裸字典，`recover()` 也返回 `AgentState` ——
journal 不解释 `values` 里的内容，只保证原样存下、原样取回。

覆盖区间是 `AgentState` 的成员（`cursor_start` / `cursor_end`），不是 `commit_state`
的入参：交回去的状态和拿回来的状态说的是同一个区间，调用方没法把两者写岔。

`commit_state` **不要求幂等键**。快照是自描述的（自带 cursor 区间），
重复提交只是多写一份快照，不会破坏任何东西。调用方在崩溃后想知道上次提交成没成功，
读回 `recover()` 即可。

### 幂等语义

幂等键在 `(user_id, session_id)` 内唯一。重复 `append` 时比较指纹
（`type` + `payload_schema_version` + `payload` 的 canonical JSON 的哈希）：

- 指纹**相同** → 认为是重放，返回原记录，`idempotent_hit=True`。不追加新行。
- 指纹**不同** → 抛 `IdempotencyKeyConflict`。不覆盖、不追加。

第二分支是必要的：同一个幂等键配不同内容，是调用方的 bug。如果只按「键存在就返回原记录」
处理，重放路径会把 bug 变成静默的丢记录——上层以为写进去了，实际上拿到的是旧值。

幂等索引在 JSONL 实现里是打开时重建的内存结构；MySQL 实现靠唯一约束。
**幂等必须在重启后依然成立**，否则重放会写入重复记录。

### 恢复（recovery）

`Recovery{state, messages, pending}`：

- `state` —— 最后一条完整状态记录，产出的 `AgentState`；覆盖区间在
  `state.cursor_start` / `state.cursor_end` 上，`values` 是 `payload`。从未提交过
  快照时 `cursor_end` 为 0、`values` 为空。
- `messages` —— 按游标顺序投影出的 LLM 消息数组，
  **只含 `cursor <= state.cursor_end` 的记录**。
- `pending` —— `state.cursor_end` 之后的全部事件记录，按游标升序。

水位线只存在于 `state` 上，不重复一份。`state` 与 `messages` 严格对齐
**同一个** `cursor_end`。这一点是必须的：把一个截至
快照的 state 和一个截至当前的 messages 配在一起交给模型，会出现「模型记得
tool.result，但 state 里没有这次调用已经生效」的错位。

**推论：从未提交过快照时，`cursor_end` 为 0，于是 `messages` 为空、全部记录落在
`pending`。** 这是设计后果不是缺陷——宁可两者一起退回起点，也不能给出一个「state 是
空的但模型记得刚才说过什么」的组合。调用方要的完整对话历史是两者相加：

```text
recovery = journal.recover(scope)
state    = fold(recovery.state, recovery.pending)     # runtime 负责，纯函数
messages = recovery.messages + project_messages(recovery.pending)
```

**journal 不折叠 pending。** fold 需要知道「tool.result 的 outcome 如何改变 agent state」，
那是 agent runtime 的业务语义，不属于 journal。把 fold 放在 journal 里会让 journal
不再是业务无知的模块边界。恢复流程因此是两段：

```text
journal.recover(scope)          →  自洽的 (state, messages) + pending
runtime: fold(state, pending)   →  最新 state（纯函数，无副作用）
journal.commit_state(...)       →  落盘，下次 recover 更快
```

这样调用方拿到的恢复结果**永远是最新的**（B 语义），但 fold 的代码归属清晰。
不预先做 `Journal.with_reducer(...)` 之类的扩展点——真出现第二个消费者时再抽。

### messages 投影规则

只有消息类记录进 messages，其余（`system.*`、纯审计记录）不进。

| 记录 | 投影出的消息 |
| --- | --- |
| `user.message` | `{role: "user", content}` |
| `assistant.message` | `{role: "assistant", content, reasoning?, tool_calls?}` |
| `tool.call` + `tool.result` | `{role: "tool", content, tool_call_id, outcome}` |

一条 `tool.call` 先占一个位置，对应的 `tool.result` 到达时填进去。因此：

- **重试只留最后一条终态。** 同一 `invocation_id` 的多次 attempt 产出多条 result 时，
  它们填同一个位置，最后一条赢。对话里不会堆出多条 tool 消息。
- **未完成的 `tool.call` 产出一条 `unresolved: true` 的 tool 消息**（`content` 为空串）。 丢掉它会产出
  一个「assistant 轮带着 `tool_calls` 却没有对应结果」的数组，交给任何 provider 都是
  坏的，所以宁可显式标出来。
- **无对应 `tool.call` 的 `tool.result` 不投影。** 合法的日志里不会出现这种情况；
  凭空造一条没有 assistant `tool_calls` 配对的 tool 消息比缺一条更糟。

`project_messages(records, cursor_end=None)` 也可直接调用：`cursor_end` 传 `None`
表示投影全部，用于把 `pending` 补进完整历史。

投影顺序 = 游标顺序，不看时间戳。

### 错误

领域不变量违反用异常（符合项目约定：状态字段用于 tool_result / inbound_event 层面，
不变量违反用异常）。

| 异常 | 触发条件 |
| --- | --- |
| `ScopeError` | `user_id` / `session_id` 不符合字符集 |
| `IdempotencyKeyConflict` | 幂等键命中但指纹不同 |
| `PayloadVersionError` | 未知 `payload_schema_version` |
| `PayloadSchemaError` | payload 不符合该 type + version 的 schema |
| `CursorError` | 游标 kind 不受本实现支持，或位置越界 |
| `CorruptRecordError` | 记录结构损坏、或记录 scope 与文件 scope 不一致 |

`CorruptRecordError` 有一条例外：**文件最后一行可能被截断**，这是崩溃时正在写的
那一行，属正常情况，读取时丢弃并可选地把文件截断到这里为止。**中间**的损坏不豁免，
直接抛错——静默跳过等于丢数据。

## 实现方案

### 数据流

```text
写入方 ──append()──► 校验 scope / payload / 幂等
                        │
                        ▼
                  幂等索引（内存，open 时从日志重建）
                        │ 命中且指纹同 → 返回原记录，结束
                        ▼
                  编码 canonical JSON 行
                        │
                        ▼
                  write() → flush() → fsync()   ← append 返回前必须完成
                        │
                        ▼
                  更新内存索引 + 返回游标

恢复   journal.recover(scope)
          ① 打开 / 扫描 events → 幂等索引、last_valid_cursor（容忍截断尾行）
          ② 打开 / 读 state 尾部 → 最后一条完整快照 → cursor_end
          ③ 按游标序扫描 events 到 cursor_end → 投影 messages
          ④ 收集 cursor_end 之后的记录 → pending
          ⑤ 返回 Recovery
```

### JSONL 布局

```text
<root>/<user_id>/<session_id>/events.jsonl
<root>/<user_id>/<session_id>/state.jsonl
```

按 scope 分目录，而不是一个全局文件：

- 隔离落在文件系统层面，删一个 session 就是删一个目录。
- 不同 session 并发写互不干扰，不需要读侧过滤 scope。
- 单个 session 可独立归档、可单独限额。

代价是文件数量多，且每个 session 至少两个文件。

`user_id` / `session_id` **必须先校验再拼路径**（不变量 7）。这两个值来自认证主体或
授权委托，不接受调用方任意伪造；但即便如此，未校验就拼进路径等于把目录穿越的口子
直接开在文件系统上。校验不通过的直接抛 `ScopeError`，不落盘、不建目录。

### 崩溃一致性

- `append` 返回成功 ⇒ 记录已 `fsync`，重启必可读。这是唯一的耐久性承诺。
- `fsync` 失败 / 磁盘满 ⇒ `append` 抛错，不返回成功，上层不 ACK。
- 崩溃发生在 `write()` 之后、`fsync` 之前 ⇒ 可能留下截断尾行，恢复时丢弃。
- 崩溃发生在「写事件」与「写快照」之间 ⇒ 事件多出来，落在 `pending` 里被重新处理。
  重新处理是安全的，因为 **fold 是不变量 9 的纯函数**：不调用工具、不调 LLM、
  不产生外部副作用。快照只是优化，不是正确性来源。

### 不变量

1. journal 不解释业务语义。`type` 与 `payload` 的含义由上层定义。
2. 同一 `(user_id, session_id)` 内记录严格有序，顺序即 append 顺序。
3. 游标单调递增；`recover` / `read_range` 拒绝 kind 不受支持的游标。
4. 幂等键在 `(user_id, session_id)` 内唯一；同键同指纹返回原记录，同键异指纹抛错。
5. `append` 返回成功后记录已持久化，重启必可读。
6. `Recovery` 的 `state.cursor_end` 与 `messages` 对齐同一个水位线 —— 它只存在于
   `state` 上，不在 `Recovery` 里重复一份。
7. scope 校验先于任何文件路径拼接。
8. 记录内的 `user_id` / `session_id` 必须与所在文件代表的 scope 一致，否则 `CorruptRecordError`。
9. fold 是纯函数：无外部副作用，不重放工具调用，不调用模型。
10. 未知的 `payload_schema_version` 抛 `PayloadVersionError`，不静默丢弃。
11. 正确性不依赖任何时间戳。`occur_dt` / `commit_dt` 只用于展示与排查。

### 已知限制（写在这里，不假装没有）

- **恢复是 O(总记录数) 的全量扫描。** 快照的作用是**跳过 state 的重算**，
  不跳过日志扫描——messages 需要从头投影。v1 接受这个代价。
  后续可在快照里附带 messages 水位线来避免全量扫。
- **JSONL 只支持单进程单写者。** 多进程同时写一个 session 的文件会产生交错写，
  实现不加文件锁——不支持的场景不假装支持。
- **幂等索引常驻内存**，与事件条数线性相关。长时间运行的大 session 有内存压力，
  v1 不处理。
- **不做日志裁剪**，`events.jsonl` 无限增长。

## 失败与恢复

| 场景 | 行为 |
| --- | --- |
| append 超时后重试 | 幂等命中，返回原记录，不产生重复行 |
| 崩溃留下截断尾行 | 恢复时丢弃该行，可选截断文件；不算错误 |
| 文件中间损坏 | 抛 `CorruptRecordError`，**不静默跳过** |
| `fsync` 失败 / 磁盘满 | `append` 抛错，不 ACK，不返回成功 |
| 重复写入同一幂等键、内容不同 | 抛 `IdempotencyKeyConflict` |
| 快照未提交而进程崩溃 | 多出的记录落在 `pending`，被重新 fold |
| 时钟回拨 | 无影响：不变量 11 |
| 工具执行过但结果丢失 | 由 `tool.result` 的 `outcome=outcome_unknown` 表达，journal 只存不判 |
| 恢复时遇到未知 payload 版本 | 抛 `PayloadVersionError`，要求先升级 reader |

## 实施步骤

1. `core/protocol/datatype/` —— `Scope` / `Message` / `AgentState` / `Cursor` 与 journal
   专属的记录格式、值对象；`core/protocol/interface/` —— `Journal`；
   `core/protocol/error.py` —— 契约层异常；`core/utils/encoding.py` —— JSON 基础
   类型与 canonical 编码。纯数据，无 I/O。
2. `core/journal/` —— `Cursor` 运算与幂等指纹、记录结构校验、messages 投影。
3. `core/journal/impl/file_journal.py` —— 打开/扫描、追加与 fsync、幂等索引、
   恢复与投影。
4. `tests/` —— 契约测试跑在 journal 接口上；损坏测试与崩溃测试分开。

每个步骤结束时全量测试通过 + 手工 e2e 有真实输出，再进下一步。已全部完成，见实施记录。

## 验收与验证

正常场景：

- 连续 append N 条后重开，`recover()` 返回的 messages 顺序与写入顺序一致。
- 同一幂等键重复 append，返回 `idempotent_hit=True` 且日志行数不变。
- commit_state 后重开，`state` 是同一个 `AgentState`，`pending` 为空。
- commit_state 后再 append，重开时这些记录出现在 `pending`，`messages` 不含它们
  （与 state 对齐）。

异常场景：

- 截断最后一行 → 恢复成功，其余记录完整。
- 破坏中间某一行 → 抛 `CorruptRecordError`。
- `user_id = "../../etc"` → 构造 `Scope` 时就抛 `ScopeError`，journal 拿不到 scope，
  拼路径那一步不会发生（`test_scope_validation_is_what_prevents_escaping_root`）。
- 同幂等键不同 payload → 抛 `IdempotencyKeyConflict`。
- 未知 `payload_schema_version` → 抛 `PayloadVersionError`。
- 写进程在 append 中途被 SIGKILL（真实进程，slow）→ 已确认返回成功的 append
  一条不少，重开后无损坏；连续两次崩溃后重放仍命中幂等。

检查项：`uv run pytest tests/ -q`（含 slow）、`uv run ruff check .`、`uv run mypy .`。

**半行怎么测**：`SIGKILL` 在普通文件系统上杀不掉页缓存里的数据 —— `os.write()`
返回后内容已在页缓存，进程死掉不会留下半行。所以「崩溃留下半行」在本地复现
不出来，损坏测试改用 `truncate()` 人工切出半行；真实 kill 测试验证的是另一条
性质（已确认的 append 不丢）。这两件事不互相替代，两边都测了。

跨仓库：`core/protocol` 的字段与 type 枚举定稿后，Hymio 与客户端侧需要各自同步一份
读取实现。字段只增不删；删改走新 `payload_schema_version` + reader 双读。

## 迁移与发布

不适用（首次实现，无历史数据、无已发布消费者）。协议一旦发布，
字段变更按破坏性变更处理，参见「跨仓库影响」。

## 实施记录

- **实际改动**（内容模块，`__init__.py` 的重导出不列）：

  ```text
  core/protocol/datatype/scope.py       Scope + 字符集校验
  core/protocol/datatype/message.py     Message
  core/protocol/datatype/agent_state.py AgentState
  core/protocol/datatype/cursor.py      Cursor / CursorKind
  core/protocol/datatype/journal.py     落盘记录格式 / 接口返回值
  core/protocol/interface/journal.py    Journal 接口
  core/protocol/error.py                契约层的全部异常
  core/utils/encoding.py                JsonValue / JsonObject / canonical_json / 时间戳
  core/journal/cursor.py                游标运算 + 幂等指纹
  core/journal/validation.py            记录结构校验（读写共用同一套）
  core/journal/messages.py              messages 投影
  core/journal/impl/file_journal.py     JSONL 后端
  ```

- **验证证据**（本地实跑，非推断）：

  ```text
  $ uv run pytest tests/ -q          53 passed, 3 deselected in 0.06s
  $ uv run pytest tests/ -m "" -q    56 passed in 1.87s
  $ uv run ruff check .              All checks passed!
  $ uv run ruff format --check .     33 files already formatted
  $ uv run mypy .                    Success: no issues found in 28 source files
  ```

  手工 e2e：写入 1 条 user.message + 1 条 assistant.message（带 reasoning 与
  tool_calls）+ 1 组 tool.call/tool.result + 1 条 assistant.message，提交快照，
  再补 1 条 system.*，重开进程恢复得到：

  ```text
  state   : AgentState(cursor_start=None, cursor_end=Cursor(kind='line', value=5),
                       values={'topic': '恢复语义', 'turn': 1})
    user     | 帮我查下 Oath 的恢复语义  | {}
    assistant| 先看 spec  | {'reasoning': '恢复靠水位线，不是时间戳', 'tool_calls': [...]}
    tool     | 命中 12 处  | {'tool_call_id': 'inv-1', 'outcome': 'succeeded'}
    assistant| 水位线是游标  | {'reasoning': '时间戳会回拨'}
  pending : [('system.agent_step', 6)]
  ```

- **已知限制与后续**：

  - 恢复是 O(总记录数) 的全量扫描；快照省的是 state 重算，不省日志扫描
    （messages 要从头投影）。
  - JSONL 只支持单进程单写者，不加文件锁。
  - 幂等索引常驻内存，与事件条数线性相关。
  - 不做日志裁剪，`events.jsonl` 无限增长。
  - payload schema **拒绝未知字段**。这是刻意选的严格方向：拼错字段名会被当场
    抓住，代价是将来加字段必须先升 `payload_schema_version`。`system.*` 开放族
    不做字段校验。

- **实现中发现并修掉的坑**：

  - `system.*` 开放族被当成了有固定 schema，导致任何 `system.*` payload 都被
    判为「含未知字段」。开放族应当完全跳过字段约束。
  - 状态记录曾经被写到事件日志的 fd 上（两个 fd 共用一个）。现在事件与状态
    各有各的 fd 与行号。

## 待决事项与参考

- **未决**：MySQL 实现的具体表结构与事务边界；`idempotency_key` 的全局唯一性还是
  scope 内唯一（本方案取后者）；消息裁剪 / token 预算策略；`assistant.message.reasoning`
  重放给模型时的 provider 策略。
- **未决**：是否需要 `system.*` 的注册表机制，让消费者显式声明认识的 type。
  目前是「不认识就不投影」，不报错。
- **背景来源**：durable + event-driven 两条约束见 [overview](overview.md)。