## Alexa Appliance MCP service

Run from this directory with `uv run main.py`. It serves the official Python MCP SDK's
Streamable HTTP endpoint at `http://127.0.0.1:8001/mcp` (set `MCP_PORT` to change it). It pins the official Python MCP SDK to `2.0.0`, whose v2 line supports MCP protocol revision `2026-07-28`.

All six tools return deterministic simulated data and label it with `is_simulated: true`.
`create_service_request` requires an HMAC confirmation token issued by the backend using
`MCP_CONFIRMATION_SECRET`; without it the request is rejected.
