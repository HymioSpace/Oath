# Feature — Bash 与 CLI

状态：实现中

## 1 背景与目标

agent 需要两样现在没有的能力：在自己指定的目录里跑一条命令；把外部系统的业务查询变成
模型能直接调的接口。

- **`bash` 是 core 工具**：命令在哪执行要能换（宿主机 → 沙箱），所以执行环境是接口。
  命令默认在 workspace 下跑，workspace 根目录来自配置，并写进 system prompt。
- **`oath-cli` 是模型能用的命令行**：它在 bash 里执行，把 `oath-cli echo foo` 代理到
  runtime 的 CLI server；业务逻辑在 `extensions/cli/`。

不包含：沙箱执行环境；让 CLI 命令参与调度或权限扩展。

## 2 前置依赖

- [工具系统](feature-tool-system.md)：bash 是注册进去的一个 `Tool`，权限、deadline、
  截断、终态都走同一条路。
- [user_context](feature-user-context.md)：CLI 命令拿到的额外入参。
- [Event runtime](feature-event-runtime.md)：biz server 是同进程的另一个 server。

## 3 总览

### 3.1 一句话

**bash 是 core 工具，命令在「执行环境」里跑 —— 今天是宿主机，明天是沙箱；它默认落在配置
给出的 workspace 下，而 workspace 里有什么命令可用，由 `oath-cli` 代理到 biz server 决定。**

### 3.2 流程

```mermaid flowchart TD
    M[模型] -->|bash command| T[BashTool]
    T -->|command cwd timeout env| E[ExecutionEnvironment]
    E -->|宿主机: subprocess| P[shell]
    T -->|按 scope 现算的环境变量| X[子进程 env]
    P -->|oath-cli echo foo| C[CLI client]
    C -->|POST /cli/echo| S[biz server]
    S -->|body 参数 + scope + user_context| H[extensions/cli 业务命令]
    H -->|JsonValue| S
    S -->|JsonObject| C
```

贯穿全文的规则：

- **执行环境是接口，不是实现。** core 只知道「在某个目录跑一条命令、给我 stdout/stderr/
  耗时」，不关心那是宿主机还是沙箱。
- **命令有界。** 超时由执行环境自己 kill 子进程并留下已产生的输出 —— 工具层的 deadline 到点
  只是 core 不再等那个线程，杀不掉它。
- **workspace 是默认，不是围栏。** 命令落在 workspace 下；用户点名要访问别处，模型传
  `cwd` 即可，core 不拦。是否放行由权限策略裁定。
- **CLI 是框架只做透传。** server 解析出身份、把 `user_context` 整份交给业务命令；命令
  自己决定要不要鉴权、要不要用其中某个 key。
- **CLI 不单独开 server。** 它是 biz server 里的一组 path，与观测查询同端口、同 credential。

### 3.3 一个完整的例子

配置里 `workspace_root = "./workspace"`、`cli_bin_dir = "./scripts"`。模型看到 system prompt
里说明 workspace 是 `/repo/workspace`，于是调用：

```json
{"name": "bash", "arguments": {"command": "oath-cli echo docs", "cwd": "/repo/workspace"}}
```

`HostEnvironment` 在 `/repo/workspace` 起进程，`PATH` 前面加了 `/repo/scripts`（找到
`oath-cli`），环境里带上这次 scope 的 `OATH_CLI_SERVER` 与 `OATH_CLI_TOKEN`。`oath-cli`
把它们原样放进请求头，发 `POST /cli/echo`，body 是 `{"text": "docs"}`；server 解析出
`u1/s1`，取出该 scope 的 `user_context`，调 `extensions/cli` 里的
`echo(text, scope=..., user_context=...)`。

模型拿到的工具结果是一段 JSON：

```json
{"command": "oath-cli echo docs", "cwd": "/repo/workspace", "exit_code": 0,
 "duration_ms": 61.4, "timed_out": false, "truncated": false,
 "stdout": "{\n  \"text\": \"docs\"\n}", "stderr": ""}
```

## 4 接口与数据

### 4.1 执行环境

```
class ExecutionEnvironment(ABC):
    def run(self, command: str, *, cwd: Path, timeout: float, env: Mapping[str, str]) -> ExecutionResult
```

