# Business Research Agent - Low Level Design (LLD)

## 1) Scope
- Build a chat-based research agent for business queries.
- Agent flow: user query -> clarification loop (if needed) -> research execution via Claude Agent SDK -> final response.
- Backend-first design (FastAPI), frontend can be any web chat client consuming streaming API.

## 2) Goals and Non-Goals
### Goals
- Deterministic clarification phase before expensive research.
- Streaming responses to frontend (token/event level).
- Session-based chat history and resumable conversations.
- Clear observability: request IDs, phase timings, errors.

### Non-Goals (v1)
- Multi-tenant org billing and quotas.
- Human review workflow.
- Complex workflow graph builders.

## 3) High-Level Architecture
- `Frontend Chat UI`
- `Backend API (FastAPI)`
- `Agent Orchestrator` (state machine)
- `Clarification Engine` (decide if more context is needed)
- `Research Executor` (Claude Agent SDK wrapper)
- `Persistence` (Postgres recommended, SQLite acceptable for local dev)
- `Optional Cache` (Redis for session/event cache)

## 4) Runtime Flow
1. User sends message to `/v1/chat/send`.
2. Backend stores message and current session state.
3. Orchestrator runs `needs_clarification()` against latest context.
4. If clarification needed:
1. Generate 1-3 focused clarification questions.
2. Return state `AWAITING_CLARIFICATION`.
5. If clarification not needed:
1. Build research prompt pack (system + user context + constraints).
2. Start Claude Agent SDK execution.
3. Stream incremental events/tokens to frontend.
4. Persist final answer and citations/sources.
5. Return state `COMPLETED`.

## 5) State Machine
- `NEW`: Session created.
- `INTAKE`: Received first business query.
- `AWAITING_CLARIFICATION`: Waiting for user answers.
- `READY_FOR_RESEARCH`: Sufficient context captured.
- `RESEARCH_IN_PROGRESS`: Claude SDK running.
- `COMPLETED`: Final answer delivered.
- `FAILED`: Error state (retry possible).

Transitions:
- `NEW -> INTAKE`
- `INTAKE -> AWAITING_CLARIFICATION` or `INTAKE -> READY_FOR_RESEARCH`
- `AWAITING_CLARIFICATION -> READY_FOR_RESEARCH`
- `READY_FOR_RESEARCH -> RESEARCH_IN_PROGRESS -> COMPLETED`
- `ANY -> FAILED`

## 6) API Design (Backend)
### `POST /v1/sessions`
Creates chat session.

Request:
```json
{
  "user_id": "u_123",
  "title": "Market sizing for EV charging startup"
}
```

Response:
```json
{
  "session_id": "s_abc",
  "state": "NEW",
  "created_at": "2026-03-02T22:00:00Z"
}
```

### `POST /v1/chat/send`
Sends user message, triggers clarification or research.

Request:
```json
{
  "session_id": "s_abc",
  "message": "I want to start a D2C protein snack brand in the US. Help with TAM and GTM.",
  "stream": true
}
```

Response (non-stream):
```json
{
  "session_id": "s_abc",
  "state": "AWAITING_CLARIFICATION",
  "assistant_message": "Before I research, I need a few details...",
  "clarification_questions": [
    "Target customer segment?",
    "Price range per unit?",
    "Any initial region focus in the US?"
  ]
}
```

Streaming response (`text/event-stream`) events:
- `phase_update`
- `assistant_delta`
- `source_found`
- `final_answer`
- `error`

### `GET /v1/sessions/{session_id}`
Returns session metadata, state, and latest messages.

### `GET /health`
Liveness/readiness check for Railway.

## 7) Data Model
### `sessions`
- `id` (pk)
- `user_id`
- `title`
- `state`
- `created_at`
- `updated_at`

### `messages`
- `id` (pk)
- `session_id` (fk)
- `role` (`user|assistant|system`)
- `content` (text/json)
- `phase` (`intake|clarification|research|final`)
- `created_at`

### `research_runs`
- `id` (pk)
- `session_id` (fk)
- `status` (`started|completed|failed`)
- `sdk_run_id` (nullable)
- `input_summary`
- `output_summary`
- `error_message` (nullable)
- `started_at`
- `finished_at` (nullable)

