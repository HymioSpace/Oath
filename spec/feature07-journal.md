# Feature 07 — 追加日志

状态：Implementing。`core/journal/` 已实现并验证（见文末实施记录）；事件运行时、快照与
恢复分别在 feature01 / feature03，还没开始。

## 目标与边界

日志是所有状态的唯一来源：任何模块的每一次可观察改变，都要在这里留下一条记录；进程
崩溃后，靠重放这些记录把状态算回来。

| 谁 | 用它做什么 |
| --- | --- |
| 事件入口（ingress） | 追加入站事件，提交即意味着「已持久化，等待投递」 |
| agent 状态机 | 写执行状态迁移、effect 意图、终态与后继命令 |
| 工具 / 模型 worker | 写调用参数、结果、失败终态 |
| Supervisor、biz、扩展 | 写诊断与 trace |

独立指职责、接口和依赖独立：首版就是进程内的一个库，不要求另部署一套日志服务。
Journal 不识别具体模型供应商、数据库或业务 Kit，也不替调用方判断工具是否成功 ——
它只保证「写进去的东西按顺序、原子、可重放地存着」。

## 隔离与 trace

日志按 `(user_id, session_id)` 隔离，这就是 `Scope`。它不是查询条件，是存储键：
两个用户的同名 session 落在不同文件里，读不到对方的记录是因为根本不在同一个流里。

已开始运行的记录必须带 `run_id`；会话级记录（比如会话刚建立）可以显式为 null。
同一个 run 在暂停、恢复、进程重启之后保留原来的 ID —— 恢复不换 ID，否则 trace 断成
两截。另起一次运行才用新 ID。跨 session 的引用走 `correlation_id`，它不是访问令牌：
能不能读那个 scope，仍然由 principal 决定。

## 记录

一次提交里的最小单位叫记录。生产者提交的是 `RecordDraft`（不含 `seq` / `committed_at`），
存储端返回的是 `JournalRecord`（补齐了这两个字段）。

### 字段

| 字段 | 谁赋值 | 含义与约束 |
| --- | --- | --- |
| `record_id` | 生产者 | 记录标识。同一个 commit 内不能重复。 |
| `seq` | 存储端 | scope 内从 1 连续递增，无空洞无重复。一次 commit 内的记录按顺序连号。 |
| `commit_id` | 生产者 | 一次提交的幂等键。丢了回执就用同一个 `commit_id` 重试。 |
| `category` | 生产者 | `event` / `state` / `observation` 三选一，见下节。 |
| `type` | 生产者 | 业务类型，如 `user.message`、`tool.result`。按 `type` 可以配写入白名单。 |
| `producer` | **写入方覆盖** | 是谁写的。记录里带什么都以 `bind` 时的 producer 为准，扩展无法冒充 core 写终态。 |
| `schema_version` | 生产者 | 记录内容格式的版本，当前为 1。与「怎么折叠这些记录」的版本各走各的。 |
| `payload` | 生产者 | 内容，任意 JSON 值。不接受 float：不同实现算出的字节可能不同，checksum 会失效。 |
| `payload_ref` | 生产者 | 大 payload 的引用 `{blob_id, hash, size}`。与 `payload` 二选一。 |
| `payload_hash` | 存储端 | 内容的 sha256。`payload` 的哈希，或 blob 内容本身的哈希（不是引用结构的哈希）。 |
| `occurred_at` | 生产者 | 生产者声称这件事发生的时间。不填就用存储端的提交时间。 |
| `committed_at` | 存储端 | 这次提交落盘的时间。同一批的记录共用同一个值。 |
| `run_id` / `step_id` | 生产者 | 属于哪次运行 / 哪一步。已开始运行的记录必须带 `run_id`。 |
| `correlation_id` | 生产者 | 跨 session 的关联。 |
| `causation_id` | 生产者 | 直接触发这条记录的上一件事。 |
| `queue` | 生产者 | 逻辑队列，`user` / `tool` / `system` 之一。只有 `event` 能带。 |
| `event_id` | 生产者 | 入站事件的去重键。`event` 必填，其余类别不能带。 |

