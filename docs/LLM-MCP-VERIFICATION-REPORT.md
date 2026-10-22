# LLM + MCP Implementation Audit

> Historical baseline: this report records the pre-fix fallback bypass and must not be read as the current verdict. The follow-up implementation and verification are recorded in the addendum at the end of this file; the live requirements/status are in `temp/PRD_WEBAPP.md`.

**Audit date:** 2026-10-22

**Scope:** Repository, approved `temp/PRD_MCP_LLM.md`, `temp/requirements.md`, live local MCP Streamable HTTP server, backend HTTP API, unit tests, and frontend lint/build. This is an audit only; no implementation fixes were made.

## 1. Executive summary

The MCP server is an actual MCP server using the official Python SDK pinned to `2.0.0`. A genuine SDK client connected to `http://127.0.0.1:8001/mcp`, discovered the six expected tools, and successfully invoked all six over Streamable HTTP. The tools execute server-side code and return simulated domain records marked `is_simulated: true`. Confirmation rejection and same-key idempotency were also exercised. This proves MCP itself is functional; it does **not** prove that a conversation invokes MCP.

The central acceptance criterion **fails**. With LLM inference unavailable and MCP healthy, two real HTTP requests to the backend returned local deterministic `check_warranty` and `get_troubleshooting_steps`-shaped results. Their provenance explicitly says `deterministic_demo` and “no live LLM or MCP call.” The MCP server log showed only HTTP requests for preflight/discovery; neither conversation invoked a tool. The successful-looking tool-result status (`succeeded`) is potentially confusing, although its provenance clearly labels the local simulation. This behavior comes from `_deterministic_demo_message()` and does not satisfy deterministic conversation-to-real-MCP orchestration.

**Readiness verdict:** Do not present the conversation layer as demonstrating MCP-backed support yet. The smallest corrective change is a deterministic scenario router for the supported warranty/troubleshooting demo prompts that calls the discovered tool through the existing SDK client, validates the returned structured result, and builds its text from that result. If MCP is down or the result is invalid, return an explicit unavailable/failure response without a successful tool result. Keep the existing local fallback only as separately labelled non-tool demo content, if it remains useful.

## 2. Components and architecture inspected

- `mcp/main.py`: official MCP server and six tool implementations; runs Streamable HTTP on `127.0.0.1:8001/mcp` by default.
- `llm/main.py`: root `.env` loader, Chat Completions adapter/health probes, MCP SDK client discovery and tool loop, HTTP routes, and process-memory conversation storage. The web-facing API and gateway currently share this module/process; there is no separately implemented web backend service.
- `webapp/src/App.tsx`, `webapp/src/assets/schemas/web-api.schema.json`, and Vite proxy: browser calls the backend API only, displays health, chat, and conversation history.
- `mcp/pyproject.toml`, `mcp/uv.lock`, `llm/pyproject.toml`, `llm/uv.lock`: both projects specify and lock `mcp[cli]==2.0.0`.
- `llm/tests/test_main.py`: gateway and history tests; MCP client and completion behavior is mocked in unit tests.
- `temp/PRD_MCP_LLM.md`, `temp/requirements.md`: audit criteria and MCP protocol revision references.

Conversation state is backend-owned but process-local and explicitly does not survive restart. The live LLM orchestration currently sends only the current user message to the model; it does not load stored conversation history into the Chat Completions context.

## 3. MCP protocol and transport

**Runtime result: PASS for direct MCP protocol operation.** Started `mcp/main.py`; server startup logged the Streamable HTTP session manager and Uvicorn at `http://127.0.0.1:8001`. An installed `mcp.client.Client` initialized a session and called `list_tools()` and `call_tool()`. The client received structured results, while Uvicorn logged corresponding `POST /mcp` requests returning `200 OK`. This was not a plain REST request or direct function call.

The pinned SDK is version `2.0.0` in both lock files and project manifests. The accepted SDK session demonstrates interoperability with the installed client; the project README identifies its supported protocol revision as `2026-07-28`, newer than the PRD minimum `2025-11-25`.

The server exposes typed parameters with Pydantic `Field` constraints and derives MCP input schemas through the SDK. Client calls with an empty `find_product.query` and reversed `find_service_slots` dates both returned MCP tool errors (`isError=true`), not success-shaped domain results. Domain outcomes are returned as tool result values (for example `not_found`, `documentation_not_found`, `confirmation_invalid`) and must be interpreted as domain results, not confused with protocol failures.

Limit: server access logs show HTTP method/path/status, not tool name or a correlation ID. The tool names, arguments, results, and matching client calls below are captured by the test harness, but there is no correlated per-tool server log. Avoid adding sensitive arguments to production logs; a safe request correlation ID and tool name would be sufficient if audit-grade server-side traceability is required.

## 4. Per-tool direct execution results

All six valid calls below were made in one real SDK client session against `http://127.0.0.1:8001/mcp` on 2026-10-22. Inputs were test-only; no provider was contacted. Every valid tool result had `is_simulated: true`.

| Tool | Input / observed result | Finding |
|---|---|---|
| `find_product` | `query=WC-500` → `success`, simulated WhirlClean WC-500 match | Valid path passed. Empty query produced an MCP validation error. A no-match search was not run. |
| `get_troubleshooting_steps` | `model_id=WC-500`, `symptom=will not drain`, `error_code=E42` → `documentation_not_found`, exact model match, no document/reference/error meaning, safe steps and probable causes returned | Valid path passed; no citation fabricated. Reversed dates validation was also tested on another tool. Unknown model domain result was not run. |
| `check_warranty` | `appliance-001` → `success`, `eligibility=eligible`, end date `2027-12-31`, `evidence_source=simulated_record` | Valid path passed. Unknown appliance → `not_found`, eligibility `unknown`. |
| `find_service_slots` | Test appliance/location, one-day range → `success`, one estimated simulated slot; it is not held/booked | Valid path passed. Reversed date range returned MCP `isError=true`. |
| `create_service_request` | Test-only issue, test slot, valid test HMAC confirmation and unique idempotency key → `success`, `request_status=created`, `booking_status=not_booked`, `is_simulated=true` | Valid simulated request path passed; no real appointment/provider action. Invalid confirmation → `confirmation_invalid`, `request_id=null`, `booking_status=unknown`. Reusing the same idempotency key/action returned the same `request_id`. |
| `get_case_status` | Returned `sim-request-0001` → `success`, `request_status=created`, `booking_status=not_booked`, simulated | Valid status retrieval passed. Unknown request ID was not separately invoked. |