`ExecutionResult`：`exit_code: int | None`（超时时为 `None`）、`stdout`、`stderr`、
`duration_ms`、`timed_out`、`truncated`。

**`env` 每次调用给。** 一个进程里有很多 scope，把某个身份写进构造参数等于所有 session 共用
那一个身份。执行环境本身不碰身份 —— 谁要往里放什么是调用方的事。

当前实现 `HostEnvironment`：构造参数是 `root`（workspace 根）与 `bin_dirs`（前置到子进程
`PATH`）。它负责 core 不该管的事：

- **超时 kill 整个进程组**，不是只 kill 直接子进程 —— `sh -c "a; b"` 一般不 exec 到底，
  只杀 shell 的话 `b` 还活着抱着管道，超时反而变成干等。
- **读的时候就有界**，不是读完再截。两个流各一个线程边读边留头尾，中间丢掉，内存占用
  与命令写了多少无关。
- **环境按白名单继承**：`PATH` / `HOME` / `LANG` / `TMPDIR` 等少数几个，其余不传给命令 ——
  模型 API key 这类服务端凭证不该落到任意一条 bash 命令能读到的地方。
- **真的用 bash 跑**。工具叫 bash，`[[ ]]` 就该能用；系统里没有 bash 时退回 `/bin/sh`，
  这时语法按 POSIX 写。

### 4.2 bash 工具

入参：`command`（必填）、`cwd`（可选，缺省是 workspace 根）。

结果是 JSON 字符串，字段固定：`command` / `cwd` / `exit_code` / `duration_ms` / `timed_out` /
`truncated` / `stdout` / `stderr`。执行环境先给每个流留一份上限，再由
[工具系统](feature-tool-system.md)已有的 `ResultShrinker` 截断，全文走 `detail_read`。

**非零退出码不是失败**：那是命令的结果，模型要读它。只有环境本身跑不起来
（`cwd` 进不去、进程起不来）才抛 `ToolExecutionError`。

### 4.3 workspace

`AgentConfig.workspace_root`，默认 `./workspace`；`AgentConfig.cli_bin_dir`，默认 `./scripts`。
两者在装配期解析成绝对路径：**相对路径按 runtime 进程的 cwd 算，不是按命令的 cwd** ——
命令默认落在 workspace 下，相对的 `cli_bin_dir` 若照原样进 `PATH`，命令就会去找
`workspace/scripts/oath-cli`。它们也决定 system prompt 里那段说明写的路径。

system prompt 的这一段由 core 生成（`workspace_note`），跟在配置给的 prompt 之后 ——
与技能索引同一个位置、同一条约定。

### 4.4 CLI 命令

```
def echo(text: str, *, scope: Scope, user_context: JsonObject) -> JsonValue
```

参数声明在 `parameters`（一份 JSON Schema）里，**属性顺序就是位置参数顺序** ——
`oath-cli echo docs` 里 `docs` 落在第一个属性上。同一份声明也用来校验服务端收到的
body：缺失、拼错、类型换不过去一律 400。

`scope` 与 `user_context` 由框架传入，用不上的命令也照收 —— 签名一致，要鉴权的命令才能
自己写在那儿，不必改框架。`extensions/cli/` 里的函数不知道自己被 HTTP 调过。

命令在 `<扩展目录>/cli/<名字>.py` 里声明，装配期扫出来进 `runtime.commands`；路由在
`biz/cli/cli_server.py`，那张表从 runtime 读。**声明与路由分开**：路由是框架的事，所有命令
一条路；声明是业务的事，放在业务自己的目录里。加命令不用改框架。
细节见[扩展目录](feature-extensions.md)。

`description` 与 `parameters` 用英文（随请求发给调用方的字面量），注释与文档用中文。

### 4.5 `oath-cli`

`scripts/oath-cli` 可执行文件，只用标准库。从 `OATH_CLI_SERVER` / `OATH_CLI_TOKEN` 读端点
与 credential —— 它**不做任何鉴权判断**，只是把执行环境给的东西原样放进请求头。
位置参数按 `parameters` 的声明顺序映射，也接受 `--key=value`。无参数时打印用法。

## 5 实现方案

