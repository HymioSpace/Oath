# External context provider

The application may configure a bounded authenticated HTTP context provider. Runtime passes
the authenticated owner `user_id`, optional delegated `agent_id`, and current client channel
(`web`, `ios`, `mac`, or `unknown`), receives untrusted context data, and freezes it into the immutable model request
before compaction accounting. Retries reuse the frozen request. The provider owns all channel
and delegated-context policy; Oath does not know which application features belong to a channel
or context. `agent_id` selects application context and never changes authorization identity.
No personal-memory schema or storage lives in Oath. Failed providers are explicitly represented
as unavailable; application data never authorizes tools. Credentials stay in mounted private files.