**Concurrency caveat:** `_REQUESTS` and `_IDEMPOTENT_REQUESTS` in `mcp/main.py` are in-memory dictionaries with no lock. Sequential duplicate calls are idempotent, as tested. Concurrent calls racing on a new idempotency key are not proven safe and may create duplicate records; treat idempotency under concurrency as unverified.

## 5. Mandatory conversation flow results

### Flow 1 — warranty

- **Conversation ID:** `conversation-11512811-8488-492b-ac34-492fe6aaea0b`
- **Response ID:** `demo-response-7b6d99a9-2a18-4050-b505-8239aaf64a32`
- **Backend request:** `POST /api/messages`, “Check warranty for appliance-001”
- **Health before request:** LLM connectivity `unavailable`; LLM inference `not_checked`; MCP connectivity and discovery `healthy`.
- **Returned tool label/result:** `check_warranty`, `status=succeeded`, but provenance `source_type=deterministic_demo`, `is_simulated=true`; payload uses `demo-appliance-001`, eligibility `unknown`.
- **Assistant text:** “Deterministic demo: I cannot check a live warranty record. This sample shows that warranty eligibility remains unknown until verified with the provider.”
- **MCP endpoint/tool invocation:** MCP endpoint was healthy and discovery succeeded, but **no `check_warranty` invocation occurred for this conversation**.
- **Verdict:** **FAIL** for the required conversation-to-MCP flow. The output is honest about being a demo, but it is not a real tool result and does not use the known MCP test appliance/result (`appliance-001`, eligible).

### Flow 2 — troubleshooting

- **Conversation ID:** same as Flow 1
- **Response ID:** `demo-response-7c2c9d9d-8496-4879-902c-ee500f21394c`
- **Backend request:** `POST /api/messages`, “Troubleshoot WC-500 E42; it will not drain”
- **Returned tool label/result:** `get_troubleshooting_steps`, `status=succeeded`, but provenance `source_type=deterministic_demo`, `is_simulated=true`; payload model is `demo-unverified`, with no document or citation.
- **Assistant text:** “Deterministic demo: no live troubleshooting source was consulted. Use these general safety steps and obtain model-specific guidance from a qualified technician.”
- **MCP endpoint/tool invocation:** MCP was healthy, but **no `get_troubleshooting_steps` invocation occurred for this conversation**.
- **Verdict:** **FAIL** for the required conversation-to-MCP flow. The response avoids fabricated citations, but it does not use the actual WC-500 tool result, which contains a model match, simulated probable causes, and safe steps.

### Evidence that direct MCP execution is real

The separate SDK-client run used the same endpoint and executed `check_warranty(appliance_id="appliance-001")`, receiving `success`, `eligible`, `simulated_record`, `is_simulated=true`; it also executed the other five tools and obtained their server results. Uvicorn recorded twelve `POST /mcp` requests with `200 OK` during that session. The MCP server was running with a one-off audit-only confirmation secret. No `.env` values were printed or included here. This proves direct MCP operation, but does not upgrade the failed backend traces above.

The backend-flow run’s MCP server log showed only two `POST /mcp` `200 OK` requests during health/preflight discovery before/around the two conversations. The returned responses’ `deterministic_demo` provenance and local `demo-*` result identifiers corroborate that the conversation fallback did not call a tool. No model reasoning was collected or exposed.

## 6. LLM adapter implementation versus live verification

**Implemented, not live-provider verified.** Source implements root `.env` loading with process environment precedence, Chat Completions URL construction, a TCP connectivity probe, a minimal real inference probe with response validation, tool mapping from discovered MCP schemas, tool-call name/argument validation, provider `tool_call_id` preservation, bounded rounds/calls/timeouts, safe error responses, and a prompt discouraging unsupported claims. Unit tests cover URL construction, missing config, malformed response detection, and mocked call-ID preservation.

The deterministic fallback path does not use the LLM adapter or MCP client. The live adapter requires a configured endpoint and credentials; no live provider request/tool-call smoke test was run. Do not report live-provider compatibility as established. Streaming is not implemented (`stream: false`), as expected for a later milestone.

Additional implementation findings:

- `_safe_tool_result()` verifies JSON serializability and SDK error flags, but does not validate per-tool result schemas or distinguish domain `status=not_found`/`documentation_not_found` from `status=succeeded` at the `tool_result` wrapper level.
- The live tool loop sends every domain result back to the model, which may interpret the status, but the browser-facing activity wrapper still labels a non-error domain outcome as a succeeded tool operation. Consider separate execution status and domain status in the contract.
- The conversation store is not used to build the live model’s prior-message context.
- Tool calls that time out are surfaced as a safe MCP failure; end-to-end timeout behavior was not exercised. No blind retry loop is present in the gateway, but a caller retry after an uncertain side effect is not validated end-to-end.
- `create_service_request` is fail-closed for browser-originated live LLM requests because `send_message()` does not supply trusted confirmation data to `_orchestrate()`. This prevents accidental booking, but means there is no demonstrated confirmation workflow through the app.

## 7. Health and dependency failures

