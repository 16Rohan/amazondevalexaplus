import unittest

from llm.main import load_config, probe_connectivity, probe_inference, _stream_completion


class LiveProviderIntegrationTests(unittest.TestCase):
    """Optional live provider integration tests; runs only when valid live credentials exist."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.config = load_config()
        except Exception as e:
            raise unittest.SkipTest(f"Live configuration not available: {e}")

        # Check if key is configured and not a placeholder
        if not cls.config.api_key or cls.config.api_key.startswith("replace-") or cls.config.api_key.startswith("your-") or len(cls.config.api_key) < 10:
            raise unittest.SkipTest("Valid live LLM_API_KEY not configured")

    def test_live_connectivity_and_inference_probe(self):
        conn = probe_connectivity(self.config)
        self.assertEqual(conn.status, "healthy")
        inf = probe_inference(self.config)
        self.assertEqual(inf.status, "healthy")

    def test_live_streaming_separates_reasoning(self):
        chunks_collected = []
        def on_chunk(event_type, delta):
            chunks_collected.append((event_type, delta))

        messages = [
            {"role": "user", "content": "What is 1+1? Reply with just the number."}
        ]
        msg = _stream_completion(self.config, messages, chunk_callback=on_chunk)
        self.assertEqual(msg.get("role"), "assistant")
        self.assertIsNotNone(msg.get("content"))
        self.assertIn("2", msg["content"])
        if msg.get("reasoning_content"):
            self.assertIsInstance(msg["reasoning_content"], str)
            self.assertNotIn("<think>", msg["reasoning_content"])
            self.assertNotEqual(msg["content"], msg["reasoning_content"])

    def test_live_tool_calling_round(self):
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "check_warranty",
                    "description": "Check warranty coverage for an appliance",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "appliance_id": {"type": "string", "description": "The appliance identifier"}
                        },
                        "required": ["appliance_id"]
                    }
                }
            }
        ]
        messages = [
            {"role": "user", "content": "Please check warranty for appliance-001"}
        ]
        msg = _stream_completion(self.config, messages, tools=tools)
        self.assertEqual(msg.get("role"), "assistant")
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            self.assertEqual(tool_calls[0]["function"]["name"], "check_warranty")
            self.assertIn("appliance-001", tool_calls[0]["function"]["arguments"])


if __name__ == "__main__":
    unittest.main()
