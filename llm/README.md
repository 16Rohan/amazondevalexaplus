## LLM gateway

`main.py` provides a provider-neutral Chat Completions health probe and MCP Streamable HTTP discovery through the official Python SDK. It loads the single root `.env`, validates server-only provider configuration, and never returns the API key or raw provider errors.

Run `llm-gateway` (or `python -m llm.main`) to serve `GET /api/health` and `POST /api/health/recheck` on `127.0.0.1:8000` (`BACKEND_PORT`). It probes MCP at `http://localhost:${MCP_PORT:-8001}/mcp` and checks discovery of all six required tools.

Both endpoints return a fresh safe snapshot. Live mode requires successful LLM inference and MCP discovery.

`POST /api/messages` accepts the shared `SendMessageRequest` JSON shape and returns its `SendMessageResponse` shape. It uses non-streaming Chat Completions and the MCP SDK catalogue, rejects unknown or malformed tool calls, preserves provider tool-call IDs in tool-result messages, and caps work at three rounds, six calls, and one configured timeout window. Browser requests always fail closed for `create_service_request`; an internal caller must separately provide backend-trusted confirmation and idempotency values before it can be invoked.

Conversation history is owned by the backend process: `POST /api/conversations` creates a backend-issued ID, `GET /api/conversations` lists summaries, and `GET /api/conversations/{conversation_id}` returns ordered response records. A response record contains the user message, its single assistant response, attached activity/tool results, and provenance. Repeating a `client_message_id` with the same content returns the saved response; reusing it with different content is rejected. The current store is process memory only: its API explicitly reports `survives_backend_restart: false`, so histories are lost when the backend restarts and are not shared across backend processes.

When `DEMO_MODE` is true (the default), an unavailable/degraded cached inference check or a failed live request may use only two explicit, read-only deterministic scenarios: warranty questions and troubleshooting questions. It is read from the root `.env`, with a process environment value taking precedence. Their assistant text, tool result, and structured result are labelled `deterministic_demo`/`is_simulated`; they never contact the LLM, MCP, provider, or booking system. All other requests fail safely.