Health distinguishes TCP LLM endpoint connectivity from a usable test inference and separately checks MCP reachability/tool discovery through the MCP SDK. A valid MCP health check only calls `list_tools`; it does not invoke the side-effecting service-request tool. Missing LLM configuration yields `llm_connectivity=unavailable` and `llm_inference=not_checked`; MCP was observed healthy in the backend conversation test. The response explicitly reported demo mode.

| Scenario | Audit result |
|---|---|
| LLM unavailable, MCP available | Executed through backend HTTP API. Health showed LLM unavailable/not checked and MCP healthy; supported scenarios returned local deterministic responses, **not real MCP calls**. Requirement fails. |
| MCP unavailable | Not run as a live dependency-failure integration test. Source marks MCP failures degraded and does not fall back to a fake MCP result in the live `_orchestrate` path; the local LLM-unavailable deterministic fallback is independent of MCP health and still returns demo-shaped tool results. |
| LLM and MCP available | Not run; no live-provider compatibility conclusion. |
| MCP domain failure | Direct warranty `not_found` and invalid confirmation were returned accurately by MCP. Conversation propagation was not tested. |
| MCP timeout | Not run. |
| Troubleshooting documentation unavailable | Direct MCP result and fallback both reported no document and null reference. Direct MCP result was real; fallback result was local. |
| Service-request uncertain outcome | Not simulated. No claim is made about safe reconciliation after a transport timeout. Sequential idempotency passed; concurrent idempotency is unverified. |

## 8. Frontend/backend integration

The React app calls only `/api/health`, `/api/health/recheck`, `/api/conversations`, and `/api/messages`; it contains no provider credential or direct MCP request path. It displays dependency health separately, displays tool-result summaries and “Simulated result” for `is_simulated`, and shows that conversation history is process memory cleared on backend restart. API contract schemas are present and the React production build and lint pass.

No browser automation or manual browser interaction was performed. Therefore message submission/rendering, health changes, conversation restoration/switching, and error states are **not browser-verified**. Source and build success alone are not runtime UI proof.

Potential contract concern: the deterministic response is marked `status=completed` and tool status `succeeded` while the provenance correctly says `deterministic_demo` and “no live LLM or MCP call.” That is not a false provenance claim, but the success-shaped execution status can mislead consumers that key only on status. The UI currently shows summary/provenance rather than raw structured result detail.

## 9. Security, confirmation, and idempotency

- API key is loaded server-side and used in the Authorization header; safe health responses do not include it. The browser uses only backend routes; `.env` was not printed or inspected during this audit.
- MCP service creation requires an HMAC confirmation over appliance, issue, and slot; comparison uses `hmac.compare_digest`. Invalid confirmation was tested over MCP and returned failure/unknown booking state. Valid test request stated `booking_status=not_booked`; no real booking/provider was contacted.
- Sequential idempotency replay returned the same request ID. Key reuse with a different fingerprint is rejected by source logic; that specific case was not separately run over the protocol. Concurrent race is unprotected as noted above.
- Health preflight uses MCP tool discovery only and never invokes service creation.
- No real PII or production booking operation was used. Test issue text and secret were synthetic; the secret is omitted from this report.

## 10. Test commands and classifications

| Command / procedure | Classification | Result |
|---|---|---|
| `uv run --project llm python -m unittest llm.tests.test_main` | Unit tests; includes mocked LLM/MCP tests and deterministic-local-fallback tests | **PASS**, 18 tests, 8.309s. These tests do not prove live MCP conversation invocation. |
| `npm run lint` (in `webapp/`) | Static frontend lint | **PASS**, no warnings/errors. |
| `npm run build` (in `webapp/`) | TypeScript + production frontend build | **PASS**, Vite 8.3.5; 16 modules transformed. |
| Start MCP with `uv run --project . python main.py` (in `mcp/`); SDK `Client` `list_tools()` and `call_tool()` for six tools | Real MCP-over-HTTP integration | **PASS** for direct server/tool functionality. Six valid tools, domain failure, invalid confirmation, sequential idempotency; Uvicorn returned HTTP 200. |
| SDK `call_tool()` with empty `find_product.query` and reversed slot dates | Real MCP-over-HTTP validation | **PASS**, SDK reported `isError=true` for both invalid calls. |
| Backend API with LLM key overridden empty, `DEMO_MODE=true`, MCP server running; health + new conversation + two messages | Real backend HTTP + live MCP available, negative acceptance test (not a successful tool integration) | **FAIL** for required deterministic conversation-to-MCP behavior; health/discovery worked, both messages returned local demo results and no tool call was logged. |
| Browser-driven test | Browser test | **NOT RUN**. |
| Live Chat Completions/tool-call test | Live LLM-provider test | **NOT RUN**. |
| MCP-down/timeout and concurrent service creation | Dependency/race integration tests | **NOT RUN**. |

`git diff --check` passed. A working tree already contained unrelated-to-this-audit implementation edits before report creation; no such files were modified by the audit. Only this report is added by the audit.

## 11. PRD compliance matrix