### `sources`
- `id` (pk)
- `research_run_id` (fk)
- `url`
- `title`
- `snippet`

## 8) Claude Agent SDK Integration
Use Python package `claude-agent-sdk`.

### Why `ClaudeSDKClient` over plain `query()`
- Better for multi-turn interactive flow.
- Supports hooks and optional custom tools.
- Cleaner streaming control in long research tasks.

### SDK wrapper interface
`app/agents/claude_client.py`:
- `run_research(context: ResearchContext) -> AsyncIterator[AgentEvent]`
- `build_options(context) -> ClaudeAgentOptions`
- `parse_sdk_message(msg) -> AgentEvent`

Recommended options (v1 baseline):
- `max_turns`: bounded (example 6-10)
- `system_prompt`: strict business analyst behavior
- `cwd`: isolated working directory per session
- `allowed_tools`: minimal required list only
- `permission_mode`: conservative in production

## 9) Clarification Engine
Heuristic + LLM hybrid approach:

Heuristic checks:
- Missing geography.
- Missing timeframe.
- Missing objective type (market sizing / competitor scan / GTM plan / financial model).
- Missing constraints (budget, segment, product category).

Behavior:
- Ask max 3 questions at once.
- If user answers partially, ask only unresolved items.
- Auto-advance to `READY_FOR_RESEARCH` when minimum context is met.

## 10) Prompting Strategy
### System prompt (researcher role)
- Act as business research analyst.
- Return assumptions explicitly.
- Provide source-backed claims.
- Separate facts vs inference.
- Include confidence level.

### Research output schema (assistant final message)
- `executive_summary`
- `key_findings[]`
- `market_estimate` (if requested)
- `gtm_recommendations[]`
- `risks[]`
- `sources[]`
- `next_questions[]`

## 11) Suggested Code Structure
```text
.
├── main.py
├── app/
│   ├── api/
│   │   ├── routes_sessions.py
│   │   └── routes_chat.py
│   ├── core/
│   │   ├── config.py
│   │   ├── logging.py
│   │   └── models.py
│   ├── orchestrator/
│   │   ├── state_machine.py
│   │   ├── clarification.py
│   │   └── runner.py
│   ├── agents/
│   │   ├── claude_client.py
│   │   └── prompts.py
│   ├── db/
│   │   ├── session_repo.py
│   │   ├── message_repo.py
│   │   └── research_repo.py
│   └── schemas/
│       ├── requests.py
│       └── responses.py
└── tests/
    ├── test_clarification.py
    ├── test_state_machine.py
    └── test_chat_api.py
```

## 12) Error Handling
- Timeouts for SDK calls with graceful user message.
- Retry policy:
  - network/sdk transient errors: 2 retries with backoff.
  - validation/state errors: no retry, return actionable error.
- Persist failures in `research_runs`.
- Always return a stable user-facing error envelope.

## 13) Observability
- Structured logs with `session_id`, `request_id`, `phase`, `latency_ms`.
- Metrics:
  - clarification rate
  - completion rate
  - mean research latency
  - failure count by stage
- Optional tracing for SDK call spans.

## 14) Security and Safety
- Keep `ANTHROPIC_API_KEY` in env only.
- Strict input length limits and sanitation.
- Restrict allowed SDK tools in production.
- Add PII redaction before logging.
- Add basic rate limiting by user/session.

## 15) Deployment Notes (Railway)
- Existing `railway.json` start command is compatible with FastAPI + Uvicorn.
- Required env:
  - `ANTHROPIC_API_KEY`
  - `PORT`
  - `DATABASE_URL` (if Postgres used)
- Add startup check for missing env keys.

## 16) Implementation Plan (phased)
### Phase 1 - Skeleton
- FastAPI app, health route, session route, chat route.
- In-memory store and simple state machine.

### Phase 2 - Clarification + SDK
- Clarification engine rules.
- Claude SDK integration and streaming events.

### Phase 3 - Persistence + hardening
- Postgres repositories.
- Retry/timeouts, structured logging, metrics.
- API tests and state-machine tests.

## 17) Open Decisions
- Should research be synchronous-streaming in request lifecycle, or moved to background worker with event polling?
- Is web search expected through SDK tools only, or via dedicated backend connectors too?
- What is the minimum citation quality bar (count, recency, source type)?
