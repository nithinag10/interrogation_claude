# Business Research Agent API Documentation (v1)

## Base URL
- Local: `http://127.0.0.1:8000`
- Production: your deployed Railway URL

## Overview
This API is session-based and event-driven.

Standard flow:
1. Create a session.
2. Open SSE stream for that session.
3. Send user message(s).
4. Render streamed events (`assistant_delta`, `clarification_question`, `done`, `error`).
5. Optionally interrupt active run.

## Auth
- No auth in v1 (internal/dev mode).
- Add auth before public release.

## Webhook Notifications
The API can notify an external webhook when:
- a session is created
- the first user query is submitted for a session
- the final assistant answer is generated
- feedback is submitted for a session

Environment variables:
- `WEBHOOK_ENABLED=true`
- `WEBHOOK_URL=https://your-system.example.com/webhooks/product-events`
- `WEBHOOK_AUTH_HEADER=Authorization`
- `WEBHOOK_AUTH_TOKEN=Bearer your-shared-secret-or-api-token`
- `WEBHOOK_TIMEOUT_SECONDS=5`

If your receiving system does not require auth, leave `WEBHOOK_AUTH_TOKEN` empty.

## Content Type
- JSON for all POST APIs.
- `text/event-stream` for SSE endpoint.

---

## 1) Health Check
### `GET /health`
Checks server liveness.

Response `200`:
```json
{
  "status": "ok"
}
```

---

## 2) Create Session
### `POST /v1/sessions`
Creates a new chat session.

Request:
```json
{
  "user_id": "local-user",
  "title": "Interview"
}
```

Response `200`:
```json
{
  "session_id": "s_a1b2c3d4e5",
  "state": "NEW",
  "created_at": "2026-03-03T18:00:00.000000+00:00"
}
```

Webhook emitted:
```json
{
  "event_type": "session_created",
  "occurred_at": "2026-03-17T10:00:00.000000+00:00",
  "service": "business-research-agent",
  "trigger": "POST /v1/sessions",
  "session": {
    "id": "s_a1b2c3d4e5",
    "user_id": "local-user",
    "title": "Interview",
    "state": "NEW",
    "created_at": "2026-03-17T10:00:00.000000+00:00",
    "updated_at": "2026-03-17T10:00:00.000000+00:00"
  }
}
```

Validation notes:
- `user_id` required, non-empty.
- `title` optional.

---

## 3) Get Session
### `GET /v1/sessions/{session_id}`
Fetches session metadata, state, context, and message history.

Response `200`:
```json
{
  "session_id": "s_a1b2c3d4e5",
  "user_id": "local-user",
  "title": "Interview",
  "state": "COMPLETED",
  "context": {},
  "messages": [
    {
      "id": "m_123",
      "role": "user",
      "content": "My idea is ...",
      "phase": "intake",
      "created_at": "2026-03-03T18:00:01.000000+00:00"
    }
  ],
  "created_at": "2026-03-03T18:00:00.000000+00:00",
  "updated_at": "2026-03-03T18:00:10.000000+00:00"
}
```

Response `404`:
```json
{
  "detail": "Session not found"
}
```

---

## 4) Send Chat Message
### `POST /v1/chat/send`
Queues a user message to the session worker.

Request:
```json
{
  "session_id": "s_a1b2c3d4e5",
  "message": "I want to build a churn-reduction tool for gyms.",
  "stream": true
}
```

Response `200`:
```json
{
  "session_id": "s_a1b2c3d4e5",
  "state": "RESEARCH_IN_PROGRESS",
  "assistant_message": "Accepted. Subscribe to SSE stream for live updates.",
  "clarification_questions": []
}
```

Important:
- This endpoint returns quickly after queueing.
- Actual assistant output comes via SSE stream endpoint.
- On the first user message for a session, the backend also emits a `first_query_submitted` webhook.

