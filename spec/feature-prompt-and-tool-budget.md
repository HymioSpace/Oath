# System prompt 与工具续行预算

状态：Implemented

## 总览

应用可以用 Markdown 文件替换角色与行为指令。Oath 仍提供实际工作区、技能目录和工具协议。固定指令排在前面，运行输入只追加到历史，不把时间、设备列表或状态写进固定前缀。

`应用 Markdown → 启动时读取 → 固定能力说明 → 固定技能索引 → 历史与本步输入`

工具结果可以继续工作，但不能无限产生模型调用。每用户连续纯工具 step 有独立持久计数，默认上限 100。到上限后保留 buffer，等待新的用户输入或有意义的系统干预。

## Prompt

`[agent].system_prompt_file` 相对配置文件解析，UTF-8、非空、最多 128 KiB。与非空内联 system_prompt 互斥；`oath start --system-prompt FILE` 明确覆盖配置中的正文。Docker 只读挂载该文件，restart 沿用路径。修改后重启才生效；已经冻结的请求不改写。

文件仅替换应用正文，不能改权限、身份或调度。没有文件时保留内联配置。技能正文按需读取；目录排序确定，工具定义顺序稳定。缓存是否命中仍以 provider usage 为准。

## 续行预算

`[agent].max_tool_only_steps = 100` 限制连续只由工具结果驱动的 step，不是工具调用数量，也不计同一请求的模型重试。含用户消息的 step 重置计数；显式系统干预可重置。system.invoke、自动权限过期、状态查询和重连不重置。原有 run 总步数和时限继续独立生效。

Consumer 不再为耗尽预算的纯工具输入请求唤醒；Intake 在认领事务中再次检查，防止旧调度请求、Supervisor 或并发投递绕过。到限不认领、不丢弃输入，不取消正在执行的工具。下一次外部输入会连同这些结果一起认领。

计数、暂停与恢复随 journal 持久化。暂停通知经同一状态提交投影到客户端 outbox，不额外调用模型。崩溃和重复投递不增加计数；已经准备好的 step 恢复执行不重复计算。Supervisor 空检查及暂停检查均无模型成本。

对外追加 agent.paused / agent.resumed 事件，暂停包含 reason=tool_only_budget、steps、limit。state.summary 的 meta.tool_wake_pause 与会话历史保留该状态；旧客户端可以忽略新增事件，Hymio 显示无需模型生成的暂停提示。
