import unittest
from pathlib import Path

import main


class LauncherConfigTests(unittest.TestCase):
    def test_dotenv_values_and_process_precedence(self):
        env_file = Path(__file__).resolve().parents[1] / ".env.example"
        values = main.read_dotenv(env_file, {"BACKEND_PORT": "9000"})
        self.assertEqual(values["BACKEND_PORT"], "9000")
        self.assertEqual(values["MCP_TRANSPORT"], "streamable-http")

    def test_port_validation(self):
        self.assertEqual(main.port({"BACKEND_PORT": "8123"}, "BACKEND_PORT", 8000), 8123)
        with self.assertRaises(ValueError):
            main.port({"BACKEND_PORT": "70000"}, "BACKEND_PORT", 8000)


if __name__ == "__main__":
    unittest.main()