时间一律写成 UTC 的 ISO 字符串（末尾 `Z`）。`+08:00` 和 `Z` 是同一时刻但字节不同，
而 checksum 和去重都比字节。

### 三种 category

| category | 谁写 | 消费方式 |
| --- | --- | --- |
| `event` | 事件入口、工具 worker、模型 worker、系统控制服务 | 提交通过校验后即代表「已持久化，等待投递」，由事件运行时路由到队列 |
| `state` | 当前 session owner | 原子地记录消费到哪、状态怎么迁移、后果意图、终态与后继命令。恢复只认这一类 |
| `observation` | 任何模块，包括 Supervisor、biz 与扩展 | trace 与诊断。不影响执行状态，也不驱动下一步 |

`event` 必须过该类型的身份与 schema 校验才能写；只记一行「tool succeeded」不等于产生了
合法终态。`state` 只能通过 `bind_state` 写，普通 writer 拿不到这个权限 —— 诊断记录搭执行
状态提交的便车，会让「哪些记录参与恢复」和「哪些只是日志」在日志里分不出来。

读取日志的一方不能把它自己写入的记录再读回来处理，否则会形成回环。

### 一条记录的例子

下面是一个真实存储里的一条记录（`payload_ref` 为 null 时字段仍然写出，便于直接读回）：

```json
{
  "seq": 1,
  "record_id": "r_1",
  "commit_id": "c_1",
  "type": "user.message",
  "category": "event",
  "producer": "ingress",
  "schema_version": 1,
  "payload": {"text": "你好"},
  "payload_ref": null,
  "payload_hash": "ac72dedbe8e0d53dda6f5cbfd5e1c9e6f7f8bac70c7423177e450174b1c09d83",
  "occurred_at": "2026-10-12T03:04:05Z",
  "committed_at": "2026-10-11T16:25:58.645630Z",
  "run_id": "run_5",
  "step_id": null,
  "correlation_id": null,
  "causation_id": null,
  "queue": "user",
  "event_id": "ev_1"
}
```

用 `payload_ref` 时 `payload` 为 null，引用本身长这样：

```json
"payload_ref": {
  "blob_id": "2479a22536c2e63f8c48b2df5de491dd6780da1775a177bdf903d50907d52e56",
  "hash": "2479a22536c2e63f8c48b2df5de491dd6780da1775a177bdf903d50907d52e56",
  "size": 40
}
```

## 提交

### 一行一次提交，不是一行一条记录

日志文件的一行就是一次提交：整个批次装在一个 JSON 对象里。一批记录全有或全无，因此
退化成「这一行要么完整、要么不存在」—— 崩溃只可能让最后一行写了一半，重开时截掉即可。

若改成一行一条记录，一批写到一半时无法判断该保留几条：前两条是完整行，看不出第
三条丢了，恢复出来的状态就多了一条不存在的记录。

### 信封字段

| 字段 | 含义 |
| --- | --- |
| `v` | 行格式版本。读到不认识的版本就停下并报错，不猜。 |
| `kind` | `commit` 或 `lease`。 |
| `commit_id` | 这次提交的幂等键。 |
| `checksum` | 提交内容的哈希。**不含 `seq` 和 `committed_at`** —— 这两个是存储端赋的，生产者重试时算不出来，算进去会让重试必然被判成「同键不同内容」。 |
| `first_seq` | 本批第一条记录的 seq。等于上一次提交的 `first_seq + record_count`，这个等式必须成立，否则日志是坏的。 |
| `record_count` | 本批记录数。 |
| `committed_at` | 落盘时间，本批共用一个值。 |
| `state_version` | 这是一次状态提交时才有值，否则 null。 |
| `owner_epoch` | 状态提交时写的 epoch，否则 null。 |
| `records` | 记录数组，按 seq 顺序。 |

