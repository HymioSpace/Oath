# Feature 06 — Runtime 管理 API

状态：Draft；统一前缀 `/v1/runtime/users/{user_id}`，HTTP 为初始对外协议。认证主体必须有目标 user_id 的访问权；单用户客户端可以由 SDK 自动填入，但不省略服务端隔离检查。

| 方法与路径 | 语义 |
| --- | --- |
| POST `/sessions` | 建会话，201 + session_id |
| GET `/sessions/{id}` | 状态投影及 through_seq |
| POST `/sessions/{id}/inputs` | durable 接受用户输入，202 + operation_id/event_id |
| POST `/sessions/{id}/commands` | interrupt、resume、configure 等受控命令，202 |
| GET `/sessions/{id}/operations/{operation_id}` | accepted/running/completed/failed 状态、result/error |
| GET `/sessions/{id}/events?after_seq=…` | 已提交事件分页 |
| GET `/sessions/{id}/stream?after_seq=…` | SSE，事件 ID 为 session seq，断线后重读 |
| GET `/sessions/{id}/runs` | 该用户会话内的运行列表 |
| GET `/sessions/{id}/runs/{run_id}` | 单次运行状态及输入绑定 |
| GET `/sessions/{id}/traces?run_id=…` | 通过 Journal 查询该次运行的 step、工具与恢复记录 |
| GET `/sessions/{id}/tools` | 当前授权后的工具描述 |
| GET `/sessions/{id}/context` | agent 最近一次实际使用的版本化 context 引用 |
| GET `/sessions/{id}/memory` | 已安装 memory provider 的授权视图；不存在返回 capability_unavailable |

写入提供 Idempotency-Key，同主体/user/session/路径/键/相同 body 返回既有 operation，不同 body 冲突。输入 ACK 只表示 durable 接受，不能当作 agent 完成；重连按 operation 查询，不重复创建新输入。

Principal 由宿主 Hymio 或独立部署的认证适配器提供；core 校验 `(user_id, session_id)` scope，run_id 是其下的 trace 过滤条件，不能替代授权。trace/raw context 可能含个人数据，所有读 API 均按 scope 过滤。memory 读写权与 trace 读权独立；memory 修改走 provider tools，不允许直接改视图。

统一错误体 `{error:{code,message,retryable,request_id,details?}}`，400 schema 错误、401 未认证、403 无权限、404 不存在、409 幂等/状态冲突、429 背压、503 暂不可持久化。列表返回 `{items,next_cursor,through_seq}`；cursor 绑定 principal、user_id、session_id、run_id 过滤条件和数据前缀，最大页大小 100。

验收：已 ACK 输入重启后可查；SSE 重连无缺口（客户端可按 seq 去重）；旧 cursor 已不可用时显式返回错误而非跳过事件；客户端取消等待不取消 agent；未授权用户不能通过 trace 得到其它 session 的 context。