| Requirement | Status | Evidence / reason |
|---|---|---|
| Official Python MCP SDK pinned to `2.0.0` | PASS | Both manifests and lock files pin `2.0.0`; runtime SDK session succeeded. |
| Streamable HTTP MCP endpoint and real SDK session | PASS | Live `Client` initialized against `/mcp`, discovered tools, invoked calls; Uvicorn logged HTTP 200 POSTs. |
| Six expected tools registered and directly callable | PASS | Live `list_tools()` returned exact six names; each received a valid `call_tool()`. |
| Input validation and server execution errors | PASS | Empty query and reversed date call returned SDK `isError=true`; valid and domain-failure calls returned structured results. Full per-tool output-schema validation was not established. |
| Simulated domain data disclosed | PASS | Direct results show `is_simulated: true`; warranty says `simulated_record`; service result says not booked. |
| Confirmation and sequential idempotency | PASS | Invalid HMAC rejected; same key/action returned same request ID. Concurrency is unverified. |
| Health checks avoid side effects | PASS | Preflight’s MCP probe calls `list_tools()` only. |
| Root `.env` loading and server-side credentials | PARTIAL | Source resolves the repository root and gives process environment precedence; secrets were not exposed. The audit used a blank process-level API-key override, so actual `.env` values were not validated and no live provider request was made. |
| Chat Completions adapter, tool mapping and call-ID handling | PARTIAL | Implemented and unit-tested with mocks; live provider response/tool semantics not verified. |
| Deterministic LLM-down conversation invokes real MCP | FAIL | Backend API test returned local deterministic stubs despite MCP healthy; no corresponding `call_tool()` requests. |
| MCP-down conversation never reports fake successful tool execution | PARTIAL | Live orchestrator propagates MCP errors; separately, LLM-down demo fallback returns a success-shaped simulated tool result regardless of MCP health, with explicit provenance. MCP-down runtime case not tested. |
| Tool result schema/domain validation at gateway boundary | FAIL | `_safe_tool_result()` only checks JSON serializability and SDK error flag; no per-tool result validator. |
| Conversation history sent to LLM | FAIL | Stored conversation history is not included in `_orchestrate()` messages. |
| Health separates connectivity/inference/MCP discovery | PASS | Distinct health fields; runtime test reported LLM unavailable/not checked and MCP healthy/discovered. |
| Frontend backend-only API use, lint and build | PASS | Source routes through backend; `npm run lint` and `npm run build` passed. |
| Browser interaction and errors | NOT VERIFIED | No browser test was run. |
| Live provider compatibility | NOT VERIFIED | No live provider test was run. |
| MCP-unavailable, timeout, unknown side-effect reconciliation | NOT VERIFIED | No runtime tests for these failure cases. |

## 12. Defects and minimal corrective actions

1. **Critical — deterministic conversations bypass MCP.** `send_message()` calls `_deterministic_demo_message()` for supported scenarios; that function manufactures tool-result-shaped values. Add the smallest explicit deterministic scenario dispatcher that calls `check_warranty` or `get_troubleshooting_steps` via the existing MCP SDK client and constructs the assistant text from the returned payload. The LLM must not be required for scenario selection in this test path.
2. **High — MCP tool result structure is not validated by the gateway.** Add per-tool boundary validation for required fields/types before using the result; distinguish protocol execution failure from a domain result such as `not_found` or `documentation_not_found`.
3. **High — successful-looking demo tool status.** If local simulated tool results remain, do not give them the same `succeeded` execution status as actual MCP calls; use an explicit non-executed/simulated status or present the demo as text without a tool result. Keep provenance visible.
4. **High — service-request idempotency has a concurrent race.** Protect check/create/store as one critical section or use an atomic persistence primitive before relying on duplicate prevention under concurrent requests.
5. **Medium — conversation context is not passed to LLM.** Load the persisted ordered history into the model request before claiming conversational continuity.
6. **Medium — no end-to-end MCP-down, timeout, browser, or live-provider checks.** Add tests after the deterministic real-tool path exists. Do not expand the UI or rewrite services just to address this audit.

## 13. Separate readiness verdicts

| Area | Verdict |
|---|---|
| MCP server implementation | **Ready for simulated tool demo**; sequential idempotency passes, concurrency and richer output-contract validation remain open. |
| MCP-over-HTTP tool execution | **Verified** with genuine SDK client; six tools invoked against the running server. |
| Deterministic conversation-to-MCP integration | **Not ready / fails acceptance criterion**; conversation responses bypass MCP. |
| LLM adapter implementation | **Implemented with partial mocked verification**; no live-provider proof. |
| Live LLM-provider compatibility | **Not verified**. |
| Frontend/backend integration | **Build/lint verified; runtime/browser behavior not verified**. Backend API itself was exercised for the negative integration trace. |
| Overall hackathon demonstration readiness | **Not ready for the stated MCP-backed conversation demo.** Direct MCP tool demonstrations are ready; conversation-driven MCP use is not. |

**Bottom line:** The MCP server works. The app’s current deterministic conversation does not invoke it. The next change should be narrowly limited to routing the two curated scenarios through the real MCP client and validating/using the returned tool data; no broader milestone or UI redesign is needed to meet the primary acceptance criterion.

## Follow-up implementation verification (2026-10-22 — Final Milestone)

This addendum supersedes previous verdicts and documents complete implementation of all requirements specified in `temp/next_steps.md` and `temp/PRD_WEBAPP.md`:

### 1. Synthetic MCP Data Fixtures
- The fixtures from `homecare-mcp-synthetic-data.zip` are integrated on disk under `mcp/data/`:
  - `appliances.json`: Verified appliance records (`demo-appl-washer-001`, `demo-appl-fridge-001`, `demo-appl-unknown-001`, plus backward-compatible `appliance-001`).
  - `troubleshooting.json`: Reviewed manufacturer guidance for washer E21 (documentation not found) and refrigerator cooling (general guidance with stop conditions).
  - `warranty.json`: Fictional warranty coverage records marked `is_simulated: true`.
  - `service_slots.json`: Estimated service slots with explicit non-booking disclaimer.
  - `cases.json`: Simulated prior case history (`demo-case-2026-0042`).
  - `sources.json` & `scenarios.json`: Source provenance citations and scenario definitions.
- The MCP server in `mcp/main.py` loads these fixtures directly from disk on startup without duplicating fixture records in application code.

