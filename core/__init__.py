"""Oath core —— 稳定内核。

依赖方向（不可违反）：所有外层 → `core/`。core 不反向依赖。

core 只包含：事件消费与状态机、agent step、工具生命周期、journal/recovery、deadline、
权限判定、context 组装、skill 加载、模型调用。core 的内置工具保持最小（运行状态查询、
显式等待/取消）；文件、shell、网络、MCP、模型供应商、Nostalgia、业务 Kit 都通过扩展接入。

core 不禁止第三方库 —— 需要什么就 import 什么，装在 `pyproject.toml` 里。

各模块直接 `from core.clock import ...` 这样引用，这里不做汇总 re-export。
"""
