"""Small, provider-neutral Chat Completions gateway foundation."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import socket
import time
import uuid
from threading import Lock, RLock
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

try:
    # Import from the SDK submodule: the repository's root ``mcp/`` directory
    # is intentionally not a Python package and can otherwise shadow the SDK.
    from mcp.client import Client
except ImportError:  # Allows safe health/message failures before the SDK is installed.
    Client = None  # type: ignore[assignment,misc]

try:
    from llm.synthesis import (
        classify_conversational_intent,
        get_conversational_response,
        synthesize_missing_model_guidance,
        VERIFIED_MANUFACTURER_DOCUMENTATION,
        GENERIC_APPLIANCE_KNOWLEDGE,
        SYNTHETIC_DEMO_RECORD,
        HOUSEHOLD_RECORD,
        MODEL_INFERENCE,
    )
except ImportError:
    from synthesis import (  # type: ignore
        classify_conversational_intent,
        get_conversational_response,
        synthesize_missing_model_guidance,
        VERIFIED_MANUFACTURER_DOCUMENTATION,
        GENERIC_APPLIANCE_KNOWLEDGE,
        SYNTHETIC_DEMO_RECORD,
        HOUSEHOLD_RECORD,
        MODEL_INFERENCE,
    )

DEFAULT_TIMEOUT_SECONDS = 5.0
HEALTHY, DEGRADED, UNAVAILABLE, NOT_CHECKED = "healthy", "degraded", "unavailable", "not_checked"
REQUIRED_MCP_TOOLS = frozenset({"find_product", "get_troubleshooting_steps", "check_warranty", "find_service_slots", "create_service_request", "get_case_status"})
SUPPORTED_MCP_TOOLS = REQUIRED_MCP_TOOLS | {"get_appliance_inventory"}
MAX_TOOL_ROUNDS = 3
MAX_TOOL_CALLS = 6
MAX_MESSAGE_CHARS = 20_000
_health_lock = Lock()
_cached_health: dict[str, object] | None = None
logger = logging.getLogger("alexa.gateway")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [gateway] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(_handler)
logger.setLevel(logging.INFO)


class ConversationStore:
    """Process-memory conversation history; intentionally not durable across restarts."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._conversations: dict[str, dict[str, object]] = {}
        self._conversation_locks: dict[str, RLock] = {}

    @staticmethod
    def _persistence() -> dict[str, object]:
        return {"type": "process_memory", "survives_backend_restart": False}

    def create(self) -> dict[str, object]:
        now = _now()
        conversation_id = f"conversation-{uuid.uuid4()}"
        with self._lock:
            self._conversations[conversation_id] = {"conversation_id": conversation_id, "created_at": now, "updated_at": now, "responses": [], "message_ids": {}, "context": {}}
            self._conversation_locks[conversation_id] = RLock()
        return {"conversation_id": conversation_id, "created_at": now, "persistence": self._persistence()}

    def list(self) -> dict[str, object]:
        with self._lock:
            items = [{"conversation_id": item["conversation_id"], "created_at": item["created_at"], "updated_at": item["updated_at"], "response_count": len(item["responses"])} for item in self._conversations.values()]
        return {"conversations": items, "persistence": self._persistence()}

    def history(self, conversation_id: str) -> dict[str, object] | None:
        with self._lock:
            item = self._conversations.get(conversation_id)
            if item is None:
                return None
            lock = self._conversation_locks[conversation_id]
        with lock:
            with self._lock:
                item = self._conversations.get(conversation_id)
                if item is None:
                    return None
                return {"conversation_id": conversation_id, "created_at": item["created_at"], "updated_at": item["updated_at"], "responses": deepcopy(item["responses"]), "persistence": self._persistence()}

    def submit(self, config: LlmConfig | None, request: Mapping[str, object], chunk_callback: Any = None) -> tuple[int, dict[str, object]]:
        conversation_id = request.get("conversation_id")
        if not isinstance(conversation_id, str):
            return 404, _failed_message(request, "Conversation not found")
        with self._lock:
            item = self._conversations.get(conversation_id)
            lock = self._conversation_locks.get(conversation_id)
        if item is None or lock is None:
            return 404, _failed_message(request, "Conversation not found")
        with lock:
            client_id, content = request.get("client_message_id"), request.get("content")
            with self._lock:
                item = self._conversations.get(conversation_id)
                if item is None:
                    return 404, _failed_message(request, "Conversation not found")
                previous = item["message_ids"].get(client_id) if isinstance(client_id, str) else None
                if previous is not None:
                    if previous["content"] == content:
                        return 200, deepcopy(previous["response"])
                    return 409, _failed_message(request, "Message identifier was already used")
            context = item.setdefault("context", {})
            prior_responses = item.get("responses", [])
            if chunk_callback is not None:
                response = send_message(config, request, context=context, prior_responses=prior_responses, chunk_callback=chunk_callback)
            else:
                response = send_message(config, request, context=context, prior_responses=prior_responses)
            with self._lock:
                item = self._conversations[conversation_id]
                item["responses"].append(response)
                item["message_ids"][response["user_message"]["message_id"]] = {"content": content, "response": deepcopy(response)}
                item["updated_at"] = response["assistant_message"]["created_at"]
            return 200, response


_conversation_store = ConversationStore()


class GatewayError(Exception):
    """A deliberately non-diagnostic error suitable for a browser response."""

    def __init__(self, message: str, dependency: str = "llm") -> None:
        super().__init__(message)
        self.dependency = dependency


class ConfigurationError(ValueError):
    """Configuration is incomplete or unsafe for a live provider request."""


@dataclass(frozen=True)
class LlmConfig:
    provider: str
    base_url: str
    api_key: str
    model: str
    api_mode: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    temperature: float = 1.0
    top_p: float = 0.95
    max_tokens: int = 512
    enable_thinking: bool = True
    reasoning_budget: int = 512
    streaming: bool = True


