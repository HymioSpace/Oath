<p align="center">
  <img src="assets/oath-banner.png" alt="Oath — 事件驱动、持久化、独立监督" width="880" />
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-4b70ff?style=flat-square" alt="MIT License" /></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.11%2B-101b2d?style=flat-square&logo=python&logoColor=white" alt="Python 3.11+" /></a>
  <a href="https://github.com/HymioSpace"><img src="https://img.shields.io/badge/by-HymioSpace-101b2d?style=flat-square" alt="By HymioSpace" /></a>
</p>

<p align="center">
  <a href="README.md">English</a> · <strong>简体中文</strong><br />
  <a href="#三个核心特性">三个核心特性</a> · <a href="#快速开始">快速开始</a> · <a href="spec/overview.md">架构</a> · <a href="spec/manual.md">接入文档</a> · <a href="#参与贡献">参与贡献</a>
</p>

Oath 是 personal agent runtime，围绕 **事件驱动、持久化、独立监督** 构建。

一个 user_id 对应一个持续存在的 Agent。用户断线后，消息、工具和系统事件仍然驱动执行；重连后按设备游标补发。
RabbitMQ 提供三条独立队列；有界 Worker 池按用户租约执行；SQLite 由独立 Storage 服务持有。
不创建 session，也没有每个用户永久占用的线程。

## 快速开始

```bash
scripts/oath-install
# 在 oath.toml 配置模型、端点与 Vault 别名；首次启动会提示输入 API Key
scripts/oath-docker up
.venv/bin/python scripts/terminal_client.py
```

默认 WS 端口 8765，业务 HTTP 端口 8766，Vault 本机端口 8200，只绑定本机。数据保存在独立 Docker volumes。
本机 SQLite 和单节点 RabbitMQ 不提供多机高可用，默认 Bash 也不是多租户沙箱。

- [English README](README.md)
- [架构与核心约束](spec/feature-distributed-personal-agent.md)
- [接入与部署](spec/manual.md)
- [验证记录](docs/verification-distributed-agent.md)

MIT License。Oath 得名于 MONO 的同名作品。
