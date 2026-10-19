# 运行摘要与模型性能指标

## 总览

客户端状态查询只传输运行摘要，不包含历史 prompt、模型响应、工具结果或输入正文。模型缓存只报告 provider 实际返回的 usage；缺失时未知，不推断为零。

`agent.status → state.summary → SQLite 派生状态表 → 小型状态响应`

## 运行摘要

Storage 从用户隔离的 steps/tool_invocations/inbox_events 投影查询当前状态，避免解码与传输完整 state_json。响应包含 user_id、event_seq、run_id、busy、各状态计数、最多 200 个活动步骤与工具，以及最近步骤。字段明确列出，不复制任意 JSON 内容；错误文本限长。Gateway 只暴露摘要，内部 Brain 的 state.read 不变。

活动工具指 admitting / queued / running；waiting_approval 单独计数，不构成执行中。列表截断不影响 busy 与计数。详情仍通过 trace、历史或工具详情接口读取。

## 模型指标

原始 usage 保留在 trace response。报告 prompt_tokens_details.cached_tokens、cache_read_input_tokens 等明确来源的缓存 token，缺失不造数。缓存前缀变化与 provider 是否实际命中是两个问题。

TTFT 表示第一段模型增量（包括 reasoning）到达；额外记录首正文时间，使 UI 等待与模型 prefill、思考阶段可区分。流式 usage 通过 provider extra_params 的 stream_options.include_usage 开启，保持供应商参数显式配置。