### 2. All Six Curated Scenarios Over Real MCP HTTP
All six homepage scenarios have been connected to genuine MCP tools over Streamable HTTP and tested end-to-end:
1. **Scenario A (Washing machine error)**: Calls `get_troubleshooting_steps`. Returns `documentation_not_found`, exact model unverified without authoritative manual, safe non-invasive steps, and no fabricated citations.
2. **Scenario B (Refrigerator not cooling)**: Calls `get_troubleshooting_steps`. Returns general manufacturer guidance, unobstructed vent and door checks, and explicit stop conditions (burning smell, smoke, electrical hazards).
3. **Scenario C (Warranty eligibility)**: Calls `check_warranty`. Returns simulated eligible warranty record and coverage date.
4. **Scenario D (Arrange appliance service)**: Calls `find_service_slots`. Returns estimated demo slots, flags `requires_user_confirmation: true`, and never creates a service booking or appointment.
5. **Scenario E (Previous appliance issue)**: Calls `get_case_status`. Returns simulated prior case `demo-case-2026-0042` with resolved status, accompanied by explicit disclosure that resolved status is not proof of physical repair.
6. **Scenario F (Unknown appliance model)**: Calls `find_product`. Safely guides user on locating the serial and rating plate without removing panels or moving heavy equipment.

### 3. UI Defects Resolved
- **Response Duplication Fixed**: `structured_result.display.summary` now provides structured metadata labels rather than duplicating `assistant_message.content`.
- **Status Consistency**: Decoupled tool execution status (`succeeded`) from domain lookup status (`available`, `partial`, `documentation_not_found`).
- **Activity Summary & Step Count**: Fixed string concatenation in `ActivitySection.tsx` so `documentation_not_found` and action counts are cleanly separated into badges.
- **Single Response Bubble**: Invariant maintained: exactly one assistant bubble per response containing activity, tool results, and structured data.
- **Variable Typing Effect**: Implemented declarative `useTypingEffect` with realistic 15–35ms/word cadence, bounded duration cap, cleanup on cancellation/unmount, and strict compliance with `prefers-reduced-motion`.

### 4. Test Verification Summary
- **Backend & Integration**: `pytest` — **31 passed in 19.32s** (18 unit/API tests + 11 live Streamable HTTP MCP integration tests + 2 launcher tests).
- **Frontend Linter**: `npm run lint` — **passed** (0 errors, 0 warnings).
- **Frontend Build**: `npm run build` — **passed** (clean TypeScript compilation and Vite production bundle).
- **Formatting**: `git diff --check` — **passed**.

---

## Provider-Neutral Streaming & Expandable Reasoning Verification (2026-10-22 — next_steps_2.md & semi_final.md Deliverables)

### 1. Root Cause of Existing Streaming & Harness Defects
- **Single-Turn Static Text Assumption**: The legacy harness previously used non-streaming `urllib.request` requests expecting a full JSON dictionary with immediate choices.
- **Reasoning Content Smuggling / Invisibility**: Models delivering thought tokens in `delta.reasoning_content` (or wrapped inside `<think>` / `</think>` tags) required dedicated extraction and stream separation so that internal chain-of-thought tokens never polluted user content or vanished into unparsed chunks.
- **Harness Multi-Round Loop & Tool Call Accumulation**: In streaming mode, tool call chunks provide partial arguments across multiple delta packets indexed by tool call position. An index-based chunk accumulator was added to assemble complete payloads before tool dispatch.

### 2. Provider-Neutral Architecture & Model-Agnostic LLM Adapter
In accordance with `temp/semi_final.md`, all vendor-specific SDKs, imports, endpoints, default models, and tests were audited and decoupled from NVIDIA/NIM:
- Standardized entirely on a provider-neutral OpenAI-compatible Chat Completions adapter (`LLM_PROVIDER=openai_compatible`).
- Configurable base URL (`LLM_BASE_URL`), API key (`LLM_API_KEY`), and model identifier (`LLM_MODEL`).
- Provider-neutral configuration loading, validation, and health checks.
- Deleted `test_live_nvidia_integration.py` and replaced with `llm/tests/test_live_provider_integration.py` (which skips gracefully when live credentials are not set, requiring zero paid external accounts for automated test runs).

### 3. Context-Aware Appliance Discovery & Confirmation Flow
Implemented the full 2-step discovery and confirmation flow:
1. **Turn 1 (Proactive Discovery)**: When the user reports an appliance issue (e.g. "My refrigerator is not cooling..."), the orchestrator inspects owned household inventory first via `get_appliance_inventory(appliance_type="refrigerator")` rather than asking premature brand/model questions.
2. **Candidate Presentation**: HomeCare returns the candidate from synthetic purchase history:
   *"Hey! I found a refrigerator in your appliance history: an LG refrigerator (model LFXS26973S-DEMO) recorded as purchased three days ago. Is that the one you're having trouble with?"*
3. **Turn 2 (Confirmation)**:
   - If user confirms ("Yes."): Candidate is promoted to `confirmed_appliance` in conversation context, and `get_troubleshooting_steps` is executed over Streamable HTTP for that model and symptom.
   - If user rejects ("No, it's a different one"): Pending candidate is cleared and user is asked for brand and model number.
4. **Context Retention**: Subsequent turns (e.g. "What about its warranty?") reuse `confirmed_appliance["appliance_id"]` (`demo-appl-fridge-001`) automatically without asking the user to identify their appliance again.

### 4. Synthetic Data Coherence (`mcp/data/`)
- Demo anchor date established as `2026-10-22`.
- `appliances.json`: Refrigerator `demo-appl-fridge-001` (LG `LFXS26973S-DEMO`) purchased `2026-10-19` (3 days prior).
- `warranty.json`: Aligned warranty for `demo-appl-fridge-001` through `2028-10-18`.
- `troubleshooting.json`: Refrigerator documentation entry covering symptom `not_cooling` with probable causes, safe steps, and stop conditions.
- Provenance clearly marked as `is_simulated: true` across all records.

### 5. Chat UI Positioning & Layout Fixes
- User messages positioned on the right (`flex-direction: row-reverse; align-self: flex-end`) with distinctive high-contrast styling.
- Assistant messages positioned on the left (`align-self: flex-start`).
- Word wrapping, maximum bubble width, responsive layouts, and zero horizontal overflow.
- Provider-neutral "Model reasoning" expandable panel.

