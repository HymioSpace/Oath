# TODO

| 领域 | 后续工作 |
| --- | --- |
| Vault | 用 KMS / HSM auto-unseal 替代本机 unseal 文件；根 token 初始化后撤销或离线保管；定期轮换 AppRole SecretID。 |
| Vault | 服务间 TLS、审计日志、密钥轮换通知与缓存策略；支持按用户和扩展划分更细的 Vault policy。 |
| 凭据 | 将内部服务与 RabbitMQ 凭据也移出 `oath.toml`；为各服务分配独立身份，Tools 不持有其他服务凭据。 |
| 部署 | 多节点 Vault、RabbitMQ 与 Storage 的 HA；密钥与数据库联合备份、恢复演练。 |
| 工具 | Bash 沙箱与网络限制；当前宿主机执行环境允许模型运行命令，不能承载不可信多租户。 |
| 执行 | 外部工具副作用对账、取消后的终态语义、长期运行步骤的容量控制。 |
| 数据 | 历史状态归档、Outbox 保留与客户端重新同步、精确 token 计量。 |
| 可观测 | OTLP 导出、服务级指标与故障告警、Supervisor 的 journal 观察策略。 |
