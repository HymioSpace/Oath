# Message feedback

Status: Implemented

`POST /messages/react` accepts actor, target (`user:<event_seq>` or
`assistant:<step_id>`), emoji (null removes), and operation_id. Identity is derived
from credentials. Applications must restrict the actor based on their caller.
Storage validates the target belongs to that user and the opposite speaker.
An operation ID is immutable and idempotent. The output event
`message.reaction.changed` and user feedback admission are one transaction.
User feedback enters the user queue as `app.message.reaction`, without generating
a user.message bubble. Assistant feedback only emits an output event.
History includes latest feedback on messages in each page. Unknown events can be
ignored by old clients. Feedback is not a conversation boundary.

Generic user message_id equals command ID; input rendering exposes a user_message
reference. The mutation resolves either that reference or event_seq to the canonical
user event sequence. Existing history without message_id still accepts event_seq.
Feedback does not supersede a pending tool run or invalidate its results. Repeating
the current emoji with a fresh operation ID does not wake the agent again.
