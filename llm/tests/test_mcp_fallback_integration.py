"""End-to-end checks for deterministic backend requests against real MCP HTTP."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import socket
import subprocess
import sys
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.request import Request, urlopen

from llm.main import Client, ConversationStore, LlmConfig, make_health_handler


ROOT = Path(__file__).resolve().parents[2]
MCP_ENTRY = ROOT / "mcp" / "main.py"
DISABLED_LLM = LlmConfig("openai_compatible", "http://127.0.0.1:1/v1", "", "disabled", "chat_completions", 1)


def unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class DeterministicMcpHttpIntegrationTests(unittest.TestCase):
    """No MCP client, tool, or transport behavior is mocked in these tests."""

    @classmethod
    def setUpClass(cls) -> None:
        if Client is None:
            raise unittest.SkipTest("MCP Python SDK is not installed in the test environment")
        cls.mcp_port = unused_port()
        cls.server_log: queue.Queue[str] = queue.Queue()
        child_env = os.environ.copy()
        child_env["MCP_PORT"] = str(cls.mcp_port)
        cls.mcp_process = subprocess.Popen(
            [sys.executable, str(MCP_ENTRY)],
            cwd=ROOT,
            env=child_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        def collect_server_log() -> None:
            assert cls.mcp_process.stderr is not None
            for line in cls.mcp_process.stderr:
                cls.server_log.put(line.strip())

        cls.log_thread = Thread(target=collect_server_log, daemon=True)
        cls.log_thread.start()
        endpoint = f"http://127.0.0.1:{cls.mcp_port}/mcp"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if cls.mcp_process.poll() is not None:
                raise RuntimeError("The MCP server process exited during startup")
            try:
                async def discover() -> None:
                    async with Client(endpoint) as client:
                        tools = await client.list_tools()
                    names = {tool.name for tool in tools.tools}
                    if not {"check_warranty", "get_troubleshooting_steps"} <= names:
                        raise AssertionError("Required demo tools were not discovered")

                asyncio.run(discover())
                break
            except Exception:
                time.sleep(0.1)
        else:
            cls.mcp_process.terminate()
            cls.mcp_process.wait(timeout=5)
            raise RuntimeError("The MCP Streamable HTTP endpoint did not start")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.mcp_process.terminate()
        try:
            cls.mcp_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.mcp_process.kill()
            cls.mcp_process.wait(timeout=5)

    def setUp(self) -> None:
        self.store_patch = patch("llm.main._conversation_store", ConversationStore())
        self.store_patch.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_health_handler(DISABLED_LLM))
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.environment = patch.dict(os.environ, {"DEMO_MODE": "true", "MCP_PORT": str(self.mcp_port)})
        self.environment.start()
        self.conversation_id = self._create_conversation()

    def tearDown(self) -> None:
        self.environment.stop()
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.store_patch.stop()

    def _request_json(self, path: str, body: dict[str, object] | None = None) -> dict[str, object]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Accept": "application/json", **({"Content-Type": "application/json"} if data is not None else {})},
            method="POST" if data is not None else "GET",
        )
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    def _create_conversation(self) -> str:
        result = self._request_json("/api/conversations", {})
        return str(result["conversation_id"])

    def _send(self, client_message_id: str, content: str) -> dict[str, object]:
        return self._request_json("/api/messages", {
            "conversation_id": self.conversation_id,
            "client_message_id": client_message_id,
            "content": content,
        })

    def _assert_server_executed(self, tool_name: str) -> None:
        expected = f"MCP_TOOL_EXECUTED tool={tool_name}"
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                line = self.server_log.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if line == expected:
                return
        self.fail(f"No server-side execution record received for {tool_name}")

    def _assert_deterministic_mcp_provenance(self, response: dict[str, object], tool_name: str) -> dict[str, object]:
        assistant = response["assistant_message"]
        self.assertIsInstance(assistant, dict)
        results = assistant["tool_results"]
        self.assertIsInstance(results, list)
        tool_result = next((item for item in results if item.get("tool_name") == tool_name), None)
        self.assertIsNotNone(tool_result, f"Expected the real {tool_name} result in the response")
        provenance = tool_result["provenance"]
        self.assertEqual(provenance.get("source_type"), "mcp")
        self.assertTrue(provenance.get("is_simulated"), "MCP domain records are synthetic even though MCP executed")
        assistant_provenance = assistant.get("provenance")
        self.assertIsInstance(assistant_provenance, dict, "Response must identify how its text was produced")
        self.assertEqual(assistant_provenance.get("source_type"), "deterministic")
        self.assertFalse(assistant_provenance.get("is_simulated"), "Scripted response text is distinct from synthetic domain data")
        return tool_result

    def test_llm_disabled_warranty_uses_real_mcp_result(self) -> None:
        response = self._send("warranty-message", "Check the warranty for my WhirlClean WC-500 washing machine")
        self._assert_server_executed("check_warranty")
        self.assertEqual(response["status"], "completed")
        result = self._assert_deterministic_mcp_provenance(response, "check_warranty")["result"]
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["appliance_id"], "appliance-001")
        self.assertEqual(result["eligibility"], "eligible")
        self.assertEqual(result["coverage_end_date"], "2027-12-31")

    def test_llm_disabled_troubleshooting_uses_real_mcp_result_without_fake_citations(self) -> None:
        response = self._send("troubleshooting-message", "Troubleshoot the WC-500 washing machine E21 error")
        self._assert_server_executed("get_troubleshooting_steps")
        self.assertEqual(response["status"], "completed")
        result = self._assert_deterministic_mcp_provenance(response, "get_troubleshooting_steps")["result"]
        self.assertEqual(result["status"], "documentation_not_found")
        self.assertTrue(result["exact_model_match"])
        self.assertEqual(result["model_id"], "WC-500")
        self.assertIsNone(result["document_reference"]["title"])
        self.assertEqual(len(result["probable_causes"]), 2)

    def test_mcp_unavailable_fails_without_substituting_local_domain_data(self) -> None:
        with patch.dict(os.environ, {"MCP_PORT": str(unused_port())}):
            response = self._send("mcp-down-message", "Check the warranty for my WhirlClean WC-500")
        self.assertEqual(response["status"], "failed")
        assistant = response["assistant_message"]
        self.assertEqual(assistant["tool_results"], [])
        self.assertIsNone(assistant["structured_result"])

    def test_all_six_registered_tools_are_discoverable_and_invokable_over_http(self) -> None:
        async def exercise() -> dict[str, object]:
            async with Client(f"http://127.0.0.1:{self.mcp_port}/mcp") as client:
                catalogue = await client.list_tools()
                names = {tool.name for tool in catalogue.tools}
                self.assertEqual(names, {"find_product", "get_troubleshooting_steps", "check_warranty", "find_service_slots", "create_service_request", "get_case_status", "get_appliance_inventory"})
                results = {}
                calls = {
                    "find_product": {"query": "WC-500"},
                    "get_troubleshooting_steps": {"model_id": "WC-500", "symptom": "E21"},
                    "check_warranty": {"appliance_id": "appliance-001"},
                    "find_service_slots": {"appliance_id": "appliance-001", "location": "demo", "date_from": "2026-10-23", "date_to": "2026-10-23"},
                    "create_service_request": {"appliance_id": "appliance-001", "issue_summary": "test only", "confirmation_id": "invalid", "idempotency_key": "integration-no-create"},
                    "get_case_status": {"request_id": "unknown-integration-case"},
                    "get_appliance_inventory": {"appliance_type": "refrigerator"},
                }
                for name, arguments in calls.items():
                    results[name] = await client.call_tool(name, arguments)
                return results

        result = asyncio.run(exercise())
        self.assertEqual(set(result), {"find_product", "get_troubleshooting_steps", "check_warranty", "find_service_slots", "create_service_request", "get_case_status", "get_appliance_inventory"})
        self._assert_server_executed("get_troubleshooting_steps")
        self._assert_server_executed("check_warranty")
        self._assert_server_executed("get_appliance_inventory")

    def test_scenario_a_washing_machine_error_e21(self) -> None:
        response = self._send("scen-a", "My washing machine shows error code E21. What does it mean, and what safe next step should I take?")
        self._assert_server_executed("get_troubleshooting_steps")
        self.assertEqual(response["status"], "completed")
        result = self._assert_deterministic_mcp_provenance(response, "get_troubleshooting_steps")["result"]
        self.assertEqual(result["status"], "documentation_not_found")
        self.assertIsNone(result["document_reference"]["title"])
        self.assertIsNone(result["document_reference"]["url"])
        self.assertTrue(len(result["safe_steps"]) > 0)
        assistant = response["assistant_message"]
        self.assertIn("Safe", assistant["content"])
        self.assertNotIn(assistant["content"], assistant["structured_result"]["display"]["summary"])

    def test_scenario_b_refrigerator_not_cooling(self) -> None:
        # Step 1: User reports refrigerator not cooling. HomeCare searches inventory and offers candidate.
        response = self._send("scen-b-1", "My refrigerator is not cooling. Help me check this safely and tell me when to stop and call a technician.")
        self._assert_server_executed("get_appliance_inventory")
        self.assertEqual(response["status"], "completed")
        result = self._assert_deterministic_mcp_provenance(response, "get_appliance_inventory")["result"]
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["total_count"], 1)
        self.assertEqual(result["records"][0]["appliance_id"], "demo-appl-fridge-001")
        assistant = response["assistant_message"]
        self.assertIn("found a refrigerator in your appliance history", assistant["content"])
        self.assertIn("LG refrigerator (model LFXS26973S-DEMO)", assistant["content"])
        self.assertIn("three days ago", assistant["content"])

        # Step 2: User confirms with "Yes." HomeCare retrieves troubleshooting steps for the confirmed model.
        confirm_resp = self._send("scen-b-2", "Yes.")
        self._assert_server_executed("get_troubleshooting_steps")
        self.assertEqual(confirm_resp["status"], "completed")
        trouble_result = self._assert_deterministic_mcp_provenance(confirm_resp, "get_troubleshooting_steps")["result"]
        self.assertEqual(trouble_result["status"], "success")
        self.assertTrue(trouble_result["document_found"])
        self.assertTrue(len(trouble_result["probable_causes"]) > 0)
        self.assertTrue(len(trouble_result["safe_steps"]) > 0)
        self.assertTrue(len(trouble_result["stop_conditions"]) > 0)
        confirm_assistant = confirm_resp["assistant_message"]
        self.assertIn("Stop conditions", confirm_assistant["content"])
        self.assertIn("safe checks", confirm_assistant["content"])

    def test_scenario_b_refrigerator_rejection_flow(self) -> None:
        conv_id = self._create_conversation()
        req1 = {"conversation_id": conv_id, "client_message_id": "rej-1", "content": "My refrigerator is not cooling."}
        r1 = self._request_json("/api/messages", req1)
        self.assertIn("found a refrigerator in your appliance history", r1["assistant_message"]["content"])

        req2 = {"conversation_id": conv_id, "client_message_id": "rej-2", "content": "No, it's a different one."}
        r2 = self._request_json("/api/messages", req2)
        self.assertIn("Please provide the brand and model number", r2["assistant_message"]["content"])

    def test_scenario_c_warranty_eligibility(self) -> None:
        response = self._send("scen-c", "Can you check whether my washing machine is still under warranty?")
        self._assert_server_executed("check_warranty")
        self.assertEqual(response["status"], "completed")
        result = self._assert_deterministic_mcp_provenance(response, "check_warranty")["result"]
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["eligibility"], "eligible")
        self.assertTrue(result["is_simulated"])

    def test_scenario_d_arrange_service_slots_no_booking(self) -> None:
        response = self._send("scen-d", "What service options are available for my appliance? Do not book anything without asking me first.")
        self._assert_server_executed("find_service_slots")
        self.assertEqual(response["status"], "completed")
        result = self._assert_deterministic_mcp_provenance(response, "find_service_slots")["result"]
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["availability_type"], "estimated")
        self.assertTrue(len(result["slots"]) > 0)
        assistant = response["assistant_message"]
        self.assertTrue(assistant["conclusion"]["requires_user_confirmation"])
        self.assertIn("No appointment has been booked", assistant["content"])

    def test_scenario_e_previous_appliance_issue(self) -> None:
        response = self._send("scen-e", "What happened the last time I reported an issue with my refrigerator?")
        self._assert_server_executed("get_case_status")
        self.assertEqual(response["status"], "completed")
        result = self._assert_deterministic_mcp_provenance(response, "get_case_status")["result"]
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["request_id"], "demo-case-2026-0042")
        self.assertEqual(result["request_status"], "resolved")
        assistant = response["assistant_message"]
        self.assertIn("Resolved status indicates the support case was closed", assistant["content"])

    def test_scenario_f_unknown_appliance_model(self) -> None:
        response = self._send("scen-f", "I need help troubleshooting an appliance, but I don't know its exact model.")
        self._assert_server_executed("find_product")
        self.assertEqual(response["status"], "completed")
        result = self._assert_deterministic_mcp_provenance(response, "find_product")["result"]
        self.assertTrue("status" in result)
        assistant = response["assistant_message"]
        self.assertIn("locate the serial and model rating plate", assistant["content"])

    def test_llm_inference_unavailable_washing_machine_troubleshooting_invokes_mcp_and_emits_activity(self) -> None:
        response = self._send("reg-wm-1", "My washing machine shows error code E21. What does it mean, and what safe next step should I take?")
        self._assert_server_executed("get_troubleshooting_steps")
        self.assertEqual(response["status"], "completed")
        self.assertIsNotNone(response.get("response_id"))
        assistant = response["assistant_message"]
        activity = assistant["activity"]
        self.assertEqual(activity["status"], "completed")
        self.assertGreaterEqual(len(activity["events"]), 1)
        event = activity["events"][0]
        self.assertEqual(event["status"], "succeeded")
        self.assertIn("Called get_troubleshooting_steps through MCP", event["label"])
        # Domain result was documentation_not_found, but tool execution succeeded
        tool_res = assistant["tool_results"][0]
        self.assertEqual(tool_res["status"], "succeeded")
        self.assertEqual(tool_res["result"]["status"], "documentation_not_found")
        self.assertIn("cannot be verified without exact model", assistant["content"])

    def test_llm_inference_unavailable_service_discovery_invokes_mcp_no_booking(self) -> None:
        response = self._send("reg-sd-1", "What service options are available for my appliance? Do not book anything without asking me first.")
        self._assert_server_executed("find_service_slots")
        self.assertEqual(response["status"], "completed")
        self.assertIsNotNone(response.get("response_id"))
        assistant = response["assistant_message"]
        self.assertTrue(assistant["conclusion"]["requires_user_confirmation"])
        self.assertIn("estimated service options", assistant["content"])
        self.assertIn("No appointment has been booked", assistant["content"])
        activity = assistant["activity"]
        self.assertEqual(activity["status"], "completed")
        self.assertGreaterEqual(len(activity["events"]), 1)
        # Verify no create_service_request was called
        time.sleep(0.1)
        items = []
        while not self.server_log.empty():
            items.append(self.server_log.get_nowait())
        self.assertNotIn("MCP_TOOL_EXECUTED tool=create_service_request", items)

    def test_mcp_connection_failure_returns_honest_dependency_error_with_correlation_id(self) -> None:
        with patch.dict(os.environ, {"MCP_PORT": str(unused_port())}):
            response = self._send("reg-mcp-fail", "My washing machine shows error code E21.")
        self.assertEqual(response["status"], "failed")
        self.assertIsNotNone(response.get("correlation_id"))
        self.assertGreater(len(str(response["correlation_id"])), 10)
        self.assertIn("Correlation ID:", response["assistant_message"]["content"])
        self.assertEqual(response["assistant_message"]["activity"]["events"], [])

    def test_sse_stream_receives_tool_activity_events_and_done(self) -> None:
        req = {
            "conversation_id": self.conversation_id,
            "client_message_id": "reg-sse-1",
            "content": "My washing machine shows error code E21. What does it mean, and what safe next step should I take?",
        }
        request = Request(
            f"{self.base_url}/api/messages/stream",
            data=json.dumps(req).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
            method="POST",
        )
        events_received = []
        with urlopen(request, timeout=10) as resp:
            for raw in resp:
                line = raw.decode("utf-8").strip()
                if line.startswith("event: "):
                    events_received.append(line[7:].strip())
        self.assertIn("tool_start", events_received)
        self.assertIn("tool_done", events_received)
        self.assertIn("content", events_received)
        self.assertIn("done", events_received)

    def test_fixture_files_valid_and_non_empty(self) -> None:
        data_dir = ROOT / "mcp" / "data"
        self.assertTrue(data_dir.is_dir())
        files = ("appliances.json", "troubleshooting.json", "warranty.json", "service_slots.json", "cases.json", "sources.json", "scenarios.json")
        for fname in files:
            path = data_dir / fname
            self.assertTrue(path.is_file(), f"Missing fixture file {fname}")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsInstance(data, dict)

    def test_p0_exact_transcript_multi_turn_confirmation_and_greeting(self) -> None:
        """Full multi-turn regression test exercising the exact transcript from the P0 bug report."""
        conv_id = self._create_conversation()

        # Turn 1: User reports refrigerator not cooling
        turn1_req = {
            "conversation_id": conv_id,
            "client_message_id": "turn-1-fridge",
            "content": "My refrigerator is not cooling. Help me check this safely and tell me when to stop and call a technician.",
        }
        turn1_resp = self._request_json("/api/messages", turn1_req)
        self.assertEqual(turn1_resp["status"], "completed")
        self.assertEqual(turn1_resp["conversation_id"], conv_id)
        self.assertIsNotNone(turn1_resp.get("response_id"))
        self._assert_server_executed("get_appliance_inventory")

        t1_asst = turn1_resp["assistant_message"]
        self.assertIn("found a refrigerator in your appliance history", t1_asst["content"])
        self.assertIn("LG refrigerator (model LFXS26973S-DEMO)", t1_asst["content"])
        self.assertTrue(t1_asst["conclusion"]["requires_user_confirmation"])
        pending_action = t1_asst["conclusion"].get("pending_action")
        self.assertIsNotNone(pending_action)
        self.assertEqual(pending_action["action_type"], "confirm_appliance")
        self.assertEqual(pending_action["brand"], "LG")
        self.assertEqual(pending_action["model_number"], "LFXS26973S-DEMO")
        self.assertEqual(pending_action["symptom"], "not_cooling")
        self.assertTrue(pending_action["is_simulated"])

        # Turn 2: User confirms with exact phrase: "Confirmed, this is my appliance."
        turn2_req = {
            "conversation_id": conv_id,
            "client_message_id": "turn-2-confirm",
            "content": "Confirmed, this is my appliance.",
        }
        turn2_resp = self._request_json("/api/messages", turn2_req)
        self.assertEqual(turn2_resp["status"], "completed")
        self.assertEqual(turn2_resp["conversation_id"], conv_id)
        self.assertNotEqual(turn2_resp["response_id"], turn1_resp["response_id"])
        self._assert_server_executed("get_troubleshooting_steps")

        t2_asst = turn2_resp["assistant_message"]
        self.assertEqual(len(t2_asst["tool_results"]), 1)
        tool_res = t2_asst["tool_results"][0]
        self.assertEqual(tool_res["tool_name"], "get_troubleshooting_steps")
        self.assertEqual(tool_res["status"], "succeeded")
        self.assertTrue(tool_res["result"]["document_found"])
        self.assertIn("Stop conditions", t2_asst["content"])
        self.assertIn("safe checks", t2_asst["content"].lower())
        self.assertFalse(t2_asst["conclusion"]["requires_user_confirmation"])

        # Turn 3: Follow-up question about warranty uses persisted selected appliance ID
        turn3_req = {
            "conversation_id": conv_id,
            "client_message_id": "turn-3-warranty",
            "content": "Is this refrigerator still covered by warranty?",
        }
        turn3_resp = self._request_json("/api/messages", turn3_req)
        self.assertEqual(turn3_resp["status"], "completed")
        self._assert_server_executed("check_warranty")
        t3_tool = turn3_resp["assistant_message"]["tool_results"][0]
        self.assertEqual(t3_tool["result"]["appliance_id"], "demo-appl-fridge-001")

        # Turn 4: General greeting "Hello" receives friendly welcome, not LLM failure
        turn4_req = {
            "conversation_id": conv_id,
            "client_message_id": "turn-4-greeting",
            "content": "Hello",
        }
        turn4_resp = self._request_json("/api/messages", turn4_req)
        self.assertEqual(turn4_resp["status"], "completed")
        self.assertEqual(len(turn4_resp["assistant_message"]["tool_results"]), 0)
        t4_content = turn4_resp["assistant_message"]["content"]
        self.assertIn("Hello! I'm Alexa", t4_content)
        self.assertNotIn("LLM is unavailable", t4_content)

    def test_confirmation_button_metadata_action(self) -> None:
        conv_id = self._create_conversation()
        r1 = self._request_json("/api/messages", {
            "conversation_id": conv_id,
            "client_message_id": "btn-1",
            "content": "My refrigerator is not cooling.",
        })
        self.assertTrue(r1["assistant_message"]["conclusion"]["requires_user_confirmation"])

        r2 = self._request_json("/api/messages", {
            "conversation_id": conv_id,
            "client_message_id": "btn-2",
            "content": "Confirmed",
            "metadata": {"action": "confirm_appliance"},
        })
        self.assertEqual(r2["status"], "completed")
        self._assert_server_executed("get_troubleshooting_steps")
        self.assertEqual(r2["assistant_message"]["tool_results"][0]["tool_name"], "get_troubleshooting_steps")

    def test_unbound_confirmation_does_not_execute_consequential_action(self) -> None:
        conv_id = self._create_conversation()
        # User says "Confirmed, this is my appliance." with NO pending appliance state
        resp = self._request_json("/api/messages", {
            "conversation_id": conv_id,
            "client_message_id": "unbound-1",
            "content": "Confirmed, this is my appliance.",
        })
        self.assertEqual(resp["status"], "completed")
        self.assertEqual(len(resp["assistant_message"]["tool_results"]), 0)
        self.assertIn("There is no pending appliance confirmation", resp["assistant_message"]["content"])

    def test_appliance_confirmation_never_calls_service_booking(self) -> None:
        conv_id = self._create_conversation()
        self._request_json("/api/messages", {
            "conversation_id": conv_id,
            "client_message_id": "no-book-1",
            "content": "My refrigerator is not cooling.",
        })
        self._request_json("/api/messages", {
            "conversation_id": conv_id,
            "client_message_id": "no-book-2",
            "content": "Confirmed, this is my appliance.",
        })
        time.sleep(0.1)
        items = []
        while not self.server_log.empty():
            items.append(self.server_log.get_nowait())
        self.assertNotIn("MCP_TOOL_EXECUTED tool=create_service_request", items)

    def test_distinct_mcp_service_failure_message(self) -> None:
        with patch.dict(os.environ, {"MCP_PORT": str(unused_port())}):
            response = self._send("reg-mcp-distinct-err", "My washing machine shows error code E21.")
        self.assertEqual(response["status"], "failed")
        content = response["assistant_message"]["content"]
        self.assertIn("Support tools are unavailable (MCP service error)", content)
        self.assertNotIn("LLM is unavailable", content)


if __name__ == "__main__":
    unittest.main()

