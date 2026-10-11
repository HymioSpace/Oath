"""业务层 —— 用 core 的能力实现产品语义，但 core 不知道它存在。

对照 spec/overview.md 三层划分：
- `core/`     机制。事件驱动、journal 唯一真相、工具生命周期、恢复。
- `biz/`      语义。什么算一次成功运行、什么该进 pending_reconciliation、
              对用户说什么。
- `extensions/` 业务扩展（可缺省）。

Stage 1-5 全在 `core/`。这里留空到 Stage 5 的端到端场景开始。
"""
