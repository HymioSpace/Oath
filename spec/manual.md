# 接入与运行

## Docker

```bash
scripts/oath-install
# 编辑 oath.toml：模型、api_base、prompt 等；.env：OPENAI_API_KEY 和凭据
docker compose up -d --build
docker compose logs -f worker consumer
.venv/bin/python scripts/terminal_client.py
```

脚本生成本地凭据并保留已有配置。`.env`、`oath.toml`、数据 volume 不进版本库。
Compose 只读挂载同一份 oath.toml；Docker profile 将服务地址、监听地址和运行目录映射到容器。修改配置后重启服务即可，无需重建镜像。
模型选择和 prompt 等始终以 oath.toml 为准；.env 只放凭据与 Compose 的宿主机端口映射。
端口：WS `ws://127.0.0.1:8765`；Biz `http://127.0.0.1:8766`。默认不发布内部 RPC / AMQP。
使用反向代理暴露远程访问时配置 TLS；设置 OATH_BIND_HOST 可更改宿主机监听地址。

停止：`docker compose down` 保留数据；不要加 `-v`，除非确实要删除数据。
Worker 扩容：`docker compose up -d --scale worker=3`。同用户仍由租约串行。
RabbitMQ 单节点和 SQLite 单 owner 的部署限制见 feature spec。

## 非 Docker

安装 Python 3.11+ 与 uv 后执行 scripts/oath-install；另行启动 RabbitMQ，设置 OATH_AMQP_URL。
编辑 oath.toml（以 oath.example.toml 为准），运行 scripts/oath-up。scripts/oath-down 或 Ctrl-C 停止。
单独进程：`.venv/bin/python -m bootstrap worker --config oath.toml`。

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

用户 bearer token 与 WS 相同；CLI capability 只能访问 CLI，不可查询 trace。

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
