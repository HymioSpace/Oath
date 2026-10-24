# External context provider

The application may configure a bounded authenticated HTTP context provider. Runtime passes
the authenticated user identity and the current client channel (`web`, `ios`, `mac`, or
`unknown`), receives untrusted context data, and freezes it into the immutable model request
before compaction accounting. Retries reuse the frozen request. The provider owns all channel
policy; Oath does not know which application features belong to a channel.
No personal-memory schema or storage lives in Oath. Failed providers are explicitly represented
as unavailable; application data never authorizes tools. Credentials stay in mounted private files.