Webhook emitted on first user message only:
```json
{
  "event_type": "first_query_submitted",
  "occurred_at": "2026-03-17T10:05:00.000000+00:00",
  "service": "business-research-agent",
  "trigger": "POST /v1/chat/send",
  "query": {
    "length": 47,
    "text": "I want to build a churn-reduction tool for gyms."
  },
  "session": {
    "id": "s_a1b2c3d4e5",
    "user_id": "local-user",
    "title": "Interview",
    "state": "NEW",
    "created_at": "2026-03-17T10:00:00.000000+00:00",
    "updated_at": "2026-03-17T10:00:00.000000+00:00"
  }
}
```

Webhook emitted when the assistant finishes:
```json
{
  "event_type": "final_answer_generated",
  "occurred_at": "2026-03-17T10:06:30.000000+00:00",
  "service": "business-research-agent",
  "trigger": "session completion",
  "answer": {
    "length": 320,
    "text": "Here is the final answer shown to the user..."
  },
  "session": {
    "id": "s_a1b2c3d4e5",
    "user_id": "local-user",
    "title": "Interview",
    "state": "COMPLETED",
    "created_at": "2026-03-17T10:00:00.000000+00:00",
    "updated_at": "2026-03-17T10:06:30.000000+00:00"
  }
}
```

Response `404`:
```json
{
  "detail": "Session not found"
}
```

---

## 5) Submit Feedback
### `POST /v1/feedback`
Stores a user rating for a session and sends a webhook notification.

Request:
```json
{
  "session_id": "s_a1b2c3d4e5",
  "rating": 4,
  "comment": "Good answer, but it needed more competitor detail."
}
```

Validation notes:
- `session_id` required
- `rating` required, integer from `1` to `5`
- `comment` optional, max `2000` chars

Response `200`:
```json
{
  "session_id": "s_a1b2c3d4e5",
  "state": "COMPLETED",
  "rating": 4,
  "comment": "Good answer, but it needed more competitor detail.",
  "message": "Feedback received"
}
```

Webhook emitted:
```json
{
  "event_type": "feedback_received",
  "occurred_at": "2026-03-17T10:07:00.000000+00:00",
  "service": "business-research-agent",
  "trigger": "POST /v1/feedback",
  "feedback": {
    "rating": 4,
    "comment": "Good answer, but it needed more competitor detail."
  },
  "session": {
    "id": "s_a1b2c3d4e5",
    "user_id": "local-user",
    "title": "Interview",
    "state": "COMPLETED",
    "created_at": "2026-03-17T10:00:00.000000+00:00",
    "updated_at": "2026-03-17T10:07:00.000000+00:00"
  }
}
```

Response `404`:
```json
{
  "detail": "Session not found"
}
```

---

## 6) Interrupt Active Run
### `POST /v1/chat/interrupt`
Sends interrupt signal to active Claude run for a session.

Request:
```json
{
  "session_id": "s_a1b2c3d4e5"
}
```

Response `200`:
```json
{
  "session_id": "s_a1b2c3d4e5",
  "state": "RESEARCH_IN_PROGRESS",
  "message": "Interrupt requested"
}
```

Errors:
- `404`: session missing
- `409`: no active worker/run to interrupt

Example `409`:
```json
{
  "detail": "No active run to interrupt"
}
```

---

## 6) Stream Chat Events (SSE)
### `GET /v1/chat/stream/{session_id}`
Opens server-sent events stream for session updates and assistant output.

Headers:
- `Accept: text/event-stream`

Response:
- HTTP `200` with stream.

Keep-alive:
- Server periodically sends comment lines:
```text
: keep-alive
```

If session missing:
- `404 { "detail": "Session not found" }`

---

## SSE Event Contract
Each event is formatted as:
```text
event: <event_name>
data: <json_payload>
```

### `connected`
Sent immediately when stream connects.

Example:
```text
event: connected
data: {"session_id":"s_a1b2c3d4e5"}
```

### `phase`
Execution phase updates.

