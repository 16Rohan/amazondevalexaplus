# Web Backend Architecture

Per Web App PRD §2 and §2.1:
- The web backend owns conversation/session state, orchestrates model and MCP interactions, validates consequential-action confirmations, normalizes events/results, and exposes the HTTP API (`/api/health`, `/api/conversations`, `/api/chat`).
- In this repository, the runtime implementation of the backend is served by the Python HTTP service located at [`llm/main.py`](../../llm/main.py).
- During development, Vite forwards all `/api/*` requests to this backend service (port 8000 by default) as configured in [`vite.config.ts`](../vite.config.ts).
- API request and response schemas are formally specified in [`src/assets/schemas/web-api.schema.json`](../assets/schemas/web-api.schema.json).
