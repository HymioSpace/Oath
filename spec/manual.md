# 接入与运行

## Docker

```bash
scripts/oath-install
# 编辑 oath.toml：[agent.provider] 的 model、api_base、credential、import_env。
oath start --config ./oath.toml
.venv/bin/python scripts/terminal_client.py
```

`oath.toml` 是配置入口，本地文件不进 Git。`credential` 是 Vault 中的别名；
首次启动从宿主机的 `import_env` 读取 API Key，未提供时终端安全输入。
启动器先启动并解封持久化 Vault，设置只读 Worker 身份、注入缺失的模型 Key，再启动 Oath。
模型 Key 不写入 TOML，也不进入 Agent 服务环境。旧 `.env` 若存在，安装器会归档为 `data/private/legacy-environment.backup`。

```bash
oath status
oath restart
oath stop                      # 保留数据和 Vault volume
```

外部扩展可在 `oath start` 时追加：`--skill DIR` 包含多个技能子目录，`--tool DIR` 与
`--cli DIR` 直接包含 `.py` 模块。相同选项可重复传入。命令保存有效配置，后续
`restart` 不需要重复输入目录。目录在容器中只读挂载；详见 [命令与扩展设计](feature-oath-command.md)。

默认只在本机发布 WS `8765`、Biz `8766` 与 Vault 管理端点 `8200`。
Compose 服务读取同一份 `oath.toml`；Docker profile 只映射地址和目录。
容器内 RabbitMQ、Storage 等不发布到宿主机。再次启动会解封 Vault；
`data/private/vault-bootstrap.json` 是恢复所必需的本机文件，应与 Vault volume 一起备份。
首版的运维边界见 [Vault 设计](feature-vault.md)。

## WS v2

所有 command 为 `{v:2,id,type,payload}`。id 应稳定重试，同 id 不允许换内容。
连接第一条：

```json
{"v":2,"id":"attach-1","type":"agent.attach","payload":{"credential":"<user token>","client_id":"iphone"}}
```

凭据在 Gateway / Biz 解析成 user_id；客户端不能自行声明目标用户。无需创建 session。

```json
{"v":2,"id":"msg-1","type":"user.message","payload":{"content":"午饭35，帮我记一下","user_context":{"locale":"zh-CN"}}}
```

收到 ack 表示已持久接纳，不表示模型已处理。user_context 同名覆盖，每个 key 保留更新时间，24h 后读取失效。
返回的事件：

```json
{"v":2,"type":"event","user_id":"local","event_seq":12,"event_type":"assistant.message","payload":{"step_id":"...","content":"..."},"occur_dt":"..."}
```

客户端保存 / 展示后确认：

```json
{"v":2,"id":"ack-12","type":"event.ack","payload":{"cursor":12}}
```

重连先收到 offline.start，再重放未确认事件，再收到 offline.end，之后才是在线流。
设备 id 必须稳定；不同设备用不同 id。事件按 user_id + event_seq 去重，不能仅靠消息正文去重。

支持的其他命令：

| type | payload |
| --- | --- |
| permission.decide | invocation_id、status=approved / denied |
| run.cancel | 可选 run_id，省略为当前 run |
| step.retry | 空对象；显式恢复 failed / paused step |
| agent.status | 空对象；调试用户状态 |
| ping | 空对象 |

## HTTP

用户 bearer token 与 WS 相同；调试时从本地 `oath.toml` 的 `services.users` 读取对应 token。CLI capability 只能访问 CLI，不可查询 trace。

```bash
curl -H "Authorization: Bearer $OATH_USER_TOKEN" 'http://127.0.0.1:8766/list_traces?limit=10'
curl -H "Authorization: Bearer $OATH_USER_TOKEN" 'http://127.0.0.1:8766/query_trace?trace_id=<id>'
```

CLI 目录 `/cli_commands`，执行 `POST /cli/<name>`，参数 JSON。工具执行环境注入 oath-cli 的端点与短期用户 capability，
Biz 获取当前未过期的 user_context 交给扩展。服务级凭据不进入 Bash 环境。

## 验证

`scripts/e2e_services.py --amqp-url <disposable RabbitMQ URL>` 启动真实独立服务、临时 SQLite 和本地 HTTP 模型桩。
它验证首轮输入、执行期间新输入、断线持续执行、边界重放、设备 ACK、防重、Bash 结果唤醒、用户隔离、trace 查询与 Worker 崩溃恢复。
模型桩只替代外部模型，不替代 RabbitMQ、数据库、WS 或进程；不调用收费模型。