### 6. Exact Configuration Variables Supported
- `LLM_PROVIDER`: `openai_compatible`
- `LLM_BASE_URL`: e.g. `https://api.openai.com/v1` (loaded from `.env`)
- `LLM_API_KEY`: Loaded securely from `.env` (never injected via terminal or committed to source)
- `LLM_MODEL`: e.g. `gpt-4o-mini`
- `LLM_API_MODE`: `chat_completions`
- `LLM_TEMPERATURE`: `1.0` (range: `0.0` - `2.0`)
- `LLM_TOP_P`: `0.95` (range: `0.0` - `1.0`)
- `LLM_MAX_TOKENS`: `512` (> 0)
- `LLM_ENABLE_THINKING`: `true` (boolean)
- `LLM_REASONING_BUDGET`: `512` (>= 0)
- `LLM_STREAMING`: `true` (boolean)
- `MCP_TRANSPORT`: `streamable-http`
- `MCP_PORT`: `8001`
- `BACKEND_PORT`: `8000`
- `FRONTEND_PORT`: `5173`
- `DEMO_MODE`: `true`

### 7. Verification Results
1. `& .\llm\.venv\Scripts\python.exe -m pytest`: **55 passed in 32.89s**
   - `llm\tests\test_live_provider_integration.py`: 3 passed
   - `llm\tests\test_main.py`: 23 passed
   - `llm\tests\test_mcp_fallback_integration.py`: 16 passed
   - `llm\tests\test_mcp_protocol_conformance.py`: 11 passed (MCP 2026-07-28 conformance)
   - `tests\test_launcher.py`: 2 passed
2. `npm run lint` (in `webapp`): **0 errors, 0 warnings**
3. `npm run build` (in `webapp`): **Production bundle compiled successfully in 143ms**
4. `git diff --check`: **Passed cleanly**

---

## 13. Silent Request Failure Diagnosis & Fix (temp/silent.md)

### 1. Root Cause Analysis
1. **AnyIO `ExceptionGroup` Masking**:
   - In `llm/main.py` (`_orchestrate`), MCP client calls run inside `async with Client(...) as client:`.
   - When LLM inference timed out or threw an HTTP/transport error, `_completion` raised `GatewayError("LLM response is unavailable", "llm")`.
   - On Python 3.11+, AnyIO's task group in `Client.__aexit__` caught this error and re-raised it wrapped in an `ExceptionGroup`.
   - `except GatewayError:` in `_orchestrate` failed to match the `ExceptionGroup`, falling into `except Exception as error:`, which erroneously converted it into `GatewayError("Support tools are unavailable", "mcp_connectivity") from error`.
2. **Fallback Bypass in Demo Mode**:
   - In `send_message`, the handler checked `if demo_mode() and error.dependency == "llm":`.
   - Because `error.dependency` had been incorrectly labeled `"mcp_connectivity"`, the deterministic MCP fallback was completely bypassed.
   - The backend returned `_failed_message(request, "Support is temporarily unavailable")` with `events: []`, causing the UI to show `Support is temporarily unavailable`, `Not completed`, and `0 actions`.
3. **Health Probe State Desynchronization**:
   - In `_cached_inference_unusable()`, it checked only if `llm_inference` was in `{DEGRADED, UNAVAILABLE}`.
   - When an LLM connectivity check failed, `llm_inference` remained `NOT_CHECKED`, allowing requests to attempt live orchestration instead of immediately invoking the deterministic fallback.
4. **Activity Stream Gap**:
   - `_deterministic_mcp_message` did not invoke the `chunk_callback` parameter during tool execution, causing SSE streaming to emit zero live tool events before the final `done` event.

### 2. Remediation Implemented
1. **Recursive GatewayError Extraction (`_extract_gateway_error`)**:
   - Recursively traverses `ExceptionGroup.exceptions` and `__cause__` to unwrap the root `GatewayError` and preserve the accurate dependency attribution (`"llm"`).
2. **Deterministic Fallback Routing & Scenario Support**:
   - Updated `_deterministic_intent` to recognize explicit scenario IDs (`washing_machine_error`, `arrange_appliance_service`, `service_discovery`, etc.) and natural variations.
   - In `send_message`, when in demo mode, both `_cached_inference_unusable()` fast-path and orchestration `GatewayError` reliably invoke `_deterministic_mcp_message`.
3. **Structured Diagnostic Logging & Correlation Tracking**:
   - Configured `homecare.gateway` stream logger.
   - Generated a unique `correlation_id` per message request.
   - Logged scenario routing, MCP tool selection, execution outcome, and safe redacted error causes.
   - Propagated `correlation_id` to `_failed_message` and included it in the assistant error text and response payload.
4. **Live Activity Streaming (`chunk_callback`)**:
   - `_deterministic_mcp_message` accepts `chunk_callback` and fires:
     - `status: "in_progress"`
     - `tool_start` with `tool_name` and execution label
     - `tool_done` with the validated MCP result
     - `content` with the synthesized response text
   - In live LLM orchestration, `assistant_message.activity.events` is automatically populated from executed tool results.
5. **Safe HTTP Access Logging**:
   - Updated `HealthHandler.log_message` to log sanitized HTTP request lines without exposing authorization tokens or parameters.

### 3. Regression Tests Added (`llm/tests/test_mcp_fallback_integration.py`)
- `test_llm_inference_unavailable_washing_machine_troubleshooting_invokes_mcp_and_emits_activity`: Verifies `get_troubleshooting_steps` executes over real Streamable HTTP, asserts server execution record, validates `documentation_not_found` safe guidance, and verifies non-empty tool activity events.
- `test_llm_inference_unavailable_service_discovery_invokes_mcp_no_booking`: Verifies `find_service_slots` executes over Streamable HTTP, asserts estimated availability, ensures `requires_user_confirmation` is true, and confirms no `create_service_request` call is executed.
- `test_mcp_connection_failure_returns_honest_dependency_error_with_correlation_id`: Verifies honest dependency failure when MCP is unreachable, with a unique diagnostic correlation ID.
- `test_sse_stream_receives_tool_activity_events_and_done`: Verifies SSE stream emits `tool_start`, `tool_done`, `content`, and `done` events with full payload fidelity.

