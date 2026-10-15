<p align="center">
  <img src="assets/oath-banner.png" alt="Oath — 持久化、事件驱动的 agent runtime" width="880" />
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-4b70ff?style=flat-square" alt="MIT License" /></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.11%2B-101b2d?style=flat-square&logo=python&logoColor=white" alt="Python 3.11+" /></a>
  <a href="https://github.com/HymioSpace"><img src="https://img.shields.io/badge/by-HymioSpace-101b2d?style=flat-square" alt="By HymioSpace" /></a>
</p>

<p align="center">
  <a href="README.md">English</a> · <strong>简体中文</strong><br />
  <a href="#快速开始">快速开始</a> · <a href="spec/overview.md">架构</a> · <a href="spec/manual.md">接入文档</a> · <a href="#参与贡献">参与贡献</a>
</p>

Oath 是一个用 Python 构建的 agent runtime，为需要接收消息、执行工具、等待审批、在中断后恢复的 agent 提供统一基座。执行状态、上下文管理和 trace 都由 runtime 管理，你可以专注于应用本身。

通过 WebSocket 接入客户端，添加自己的工具和技能，通过 HTTP 查询执行情况。Oath 属于 [HymioSpace](https://github.com/HymioSpace)，也可以独立使用。

> **早期开发阶段。** 当前采用单进程与本地 JSONL 持久化，接口可能变化。分布式执行和自动监督恢复尚未提供。

## 为什么选择 Oath？

- **事件驱动执行。** 用户消息、工具结果和系统决策分别进入三条队列，各自决定何时唤醒 agent。某个工具等待批准时，其他工具的结果仍可推动执行。
- **重启后恢复状态。** 追加日志记录已接纳的输入和执行状态，恢复时保留调用身份与待审批请求；无法确认结果的工具会明确报告，而不会自动重做。
- **完整的工具生命周期。** 统一管理参数校验、权限、超时、取消和终态。内置 Bash 通过可替换的执行环境运行。
- **上下文管理。** 大段工具结果截断后可按需取回原文；异步与同步压缩保留完整的工具调用与结果组合。
- **可检查的执行过程。** 基于 OpenTelemetry span 模型组织 trace，查看输入事件、模型请求与响应、步骤间因果关系和耗时。
- **简单的扩展方式。** 通过目录加载 Python 工具、CLI 命令和 Markdown 技能；私有扩展可以放在已被 Git 忽略的 `extensions-local/` 中。

## 快速开始

需要 **Python 3.11+**、[uv](https://docs.astral.sh/uv/getting-started/installation/)、Bash，以及一个兼容 OpenAI API 的模型服务。

```bash
git clone https://github.com/HymioSpace/Oath.git
cd Oath
uv sync
cp oath.example.toml oath.toml
mkdir -p workspace
```

在 `oath.toml` 的 `[agent.provider]` 中配置 `model`、`api_base` 和 `api_key_env`。示例使用 `OPENAI_API_KEY`，然后从仓库根目录启动：

```bash
export OPENAI_API_KEY="your-api-key"
uv run python scripts/run_runtime.py --config oath.toml
```

启动后，WebSocket 默认监听 `ws://127.0.0.1:8765`，负责消息、审批和事件；HTTP 默认监听 `http://127.0.0.1:8766`，提供 trace 查询与 CLI 命令。

示例面向本地体验：包含演示凭据，默认工具策略允许所有调用。Bash 在宿主机执行，`workspace_root` 只是默认工作目录，**不是沙箱边界**。面向其他用户部署前，请阅读[工具权限](spec/feature-tool-system.md)和[执行环境](spec/feature-bash-cli.md)设计。

另开一个终端，在仓库根目录连接：

```bash
PYTHONPATH=. uv run python scripts/terminal_client.py \
  --url ws://127.0.0.1:8765 \
  --token demo-token
```

试着发送「用 bash 看看工作区有哪些文件」。终端会显示回复与 runtime 事件，输入 `:quit` 退出。

查询最近的 trace：

```bash
curl -H "Authorization: Bearer demo-token" \
  "http://127.0.0.1:8766/list_traces?limit=10"
```

## 工作方式与扩展

三个 consumer 各自缓存事件、判断唤醒条件。agent 被唤醒后，通过一次持久化操作认领三个 buffer 中可用的输入。同一 session 的模型步骤串行执行，工具有独立的执行生命周期。

状态按 `user_id` 和 `session_id` 隔离，通过 run、step 和 trace ID 关联执行过程。日志负责恢复，trace 负责观察。存储与执行环境通过接口定义，当前提供本地文件存储与宿主机执行实现。

在扩展目录加入能力后重启即可加载：

```text
extensions-local/
├── tools/
│   └── notes.py
├── cli/
│   └── echo.py
└── skills/
    └── research/
        └── SKILL.md
```

工具直接暴露给模型，技能提供可复用的指令，CLI 命令通过 Bash 中的 `oath-cli <command>` 调用。`extensions/` 与 `extensions-local/` 使用同一套加载规则。

## 文档

- [整体架构](spec/overview.md)
- [客户端接入：WebSocket、HTTP、审批与重连](spec/manual.md)
- [事件处理与工具批次](spec/feature-intake.md)
- [持久化与恢复](spec/feature-journal.md)
- [上下文压缩](spec/feature-compaction.md)
- [Trace](spec/feature-trace.md)
- [扩展开发](spec/feature-extensions.md) · [CLI 示例](extensions/cli/echo.py)

## 参与贡献

欢迎提交问题、改进文档或发起范围明确的 PR。涉及执行语义或公共接口的变化，请先通过 [issue](https://github.com/HymioSpace/Oath/issues) 讨论设计。架构与功能设计位于 [spec/](spec/)。

```bash
uv sync
uv run ruff check .
uv run mypy .
uv run pytest tests/ -q
```

默认测试跳过 slow 测试，完整配置见 [pyproject.toml](pyproject.toml)。

## HymioSpace

[Hymio](https://github.com/HymioSpace/Hymio) 提供应用体验，[Nostalgia](https://github.com/HymioSpace/Nostalgia) 管理个人 context，Oath 提供运行基座。

名字来自 [MONO 的《Oath》](https://monoofjapan.bandcamp.com/album/oath)，也代表在中断之后仍把工作继续下去的承诺。

## 许可证

[MIT](LICENSE) © HymioSpace。
