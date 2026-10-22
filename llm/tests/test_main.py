import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.request import Request, urlopen

from llm.main import ConfigurationError, ConversationStore, GatewayError, LlmConfig, _mark_dependency_degraded, _orchestrate, _stream_completion, _valid_assistant_response, chat_completions_url, demo_mode, load_config, make_health_handler, mcp_endpoint, preflight, probe_mcp, send_message, validate_config


class GatewayTests(unittest.TestCase):
    def test_chat_url_handles_both_supported_base_forms(self):
        self.assertEqual(chat_completions_url("https://api.example.test/v1"), "https://api.example.test/v1/chat/completions")
        self.assertEqual(chat_completions_url("https://api.example.test"), "https://api.example.test/v1/chat/completions")

    def test_invalid_response_is_not_healthy(self):
        self.assertFalse(_valid_assistant_response({"choices": [{"message": {"role": "assistant", "content": ""}}]}))
        self.assertTrue(_valid_assistant_response({"choices": [{"message": {"role": "assistant", "content": "OK"}}]}))

    def test_config_does_not_accept_missing_secret(self):
        with self.assertRaises(ConfigurationError):
            validate_config(LlmConfig("openai_compatible", "https://api.example.test/v1", "", "model", "chat_completions"))

    def test_config_comes_from_the_given_project_root(self):
        config = load_config(project_dir=Path("missing-test-root"), environ={
            "LLM_PROVIDER": "openai_compatible", "LLM_BASE_URL": "https://api.example.test",
            "LLM_API_KEY": "secret", "LLM_MODEL": "model", "LLM_API_MODE": "chat_completions",
        })
        self.assertEqual(config.model, "model")

    def test_missing_config_returns_safe_snapshot(self):
        snapshot = preflight(LlmConfig("openai_compatible", "https://api.example.test/v1", "", "model", "chat_completions"))
        self.assertEqual(snapshot["services"]["llm_connectivity"]["status"], "unavailable")
        self.assertEqual(snapshot["services"]["web_backend"]["status"], "healthy")
        self.assertNotIn("secret", str(snapshot))

    def test_health_route_returns_the_safe_snapshot(self):
        config = LlmConfig("openai_compatible", "https://api.example.test/v1", "", "model", "chat_completions")
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_health_handler(config))
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            with urlopen(f"http://127.0.0.1:{server.server_port}/api/health") as response:
                self.assertEqual(response.status, 200)
                self.assertIn(b'"mcp_connectivity"', response.read())
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_mcp_probe_requires_the_exact_six_tools(self):
        class ClientWithTools:
            def __init__(self, _endpoint):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def list_tools(self):
                return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in (
                    "find_product", "get_troubleshooting_steps", "check_warranty",
                    "find_service_slots", "create_service_request", "get_case_status",
                )])

        with patch("llm.main.Client", ClientWithTools):
            connectivity, discovery = probe_mcp("http://localhost:8001/mcp")
        self.assertEqual(connectivity.status, "healthy")
        self.assertEqual(discovery.status, "healthy")

    def test_mcp_endpoint_uses_the_shared_port(self):
        self.assertEqual(mcp_endpoint({"MCP_PORT": "8123"}), "http://localhost:8123/mcp")

    def test_tool_loop_preserves_provider_call_id(self):
        schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False}
        tools = [SimpleNamespace(name=name, description="", inputSchema=schema) for name in ("find_product", "get_troubleshooting_steps", "check_warranty", "find_service_slots", "create_service_request", "get_case_status")]
        calls = []

        class FakeClient:
            def __init__(self, _endpoint): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *_args): return None
            async def list_tools(self): return SimpleNamespace(tools=tools)
            async def call_tool(self, name, arguments):
                calls.append((name, arguments))
                return SimpleNamespace(structuredContent={"status": "success", "matches": [], "is_simulated": True})

        config = LlmConfig("openai_compatible", "https://api.example.test/v1", "key", "model", "chat_completions", 5)
        responses = [{"role": "assistant", "content": None, "tool_calls": [{"id": "provider-call-1", "function": {"name": "find_product", "arguments": '{"query":"washer"}'}}]}, {"role": "assistant", "content": "Found it."}]
        with patch("llm.main.Client", FakeClient), patch("llm.main._completion", side_effect=responses):
            text, results, status = asyncio.run(_orchestrate(config, "Find a washer"))
        self.assertEqual((text, status, calls), ("Found it.", "completed", [("find_product", {"query": "washer"})]))
        self.assertEqual(results[0]["tool_name"], "find_product")

    def test_unavailable_live_warranty_uses_mcp_result(self):
        config = LlmConfig("openai_compatible", "https://api.example.test/v1", "", "model", "chat_completions")
        actual = {"status": "success", "appliance_id": "appliance-001", "eligibility": "eligible", "coverage_end_date": "2027-12-31", "reason": "Verified simulated fixture", "evidence_source": "simulated_record", "is_simulated": True}
        with patch("llm.main._execute_mcp_tool", return_value=actual) as execute:
            response = send_message(config, {"conversation_id": "conversation-1", "client_message_id": "message-1", "content": "Please check my warranty"})
        result = response["assistant_message"]["tool_results"][0]
        self.assertEqual(response["status"], "completed")
        self.assertEqual(execute.call_args.args[:2], ("check_warranty", {"appliance_id": "appliance-001"}))
        self.assertEqual(result["provenance"]["source_type"], "mcp")
        self.assertTrue(result["result"]["is_simulated"])
        self.assertEqual(response["assistant_message"]["provenance"]["source_type"], "deterministic")

    def test_unrecognised_request_does_not_get_a_demo_answer(self):
        config = LlmConfig("openai_compatible", "https://api.example.test/v1", "", "model", "chat_completions")
        response = send_message(config, {"conversation_id": "conversation-1", "client_message_id": "message-1", "content": "Book a repair tomorrow"})
        self.assertEqual(response["status"], "failed")

    def test_demo_mode_uses_root_env_unless_process_overrides(self):
        with patch("llm.main._read_dotenv", return_value={"DEMO_MODE": "false"}):
            self.assertFalse(demo_mode({}))
            self.assertTrue(demo_mode({"DEMO_MODE": "true"}))

    def test_mcp_failure_does_not_overwrite_llm_health(self):
        snapshot = {"overall_status": "healthy", "mode": "live", "services": {"llm_inference": {"status": "healthy"}, "mcp_connectivity": {"status": "healthy"}, "mcp_discovery": {"status": "healthy"}}}
        with patch("llm.main._cached_health", snapshot):
            _mark_dependency_degraded("mcp_connectivity")
        self.assertEqual(snapshot["services"]["llm_inference"]["status"], "healthy")
        self.assertEqual(snapshot["services"]["mcp_connectivity"]["status"], "degraded")

    def test_http_message_demo_and_safe_failure_with_health_snapshot(self):
        config = LlmConfig("openai_compatible", "https://api.example.test/v1", "", "model", "chat_completions")
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_health_handler(config))
        thread = Thread(target=server.serve_forever)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(f"{base}/api/health") as response:
                self.assertIn("services", json.loads(response.read()))
            def post(content):
                with urlopen(Request(f"{base}/api/conversations", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")) as response:
                    conversation_id = json.loads(response.read())["conversation_id"]
                body = json.dumps({"conversation_id": conversation_id, "client_message_id": "message-1", "content": content}).encode()
                with urlopen(Request(f"{base}/api/messages", data=body, headers={"Content-Type": "application/json"}, method="POST")) as response:
                    return json.loads(response.read())
            self.assertEqual(post("Please check warranty")["status"], "failed")
            self.assertEqual(post("Book a repair tomorrow")["status"], "failed")
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_cached_unavailable_inference_bypasses_live_orchestration(self):
        config = LlmConfig("openai_compatible", "https://api.example.test/v1", "key", "model", "chat_completions")
        snapshot = {"services": {"llm_inference": {"status": "unavailable"}}}
        request = {"conversation_id": "conversation-1", "client_message_id": "message-1", "content": "Check my warranty"}
        actual = {"status": "success", "appliance_id": "appliance-001", "eligibility": "unknown", "coverage_end_date": None, "reason": "Fixture", "evidence_source": "simulated_record", "is_simulated": True}
        with patch("llm.main._cached_health", snapshot), patch("llm.main.demo_mode", return_value=True), patch("llm.main._execute_mcp_tool", return_value=actual) as execute, patch("llm.main._orchestrate") as orchestrate:
            response = send_message(config, request)
        orchestrate.assert_not_called()
        execute.assert_called_once()
        self.assertEqual(response["assistant_message"]["tool_results"][0]["provenance"]["source_type"], "mcp")

    def test_live_failure_degrades_llm_before_curated_fallback(self):
        config = LlmConfig("openai_compatible", "https://api.example.test/v1", "key", "model", "chat_completions")
        snapshot = {"overall_status": "healthy", "mode": "live", "services": {"llm_inference": {"status": "healthy"}, "mcp_connectivity": {"status": "healthy"}}}
        request = {"conversation_id": "conversation-1", "client_message_id": "message-1", "content": "Troubleshoot this error"}
        actual = {"status": "documentation_not_found", "model_id": "WC-500", "document_found": False, "document_reference": {}, "probable_causes": [], "safe_steps": ["Stop using it"], "is_simulated": True}
        with patch("llm.main._cached_health", snapshot), patch("llm.main.demo_mode", return_value=True), patch("llm.main._orchestrate", side_effect=GatewayError("LLM unavailable")), patch("llm.main._execute_mcp_tool", return_value=actual):
            response = send_message(config, request)
        self.assertEqual(response["status"], "completed")
        self.assertEqual(response["assistant_message"]["tool_results"][0]["result"], actual)
        self.assertEqual(response["assistant_message"]["provenance"]["source_type"], "deterministic")
        self.assertEqual(snapshot["services"]["llm_inference"]["status"], "degraded")
        self.assertEqual(snapshot["services"]["mcp_connectivity"]["status"], "healthy")

    def test_canonical_llm_config_loading_and_validation(self):
        config = load_config(project_dir=Path("missing"), environ={
            "LLM_PROVIDER": "openai_compatible",
            "LLM_BASE_URL": "https://api.openai.com/v1",
            "LLM_API_KEY": "test-key",
            "LLM_MODEL": "gpt-4o-mini",
            "LLM_API_MODE": "chat_completions",
            "LLM_TEMPERATURE": "1.0",
            "LLM_TOP_P": "0.95",
            "LLM_MAX_TOKENS": "512",
            "LLM_ENABLE_THINKING": "true",
            "LLM_REASONING_BUDGET": "512",
            "LLM_STREAMING": "true",
        })
        self.assertEqual(config.temperature, 1.0)
        self.assertEqual(config.top_p, 0.95)
        self.assertEqual(config.max_tokens, 512)
        self.assertTrue(config.enable_thinking)
        self.assertEqual(config.reasoning_budget, 512)
        self.assertTrue(config.streaming)

        with self.assertRaises(ConfigurationError):
            validate_config(LlmConfig("openai_compatible", "https://api.test/v1", "k", "m", "chat_completions", 5, temperature=3.0))
        with self.assertRaises(ConfigurationError):
            validate_config(LlmConfig("openai_compatible", "https://api.test/v1", "k", "m", "chat_completions", 5, top_p=1.5))
        with self.assertRaises(ConfigurationError):
            validate_config(LlmConfig("openai_compatible", "https://api.test/v1", "k", "m", "chat_completions", 5, max_tokens=0))
        with self.assertRaises(ConfigurationError):
            validate_config(LlmConfig("openai_compatible", "https://api.test/v1", "k", "m", "chat_completions", 5, reasoning_budget=-1))

    def test_streaming_chunk_assembly_keeps_reasoning_and_content_separate(self):
        config = LlmConfig("openai_compatible", "https://api.test/v1", "key", "model", "chat_completions")
        chunks = [
            b"data: {}\n\n",  # empty chunk without choices
            b'data: {"choices": [{"delta": {"reasoning_content": "<think>Analyzing user model\\n"}, "finish_reason": null}]}\n\n',
            b'data: {"choices": [{"delta": {"reasoning_content": "Found warranty criteria</think>"}, "finish_reason": null}]}\n\n',
            b'data: {"choices": [{"delta": {"content": "Your appliance "}, "finish_reason": null}]}\n\n',
            b'data: {"choices": [{"delta": {"content": "is covered."}, "finish_reason": "stop"}]}\n\n',
            b"data: [DONE]\n\n",
        ]
        class MockStreamResponse:
            def __enter__(self): return chunks
            def __exit__(self, *_args): pass

        collected_events = []
        def callback(event_type, delta):
            collected_events.append((event_type, delta))

        with patch("llm.main.urlopen", return_value=MockStreamResponse()):
            msg = _stream_completion(config, [{"role": "user", "content": "hi"}], chunk_callback=callback)

        self.assertEqual(msg["role"], "assistant")
        self.assertEqual(msg["content"], "Your appliance is covered.")
        self.assertEqual(msg["reasoning_content"], "Analyzing user model\nFound warranty criteria")
        self.assertNotIn("<think>", msg["reasoning_content"])
        self.assertNotIn("Analyzing", msg["content"])
        self.assertEqual(len(collected_events), 4)

    def test_streaming_tool_call_assembly(self):
        config = LlmConfig("openai_compatible", "https://api.test/v1", "key", "model", "chat_completions")
        chunks = [
            b'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "tc-1", "type": "function", "function": {"name": "check_warranty", "arguments": "{\\"appliance_id\\": "}}]}, "finish_reason": null}]}\n\n',
            b'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "\\"appl-1\\"}"}}]}, "finish_reason": "tool_calls"}]}\n\n',
            b"data: [DONE]\n\n",
        ]
        class MockStreamResponse:
            def __enter__(self): return chunks
            def __exit__(self, *_args): pass

        with patch("llm.main.urlopen", return_value=MockStreamResponse()):
            msg = _stream_completion(config, [{"role": "user", "content": "check"}])

        self.assertEqual(len(msg["tool_calls"]), 1)
        self.assertEqual(msg["tool_calls"][0]["id"], "tc-1")
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "check_warranty")
        self.assertEqual(msg["tool_calls"][0]["function"]["arguments"], '{"appliance_id": "appl-1"}')

    def test_truncation_on_length_handled_truthfully(self):
        tools = [SimpleNamespace(name=name, description="", inputSchema={}) for name in ("find_product", "get_troubleshooting_steps", "check_warranty", "find_service_slots", "create_service_request", "get_case_status")]
        class FakeClient:
            def __init__(self, _endpoint): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *_args): return None
            async def list_tools(self): return SimpleNamespace(tools=tools)

        config = LlmConfig("openai_compatible", "https://api.test/v1", "key", "model", "chat_completions")
        fake_response = {"role": "assistant", "content": None, "finish_reason": "length", "reasoning_content": "budget exhausted"}
        with patch("llm.main._completion", return_value=fake_response), patch("llm.main.Client", FakeClient):
            res = asyncio.run(_orchestrate(config, "long query"))
            text, results, status = res
            self.assertEqual(status, "partially_completed")
            self.assertIn("maximum token budget", text)
            self.assertEqual(res.reasoning, "budget exhausted")

    def test_sse_streaming_endpoint(self):
        config = LlmConfig("openai_compatible", "https://api.test/v1", "", "model", "chat_completions")
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_health_handler(config))
        thread = Thread(target=server.serve_forever)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        snapshot = {"services": {"llm_inference": {"status": "unavailable"}}}
        try:
            with urlopen(Request(f"{base}/api/conversations", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")) as resp:
                cid = json.loads(resp.read())["conversation_id"]

            req_body = json.dumps({"conversation_id": cid, "client_message_id": "sse-msg-1", "content": "warranty"}).encode()
            actual_tool = {"status": "success", "appliance_id": "appliance-001", "eligibility": "eligible", "coverage_end_date": "2027-12-31", "evidence_source": "mcp", "is_simulated": True}
            with patch("llm.main._cached_health", snapshot), patch("llm.main._execute_mcp_tool", return_value=actual_tool):
                req = Request(f"{base}/api/messages/stream", data=req_body, headers={"Content-Type": "application/json", "Accept": "text/event-stream"}, method="POST")
                with urlopen(req) as resp:
                    lines = []
                    for raw_line in resp:
                        line = raw_line.decode("utf-8")
                        lines.append(line)
                        if "event: done" in line:
                            lines.append(resp.readline().decode("utf-8"))
                            break
                    raw = "".join(lines)
                    self.assertIn("event: done", raw)
                    self.assertIn("appliance-001", raw)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()



class ConversationHistoryTests(unittest.TestCase):
    @staticmethod
    def response(_config, request, *args, **kwargs):
        simulated = request["content"] == "demo"
        now = "2026-01-01T00:00:00Z"
        return {"conversation_id": request["conversation_id"], "response_id": f"response-{request['client_message_id']}", "status": "completed", "user_message": {"message_id": request["client_message_id"], "role": "user", "content": request["content"], "created_at": now}, "assistant_message": {"message_id": f"assistant-{request['client_message_id']}", "role": "assistant", "content": "ok", "created_at": now, "activity": {"status": "completed", "summary": "done", "events": [{"sequence": 1}]}, "tool_results": [], "structured_result": {"provenance": {"source_type": "deterministic_demo" if simulated else "mcp", "is_simulated": simulated}}, "conclusion": {"status": "completed"}}}

    def test_isolation_order_idempotence_and_provenance(self):
        store = ConversationStore()
        first, second = store.create(), store.create()
        self.assertNotEqual(first["conversation_id"], second["conversation_id"])
        self.assertEqual(first["persistence"], {"type": "process_memory", "survives_backend_restart": False})
        with patch("llm.main.send_message", side_effect=self.response) as send:
            req = {"conversation_id": first["conversation_id"], "content": "live", "client_message_id": "m1"}
            _, live = store.submit(None, req)
            _, retry = store.submit(None, req)
            self.assertEqual(retry["response_id"], live["response_id"])
            self.assertEqual(store.submit(None, {**req, "content": "different"})[0], 409)
            store.submit(None, {**req, "client_message_id": "m2", "content": "demo"})
            isolated = store.submit(None, {"conversation_id": second["conversation_id"], "client_message_id": "other", "content": "live"})[1]
        self.assertEqual(send.call_count, 3)
        history = store.history(first["conversation_id"])
        self.assertEqual([x["response_id"] for x in history["responses"]], ["response-m1", "response-m2"])
        self.assertIn("activity", history["responses"][0]["assistant_message"])
        self.assertFalse(history["responses"][0]["assistant_message"]["structured_result"]["provenance"]["is_simulated"])
        self.assertTrue(history["responses"][1]["assistant_message"]["structured_result"]["provenance"]["is_simulated"])
        self.assertEqual(store.history(second["conversation_id"])["responses"], [isolated])
        self.assertIsNone(store.history("unknown"))

    def test_http_conversation_contract_and_history_restore(self):
        store = ConversationStore()
        with patch("llm.main._conversation_store", store), patch("llm.main.send_message", side_effect=self.response):
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_health_handler(None))
            thread = Thread(target=server.serve_forever)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(Request(f"{base}/api/conversations", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")) as response:
                    created = json.loads(response.read())
                for ident in ("one", "two"):
                    body = json.dumps({"conversation_id": created["conversation_id"], "client_message_id": ident, "content": ident}).encode()
                    urlopen(Request(f"{base}/api/messages", data=body, headers={"Content-Type": "application/json"}, method="POST")).close()
                with urlopen(f"{base}/api/conversations/{created['conversation_id']}") as response:
                    restored = json.loads(response.read())
                self.assertEqual([x["response_id"] for x in restored["responses"]], ["response-one", "response-two"])
                self.assertFalse(restored["persistence"]["survives_backend_restart"])
            finally:
                server.shutdown()
                thread.join()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