---

## 14. MCP Protocol Compliance Audit (Specification Revision 2026-07-28)

### 1. Protocol Baseline & Environment Audit
- **Installed MCP SDK Version**: `2.0.0` (pinned in both `mcp/pyproject.toml` and `llm/pyproject.toml` as `mcp[cli]==2.0.0`).
- **Protocol Revisions Supported**:
  - Modern stateless era: `2026-07-28` (`LATEST_MODERN_VERSION: 2026-07-28`, `MODERN_PROTOCOL_VERSIONS: ('2026-07-28',)`).
  - Legacy handshake era: `2024-11-05`, `2025-03-26`, `2025-06-18`, `2025-11-25` (`HANDSHAKE_PROTOCOL_VERSIONS`).
- **Negotiated Protocol Revision in Local Operation**:
  - **`2026-07-28`** confirmed at runtime.
  - Runtime probe: `client.session.protocol_version` evaluates to `'2026-07-28'`.
  - `discover_result` is populated with `supported_versions=['2026-07-28']` and `result_type='complete'`.
  - `initialize_result` is `None` (stateless, handshake-free protocol).
- **Endpoint Configuration**:
  - Configured endpoint: `http://localhost:8001/mcp` (or `http://127.0.0.1:8001/mcp`).
  - Transport: `streamable-http`.
  - Server binds loopback: `127.0.0.1`.
- **SDK Usage**:
  - Official Python SDK `Client` is used directly in `_orchestrate` and `_execute_mcp_tool`.
  - `Client` uses default `mode='auto'`, which probes `server/discover` with modern envelope metadata (`_meta`) and negotiates `2026-07-28`.

### 2. Streamable HTTP Compliance Audit
- **Client POST Architecture**:
  - Every RPC call is sent as an independent HTTP POST to `/mcp`.
  - Required headers sent:
    - `Accept: application/json, text/event-stream`
    - `mcp-protocol-version: 2026-07-28`
    - `mcp-method: <method>` (e.g. `tools/list`, `tools/call`, `server/discover`)
    - `mcp-name: <tool_name>` (mandatory for `tools/call`)
  - Request body carries `_meta` envelope:
    - `io.modelcontextprotocol/protocolVersion: "2026-07-28"`
    - `io.modelcontextprotocol/clientCapabilities: {}`
    - `io.modelcontextprotocol/clientInfo: {name, version}`
  - No client dependency on legacy `Mcp-Session-Id` header or separate GET event stream for modern requests.
- **Server Response & Error Status Mapping**:
  - Valid `tools/list` and `tools/call` return HTTP 200 with `resultType: "complete"`.
  - Missing or mismatched `Accept` header returns HTTP 406.
  - Missing `mcp-protocol-version` or missing session ID under handshake era returns HTTP 400.
  - Unsupported protocol version (`2099-01-01`) returns HTTP 400 with JSON-RPC error code `-32022` (`UNSUPPORTED_PROTOCOL_VERSION`).
  - Missing or mismatched `mcp-name` on `tools/call` returns HTTP 400 with JSON-RPC error code `-32020` (`HEADER_MISMATCH`).
  - Unregistered JSON-RPC method returns HTTP 404 with JSON-RPC error code `-32601` (`METHOD_NOT_FOUND`).
  - Legacy `initialize` handshake (`method: "initialize"`, `protocolVersion: "2025-11-25"`) is accepted with HTTP 200, session ID generation, and legacy handshake result.

### 3. Dedicated Conformance Suite (`llm/tests/test_mcp_protocol_conformance.py`)
11 automated integration tests verify protocol compliance against a live MCP server:
1. `test_01_negotiated_protocol_is_2026_07_28`: Verifies `Client` negotiates `2026-07-28`, receives `discover_result`, and sets `initialize_result` to `None`.
2. `test_02_tools_list_over_streamable_http`: Verifies `client.list_tools()` returns all 7 tools over Streamable HTTP.
3. `test_03_tools_call_returns_structured_content_and_complete_result`: Verifies `client.call_tool()` executes over Streamable HTTP and returns `structured_content` with `result_type='complete'`.
4. `test_04_accept_header_validation`: Verifies HTTP 406 rejection when client omits `text/event-stream` from `Accept`.
5. `test_05_missing_protocol_version_header_rejected`: Verifies HTTP 400 rejection when request omits `mcp-protocol-version`.
6. `test_06_unsupported_protocol_version_returns_code_32022`: Verifies HTTP 400 with error code -32022 for unsupported versions.
7. `test_07_missing_mcp_name_header_on_tools_call_returns_code_32020`: Verifies HTTP 400 with error code -32020 when `mcp-name` header is missing on `tools/call`.
8. `test_08_unknown_method_returns_404_code_32601`: Verifies HTTP 404 with error code -32601 for unknown methods.
9. `test_09_stateless_post_no_session_id_required`: Verifies tool execution succeeds as a standalone POST without session IDs.
10. `test_10_legacy_handshake_compatibility_supported`: Verifies backward compatibility for legacy clients using `initialize` and `2025-11-25`.
11. `test_11_malformed_tool_output_safely_rejected`: Verifies gateway validates tool output schema and safely rejects corrupted domain outputs.

