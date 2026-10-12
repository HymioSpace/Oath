"""core：冻结内核。

事件运行时、状态机、agent step、工具系统、journal、恢复。不反向依赖
任何上层（extensions / bootstrap / biz），不耦合渠道、UI、LLM SDK。
"""
