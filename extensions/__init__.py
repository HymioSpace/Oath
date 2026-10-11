"""业务扩展 —— 可缺省。

只提供业务内容和动作：业务 tool、业务 skill（目录里的 SKILL.md）、额外 context 来源。
权限、超时、状态迁移、生命周期这些机制都在 core，不在这里实现。

删掉本目录 runtime 仍能启动，只是没有业务能力可用。详见 `spec/feature05-extensions.md`。
"""