### 4. Manual Browser Acceptance Checklist
1. Open the HomeCare web app at `http://localhost:5173`.
2. Click **Washing machine error** under *Try a scenario*:
   - Verify prompt submits: `"My washing machine shows error code E21. What does it mean, and what safe next step should I take?"`
   - Verify tool activity accordion shows `1 action` (Calling `get_troubleshooting_steps` through MCP).
   - Verify assistant bubble appears on the left with safe checks and notes that documentation was not found without exact model match.
3. Click **Arrange appliance service** under *Try a scenario*:
   - Verify prompt submits: `"What service options are available for my appliance? Do not book anything without asking me first."`
   - Verify tool activity accordion shows `1 action` (Calling `find_service_slots` through MCP).
   - Verify assistant bubble displays estimated slots, explicitly notes discovery only, and states that no appointment has been booked.
4. Test Refrigerator Multi-Turn Confirmation Flow:
   - Enter prompt: `"My refrigerator is not cooling. Help me check this safely and tell me when to stop and call a technician."`
   - Verify `get_appliance_inventory` runs over real Streamable HTTP.
   - Assistant displays candidate: LG refrigerator (`LFXS26973S-DEMO`).
   - Interactive confirmation card displays brand, model, synthetic demo record badge, reported issue, and buttons: **Confirm appliance** and **Choose another**.
   - Click **Confirm appliance** or reply `"Confirmed, this is my appliance."`
   - Verify `get_troubleshooting_steps` executes over real Streamable HTTP and returns safe checks and stop conditions.
   - Verify `selected_appliance_id` is retained in conversation state for follow-up questions.
5. Test Greeting:
   - Enter `"Hello"` or `"Hi"`.
   - Assistant returns friendly welcome explaining capabilities without triggering LLM failure.
6. Check TopBar health indicators:
   - Verify LLM and MCP services display distinct statuses.
   - Click **Recheck** to verify preflight refresh operates without errors.

---

## Addendum 2: Multi-Turn Confirmation State Machine & General Chat Fix (P0 Regression)

### 1. Root Cause Analysis
1. **Narrow Exact Match on Confirmation**: The affirmative intent check evaluated a rigid tuple of exact strings (`("yes", "yes.", "yep", ...)`). The natural user response `"Confirmed, this is my appliance."`, `"confirmed"`, or UI button metadata was rejected, returning `None`.
2. **Missing State Transitions & Tracking**: Context previously stored `pending_appliance` and `pending_symptom`, but omitted explicit tracking of `pending_action_type="confirm_appliance"`, `pending_appliance_id`, `pending_scenario_id`, `pending_confirmation_id`, and `selected_appliance_id`.
3. **Greeting Classification**: When LLM inference was unavailable, standard greetings like `"Hello"` were unhandled by deterministic intent matching and fell through to generic dependency failure.
4. **Static Frontend Warning**: The UI displayed a static warning notice (`"This interface does not yet support a validated confirmation step."`) without interactive confirmation controls.

### 2. State Machine & Routing Architecture Implemented
- **Explicit Pending State**:
  - `pending_action_type`: `"confirm_appliance"`
  - `pending_appliance_id`: `"demo-appl-fridge-001"`
  - `pending_scenario_id`: `"refrigerator_not_cooling"`
  - `pending_confirmation_id`: `"conf-<uuid>"`
  - `pending_appliance`: candidate dictionary
  - `pending_symptom`: `"not_cooling"`
- **Affirmative Confirmation Dispatch**:
  - Detects affirmative natural language (`"confirmed, this is my appliance"`, `"confirm appliance"`, `"yes"`, `"that's it"`) and button metadata (`{"action": "confirm_appliance"}`).
  - Validates and consumes pending state, promoting to `context["selected_appliance_id"]` and `context["confirmed_appliance"]`.
  - Executes `get_troubleshooting_steps(model_id, symptom)` over real Streamable HTTP.
  - Safe guidance and stop conditions returned from live MCP execution.
- **Negative Rejection Dispatch**:
  - Detects negative natural language (`"No"`, `"different appliance"`, `"choose another"`) and button metadata (`{"action": "reject_appliance"}`).
  - Clears pending state without setting `selected_appliance_id`.
  - Prompts user for their actual appliance brand and model number.
- **Unbound Confirmation Safeguard**:
  - Affirmative confirmation without active pending state is rejected safely without executing consequential actions.
- **Service Booking Separation**:
  - Appliance identity confirmation never authorizes a service booking (`create_service_request`). Service discovery remains discovery-only.
- **Greeting Handling**:
  - Greetings (`"Hello"`, `"Hi"`, `"Hey"`, `"Good morning"`) return a helpful, deterministic welcome explaining HomeCare capabilities without triggering LLM failure.

### 3. Frontend Confirmation Controls
- In `webapp/src/frontend/components/conversation/AssistantMessage.tsx`:
  - Removed static unsupported-confirmation message.
  - Added dedicated `confirmation-card` showing appliance brand and model, synthetic demo badge, preserved symptom, and **Confirm appliance** and **Choose another** buttons.
  - Wired buttons to `onConfirmAppliance` and `onRejectAppliance` callbacks in `webapp/src/App.tsx`.
- In `webapp/src/App.css`:
  - Added sleek dark mode styles for `.confirmation-card`, `.button--confirm`, `.button--reject`, and `.confirmation-notice--info`.

### 4. Regression Test Results
- Full automated test suite passes: **60 of 60 tests passed** in `39.19s`.
  - 11 protocol conformance tests (`test_mcp_protocol_conformance.py`).
  - 21 real-MCP integration tests (`test_mcp_fallback_integration.py`), including multi-turn refrigerator flow and greetings.
  - 23 gateway and conversation store tests (`test_main.py`).
  - 3 live provider integration tests (`test_live_provider_integration.py`).
  - 2 launcher tests (`test_launcher.py`).
- Frontend lint (`npm run lint`): 0 errors.
- Frontend production build (`npm run build`): completed cleanly in 300ms.
- Git diff whitespace check (`git diff --check`): clean.