### 一次提交的例子

真实文件里的第二行（`checksum` 和 `payload_hash` 是真实值）：

```json
{
  "v": 1,
  "kind": "commit",
  "commit_id": "c_1",
  "checksum": "80326c3a55b73119ec6cb6d52c2e46459ac500e16cc50c4957e004d99b51f03d",
  "first_seq": 1,
  "record_count": 2,
  "committed_at": "2026-10-11T16:25:58.645630Z",
  "state_version": null,
  "owner_epoch": null,
  "records": [
    {"seq": 1, "record_id": "r_1", "type": "user.message", "event_id": "ev_1", "...": "同上一节"},
    {"seq": 2, "record_id": "r_2", "type": "user.message", "event_id": "ev_2", "...": "同形"}
  ]
}
```

一次状态提交只有两个字段不同 —— `state_version` 与 `owner_epoch` 有值：

```json
{
  "v": 1, "kind": "commit", "commit_id": "c_3", "first_seq": 4, "record_count": 1,
  "committed_at": "2026-10-11T16:25:58.645785Z",
  "state_version": 1, "owner_epoch": 1,
  "records": [{"seq": 4, "record_id": "r_4", "category": "state", "type": "session.state", "...": "..."}]
}
```

租约也是一行，但只记 owner 是谁、epoch 涨到了几：

```json
{"v": 1, "kind": "lease", "owner_epoch": 1, "holder": "worker-a", "at": "2026-10-11T16:25:58.645009Z"}
```

### 校验必须在分配 seq 之前

顺序固定为：结构校验 → commit 幂等 → 事件去重 → epoch 与 state_version 检查 → 分配 seq →
落盘 → 推进计数器和索引。

把校验放在分配 seq 之后，一次被拒的提交会在日志里留下空洞，恢复时就无法判断 seq 之间
到底有没有丢过东西。所以 `head` 在失败提交前后必须逐字节相等。

### 幂等与冲突

| 情况 | 结果 |
| --- | --- |
| 同 `commit_id` + 同内容 | `duplicate`，返回原来那份回执（含原来的 `first_seq`），不再写一遍 |
| 同 `commit_id` + 不同内容 | `conflict`，拒绝且不覆盖既有记录 —— 这已经属于数据损坏 |
| 同 `event_id` + 同内容 | `duplicate`，`commit_id` 指向最早接收它的那次提交 |
| 同 `event_id` + 不同内容 | `conflict` |

`CommitReceipt.outcome` 只有三个取值：`accepted` / `duplicate` / `conflict`。超限和写不进去
是异常（`LimitExceeded` / `OathError`），不是 outcome —— 调用方必须处理，不能靠读
outcome 分支来决定要不要重试。

### 两个计数器

- `head_seq`：每写一条记录都推进。trace 的高频写入会让它一直涨。
- `state_version`：只有状态提交才推进，且一次提交只 +1（不是 +记录条数）—— 一次状态提交
  是一个原子决定。

分成两个是因为 owner 做 CAS 时比对的是 `state_version`。如果两者合并，高频的 observation
会把 owner 记住的版本号顶掉，导致状态提交永远冲突。

`head` 同时返回这两个值加上 `owner_epoch`；对还没写过的 scope 返回全零，且不会因为
「读了一下」就在磁盘上凭空造出目录。

## 存储布局

JSONL 实现（`core/journal/jsonl.py`）的文件布局：

```text
<root>/<base64(user_id)>/<base64(session_id)>/journal.jsonl     一个 scope 一个文件
<root>/blobs/<hash 前两位>/<hash>                                内容寻址的大 payload
```

目录名用 urlsafe base64 而不是 sha256：出问题时能从目录名反解回 `(user_id, session_id)`；
base64 的字符集不含 `/`，不会造成路径穿越。填充符 `=` 保留，所以目录名可能以它结尾。