| 模块 | 职责 |
| --- | --- |
| `core/protocol/interface/execution.py` | 执行环境接口与 `ExecutionResult` |
| `core/shell/impl/host_environment.py` | 宿主机实现：subprocess、超时 kill、输出上限、PATH |
| `core/shell/bash_tool.py` | `BashTool` 与 system prompt 的 workspace 段落 |
| `biz/cli/cli_server.py` | 路由、参数校验、透传；命令表从 `runtime.commands` 读 |
| `biz/session_scope.py` | 从请求认出 scope；认证与参数错误的回法，观测查询与 CLI 共用 |
| `extensions/cli/` | 业务命令：纯函数 + 模块级 `COMMANDS` 声明 |

命令走 `POST /cli/<name>`、参数是 JSON body，命令目录是 `GET /cli_commands`。**执行不用
GET**：命令是动作，将来代理写操作时 GET 的缓存、预取与重试语义都不对。

装配在 `bootstrap/runtime.py`：`HostEnvironment` 无条件建，`BashTool` 无条件注册 ——
与 `detail_read`、`skill` 一样属于 core 给模型的固有工具。`cli_env(scope)` 由 bootstrap
注入，按 scope 现算 `OATH_CLI_SERVER` / `OATH_CLI_TOKEN`。

CLI 路由与 `/list_traces`、`/query_trace` 同一个 server、同一个端口：
`GET /cli/<name>` 是命令，`GET /cli_commands` 列出命令与参数。

## 6 失败与恢复

- **命令超时。** 执行环境 kill 子进程，返回已产生的 stdout/stderr 与 `timed_out=True`，
  `exit_code` 为 `None`。模型看到自己读到了什么、能判断是超时而不是失败。
- **命令 fork 出去的子孙进程。** 超时时杀整个进程组（子进程自己开 session，kill 组而不是
  kill 进程）—— 只杀 shell 的话，`sleep` 之类的进程会接着跑、继续占着管道，超时反而变成
  干等。命令自己 `setsid` 逃出去的进程不在管辖范围内。
- **输出爆炸。** stdout / stderr 各留头尾，中间插省略标记，`truncated=True`。没有上限的话
  一条 `yes` 就能把内存吃满，早于任何超时。
- **`cwd` 进不去。** 抛 `ToolExecutionError`，工具终态 `failed`，模型的下一轮看得见原因。
- **命令不存在（`oath-cli` 不在 PATH 上）。** bash 的 exit code 127 与 stderr 如实返回，
  不是工具失败。
- **命令把自己 `setsid` 出去。** 进程组杀不到逃出去的进程，管道还可能被它占着；读线程
  会等 5 秒再放弃，返回已经拿到的内容。这是宿主机实现的边界。
- **CLI 请求没有 credential。** server 回 401，`oath-cli` 以非零码退出、错误写 stderr。
  bash 的 exit code 照样如实反映。
- **沙箱执行环境未做。** 当前只有宿主机实现。这是本设计的扩展点，不是缺陷：接口按
  「在某个目录跑一条命令」定，换实现不改工具与 server。
- **默认配置等于放行任意命令。** `AllowAllPolicy` 是演示实现，它不拦任何 bash 调用。生产
  由 Hymio 的权限策略在每次执行前裁定 —— core 每次执行都问策略，策略说不行就不跑。

## 7 跨仓库协议变更

- **Hymio / 客户端**：无新增字段。客户端看到的是 bash 工具调用与终端里 `oath-cli` 的输出。
- **Hymio**：要提供 bash 的权限策略，否则默认配置下 agent 可以在宿主机上执行任意命令。
- **Hymio 生产认证**：需要能按 scope 出 credential（当前从配置里的 credential 映射反查）。
  做不到时要签一个范围更小的委托 token，不能给所有 session 共用一个 credential。

## 8 待决事项

- 沙箱执行环境的接口细化：远端还是本地进程、凭据怎么带、能不能访问宿主网络。
- 沙箱里 `oath-cli` 是预装还是同样由 PATH 注入 —— 预装更简单，但要保证版本跟 runtime 对得上。
- 单条命令能读多少文件、能不能出网，由执行环境决定；沙箱方案出来前没有约束。