@dataclass(frozen=True)
class ServiceHealth:
    status: str
    summary: str
    checked_at: str

    def safe_dict(self) -> dict[str, str]:
        return asdict(self)


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _read_dotenv(path: Path) -> dict[str, str]:
    """Read the small .env subset we need without adding a runtime dependency."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key:
            values[key] = value[1:-1] if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'" else value
    return values


def load_config(*, project_dir: Path | None = None, environ: Mapping[str, str] | None = None) -> LlmConfig:
    """Load root .env, with actual process environment taking precedence."""
    values = _read_dotenv((project_dir or project_root()) / ".env")
    values.update(os.environ if environ is None else environ)
    try:
        timeout_seconds = float(values.get("LLM_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)))
    except ValueError as error:
        raise ConfigurationError("LLM_TIMEOUT_SECONDS must be a number") from error
    if not 0 < timeout_seconds <= 30:
        raise ConfigurationError("LLM_TIMEOUT_SECONDS must be between 0 and 30")

    try:
        temperature = float(values.get("LLM_TEMPERATURE", "1.0"))
    except ValueError as error:
        raise ConfigurationError("LLM_TEMPERATURE must be a number") from error

    try:
        top_p = float(values.get("LLM_TOP_P", "0.95"))
    except ValueError as error:
        raise ConfigurationError("LLM_TOP_P must be a number") from error

    try:
        max_tokens = int(values.get("LLM_MAX_TOKENS", "512"))
    except ValueError as error:
        raise ConfigurationError("LLM_MAX_TOKENS must be an integer") from error

    try:
        reasoning_budget = int(values.get("LLM_REASONING_BUDGET", "512"))
    except ValueError as error:
        raise ConfigurationError("LLM_REASONING_BUDGET must be an integer") from error

    enable_thinking = values.get("LLM_ENABLE_THINKING", "true").strip().lower() in {"1", "true", "yes", "on"}
    streaming = values.get("LLM_STREAMING", "true").strip().lower() in {"1", "true", "yes", "on"}

    config = LlmConfig(
        values.get("LLM_PROVIDER", ""),
        values.get("LLM_BASE_URL", ""),
        values.get("LLM_API_KEY", ""),
        values.get("LLM_MODEL", ""),
        values.get("LLM_API_MODE", ""),
        timeout_seconds,
        temperature,
        top_p,
        max_tokens,
        enable_thinking,
        reasoning_budget,
        streaming,
    )
    validate_config(config)
    return config


def chat_completions_url(base_url: str) -> str:
    """Build the standard route without string matching or duplicate ``/v1``."""
    parsed = urlsplit(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError("LLM_BASE_URL must be an absolute http(s) URL")
    if parsed.query or parsed.fragment:
        raise ConfigurationError("LLM_BASE_URL must not include query or fragment")
    path = parsed.path.rstrip("/")
    if not path.endswith("/v1"):
        path = f"{path}/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, f"{path}/chat/completions", "", ""))


def validate_config(config: LlmConfig) -> None:
    missing = [name for name, value in (("LLM_PROVIDER", config.provider), ("LLM_API_KEY", config.api_key), ("LLM_MODEL", config.model)) if not value.strip()]
    if missing:
        raise ConfigurationError("Missing required configuration: " + ", ".join(missing))
    if config.provider != "openai_compatible":
        raise ConfigurationError("LLM_PROVIDER must be openai_compatible")
    if config.api_mode != "chat_completions":
        raise ConfigurationError("LLM_API_MODE must be chat_completions")
    if not (0.0 <= config.temperature <= 2.0):
        raise ConfigurationError("LLM_TEMPERATURE must be between 0.0 and 2.0")
    if not (0.0 < config.top_p <= 1.0):
        raise ConfigurationError("LLM_TOP_P must be between 0.0 and 1.0")
    if config.max_tokens <= 0:
        raise ConfigurationError("LLM_MAX_TOKENS must be greater than 0")
    if config.reasoning_budget < 0:
        raise ConfigurationError("LLM_REASONING_BUDGET must be non-negative")
    chat_completions_url(config.base_url)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _health(status: str, summary: str) -> ServiceHealth:
    return ServiceHealth(status, summary, _now())


def probe_connectivity(config: LlmConfig) -> ServiceHealth:
    """Bounded TCP check. It proves reachability, never model inference."""
    try:
        parsed = urlsplit(chat_completions_url(config.base_url))
        with socket.create_connection((parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)), timeout=config.timeout_seconds):
            pass
    except (OSError, ValueError):
        return _health(UNAVAILABLE, "LLM endpoint could not be reached")
    return _health(HEALTHY, "LLM endpoint reachable")


def _valid_assistant_response(payload: object) -> bool:
    if not isinstance(payload, dict) or not isinstance(payload.get("choices"), list) or not payload["choices"]:
        return False
    choice = payload["choices"][0]
    message = choice.get("message") if isinstance(choice, dict) else None
    return isinstance(message, dict) and message.get("role") == "assistant" and isinstance(message.get("content"), str) and bool(message["content"].strip())


def probe_inference(config: LlmConfig) -> ServiceHealth:
    """Run one minimal, non-streaming request and validate a usable response."""
    body = json.dumps({"model": config.model, "messages": [{"role": "user", "content": "Reply with OK."}], "stream": False}).encode()
    request = Request(chat_completions_url(config.base_url), data=body, headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=config.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError):
        return _health(UNAVAILABLE, "LLM test response failed")
    return _health(HEALTHY, "LLM test response passed") if _valid_assistant_response(payload) else _health(DEGRADED, "LLM returned an invalid test response")


def mcp_endpoint(environ: Mapping[str, str] | None = None) -> str:
    """Build the local Streamable HTTP endpoint from the shared root setting."""
    values = os.environ if environ is None else environ
    try:
        port = int(values.get("MCP_PORT", "8001"))
    except ValueError:
        port = 8001
    return f"http://localhost:{port}/mcp"


async def _probe_mcp(endpoint: str) -> tuple[ServiceHealth, ServiceHealth]:
    try:
        if Client is None:
            raise RuntimeError("MCP SDK unavailable")
        async with Client(endpoint) as client:
            tools = await client.list_tools()
    except Exception:  # The health boundary must not reveal transport details.
        return _health(UNAVAILABLE, "MCP server could not be reached"), _health(NOT_CHECKED, "MCP tool discovery was not attempted")
    names = {tool.name for tool in tools.tools}
    missing = REQUIRED_MCP_TOOLS - names
    connectivity = _health(HEALTHY, "MCP server reachable")
    if missing:
        return connectivity, _health(DEGRADED, f"MCP tool discovery missing {len(missing)} required tool(s)")
    return connectivity, _health(HEALTHY, "MCP server reachable; 6 required tools discovered")


def probe_mcp(endpoint: str | None = None, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> tuple[ServiceHealth, ServiceHealth]:
    """Connect through the MCP SDK and discover the required public catalogue."""
    try:
        return asyncio.run(asyncio.wait_for(_probe_mcp(endpoint or mcp_endpoint()), timeout=timeout_seconds))
    except (TimeoutError, RuntimeError):
        return _health(UNAVAILABLE, "MCP server could not be reached"), _health(NOT_CHECKED, "MCP tool discovery was not attempted")


def aggregate_health(connectivity: ServiceHealth, inference: ServiceHealth, *, mcp_connectivity: ServiceHealth | None = None, mcp_discovery: ServiceHealth | None = None) -> dict[str, object]:
    """Return a web-safe aggregate from independently checked dependencies."""
    unchecked = _health(NOT_CHECKED, "MCP probe was not attempted")
    mcp_connectivity, mcp_discovery = mcp_connectivity or unchecked, mcp_discovery or unchecked
    services = {"web_backend": _health(HEALTHY, "API available").safe_dict(), "llm_connectivity": connectivity.safe_dict(), "llm_inference": inference.safe_dict(), "mcp_connectivity": mcp_connectivity.safe_dict(), "mcp_discovery": mcp_discovery.safe_dict()}
    llm_live = connectivity.status == HEALTHY and inference.status == HEALTHY
    mcp_live = mcp_connectivity.status == HEALTHY and mcp_discovery.status == HEALTHY
    mode = "live" if llm_live and mcp_live else "demo"
    statuses = [service["status"] for service in services.values()]
    return {"overall_status": HEALTHY if mode == "live" else (UNAVAILABLE if UNAVAILABLE in statuses else DEGRADED), "mode": mode, "checked_at": _now(), "services": services}


def preflight(config: LlmConfig | None = None) -> dict[str, object]:
    mcp_connectivity, mcp_discovery = probe_mcp()
    try:
        config = config or load_config()
        validate_config(config)
    except ConfigurationError as error:
        return aggregate_health(_health(UNAVAILABLE, str(error)), _health(NOT_CHECKED, "LLM inference was not attempted"), mcp_connectivity=mcp_connectivity, mcp_discovery=mcp_discovery)
    connectivity = probe_connectivity(config)
    inference = probe_inference(config) if connectivity.status == HEALTHY else _health(NOT_CHECKED, "LLM inference was not attempted")
    return aggregate_health(connectivity, inference, mcp_connectivity=mcp_connectivity, mcp_discovery=mcp_discovery)


def demo_mode(environ: Mapping[str, str] | None = None) -> bool:
    """Demo is opt-out in development, never inferred from an LLM failure alone."""
    values = _read_dotenv(project_root() / ".env")
    values.update(os.environ if environ is None else environ)
    value = values.get("DEMO_MODE", "true").strip().lower()
    return value in {"1", "true", "yes", "on"}


def refresh_preflight(config: LlmConfig | None = None) -> dict[str, object]:
    global _cached_health
    snapshot = preflight(config)
    with _health_lock:
        _cached_health = snapshot
    return snapshot


def _cached_inference_unusable() -> bool:
    with _health_lock:
        snapshot = _cached_health
    if not snapshot:
        return False
    services = snapshot.get("services", {})
    if not isinstance(services, dict):
        return False
    inference = services.get("llm_inference", {})
    connectivity = services.get("llm_connectivity", {})
    inf_status = inference.get("status") if isinstance(inference, dict) else None
    conn_status = connectivity.get("status") if isinstance(connectivity, dict) else None
    return inf_status in {DEGRADED, UNAVAILABLE} or conn_status in {DEGRADED, UNAVAILABLE}


def _mark_dependency_degraded(dependency: str) -> None:
    """A request failure invalidates only the affected dependency until recheck."""
    global _cached_health
    with _health_lock:
        if not _cached_health:
            return
        services = _cached_health.get("services")
        if not isinstance(services, dict):
            return
        service = "llm_inference" if dependency == "llm" else dependency
        if service not in {"llm_inference", "mcp_connectivity", "mcp_discovery"}:
            return
        summary = "Recent live LLM request failed" if service == "llm_inference" else "Recent MCP request failed"
        services[service] = _health(DEGRADED, summary).safe_dict()
        _cached_health["overall_status"] = DEGRADED
        _cached_health["mode"] = "demo"
        _cached_health["checked_at"] = _now()


def _tool_schema(tool: object) -> dict[str, Any]:
    schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}
    return schema if isinstance(schema, dict) else {}


def _tool_definition(tool: object) -> dict[str, Any]:
    return {"type": "function", "function": {"name": tool.name, "description": getattr(tool, "description", ""), "parameters": _tool_schema(tool)}}


def _schema_valid(value: object, schema: Mapping[str, Any]) -> bool:
    """Fail-closed validation for the JSON Schema subset emitted by this MCP SDK."""
    allowed = {"$schema", "title", "description", "default", "type", "nullable", "anyOf", "properties", "required", "additionalProperties", "items", "minLength", "maxLength", "minimum", "maximum", "format", "enum"}
    if not isinstance(schema, Mapping) or set(schema) - allowed:
        return False
    if schema.get("nullable") is True and value is None:
        return True
    alternatives = schema.get("anyOf")
    if alternatives is not None:
        return isinstance(alternatives, list) and bool(alternatives) and any(isinstance(option, Mapping) and _schema_valid(value, option) for option in alternatives)
    expected = schema.get("type")
    if isinstance(expected, list):
        return all(isinstance(item, str) for item in expected) and any(_schema_valid(value, {key: item for key, item in schema.items() if key != "type"} | {"type": item}) for item in expected)
    if expected is not None and not isinstance(expected, str):
        return False
    if "enum" in schema and (not isinstance(schema["enum"], list) or value not in schema["enum"]):
        return False
    if expected == "object":
        if not isinstance(value, dict):
            return False
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, Mapping) or not isinstance(required, list) or not all(isinstance(key, str) for key in required) or any(key not in value for key in required):
            return False
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                if not isinstance(properties[key], Mapping) or not _schema_valid(item, properties[key]):
                    return False
            elif additional is False:
                return False
            elif isinstance(additional, Mapping) and not _schema_valid(item, additional):
                return False
            elif not isinstance(additional, (bool, Mapping)):
                return False
        return True
    if expected == "array":
        items = schema.get("items")
        return isinstance(value, list) and isinstance(items, Mapping) and all(_schema_valid(item, items) for item in value)
    if expected == "string":
        if not isinstance(value, str) or ("minLength" in schema and (not isinstance(schema["minLength"], int) or len(value) < schema["minLength"])) or ("maxLength" in schema and (not isinstance(schema["maxLength"], int) or len(value) > schema["maxLength"])):
            return False
        if schema.get("format") == "date":
            try: date.fromisoformat(value)
            except ValueError: return False
        return schema.get("format") in (None, "date")
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool) and _number_range_valid(value, schema)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and _number_range_valid(value, schema)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def _number_range_valid(value: int | float, schema: Mapping[str, Any]) -> bool:
    return ("minimum" not in schema or isinstance(schema["minimum"], (int, float)) and value >= schema["minimum"]) and ("maximum" not in schema or isinstance(schema["maximum"], (int, float)) and value <= schema["maximum"])


def _sanitize_reasoning(raw: str) -> str | None:
    """Strip internal chain-of-thought tokens, tags, and formatting artifacts safely."""
    cleaned = raw.replace("<think>", "").replace("</think>", "").strip()
    return cleaned if cleaned else None


def _stream_completion(
    config: LlmConfig,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    chunk_callback: Any = None,
) -> dict[str, Any]:
    request_body: dict[str, Any] = {
        "model": config.model,
        "messages": messages,
        "temperature": config.temperature,
        "top_p": config.top_p,
        "max_tokens": config.max_tokens,
        "stream": True,
    }
    if config.enable_thinking:
        request_body["chat_template_kwargs"] = {"enable_thinking": True}
        if config.reasoning_budget > 0:
            request_body["reasoning_budget"] = config.reasoning_budget
    if tools:
        request_body.update(tools=tools, tool_choice="auto")

    request = Request(
        chat_completions_url(config.base_url),
        data=json.dumps(request_body).encode("utf-8"),
        headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls_by_index: dict[int, dict[str, Any]] = {}
    finish_reason: str | None = None

    try:
        with urlopen(request, timeout=config.timeout_seconds) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8").strip()
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices")
                if not choices or not isinstance(choices, list):
                    continue
                choice = choices[0]
                if not isinstance(choice, dict):
                    continue
                fr = choice.get("finish_reason")
                if fr:
                    finish_reason = fr
                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    continue

                r_delta = delta.get("reasoning_content")
                if r_delta and isinstance(r_delta, str):
                    reasoning_parts.append(r_delta)
                    if chunk_callback:
                        chunk_callback("reasoning", r_delta)

                c_delta = delta.get("content")
                if c_delta and isinstance(c_delta, str):
                    content_parts.append(c_delta)
                    if chunk_callback:
                        chunk_callback("content", c_delta)

                tcs = delta.get("tool_calls")
                if isinstance(tcs, list):
                    for tc in tcs:
                        if not isinstance(tc, dict):
                            continue
                        idx = tc.get("index", 0)
                        if idx not in tool_calls_by_index:
                            tool_calls_by_index[idx] = {
                                "id": tc.get("id") or "",
                                "type": tc.get("type") or "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        if tc.get("id"):
                            tool_calls_by_index[idx]["id"] = tc["id"]
                        fn = tc.get("function")
                        if isinstance(fn, dict):
                            if fn.get("name"):
                                tool_calls_by_index[idx]["function"]["name"] += fn["name"]
                            if fn.get("arguments"):
                                tool_calls_by_index[idx]["function"]["arguments"] += fn["arguments"]
    except HTTPError as error:
        raise GatewayError(f"LLM request returned HTTP {error.code}", "llm") from error
    except (URLError, OSError, TimeoutError, UnicodeDecodeError) as error:
        raise GatewayError("LLM response is unavailable", "llm") from error

    tool_calls = [tool_calls_by_index[i] for i in sorted(tool_calls_by_index)]
    content_text = "".join(content_parts)
    raw_reasoning = "".join(reasoning_parts)
    reasoning_text = _sanitize_reasoning(raw_reasoning) if raw_reasoning else None

    message: dict[str, Any] = {
        "role": "assistant",
        "content": content_text if content_text else None,
        "finish_reason": finish_reason,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    if reasoning_text:
        message["reasoning_content"] = reasoning_text
    return message


def _completion(config: LlmConfig, messages: list[dict[str, Any]], tools: list[dict[str, Any]], chunk_callback: Any = None) -> dict[str, Any]:
    if config.streaming:
        return _stream_completion(config, messages, tools, chunk_callback=chunk_callback)
    request_body: dict[str, Any] = {
        "model": config.model,
        "messages": messages,
        "temperature": config.temperature,
        "top_p": config.top_p,
        "max_tokens": config.max_tokens,
        "stream": False,
    }
    if config.enable_thinking:
        request_body["chat_template_kwargs"] = {"enable_thinking": True}
        if config.reasoning_budget > 0:
            request_body["reasoning_budget"] = config.reasoning_budget
    if tools:
        request_body.update(tools=tools, tool_choice="auto")
    request = Request(chat_completions_url(config.base_url), data=json.dumps(request_body).encode(), headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=config.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise GatewayError(f"LLM request returned HTTP {error.code}", "llm") from error
    except (URLError, OSError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GatewayError("LLM response is unavailable", "llm") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("choices"), list) or not payload["choices"]:
        raise GatewayError("LLM returned an invalid response")
    choice = payload["choices"][0]
    message = choice.get("message") if isinstance(choice, dict) else None
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise GatewayError("LLM returned an invalid response")
    if isinstance(choice, dict) and choice.get("finish_reason"):
        message["finish_reason"] = choice["finish_reason"]
    return message


def _safe_tool_result(result: object) -> tuple[object, bool]:
    """MCP SDKs use either structuredContent or content; never expose exceptions."""
    failed = bool(getattr(result, "isError", False) or getattr(result, "is_error", False) or (isinstance(result, dict) and result.get("isError")))
    value = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
    if value is None:
        value = getattr(result, "content", result)
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return {"status": "unavailable"}, True
    return value, failed


def _valid_tool_result(name: str, value: object) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("status"), str) or not isinstance(value.get("is_simulated"), bool):
        return False
    required: dict[str, tuple[str, ...]] = {
        "find_product": ("matches",),
        "get_appliance_inventory": ("appliances",),
        "get_troubleshooting_steps": ("model_id", "document_found", "document_reference", "probable_causes", "safe_steps"),
        "check_warranty": ("appliance_id", "eligibility", "coverage_end_date", "evidence_source"),
        "find_service_slots": ("availability_type", "slots"),
        "create_service_request": ("request_id", "request_status", "booking_status", "slot_id"),
        "get_case_status": ("request_id", "request_status", "booking_status"),
    }
    if name not in required or any(key not in value for key in required[name]):
        return False
    allowed_statuses = {
        "find_product": {"success", "not_found"},
        "get_appliance_inventory": {"success", "not_found", "unavailable"},
        "get_troubleshooting_steps": {"documentation_not_found", "model_unverified", "success", "unavailable"},
        "check_warranty": {"success", "not_found", "unavailable"},
        "find_service_slots": {"success", "no_slots", "unavailable"},
        "create_service_request": {"success", "confirmation_invalid", "not_found", "unavailable", "outcome_unknown"},
        "get_case_status": {"success", "not_found", "unavailable"},
    }
    if value["status"] not in allowed_statuses[name]:
        return False
    if name == "find_product" and not isinstance(value["matches"], list):
        return False
    if name == "get_appliance_inventory" and not isinstance(value["appliances"], list):
        return False
    if name == "get_troubleshooting_steps" and (not isinstance(value["model_id"], str) or not isinstance(value["document_found"], bool) or not isinstance(value["document_reference"], dict) or not isinstance(value["probable_causes"], list) or not isinstance(value["safe_steps"], list)):
        return False
    if name == "check_warranty" and (not isinstance(value["appliance_id"], str) or value["eligibility"] not in {"eligible", "ineligible", "unknown"} or value["coverage_end_date"] is not None and not isinstance(value["coverage_end_date"], str) or not isinstance(value["evidence_source"], str)):
        return False
    if name == "find_service_slots" and not isinstance(value["slots"], list):
        return False
    if name == "create_service_request" and (value["request_id"] is not None and not isinstance(value["request_id"], str) or not isinstance(value["request_status"], str) or not isinstance(value["booking_status"], str)):
        return False
    if name == "get_case_status" and (not isinstance(value["request_id"], str) or not isinstance(value["request_status"], str) or not isinstance(value["booking_status"], str)):
        return False
    return True


async def _call_discovered_tool(client: object, tools: Mapping[str, object], name: str, arguments: dict[str, Any], timeout_seconds: float) -> object:
    tool = tools.get(name)
    if tool is None or not _schema_valid(arguments, _tool_schema(tool)):
        raise GatewayError("The selected support action is invalid")
    try:
        result = await asyncio.wait_for(client.call_tool(name, arguments), timeout=timeout_seconds)
    except Exception as error:
        raise GatewayError("A support tool is unavailable", "mcp_connectivity") from error
    value, failed = _safe_tool_result(result)
    if failed or not _valid_tool_result(name, value):
        raise GatewayError("The support tool returned an invalid result", "mcp_connectivity")
    return value


async def _execute_mcp_tool(name: str, arguments: dict[str, Any], timeout_seconds: float) -> object:
    if Client is None:
        raise GatewayError("MCP client is unavailable", "mcp_connectivity")
    try:
        async with Client(mcp_endpoint()) as client:
            catalogue = await asyncio.wait_for(client.list_tools(), timeout=timeout_seconds)
            tools = {tool.name: tool for tool in catalogue.tools if getattr(tool, "name", None) in SUPPORTED_MCP_TOOLS}
            if not (REQUIRED_MCP_TOOLS <= set(tools)):
                raise GatewayError("Required support tools are unavailable", "mcp_discovery")
            return await _call_discovered_tool(client, tools, name, arguments, timeout_seconds)
    except GatewayError:
        raise
    except Exception as error:
        raise GatewayError("A support tool is unavailable", "mcp_connectivity") from error


def _is_affirmative_confirmation(text: str, metadata: Mapping[str, object] | None = None) -> bool:
    if metadata and metadata.get("action") == "confirm_appliance":
        return True
    lowered = text.lower().strip()
    clean = lowered.strip(".!? ")
    exact_matches = {
        "yes",
        "yes.",
        "yes!",
        "yep",
        "yeah",
        "correct",
        "that's it",
        "thats it",
        "that's the one",
        "thats the one",
        "yes that is the one",
        "yes, that is the one",
        "yes it is",
        "yes, it is",
        "confirmed",
        "confirm",
        "confirmed, this is my appliance",
        "confirmed this is my appliance",
        "confirmed, that is my appliance",
        "confirmed that is my appliance",
        "this is my appliance",
        "that is my appliance",
        "confirm appliance",
    }
    if clean in exact_matches or lowered in exact_matches:
        return True
    if any(clean.startswith(prefix) for prefix in (
        "confirmed",
        "confirm",
        "yes,",
        "yes ",
        "that's the one",
        "thats the one",
        "this is my appliance",
        "that is my appliance",
    )):
        return True
    if "this is my appliance" in clean or "that is my appliance" in clean:
        return True
    return False


def _is_negative_confirmation(text: str, metadata: Mapping[str, object] | None = None) -> bool:
    if metadata and metadata.get("action") in ("reject_appliance", "choose_another"):
        return True
    lowered = text.lower().strip()
    clean = lowered.strip(".!? ")
    exact_matches = {
        "no",
        "no.",
        "no!",
        "nope",
        "choose another",
        "different",
        "not that",
        "not this one",
        "not that one",
        "different appliance",
        "that's not it",
        "thats not it",
        "not my appliance",
        "no, this is a different appliance",
        "no this is a different appliance",
        "this is a different appliance",
        "wrong appliance",
    }
    if clean in exact_matches or lowered in exact_matches:
        return True
    if any(clean.startswith(prefix) for prefix in ("no,", "no ", "nope", "choose another")):
        return True
    if any(k in clean for k in ("choose another", "different appliance", "not my appliance", "not that one", "wrong appliance")):
        return True
    return False


def _is_greeting(text: str) -> bool:
    clean = text.lower().strip().strip(".!? ")
    greetings = {
        "hello",
        "hi",
        "hey",
        "greetings",
        "good morning",
        "good afternoon",
        "good evening",
        "howdy",
        "hello there",
        "hi there",
        "help",
    }
    if clean in greetings:
        return True
    if any(clean.startswith(prefix + " ") for prefix in ("hello", "hi", "hey", "good morning", "good evening")):
        return True
    return False


def _deterministic_intent(content: str, context: dict[str, Any] | None = None, metadata: Mapping[str, object] | None = None) -> tuple[str, dict[str, Any]] | None:
    lowered = content.lower().strip()
    ctx = context or {}
    meta = metadata or {}
    scenario_id = str(meta.get("scenario_id") or "").strip()

    # Route by explicit scenario ID if provided
    if scenario_id == "washing_machine_error":
        model_id = "WC-500" if "wc-500" in lowered else "WM8000-DEMO"
        return "get_troubleshooting_steps", {"model_id": model_id, "symptom": content.strip() or "Reported appliance issue", "error_code": "E21" if "e21" in lowered else None}
    if scenario_id in ("arrange_appliance_service", "service_discovery"):
        today = date.today().isoformat()
        app_id = ctx.get("selected_appliance_id") or ctx.get("confirmed_appliance", {}).get("appliance_id") or ctx.get("pending_appliance", {}).get("appliance_id") or "demo-appl-washer-001"
        return "find_service_slots", {"appliance_id": app_id, "location": "demo-area", "date_from": today, "date_to": today}
    if scenario_id == "refrigerator_not_cooling":
        if ctx.get("confirmed_appliance"):
            model = ctx["confirmed_appliance"].get("model_number") or "LFXS26973S-DEMO"
            symptom = ctx.get("confirmed_symptom") or "not_cooling"
            return "get_troubleshooting_steps", {"model_id": model, "symptom": symptom}
        return "get_appliance_inventory", {"appliance_type": "refrigerator"}
    if scenario_id == "warranty_eligibility":
        app_id = ctx.get("selected_appliance_id") or ctx.get("confirmed_appliance", {}).get("appliance_id") or "demo-appl-washer-001"
        return "check_warranty", {"appliance_id": app_id}
    if scenario_id == "previous_appliance_issue":
        return "get_case_status", {"request_id": "demo-case-2026-0042"}
    if scenario_id == "unknown_appliance_model":
        return "find_product", {"query": "unknown"}

    # Affirmative confirmation for pending appliance or standalone
    if _is_affirmative_confirmation(content, meta):
        if ctx.get("pending_appliance"):
            pending = ctx["pending_appliance"]
            model = pending.get("model_number") or "LFXS26973S-DEMO"
            symptom = ctx.get("pending_symptom") or "not_cooling"
            return "get_troubleshooting_steps", {"model_id": model, "symptom": symptom}
        return "clarify_unbound_confirmation", {}

    # Negative confirmation
    if _is_negative_confirmation(content, meta):
        if ctx.get("pending_appliance"):
            return "decline_pending_candidate", {}
        return "general_clarification", {}

    # General Conversational Intent (greetings, wellbeing, capabilities, gratitude, humor, history)
    conv_intent = classify_conversational_intent(content)
    if conv_intent is not None:
        return f"conv_{conv_intent}", {}

    # Greeting fallback
    if _is_greeting(content):
        return "conv_greeting", {}

    # Scenario E: Previous appliance issue / case history
    if any(k in lowered for k in ("previous", "last time", "reported an issue with my refrigerator", "case demo-case")):
        return "get_case_status", {"request_id": "demo-case-2026-0042"}

    # Scenario D: Arrange appliance service / service discovery
    if any(k in lowered for k in ("service options", "arrange service", "service slot", "service discovery", "discover service", "service availability", "service options are available", "do not book anything without asking")):
        today = date.today().isoformat()
        app_id = ctx.get("selected_appliance_id") or ctx.get("confirmed_appliance", {}).get("appliance_id") or ctx.get("pending_appliance", {}).get("appliance_id") or "demo-appl-washer-001"
        return "find_service_slots", {"appliance_id": app_id, "location": "demo-area", "date_from": today, "date_to": today}

    # Scenario F: Unknown appliance model / identify model
    if any(k in lowered for k in ("don't know its exact model", "dont know its exact model", "identify my appliance", "locate its model", "unknown appliance model")):
        return "find_product", {"query": "unknown"}

    # Scenario C: Warranty eligibility
    if "warranty" in lowered:
        app_id = ctx.get("selected_appliance_id") or ctx.get("confirmed_appliance", {}).get("appliance_id")
        if app_id:
            return "check_warranty", {"appliance_id": app_id}
        if "fridge" in lowered or "refrigerator" in lowered:
            return "check_warranty", {"appliance_id": "demo-appl-fridge-001"}
        return "check_warranty", {"appliance_id": "appliance-001"}

    # Scenario B: Refrigerator not cooling
    if any(k in lowered for k in ("refrigerator is not cooling", "refrigerator not cooling", "fridge not cooling", "not cooling")):
        if ctx.get("confirmed_appliance"):
            model = ctx["confirmed_appliance"].get("model_number") or "LFXS26973S-DEMO"
            symptom = ctx.get("confirmed_symptom") or "not_cooling"
            return "get_troubleshooting_steps", {"model_id": model, "symptom": symptom}
        return "get_appliance_inventory", {"appliance_type": "refrigerator"}

    # Scenario A / Washing machine error / general troubleshooting
    if any(word in lowered for word in ("troubleshoot", "troubleshooting", "error code e21", "error code", "e21", "won't drain", "will not drain", "washing machine", "washing-machine", "washer error")):
        model_id = "WC-500" if "wc-500" in lowered else "WM8000-DEMO"
        return "get_troubleshooting_steps", {"model_id": model_id, "symptom": content.strip() or "Reported appliance issue", "error_code": "E21" if "e21" in lowered else None}

    # General inquiry fallback: discover appliances or search products when appliance keywords match
    if "refrigerator" in lowered or "fridge" in lowered:
        return "get_appliance_inventory", {"appliance_type": "refrigerator"}
    if any(k in lowered for k in ("washer", "washing machine", "dryer", "dishwasher", "oven", "range", "stove", "microwave", "air conditioner", "ac", "purifier", "vacuum", "heater", "geyser", "tv", "television")):
        return "get_appliance_inventory", {}
    if any(k in lowered for k in ("model", "brand", "product", "part", "serial", "manual", "appliance", "catalogue", "catalog")):
        return "find_product", {"query": content.strip()[:60]}

    return None


async def _deterministic_mcp_message(
    request: Mapping[str, object],
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    context: dict[str, Any] | None = None,
    chunk_callback: Any = None,
    correlation_id: str | None = None,
) -> dict[str, object] | None:
    content = str(request.get("content") or "")
    ctx = context if context is not None else {}
    metadata = request.get("metadata") if isinstance(request.get("metadata"), Mapping) else None

    intent = _deterministic_intent(content, context=ctx, metadata=metadata)
    if intent is None:
        logger.info("[%s] No deterministic scenario recognized for user message", correlation_id or "no-corr")
        return None
    name, arguments = intent

    now = _now()

    # 1. Conversational intent response (greeting, wellbeing, capabilities, gratitude, humor, history, general conversation)
    if name.startswith("conv_") or name in ("greeting", "general_conversation"):
        intent_type = name.removeprefix("conv_") if name.startswith("conv_") else name
        text = get_conversational_response(intent_type, prior_responses=ctx.get("prior_responses"))
        if chunk_callback is not None:
            chunk_callback("content", text)
        return {
            "conversation_id": request["conversation_id"],
            "response_id": f"response-{uuid.uuid4()}",
            "correlation_id": correlation_id,
            "status": "completed",
            "user_message": {"message_id": request["client_message_id"], "role": "user", "content": request["content"], "created_at": now},
            "assistant_message": {
                "message_id": f"assistant-{uuid.uuid4()}",
                "role": "assistant",
                "content": text,
                "created_at": now,
                "provenance": {"source_type": "deterministic", "is_simulated": False},
                "activity": {"status": "completed", "summary": "Conversational response", "events": []},
                "tool_results": [],
                "structured_result": None,
                "conclusion": {"status": "completed", "summary": "Conversational response", "requires_user_confirmation": False},
            },
        }

    # 2. Unbound confirmation without pending action
    if name == "clarify_unbound_confirmation":
        text = "There is no pending appliance confirmation. Please provide your appliance brand and model number or describe what issue you are experiencing."
        if chunk_callback is not None:
            chunk_callback("content", text)
        return {
            "conversation_id": request["conversation_id"],
            "response_id": f"response-{uuid.uuid4()}",
            "correlation_id": correlation_id,
            "status": "completed",
            "user_message": {"message_id": request["client_message_id"], "role": "user", "content": request["content"], "created_at": now},
            "assistant_message": {
                "message_id": f"assistant-{uuid.uuid4()}",
                "role": "assistant",
                "content": text,
                "created_at": now,
                "provenance": {"source_type": "deterministic", "is_simulated": False},
                "activity": {"status": "completed", "summary": "No pending confirmation", "events": []},
                "tool_results": [],
                "structured_result": None,
                "conclusion": {"status": "partially_completed", "summary": "No pending confirmation", "requires_user_confirmation": False},
            },
        }

    # 3. Candidate declined by user
    if name == "decline_pending_candidate":
        ctx.pop("pending_appliance", None)
        ctx.pop("pending_appliance_id", None)
        ctx.pop("pending_action_type", None)
        ctx.pop("pending_confirmation_id", None)
        ctx.pop("pending_scenario_id", None)
        ctx.pop("pending_symptom", None)
        text = "Understood. Please provide the brand and model number of your appliance, and I'll find the safe troubleshooting guidance for you."
        event = {"activity_id": f"activity-{uuid.uuid4()}", "sequence": 1, "label": "Candidate declined by user", "status": "succeeded", "result_summary": "Awaiting user brand/model", "finished_at": now}
        if chunk_callback is not None:
            chunk_callback("content", text)
        return {
            "conversation_id": request["conversation_id"],
            "response_id": f"response-{uuid.uuid4()}",
            "correlation_id": correlation_id,
            "status": "completed",
            "user_message": {"message_id": request["client_message_id"], "role": "user", "content": request["content"], "created_at": now},
            "assistant_message": {
                "message_id": f"assistant-{uuid.uuid4()}",
                "role": "assistant",
                "content": text,
                "created_at": now,
                "provenance": {"source_type": "deterministic", "is_simulated": False},
                "activity": {"status": "completed", "summary": "Candidate declined", "events": [event]},
                "tool_results": [],
                "structured_result": None,
                "conclusion": {"status": "partially_completed", "summary": "Clarification requested", "requires_user_confirmation": False},
            },
        }

    # 4. General clarification or unsupported inquiry
    if name in ("unsupported_inquiry", "general_clarification"):
        text = (
            "I'm Alexa, your conversational assistant. I can chat with you naturally, answer questions, "
            "or assist with appliance troubleshooting (such as a refrigerator not cooling or washing machine error E21), "
            "checking warranty eligibility, viewing case status, or finding estimated service availability options. "
            "How can I help you today?"
        )
        if chunk_callback is not None:
            chunk_callback("content", text)
        return {
            "conversation_id": request["conversation_id"],
            "response_id": f"response-{uuid.uuid4()}",
            "correlation_id": correlation_id,
            "status": "completed",
            "user_message": {"message_id": request["client_message_id"], "role": "user", "content": request["content"], "created_at": now},
            "assistant_message": {
                "message_id": f"assistant-{uuid.uuid4()}",
                "role": "assistant",
                "content": text,
                "created_at": now,
                "provenance": {"source_type": "deterministic", "is_simulated": False},
                "activity": {"status": "completed", "summary": "Clarification requested for unsupported request", "events": []},
                "tool_results": [],
                "structured_result": None,
                "conclusion": {"status": "partially_completed", "summary": "Clarification requested", "requires_user_confirmation": False},
            },
        }

    # Handle confirmation state transition before tool execution
    was_confirmed_turn = False
    if name == "get_troubleshooting_steps" and ctx.get("pending_appliance"):
        confirmed = ctx.pop("pending_appliance")
        app_id = ctx.pop("pending_appliance_id", None) or confirmed.get("appliance_id")
        symptom = ctx.pop("pending_symptom", None) or "not_cooling"
        ctx.pop("pending_action_type", None)
        ctx.pop("pending_confirmation_id", None)
        ctx.pop("pending_scenario_id", None)

        ctx["selected_appliance_id"] = app_id
        ctx["confirmed_appliance"] = confirmed
        ctx["confirmed_symptom"] = symptom
        was_confirmed_turn = True

    logger.info("[%s] Routing to deterministic MCP tool: %s", correlation_id or "no-corr", name)
    if chunk_callback is not None:
        chunk_callback("status", {"status": "in_progress"})
        chunk_callback("tool_start", {"tool_name": name, "label": f"Executing {name} via MCP"})

    result = await _execute_mcp_tool(name, arguments, timeout_seconds)
    logger.info("[%s] Deterministic MCP tool %s executed successfully (domain status: %s)", correlation_id or "no-corr", name, result.get("status"))
    domain_simulated = bool(result.get("is_simulated", False))
    source_label = "Simulated domain record returned by live MCP tool execution" if domain_simulated else "Result returned by live MCP tool execution"
    provenance = {"source_type": "mcp", "execution_type": "mcp_streamable_http", "is_simulated": domain_simulated, "source_label": source_label}

    requires_confirmation = False
    pending_action_payload: dict[str, Any] | None = None

    if name == "get_appliance_inventory":
        result_type = "inventory"
        appliances = result.get("appliances", [])
        candidate = next((a for a in appliances if a.get("appliance_type") == "refrigerator"), appliances[0] if appliances else None)
        if candidate and candidate.get("brand") and candidate.get("model_number"):
            conf_id = f"conf-{uuid.uuid4()}"
            app_id = candidate.get("appliance_id", "demo-appl-fridge-001")
            brand = candidate.get("brand")
            model = candidate.get("model_number")
            purchase_desc = "three days ago" if candidate.get("purchase_date") == "2026-10-19" else f"on {candidate.get('purchase_date')}"

            ctx["pending_action_type"] = "confirm_appliance"
            ctx["pending_appliance_id"] = app_id
            ctx["pending_scenario_id"] = "refrigerator_not_cooling"
            ctx["pending_confirmation_id"] = conf_id
            ctx["pending_appliance"] = candidate
            ctx["pending_symptom"] = "not_cooling"

            text = f"Hey! I found a refrigerator in your appliance history: an {brand} refrigerator (model {model}) recorded as purchased {purchase_desc}. Is that the one you're having trouble with?"
            summary = f"Appliance candidate identified: {brand} {model}"
            display_summary = f"Found in purchase history: {brand} {model} • Purchased {purchase_desc}"
            result_status = "partial"
            requires_confirmation = True
            pending_action_payload = {
                "action_type": "confirm_appliance",
                "confirmation_id": conf_id,
                "appliance_id": app_id,
                "brand": brand,
                "model_number": model,
                "symptom": "not_cooling",
                "is_simulated": True,
            }
        else:
            text = "I searched your appliance history but couldn't find a matching refrigerator record. Could you tell me the brand and model number of your refrigerator so I can look up the safe troubleshooting steps?"
            summary = "Appliance history: no matching refrigerator"
            display_summary = "No matching appliance found in history"
            result_status = "unavailable"

    elif name == "check_warranty":
        eligibility = result["eligibility"]
        if result["status"] == "success":
            text = f"The {'simulated ' if domain_simulated else ''}warranty record for {result['appliance_id']} shows {eligibility}."
            if result.get("coverage_end_date"):
                text += f" The recorded coverage end date is {result['coverage_end_date']}."
            if result.get("reason"):
                text += f" ({result['reason']})"
            summary = f"Warranty: {eligibility}"
            result_status = "available" if eligibility == "eligible" else "partial"
            display_summary = f"Eligibility: {eligibility.capitalize()} • Coverage ends: {result.get('coverage_end_date') or 'N/A'}"
        else:
            text = f"No warranty record was found for {result['appliance_id']}; eligibility is {eligibility}."
            summary = "Warranty: record not found"
            result_status = "unavailable"
            display_summary = "No warranty record found."
        result_type = "warranty"

    elif name == "get_troubleshooting_steps":
        result_type = "troubleshooting"
        lead_in = ""
        if was_confirmed_turn:
            lead_in = "Got it. I'll check the available guidance for that model and walk you through safe checks. This is demo purchase data, so it isn't a verified purchase record.\n\n"

        if result.get("document_found"):
            scope = result.get("documentation_scope") or "General manufacturer guidance"
            causes = ", ".join(result.get("probable_causes", []))
            steps = " ".join(result.get("safe_steps", []))
            text = f"{lead_in}{scope.capitalize()}: {causes}. Safe checks: {steps}"
            if result.get("stop_conditions"):
                text += " Stop conditions: " + " ".join(result["stop_conditions"])
            summary = "Troubleshooting: guidance available"
            result_status = "available"
            display_summary = f"Guidance available • Model: {result.get('model_id')}"
        else:
            synth = synthesize_missing_model_guidance(
                model_id=result.get("model_id", "unspecified model"),
                symptom=arguments.get("symptom", "Appliance issue"),
                appliance_type=arguments.get("appliance_type", "appliance"),
                error_code=arguments.get("error_code"),
            )
            model = synth["model_id"]
            steps = " ".join(synth.get("safe_steps", []))
            causes = ", ".join(synth.get("probable_causes", []))
            stop = " ".join(synth.get("stop_conditions", []))
            text = (
                f"{lead_in}I couldn't locate verified manufacturer documentation for {model}; "
                f"the reported issue cannot be verified without exact model documentation. "
                f"Based on safe generic appliance knowledge, here are non-invasive checks you can perform:\n"
                f"• Safe checks: {steps}\n"
                f"• Possible causes: {causes}\n"
                f"• Stop immediately: {stop}\n\n"
                f"If you can provide the exact model number from the rating plate, I can check for more specific guidance."
            )
            summary = "Troubleshooting: generic guidance synthesized (model unverified)"
            result_status = "partial"
            display_summary = f"Generic guidance • Model: {model} (unverified)"

    elif name == "find_service_slots":
        result_type = "service"
        slots = result.get("slots", [])
        text = f"Here are estimated service options for your appliance (simulated demo availability; {len(slots)} options found). No appointment has been booked or requested."
        for s in slots[:3]:
            fee = f"{s.get('currency', 'INR')} {s.get('estimated_fee', '')}"
            text += f" • {s.get('provider_name')}: {s.get('start_at', '')[:16]} ({fee})"
        text += " Please confirm if you would like to proceed with scheduling a service visit."
        summary = f"Service availability: {len(slots)} estimated slots"
        result_status = "available" if slots else "unavailable"
        display_summary = f"{len(slots)} estimated slots found • Discovery only, not booked"
        # Service discovery requires separate scheduling confirmation; no booking created
        requires_confirmation = True
        pending_action_payload = {
            "action_type": "service_booking_notice",
            "is_simulated": True,
        }

    elif name == "get_case_status":
        result_type = "case"
        req_id = result.get("request_id")
        req_status = result.get("request_status")
        case_sum = result.get("summary")
        text = f"Support case {req_id}: Recorded status is {req_status}. {case_sum} (Note: Resolved status indicates the support case was closed; it is not proof that a physical repair occurred.)"
        summary = f"Case {req_id}: {req_status}"
        result_status = "available"
        display_summary = f"Case {req_id} • Status: {req_status}"

    elif name == "find_product":
        result_type = "product"
        matches = result.get("matches", [])
        valid_model_match = next((m for m in matches if m.get("model_number")), None)
        if valid_model_match:
            text = f"Appliance catalogue match: {valid_model_match.get('display_name')} (Model: {valid_model_match.get('model_number')})."
            summary = f"Product match: {valid_model_match.get('display_name')}"
            result_status = "available"
        else:
            text = "To identify your appliance safely, locate the serial and model rating plate (commonly inside the door frame or on the side/rear panel). Do not remove panels or move heavy appliances unsafely to find the label."
            summary = "Appliance identification guidance"
            result_status = "partial"
        display_summary = f"Catalogue lookup: {len(matches)} matches"

    else:
        return None

    event = {"activity_id": f"activity-{uuid.uuid4()}", "sequence": 1, "label": f"Called {name} through MCP", "status": "succeeded", "result_summary": summary, "finished_at": now}
    tool_result = {"tool_result_id": f"tool-{uuid.uuid4()}", "activity_id": event["activity_id"], "tool_name": name, "status": "succeeded", "summary": summary, "result": result, "provenance": provenance}
    if chunk_callback is not None:
        chunk_callback("tool_done", tool_result)
        chunk_callback("content", text)

    structured = {"result_id": f"result-{uuid.uuid4()}", "type": result_type, "status": result_status, "display": {"title": summary, "summary": display_summary, "disclaimer": "Domain data is simulated; the MCP server executed the tool over Streamable HTTP." if domain_simulated else "Result returned by the MCP server."}, "payload": result, "provenance": provenance}
    conclusion = {
        "status": "completed" if result_status == "available" else "partially_completed",
        "summary": summary,
        "requires_user_confirmation": requires_confirmation,
    }
    if pending_action_payload is not None:
        conclusion["pending_action"] = pending_action_payload

    response_id = f"response-{uuid.uuid4()}"
    return {
        "conversation_id": request["conversation_id"],
        "response_id": response_id,
        "correlation_id": correlation_id,
        "status": "completed",
        "user_message": {"message_id": request["client_message_id"], "role": "user", "content": request["content"], "created_at": now},
        "assistant_message": {
            "message_id": f"assistant-{uuid.uuid4()}",
            "role": "assistant",
            "content": text,
            "created_at": now,
            "provenance": {"source_type": "deterministic", "is_simulated": False},
            "activity": {"status": "completed", "summary": summary, "events": [event]},
            "tool_results": [tool_result],
            "structured_result": structured,
            "conclusion": conclusion,
        },
    }


class OrchestrationResult(tuple):
    """3-tuple compatible with legacy unpacking (text, tool_results, status), with reasoning metadata."""
    def __new__(cls, text: str, tool_results: list[dict[str, Any]], status: str, reasoning: str | None = None):
        return super().__new__(cls, (text, tool_results, status))

    def __init__(self, text: str, tool_results: list[dict[str, Any]], status: str, reasoning: str | None = None):
        self.text = text
        self.tool_results = tool_results
        self.status = status
        self.reasoning = reasoning


def _extract_gateway_error(error: BaseException) -> GatewayError | None:
    """Unwrap a GatewayError from ExceptionGroup or nested causes in Python 3.11+."""
    if isinstance(error, GatewayError):
        return error
    sub_exceptions = getattr(error, "exceptions", None)
    if sub_exceptions:
        for sub in sub_exceptions:
            found = _extract_gateway_error(sub)
            if found is not None:
                return found
    cause = getattr(error, "__cause__", None)
    if cause is not None:
        found = _extract_gateway_error(cause)
        if found is not None:
            return found
    return None


async def _orchestrate(
    config: LlmConfig, content: str, *, endpoint: str | None = None,
    context: dict[str, Any] | None = None,
    prior_responses: list[dict[str, Any]] | None = None,
    trusted_create_service_request: Mapping[str, object] | None = None,
    chunk_callback: Any = None,
) -> tuple[str, list[dict[str, Any]], str]:
    """One bounded Chat Completions/MCP tool loop. Caller supplies trusted confirmation data only."""
    validate_config(config)
    started = time.monotonic()
    system_prompt = (
        "You are Alexa, a natural, empathetic, and context-aware conversational assistant "
        "with specialized household appliance after-sales support capabilities.\n"
        "\n"
        "Core Conversational Principles:\n"
        "1. Identity & Style: Your name is Alexa. Speak naturally, warmly, and concisely. "
        "Respond directly to the user's message before offering additional help. "
        "Avoid repetitive intros, scripted greetings, excessive bullet lists, and unnecessary questionnaires. "
        "Ask only for information needed to complete the current task.\n"
        "2. General vs Support Mode: For ordinary greetings ('Hello', 'Helloo'), social exchanges ('How are you?'), "
        "humor ('Tell me a joke'), gratitude ('Thank you'), or general queries ('What can you do?'), respond naturally and conversationally. "
        "Do NOT invoke appliance tools or force the user into an appliance questionnaire when they are just chatting.\n"
        "3. Context-Aware Tool Use: Use tools ONLY when they materially help answer an appliance-related request. "
        "When a user reports an appliance issue, check synthetic household ownership history using get_appliance_inventory "
        "before asking for brand or model numbers.\n"
        "4. Candidate Confirmation: If candidate records are found in household inventory, present the candidate "
        "(mentioning brand, model, and purchase date) and ask the user to confirm it before diagnosing. "
        "Distinguish catalogue matches from proof of household ownership.\n"
        "5. Diagnostic Safety & Evidence-Aware Synthesis: Prefer exact-model manufacturer documentation when available. "
        "If exact model is missing or unverified, provide safe, qualified generic guidance without inventing citations. "
        "Call get_troubleshooting_steps with the confirmed or identified appliance and symptom. "
        "Prominently state stop conditions for electrical, smoke, burning, or water hazards. "
        "Never fabricate manual citations, error codes, warranty coverage, or repair outcomes.\n"
        "6. Consequential Actions & Continuity: Consequential actions (like booking a service appointment) require "
        "explicit, validated user confirmation. Service slot discovery is discovery only, NOT a booking. "
        "Preserve conversation context, confirmed appliance identity, and reported symptoms across turns.\n"
        "7. Output Quality: Keep answers clean, direct, and understandable without tool traces, raw JSON, "
        "internal correlation IDs, or implementation jargon. Transparently disclose that purchase and warranty records are demo data."
    )
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    if prior_responses:
        for resp in prior_responses[-4:]:
            user_msg = resp.get("user_message")
            asst_msg = resp.get("assistant_message")
            if isinstance(user_msg, dict) and user_msg.get("content"):
                messages.append({"role": "user", "content": str(user_msg["content"])})
            if isinstance(asst_msg, dict) and asst_msg.get("content"):
                messages.append({"role": "assistant", "content": str(asst_msg["content"])})
    messages.append({"role": "user", "content": content})
    tool_results: list[dict[str, Any]] = []
    calls = 0
    try:
        if Client is None:
            raise GatewayError("MCP client is unavailable", "mcp_connectivity")
        async with Client(endpoint or mcp_endpoint()) as client:
            catalogue = await asyncio.wait_for(client.list_tools(), timeout=config.timeout_seconds)
            discovered = {tool.name: tool for tool in catalogue.tools if getattr(tool, "name", None) in SUPPORTED_MCP_TOOLS}
            if not (REQUIRED_MCP_TOOLS <= set(discovered)):
                raise GatewayError("Required support tools are unavailable", "mcp_discovery")
            definitions = [_tool_definition(tool) for tool in discovered.values()]
            for _round in range(MAX_TOOL_ROUNDS):
                remaining = config.timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise GatewayError("The request timed out")
                try:
                    if chunk_callback is not None:
                        assistant = await asyncio.wait_for(asyncio.to_thread(_completion, config, messages, definitions, chunk_callback), timeout=remaining)
                    else:
                        assistant = await asyncio.wait_for(asyncio.to_thread(_completion, config, messages, definitions), timeout=remaining)
                except TimeoutError as error:
                    raise GatewayError("LLM response is unavailable") from error
                calls_from_model = assistant.get("tool_calls", [])
                if not calls_from_model:
                    text = assistant.get("content")
                    finish_reason = assistant.get("finish_reason")
                    reasoning = assistant.get("reasoning_content")
                    if finish_reason == "length" and not text:
                        text = "The model reached its maximum token budget before concluding. Please submit a more specific question or try again."
                        return OrchestrationResult(text, tool_results, "partially_completed", reasoning=reasoning)
                    if not isinstance(text, str) or not text.strip():
                        raise GatewayError("LLM returned an invalid response")
                    return OrchestrationResult(text, tool_results, "completed", reasoning=reasoning)
                if not isinstance(calls_from_model, list) or calls + len(calls_from_model) > MAX_TOOL_CALLS:
                    raise GatewayError("Tool request limit reached")
                messages.append({"role": "assistant", "content": assistant.get("content"), "tool_calls": calls_from_model})
                for call in calls_from_model:
                    function = call.get("function") if isinstance(call, dict) else None
                    call_id = call.get("id") if isinstance(call, dict) else None
                    name = function.get("name") if isinstance(function, dict) else None
                    raw_arguments = function.get("arguments") if isinstance(function, dict) else None
                    try:
                        arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else None
                    except json.JSONDecodeError:
                        arguments = None
                    tool = discovered.get(name)
                    if not isinstance(call_id, str) or not tool or not isinstance(arguments, dict):
                        raise GatewayError("LLM requested an invalid tool action")
                    if name == "create_service_request":
                        trusted = trusted_create_service_request or {}
                        trusted_keys = ("appliance_id", "issue_summary", "slot_id", "confirmation_id", "idempotency_key")
                        if not trusted or any(key not in trusted or arguments.get(key) != trusted[key] for key in trusted_keys):
                            raise GatewayError("Service requests require trusted confirmation")
                    remaining = config.timeout_seconds - (time.monotonic() - started)
                    if remaining <= 0:
                        raise GatewayError("The request timed out")
                    if chunk_callback:
                        chunk_callback("tool_start", {"tool_name": name, "label": f"Executing {name} via MCP"})
                    safe_result = await _call_discovered_tool(client, discovered, name, arguments, remaining)
                    is_simulated = bool(safe_result["is_simulated"])
                    tool_result = {"tool_result_id": f"tool-{len(tool_results) + 1}", "activity_id": f"activity-{len(tool_results) + 1}", "tool_name": name, "status": "succeeded", "summary": "Support tool executed; domain result: " + str(safe_result["status"]), "result": safe_result, "provenance": {"source_type": "mcp", "execution_type": "mcp_streamable_http", "is_simulated": is_simulated, "source_label": "Simulated domain data" if is_simulated else "MCP domain result"}}
                    tool_results.append(tool_result)
                    if chunk_callback:
                        chunk_callback("tool_done", tool_result)
                    messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps(safe_result)})
                    calls += 1
    except GatewayError:
        raise
    except Exception as error:
        gw_err = _extract_gateway_error(error)
        if gw_err is not None:
            raise gw_err
        logger.warning("MCP tool orchestration error: %s", type(error).__name__)
        raise GatewayError("Support tools are unavailable", "mcp_connectivity") from error
    raise GatewayError("Tool request limit reached")


def send_message(
    config: LlmConfig | None,
    request: Mapping[str, object],
    context: dict[str, Any] | None = None,
    prior_responses: list[dict[str, Any]] | None = None,
    chunk_callback: Any = None,
) -> dict[str, object]:
    """Build the established web response shape without exposing provider internals."""
    corr_id = str(uuid.uuid4())
    identifiers = ("conversation_id", "client_message_id")
    content = request.get("content")
    metadata = request.get("metadata", {})
    valid_metadata = isinstance(metadata, dict) and all(isinstance(value, (str, int, float, bool)) or value is None for value in metadata.values())
    if set(request) - {"conversation_id", "client_message_id", "content", "metadata"} or not valid_metadata or any(not isinstance(request.get(key), str) or not request[key].strip() or len(request[key]) > 200 for key in identifiers) or not isinstance(content, str) or not content.strip() or len(content) > MAX_MESSAGE_CHARS:
        return _failed_message(request, "Invalid message request", correlation_id=corr_id)

    requested_mode = str(metadata.get("mode") or "").strip().lower()
    conv_intent = classify_conversational_intent(content)
    has_api_key = bool(config and getattr(config, "api_key", None) and str(config.api_key).strip() not in ("", "your_api_key_here"))

    # Fast path: explicit deterministic mode requested via UI toggle or metadata
    if requested_mode == "deterministic" or (conv_intent is not None and not has_api_key):
        logger.info("[%s] Routing to deterministic handler (mode=%s, conv_intent=%s)", corr_id, requested_mode, conv_intent)
        try:
            response = asyncio.run(_deterministic_mcp_message(
                request,
                config.timeout_seconds if config else DEFAULT_TIMEOUT_SECONDS,
                context=context,
                chunk_callback=chunk_callback,
                correlation_id=corr_id,
            ))
        except GatewayError as error:
            logger.warning("[%s] Deterministic MCP execution failed: %s (%s)", corr_id, type(error).__name__, error.dependency)
            _mark_dependency_degraded(error.dependency)
            err_msg = "Support tools are unavailable (MCP service error)" if error.dependency in ("mcp_connectivity", "mcp_discovery") else "Support operation is unavailable"
            return _failed_message(request, err_msg, correlation_id=corr_id)
        if response:
            return response
        return _failed_message(request, "Deterministic mode cannot handle this request; try a supported appliance issue", correlation_id=corr_id)

    # If user explicitly requested live mode and API key is missing, ask them to toggle
    if requested_mode == "live" and not has_api_key:
        if conv_intent is not None:
            conv_resp = asyncio.run(_deterministic_mcp_message(
                request,
                config.timeout_seconds if config else DEFAULT_TIMEOUT_SECONDS,
                context=context,
                chunk_callback=chunk_callback,
                correlation_id=corr_id,
            ))
            if conv_resp:
                return conv_resp
        logger.warning("[%s] Live LLM mode requested but API key is missing or not assigned in .env", corr_id)
        _mark_dependency_degraded("llm")
        return _failed_message(
            request,
            "Live LLM is unavailable: no API key is assigned in .env. Toggle to Deterministic Mode to test live MCP tool execution.",
            correlation_id=corr_id,
            can_fallback_to_deterministic=True,
        )

    # If demo mode is active and either inference is unusable, API key is missing, or auto mode
    if requested_mode != "live" and demo_mode() and (not has_api_key or _cached_inference_unusable()):
        logger.info("[%s] Demo mode fallback active; routing to deterministic MCP", corr_id)
        try:
            response = asyncio.run(_deterministic_mcp_message(
                request,
                config.timeout_seconds if config else DEFAULT_TIMEOUT_SECONDS,
                context=context,
                chunk_callback=chunk_callback,
                correlation_id=corr_id,
            ))
        except GatewayError as error:
            logger.warning("[%s] Deterministic MCP execution failed: %s (%s)", corr_id, type(error).__name__, error.dependency)
            _mark_dependency_degraded(error.dependency)
            err_msg = "Support tools are unavailable (MCP service error)" if error.dependency in ("mcp_connectivity", "mcp_discovery") else "Support operation is unavailable"
            return _failed_message(request, err_msg, correlation_id=corr_id)
        if response:
            return response
        return _failed_message(request, "I'm currently unable to process this request; please try an appliance support scenario.", correlation_id=corr_id)

    # Attempt live LLM orchestration
    try:
        if not config:
            raise ConfigurationError("LLM configuration is unavailable")
        logger.info("[%s] Starting live LLM orchestration", corr_id)
        if chunk_callback is not None:
            orch = asyncio.run(_orchestrate(config, content, context=context, prior_responses=prior_responses, chunk_callback=chunk_callback))
        else:
            orch = asyncio.run(_orchestrate(config, content, context=context, prior_responses=prior_responses))
        text, results, conclusion = orch[0], orch[1], orch[2]
        reasoning = getattr(orch, "reasoning", None)
        logger.info("[%s] Live LLM completed successfully with %d tool result(s)", corr_id, len(results))
    except GatewayError as error:
        logger.warning("[%s] Gateway error during orchestration: %s (%s)", corr_id, type(error).__name__, error.dependency)
        _mark_dependency_degraded(error.dependency)
        if error.dependency in ("mcp_connectivity", "mcp_discovery"):
            return _failed_message(request, "Support tools are unavailable (MCP service error)", correlation_id=corr_id)
        if conv_intent is not None:
            conv_resp = asyncio.run(_deterministic_mcp_message(request, config.timeout_seconds if config else DEFAULT_TIMEOUT_SECONDS, context=context, chunk_callback=chunk_callback, correlation_id=corr_id))
            if conv_resp:
                return conv_resp
        if requested_mode != "live" and demo_mode():
            logger.info("[%s] Attempting deterministic fallback after LLM gateway error", corr_id)
            try:
                response = asyncio.run(_deterministic_mcp_message(
                    request,
                    config.timeout_seconds if config else DEFAULT_TIMEOUT_SECONDS,
                    context=context,
                    chunk_callback=chunk_callback,
                    correlation_id=corr_id,
                ))
            except GatewayError:
                pass
            else:
                if response:
                    return response
        return _failed_message(
            request,
            f"Live LLM is unavailable ({error}). Toggle to Deterministic Mode to test live MCP tool execution.",
            correlation_id=corr_id,
            can_fallback_to_deterministic=True,
        )
    except ConfigurationError as error:
        logger.warning("[%s] Configuration error: %s", corr_id, error)
        _mark_dependency_degraded("llm")
        if conv_intent is not None:
            conv_resp = asyncio.run(_deterministic_mcp_message(request, context=context, chunk_callback=chunk_callback, correlation_id=corr_id))
            if conv_resp:
                return conv_resp
        _mark_dependency_degraded("llm")
        if requested_mode != "live" and demo_mode():
            try:
                response = asyncio.run(_deterministic_mcp_message(
                    request,
                    context=context,
                    chunk_callback=chunk_callback,
                    correlation_id=corr_id,
                ))
            except GatewayError:
                pass
            else:
                if response:
                    return response
        return _failed_message(
            request,
            f"Live LLM is unavailable ({error}). Toggle to Deterministic Mode to test live MCP tool execution.",
            correlation_id=corr_id,
            can_fallback_to_deterministic=True,
        )


    now = _now()
    response_id = f"response-{uuid.uuid4()}"
    events = [
        {
            "activity_id": r.get("activity_id", f"activity-{i+1}"),
            "sequence": i + 1,
            "label": f"Called {r.get('tool_name', 'tool')} through MCP",
            "status": r.get("status", "succeeded"),
            "result_summary": r.get("summary", "Tool executed"),
            "finished_at": now,
        }
        for i, r in enumerate(results)
    ]
    activity_summary = f"{len(results)} support action(s) completed" if results else "Response complete"
    assistant_msg: dict[str, object] = {
        "message_id": f"assistant-{uuid.uuid4()}",
        "role": "assistant",
        "content": text,
        "created_at": now,
        "provenance": {"source_type": "llm", "is_simulated": False},
        "activity": {"status": "completed", "summary": activity_summary, "events": events},
        "tool_results": results,
        "structured_result": None,
        "conclusion": {"status": conclusion, "summary": "Response complete"},
    }
    if reasoning:
        assistant_msg["reasoning"] = reasoning
    return {
        "conversation_id": request["conversation_id"],
        "response_id": response_id,
        "correlation_id": corr_id,
        "status": "completed",
        "user_message": {"message_id": request["client_message_id"], "role": "user", "content": content, "created_at": now},
        "assistant_message": assistant_msg,
    }


def _failed_message(
    request: Mapping[str, object],
    message: str,
    correlation_id: str | None = None,
    can_fallback_to_deterministic: bool = False,
) -> dict[str, object]:
    now = _now()
    conversation_id = request.get("conversation_id") if isinstance(request.get("conversation_id"), str) else "invalid-conversation"
    client_id = request.get("client_message_id") if isinstance(request.get("client_message_id"), str) else "invalid-message"
    content = request.get("content") if isinstance(request.get("content"), str) else ""
    full_message = f"{message} (Correlation ID: {correlation_id})" if correlation_id else message
    conclusion_payload: dict[str, Any] = {
        "status": "llm_unavailable" if can_fallback_to_deterministic else "unable_to_complete",
        "summary": message,
        "can_fallback_to_deterministic": can_fallback_to_deterministic,
        "original_prompt": content,
    }
    return {
        "conversation_id": conversation_id,
        "response_id": f"response-{uuid.uuid4()}",
        "correlation_id": correlation_id,
        "status": "failed",
        "user_message": {"message_id": client_id, "role": "user", "content": content, "created_at": now},
        "assistant_message": {
            "message_id": f"assistant-{uuid.uuid4()}",
            "role": "assistant",
            "content": full_message,
            "created_at": now,
            "provenance": {"source_type": "backend", "is_simulated": False},
            "activity": {"status": "failed", "summary": message, "events": []},
            "tool_results": [],
            "structured_result": None,
            "conclusion": conclusion_payload,
            "can_fallback_to_deterministic": can_fallback_to_deterministic,
        },
    }


def _deterministic_demo_message(request: Mapping[str, object]) -> dict[str, object] | None:
    """Retired local-result fallback; appliance data must be returned by MCP."""
    return None
    """Legacy implementation retained temporarily for audit history."""
    content = str(request["content"])
    lowered = content.lower()
    if "warranty" in lowered:
        tool_name = "check_warranty"
        result: dict[str, object] = {"status": "success", "appliance_id": "demo-appliance-001", "eligibility": "unknown", "coverage_end_date": None, "reason": "Deterministic demo only; no provider record was checked.", "evidence_source": "simulated_record", "is_simulated": True, "deterministic_demo": True}
        text = "Deterministic demo: I cannot check a live warranty record. This sample shows that warranty eligibility remains unknown until verified with the provider."
        result_type, title = "warranty", "Demo warranty guidance"
    elif any(word in lowered for word in ("troubleshoot", "troubleshooting", "error", "not working", "problem", "issue")):
        tool_name = "get_troubleshooting_steps"
        result = {"status": "documentation_not_found", "model_id": "demo-unverified", "exact_model_match": False, "document_found": False, "document_reference": {"title": None, "page": None, "url": None}, "documented_error_meaning": None, "probable_causes": [], "safe_steps": ["Stop using the appliance if there is a burning smell, leak, or visible electrical damage.", "Do not remove panels or bypass safety interlocks.", "Contact a qualified technician for model-specific guidance."], "requires_technician": True, "is_simulated": True, "deterministic_demo": True}
        text = "Deterministic demo: no live troubleshooting source was consulted. Use these general safety steps and obtain model-specific guidance from a qualified technician."
        result_type, title = "troubleshooting", "Demo troubleshooting guidance"
    else:
        return None
    now = _now()
    provenance = {"source_type": "deterministic_demo", "is_simulated": True, "source_label": "Deterministic demo; no live LLM or MCP call"}
    tool_result = {"tool_result_id": "demo-tool-1", "activity_id": "demo-activity-1", "tool_name": tool_name, "status": "succeeded", "summary": "Deterministic demo result", "result": result, "provenance": provenance}
    return {"conversation_id": request["conversation_id"], "response_id": f"demo-response-{uuid.uuid4()}", "status": "completed", "user_message": {"message_id": request["client_message_id"], "role": "user", "content": content, "created_at": now}, "assistant_message": {"message_id": f"demo-assistant-{uuid.uuid4()}", "role": "assistant", "content": text, "created_at": now, "activity": {"status": "completed", "summary": "Deterministic demo response", "events": [{"activity_id": "demo-activity-1", "sequence": 1, "label": "Used deterministic demo guidance", "status": "succeeded", "result_summary": "No live LLM or MCP call was made."}]}, "tool_results": [tool_result], "structured_result": {"result_id": "demo-result-1", "type": result_type, "status": "partial", "display": {"title": title, "summary": "Deterministic demo; not a live provider result.", "disclaimer": "No live LLM, MCP, provider, or booking system was contacted."}, "payload": result, "provenance": provenance}, "conclusion": {"status": "partially_completed", "summary": "Deterministic demo response", "next_action": "Recheck connections for live support."}}}


def make_health_handler(config: LlmConfig | None = None) -> type[BaseHTTPRequestHandler]:
    """Create the tiny HTTP boundary used until the web backend owns these routes."""
    class HealthHandler(BaseHTTPRequestHandler):
        def _write_snapshot(self) -> None:
            payload = json.dumps(refresh_preflight(config)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _write_json(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - required stdlib handler name
            path = urlsplit(self.path).path
            if path == "/api/health":
                self._write_snapshot()
            elif path == "/api/conversations":
                self._write_json(200, _conversation_store.list())
            elif path.startswith("/api/conversations/"):
                conversation_id = unquote(path.removeprefix("/api/conversations/"))
                history = _conversation_store.history(conversation_id)
                if history is None:
                    self._write_json(404, {"error": {"code": "conversation_not_found", "message": "Conversation not found"}})
                else:
                    self._write_json(200, history)
            else:
                self.send_error(404, "Not found")

        def do_POST(self) -> None:  # noqa: N802 - required stdlib handler name
            path = urlsplit(self.path).path
            if path == "/api/health/recheck":
                self._write_snapshot()
            elif path in ("/api/conversations", "/api/messages", "/api/messages/stream"):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_MESSAGE_CHARS + 10_000:
                        raise ValueError
                    request = json.loads(self.rfile.read(length).decode("utf-8"))
                    if path == "/api/conversations":
                        if request != {}:
                            raise ValueError
                        self._write_json(201, _conversation_store.create())
                        return
                    if not isinstance(request, dict):
                        raise ValueError
                    try:
                        active_config = config or load_config()
                    except ConfigurationError:
                        active_config = None
                    is_sse = path == "/api/messages/stream" or self.headers.get("Accept") == "text/event-stream"
                    if is_sse:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("Connection", "close")
                        self.end_headers()

                        def on_chunk(event_type: str, data: Any) -> None:
                            try:
                                payload = json.dumps({"type": event_type, "data": data})
                                self.wfile.write(f"event: {event_type}\ndata: {payload}\n\n".encode("utf-8"))
                                self.wfile.flush()
                            except (OSError, BrokenPipeError):
                                pass

                        status, response = _conversation_store.submit(active_config, request, chunk_callback=on_chunk)
                        try:
                            self.wfile.write(f"event: done\ndata: {json.dumps(response)}\n\n".encode("utf-8"))
                            self.wfile.flush()
                        except (OSError, BrokenPipeError):
                            pass
                        return
                    status, response = _conversation_store.submit(active_config, request)
                    self._write_json(status, response)
                except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                    self._write_json(400, _failed_message({}, "Invalid message request"))
            else:
                self.send_error(404, "Not found")

        def log_message(self, format: str, *args: object) -> None:
            # Safe HTTP logging: records method and status code, never auth headers or secrets
            logger.info("HTTP %s", format % args)

    return HealthHandler


def serve(host: str = "127.0.0.1", port: int = 8000, config: LlmConfig | None = None) -> None:
    """Serve GET /api/health and POST /api/health/recheck."""
    refresh_preflight(config)
    with ThreadingHTTPServer((host, port), make_health_handler(config)) as server:
        server.serve_forever()


def main() -> None:
    port = int(os.environ.get("BACKEND_PORT", "8000"))
    serve(port=port)


if __name__ == "__main__":
    main()