blob 按内容寻址：同内容必然同路径，因此重复写入不是问题（`O_EXCL` 打开，已存在即视为
已写好）。`put` 返回 `(blob_id, size)`，`verify(blob_id, expected_hash)` 用来确认内容没被
换掉。blob 目前没有清理机制。

### 谁可以读写哪些 scope

| 操作 | 检查 principal |
| --- | --- |
| `bind` / `bind_state` / `read` / `head` | 是，不匹配抛 `ScopeViolation` |
| `lease.acquire` / `renew` | 否 |

租约不检查，是因为它收的是 holder 名字而不是 principal：**谁有资格当 owner 由 core 决定，
journal 只保证 epoch 单调递增**。抢到 epoch 也写不进状态 —— 状态提交要走 `bind_state`，
那一步会检查。所以这一条不是授权漏洞，但它意味着「抢占」这件事在 journal 层拦不住，
只能靠进程内不要跑不可信的代码。

## 持久化

### 一次 append 的落盘顺序

```text
校验 → 分配 seq → 拼出整行 → 短写循环写满 → fsync → 更新内存计数器 → 返回回执
```

先写盘再改内存。顺序反过来的话，落盘失败会留下一个「声称已提交、实际不存在」的计数器，
而内存里的计数器正是下一次分配 seq 的依据。

同一 scope 的所有 append 先在进程内串行（一把异步锁），再落到文件 —— 跨进程则由
`flock` 保证。

### fsync 点

| 时机 | 动作 |
| --- | --- |
| 写一行 | `os.write` 循环写满后 `fsync(fd)`。回执返回之前数据已在盘上 |
| 首次创建 scope 文件 | 额外 `fsync` 一次父目录 |
| 截断半行 | `ftruncate` + `fsync` |

首次创建时 fsync 父目录是必须的：只 fsync 文件不保证文件名进了目录，断电后可能出现一个
内容完好但谁也不认识的 inode。

### 半行与截断

进程可能在任意一条指令上死掉，所以最后一行有可能只写了一半。重开时的处理：

1. 逐行读。按行切分天然分得清两种「读不出来的行」：
   - **不以换行结尾** —— 末尾的半行，写完换行之前进程就没了。停在这里，它不算一次提交。
   - **以换行结尾却解析不出 JSON** —— 真损坏，报 `LogCorrupt`。换行都写完了的分块不可能是
     「写到一半」，只能是内容坏了。
2. 记下「已确认完整的字节数」。
3. 文件比这个数长就 `ftruncate` 掉多出来的部分 —— **只截半行，绝不截坏行**。

把两种混为一谈（解析失败一律当成末尾半行）会静默丢数据：损坏点之后还完好的提交会被
一起截掉，且没有任何声音。日志不可原位修改，丢掉的记录再也拿不回来。

重放失败时，这个 scope 的缓存（流对象、文件句柄）必须一并撤掉。否则第一次调用报错、
第二次拿到的是没建完的流，把损坏当成一个正常的空 scope 用。

实测（真实子进程行为，数字来自一次实际运行）：

```text
追加 1 次提交后            size = 778，2 行
往文件尾写半行后             size = 840
重开（另起进程）             head = {seq: 1, state_version: 0, owner_epoch: 1}
                            size = 778  ← 半行被截掉，head 没变
重开后继续 append           first_seq = 2，行数 3
```

### 单写者

进程持有 `flock(LOCK_EX)` 直到 `close()`。第二个进程在同一 scope 上打开文件时会直接失败。
这是正确性的前提：两个写者各自分配 seq 会立刻产生重复和空洞。

因此 JSONL 实现不声明 `multi_process` 能力。多进程 owner 切换要等 SQLite 适配器
（`sqlite3` 也是标准库，在 `core/journal/` 下加一个满足 `api.py` 的实现即可）。

## 恢复

### 哪些是持久事实，哪些是派生的