Payload:
- `status`: one of
  - `worker_started`
  - `research_in_progress`
  - `awaiting_clarification`
  - `interview_in_progress`
  - `interrupted`
  - `stopped`
  - `completed`
  - `failed`
- `message`: human-readable status message for UI.

### `tool_permission`
Tool permission checks from SDK.

Payload:
- `tool_name` (example: `AskUserQuestion`, `mcp__research_server__simulate_user_interview`)
- `message`: readable description.

### `tool_started`
Fired when assistant starts a tool call.

Payload:
- `tool_name`
- `tool_use_id`
- `input`
- `message`

### `tool_completed`
Fired when a tool call returns.

Payload:
- `tool_name`
- `tool_use_id`
- `is_error`
- `message`

### `clarification_question`
Agent needs human clarification.

Payload:
- `questions`: string array

Example:
```text
event: clarification_question
data: {"questions":["What specific customer segment are you targeting?"]}
```

### `clarification_answer_received`
Emitted when backend receives user answer for a clarification question.

Payload:
- `question`
- `answer_preview`

### `assistant_delta`
Incremental assistant token/chunk.

Payload:
- `text`: string chunk

Frontend should append these chunks to current assistant message.

### `interview_transcript`
Transcript output from `simulate_user_interview` tool.

Payload:
- `tool_name`
- `transcript` (full transcript text)
- `message`

### `interview_status`
Live status updates from inside interview tool execution.

Payload:
- `status` (example: `started`, `concluded`, `completed`)
- `message`
- optional `turn`, `hypothesis`

### `interview_turn_started`
Signals beginning of each simulated interview turn.

Payload:
- `turn`
- `message`

### `interview_message`
Per-turn transcript message emitted directly by tool execution.

Payload:
- `turn`
- `role` (`interviewer` or `customer`)
- `content`

### `done`
Run completed for current user input.

Payload:
- `session_id`
- `sdk_session_id`
- `is_error` (boolean)
- `cost_usd` (number|null)

### `message`
Non-primary internal message type marker.

Payload:
- `type` (SDK message type name)

### `error`
Worker/runtime error.

Payload:
- `message`: error string

---

## Session States
Possible states:
- `NEW`
- `INTAKE`
- `AWAITING_CLARIFICATION`
- `READY_FOR_RESEARCH`
- `RESEARCH_IN_PROGRESS`
- `COMPLETED`
- `FAILED`

State updates happen during worker flow and clarification handling.

---

## Recommended Frontend Integration
1. `POST /v1/sessions`
2. `GET /v1/chat/stream/{session_id}` (open once and keep alive)
3. `POST /v1/chat/send` for user message
4. On `clarification_question`, show questions inline and collect user reply
5. Send reply via `POST /v1/chat/send`
6. On `done`, mark generation complete
7. Optional `POST /v1/chat/interrupt` for stop button

---

## cURL Examples
### Create session
```bash
curl -X POST http://127.0.0.1:8000/v1/sessions \
  -H "Content-Type: application/json" \
  -d '{"user_id":"local-user","title":"Interview"}'
```

### Open SSE stream
```bash
curl -N http://127.0.0.1:8000/v1/chat/stream/s_a1b2c3d4e5
```

### Send message
```bash
curl -X POST http://127.0.0.1:8000/v1/chat/send \
  -H "Content-Type: application/json" \
  -d '{"session_id":"s_a1b2c3d4e5","message":"My idea is ...","stream":true}'
```

### Interrupt
```bash
curl -X POST http://127.0.0.1:8000/v1/chat/interrupt \
  -H "Content-Type: application/json" \
  -d '{"session_id":"s_a1b2c3d4e5"}'
```

---

## Current v1 Constraints
- In-memory sessions/runtime only (lost on server restart).
- Single-instance assumption for session continuity.
- No authentication/authorization.
- No persistent storage.

For production hardening later:
- add auth
- persist sessions/messages
- shared pub/sub for SSE across replicas
- rate limiting
