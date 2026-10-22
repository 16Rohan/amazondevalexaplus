"""Protocol compliance tests against the MCP 2026-07-28 specification."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from mcp.client import Client

ROOT = Path(__file__).resolve().parent.parent.parent


def unused_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class McpProtocolConformanceTests(unittest.TestCase):
    """Verify compliance with the MCP 2026-07-28 stateless Streamable HTTP specification."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mcp_port = unused_port()
        env = {**os.environ, "MCP_PORT": str(cls.mcp_port), "PYTHONUNBUFFERED": "1"}
        cls.process = subprocess.Popen(
            [sys.executable, str(ROOT / "mcp" / "main.py")],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        cls.logs: queue.Queue[str] = queue.Queue()

        def reader(stream: Any) -> None:
            for line in stream:
                cls.logs.put(line.strip())

        threading.Thread(target=reader, args=(cls.process.stdout,), daemon=True).start()
        threading.Thread(target=reader, args=(cls.process.stderr,), daemon=True).start()

        # Wait for server to start
        cls.endpoint = f"http://127.0.0.1:{cls.mcp_port}/mcp"
        deadline = time.monotonic() + 10
        ready = False
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{cls.mcp_port}/mcp", timeout=0.5):
                    ready = True
                    break
            except urllib.error.HTTPError as e:
                # 400 or 406 means server is listening and processing MCP requests
                if e.code in (400, 404, 405, 406):
                    ready = True
                    break
            except Exception:
                time.sleep(0.1)
        if not ready:
            cls.process.terminate()
            raise RuntimeError(f"MCP server did not start on port {cls.mcp_port} in time")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.process.terminate()
        try:
            cls.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            cls.process.kill()

    def _post_raw(self, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, dict[str, Any] | str]:
        base_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        base_headers.update(headers)
        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers=base_headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as err:
            status = err.code
            raw = err.read().decode("utf-8")
        try:
            return status, json.loads(raw)
        except json.JSONDecodeError:
            return status, raw

    def test_01_negotiated_protocol_is_2026_07_28(self) -> None:
        """SDK client and server negotiate the modern 2026-07-28 stateless protocol."""
        async def check() -> None:
            async with Client(self.endpoint) as client:
                session = client.session
                self.assertEqual(session.protocol_version, "2026-07-28")
                # Discover result populated
                self.assertIsNotNone(session.discover_result)
                self.assertIn("2026-07-28", session.discover_result.supported_versions)
                self.assertEqual(session.discover_result.result_type, "complete")
                # Stateless: no initialize result
                self.assertIsNone(session.initialize_result)

        asyncio.run(check())

    def test_02_tools_list_over_streamable_http(self) -> None:
        """tools/list returns the 7 registered tools with complete resultType."""
        async def check() -> None:
            async with Client(self.endpoint) as client:
                result = await client.list_tools()
                names = {t.name for t in result.tools}
                expected = {
                    "find_product",
                    "get_troubleshooting_steps",
                    "check_warranty",
                    "find_service_slots",
                    "create_service_request",
                    "get_case_status",
                    "get_appliance_inventory",
                }
                self.assertEqual(names, expected)

        asyncio.run(check())

    def test_03_tools_call_returns_structured_content_and_complete_result(self) -> None:
        """tools/call executes over Streamable HTTP returning structured_content and result_type='complete'."""
        async def check() -> None:
            async with Client(self.endpoint) as client:
                res = await client.call_tool(
                    "get_troubleshooting_steps",
                    {"model_id": "WC-500", "symptom": "E21"},
                )
                self.assertFalse(res.is_error)
                self.assertEqual(res.result_type, "complete")
                self.assertIsInstance(res.structured_content, dict)
                self.assertEqual(res.structured_content.get("status"), "documentation_not_found")
                self.assertEqual(res.structured_content.get("model_id"), "WC-500")

        asyncio.run(check())

    def test_04_accept_header_validation(self) -> None:
        """Client must accept both application/json and text/event-stream; otherwise HTTP 406."""
        status, body = self._post_raw(
            {"Accept": "application/json"},
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        self.assertEqual(status, 406)
        self.assertIsInstance(body, dict)
        self.assertEqual(body.get("error", {}).get("code"), -32600)

    def test_05_missing_protocol_version_header_rejected(self) -> None:
        """Request without mcp-protocol-version header is rejected under 2026-07-28."""
        status, body = self._post_raw(
            {},
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        self.assertEqual(status, 400)
        self.assertIsInstance(body, dict)
        self.assertEqual(body.get("error", {}).get("code"), -32600)

    def test_06_unsupported_protocol_version_returns_code_32022(self) -> None:
        """Unsupported protocol version returns HTTP 400 with error code -32022."""
        meta = {
            "io.modelcontextprotocol/protocolVersion": "2099-01-01",
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "test-client", "version": "1.0"},
        }
        status, body = self._post_raw(
            {
                "mcp-protocol-version": "2099-01-01",
                "mcp-method": "tools/list",
            },
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/list",
                "params": {"_meta": meta},
            },
        )
        self.assertEqual(status, 400)
        self.assertIsInstance(body, dict)
        self.assertEqual(body.get("error", {}).get("code"), -32022)
        self.assertIn("supported", body.get("error", {}).get("data", {}))

    def test_07_missing_mcp_name_header_on_tools_call_returns_code_32020(self) -> None:
        """Missing or mismatched mcp-name header on tools/call returns HTTP 400 with code -32020."""
        meta = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "test-client", "version": "1.0"},
        }
        status, body = self._post_raw(
            {
                "mcp-protocol-version": "2026-07-28",
                "mcp-method": "tools/call",
                # Omit mcp-name header
            },
            {
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {"name": "find_product", "arguments": {"query": "test"}, "_meta": meta},
            },
        )
        self.assertEqual(status, 400)
        self.assertIsInstance(body, dict)
        self.assertEqual(body.get("error", {}).get("code"), -32020)

    def test_08_unknown_method_returns_404_code_32601(self) -> None:
        """Non-existent JSON-RPC method returns HTTP 404 with error code -32601."""
        meta = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "test-client", "version": "1.0"},
        }
        status, body = self._post_raw(
            {
                "mcp-protocol-version": "2026-07-28",
                "mcp-method": "nonexistent/custom_method",
            },
            {
                "jsonrpc": "2.0",
                "id": 12,
                "method": "nonexistent/custom_method",
                "params": {"_meta": meta},
            },
        )
        self.assertEqual(status, 404)
        self.assertIsInstance(body, dict)
        self.assertEqual(body.get("error", {}).get("code"), -32601)

    def test_09_stateless_post_no_session_id_required(self) -> None:
        """Under 2026-07-28, valid tool call succeeds without Mcp-Session-Id header."""
        meta = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "test-client", "version": "1.0"},
        }
        status, body = self._post_raw(
            {
                "mcp-protocol-version": "2026-07-28",
                "mcp-method": "tools/call",
                "mcp-name": "check_warranty",
            },
            {
                "jsonrpc": "2.0",
                "id": 13,
                "method": "tools/call",
                "params": {"name": "check_warranty", "arguments": {"appliance_id": "appliance-001"}, "_meta": meta},
            },
        )
        self.assertEqual(status, 200)
        self.assertIsInstance(body, dict)
        self.assertIn("result", body)
        self.assertEqual(body["result"].get("resultType"), "complete")

    def test_10_legacy_handshake_compatibility_supported(self) -> None:
        """Server retains compatibility for clients using the legacy 2025-11-25 initialize handshake."""
        init_body = {
            "jsonrpc": "2.0",
            "id": 100,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "legacy-test-client", "version": "1.0"},
            },
        }
        status, body = self._post_raw({}, init_body)
        self.assertEqual(status, 200)
        # Under legacy era, response is formatted as SSE or JSON with initialize result
        if isinstance(body, dict):
            res = body.get("result", {})
            self.assertEqual(res.get("protocolVersion"), "2025-11-25")
        else:
            self.assertIn("2025-11-25", str(body))

    def test_11_malformed_tool_output_safely_rejected(self) -> None:
        """The gateway validates tool output schema and safely rejects malformed or unverified outputs."""
        from llm.main import _valid_tool_result, _safe_tool_result

        # Missing required field
        invalid_res = {"status": "success", "is_simulated": True}  # missing 'slots' and 'availability_type' for find_service_slots
        self.assertFalse(_valid_tool_result("find_service_slots", invalid_res))

        # Invalid status not in allowed statuses
        bad_status_res = {"status": "random_corrupted_status", "is_simulated": True, "slots": [], "availability_type": "estimated"}
        self.assertFalse(_valid_tool_result("find_service_slots", bad_status_res))

        # Safe extraction from corrupted object
        val, failed = _safe_tool_result(object())
        self.assertTrue(failed)
        self.assertEqual(val.get("status"), "unavailable")


if __name__ == "__main__":
    unittest.main()