| 类别 | 内容 | 丢了怎么办 |
| --- | --- | --- |
| 持久事实 | 日志文件本身：每一行提交、每一行租约 | 只能从别的副本拿，重建不出来 |
| 派生 | `commits`（commit_id → 回执信息）、`dedup`（event_id → 内容哈希）、`index`（seq 区间 → 字节偏移）、三个计数器 | 重开时全量重扫重建 |

进程重启后第一次碰某个 scope 时才打开文件并重放，不是启动时扫全部 —— 没被访问过的
scope 不付出代价。

### 重放做了什么

顺序读一遍日志，逐行重建上面那张表里的派生数据，边重建边核对：

- 行版本不认识 → `LogCorrupt`
- `first_seq` 不等于上一个 `first_seq + record_count` → `LogCorrupt`（seq 不连续）
- 同一个 `commit_id` 出现两次且 checksum 不同 → `LogCorrupt`
- 记录的 seq 落在自己 commit 的区间之外 → `LogCorrupt`

`lease` 行只用来恢复 `owner_epoch` 和 holder，最后一行说了算。

### 日志为快照与恢复提供什么

快照与恢复本身是 [feature03](feature03-durability-recovery.md) 的事，journal 负责给出它
需要的东西：

| 需要什么 | journal 给什么 |
| --- | --- |
| 「快照覆盖到哪」 | `through_seq` + `state_version`。前者是扫描边界，后者是执行状态版本，快照必须记同一时刻的两个值 |
| 「从哪接着重放」 | `read(after_seq=through_seq)`，返回该 seq 之后的记录 |
| 「恢复的事实来源」 | 只有 `category=state` 的提交参与恢复。`observation` 可以是任何模块写的 trace，不能拿它推执行状态；`event` 是待投递的事实，不是状态 |
| 「谁有权接着写」 | `lease.acquire` 拿新 epoch → `bind_state(owner_epoch=新值)` → `commit_state(expected_state_version=当前值)`。旧 owner 再提交会拿到 `FencedOut` |
| 「快照坏了怎么办」 | 日志不可原位修改，快照只是派生。丢掉快照从 seq 1 完整重放，结果必须一样 |
| 「state owner 消费到哪」 | 必须显式写进 state commit，不能靠「现在日志尾部到哪了」去猜 |

按 run 或 type 过滤分页时，cursor 记的是**已扫描到的 seq**（包括被过滤掉的记录）。
否则一整页都被过滤时，客户端拿到空页会以为流结束了，就此停止消费。cursor 绑定
principal、scope、过滤条件和 limit，换任何一个都不能继续用；它是位置不是凭据，所以
不签名 —— 调用方本来就能直接指定 `after_seq`，签名拦不住任何东西。

### 高可用边界

今天能声明的只有一件事：**单个进程崩溃（被 SIGKILL、或写到一半死掉）之后，重启进程可以
从日志恢复已确认的写入**。这一点有真子进程测试，见下文。

不能声明的：

- **多进程 / 多机 owner 切换**：JSONL 是单写者，`flock` 让第二个写进程直接失败。
  要做这件事需要共享存储和 `multi_process` 能力，属于 feature03 的第 3 阶段。
- **断电**：写路径 `fsync` 了，理论上 fsync 返回后数据在盘上；但没有做过断电验证
  （子进程模拟不了断电），按 feature03 第 4 阶段的要求，没证明过的故障类型不写进声明。

已经排到后面的事：`commits` / `dedup` 两个索引现在都在内存里，随日志长度增长，重开时
全量重扫重建。日志截断、压缩、保留策略不在本 feature 范围。

## 接口

接口在 `core/journal/api.py`，是五个 Protocol。存储实现显式继承它们，mypy 逐个核对
签名 —— 换实现时要满足的是这里，不是 jsonl.py。

```text
Journal       lease() / bind(...) -> Writer / bind_state(...) -> StateWriter
              read(request) / head(principal, scope) / unseal_cursor(token, request) / close()
              capabilities / limits / blobs
Writer        append(records, commit_id) -> receipt
StateWriter   commit_state(records, commit_id, expected_state_version, owner_epoch) -> receipt
Lease         acquire(scope, holder) / renew(scope, owner_epoch)
BlobStore     put(data) / get(blob_id) / verify(blob_id, hash)
```

`reopen()` 和 `crash_on_next_commit()` 不在接口里：它们分别是 `reopen`、`simulate_crash`
能力，不是每种存储都提供。构造参数同样不在接口里。

### 部署限制

`JournalLimits` 是部署施加的上限，超限一律报错，不静默丢记录：

| 限制 | 默认值 | 超了会怎样 |
| --- | --- | --- |
| `max_record_bytes` | 1 MiB | 整批拒绝 |
| `max_batch_records` | 256 | 整批拒绝 |
| `max_page_size` | 100 | 读取时抛 `LimitExceeded` |
| `max_event_bytes` | 256 KiB | `event` 的 payload 超过它必须改用 `payload_ref` 存 blob |

## 验收与验证

- 多模块同时写，记录完整、序号唯一，各自 producer / run 保留。
- 相同 session_id 的两个 user 无法相互读取、写入或查计数器。
- 高频 observation 不造成 state_version 冲突；旧 epoch 的写者被拒。
- 同 commit 重试不重复写；进程在回执前后退出，不丢已确认记录。
- 会话级输入能从 run trace 找到；恢复不更换 run_id。
- 扩展不能伪造 core 终态；普通 observation 不触发 agent 下一步。

## 实施记录

状态：`core/journal/` 已实现并验证。事件投递、快照与恢复（feature01 / feature03）未开始。

### 实际改动

接口在 `core/journal/api.py`，`core/journal/jsonl.py` 是唯一实现，只用标准库。其余：
`records.py` 值对象、`cursor.py` 分页位置、`validation.py` 校验、`codec.py` 负责全项目
统一的字节表示（canonical JSON、哈希、UTC 时间字符串）。

### 验证证据

conformance 套件 21 项，0 跳过（`tests/conformance/`）。套件只断言通用条件（单调、不倒退、
下一条 seq 更大），不断言 `through_seq` 的具体数值 —— 换个存储实现，扫描方式本来就不同。

套件之外用真实子进程补了四项，另有一项直接改文件（`tests/test_journal_jsonl_durability.py`）：

| 测试 | 证明 |
| --- | --- |
| `test_sigkill_preserves_acked_commits` | 外部 SIGKILL 后，逐条核对每个拿到过回执的记录都还在，seq 连续无空洞 |
| `test_half_written_last_line_is_dropped_on_reopen` | 只写一半的那行不算一次提交；重开时丢掉，日志还能继续写 |
| `test_second_writer_is_rejected` | 单写者是事实不是约定 |
| `test_batch_cut_in_half_leaves_nothing_visible` | 一批记录写到一半进程死掉，这批一条都不出现 |
| `test_corrupt_middle_line_is_reported_not_truncated` | 中间一行损坏时报 `LogCorrupt`，文件一个字节不被截掉，且重复调用同样报错 |

最后一项是注入 bug 才补的：原来 `read_all_lines` 把「解析失败」和「末尾半行」当成同一
回事，于是损坏点之后的提交被静默截掉。现在把 `raise` 改回 `break` 这条测试就会失败
（`DID NOT RAISE LogCorrupt`）—— 试过。

`checksum_ignores_storage_fields` 用「同一批内容写到两个 seq 起点不同的 scope，checksum
必须相同」来验证。注入 bug 试过：把 scope 混进 checksum，这条会失败。

### 已知限制

- `commits` / `dedup` 两个索引在内存里，随日志长度增长；重开时全量重扫重建。
- 每次 append 都 `fsync`，正确优先于吞吐。
- 单写者。多进程 owner failover 要等 SQLite adapter。
- 没有 subscribe。轮询交付留给事件运行时（feature01）需要时再写，那时才有真实调用方
  和测试。
- blob 没有清理机制。
- 租约不检查 principal，见上文「谁可以读写哪些 scope」。
