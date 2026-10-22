"""Run the Alexa web app, backend/LLM gateway, and MCP server together."""

from __future__ import annotations

import os
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Mapping


ROOT = Path(__file__).resolve().parent
STARTUP_TIMEOUT = 45
SHUTDOWN_TIMEOUT = 8


def read_dotenv(path: Path, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Read root .env values; exported process values take precedence."""
    values: dict[str, str] = {}
    if path.is_file():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if key:
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key] = value
    values.update(os.environ if environ is None else environ)
    return values


def port(values: Mapping[str, str], name: str, default: int) -> int:
    try:
        value = int(values.get(name, str(default)))
    except ValueError as error:
        raise ValueError(f"{name} must be a port number") from error
    if not 1 <= value <= 65535:
        raise ValueError(f"{name} must be between 1 and 65535")
    return value


def python_command(project: str, script: str) -> list[str]:
    executable = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    for environment in (ROOT / project / ".venv", ROOT / "llm" / ".venv"):
        venv_python = environment / executable
        if venv_python.is_file():
            return [str(venv_python), script]
    return ["uv", "run", "--project", str(ROOT / project), "python", script]


def check_port_free(port_number: int) -> None:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port_number))
        except OSError as error:
            raise RuntimeError(f"Port {port_number} is already in use; no server was started there") from error


def start(name: str, command: list[str], cwd: Path, env: dict[str, str]) -> subprocess.Popen[bytes]:
    print(f"Starting {name}…", flush=True)
    options: dict[str, object] = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    return subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, **options)


def wait_for_port(name: str, server: subprocess.Popen[bytes], port_number: int) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError(f"{name} exited during startup (code {server.returncode})")
        try:
            with socket.create_connection(("127.0.0.1", port_number), timeout=0.3):
                print(f"{name} is listening on 127.0.0.1:{port_number}", flush=True)
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for {name} on port {port_number}")


def stop(server: subprocess.Popen[bytes], name: str) -> None:
    if server.poll() is not None:
        return
    print(f"Stopping {name}…", flush=True)
    try:
        if os.name == "nt":
            server.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(server.pid, signal.SIGTERM)
        server.wait(timeout=SHUTDOWN_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        server.terminate()
        try:
            server.wait(timeout=3)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()


def wait_for_quit() -> None:
    try:
        while input().strip().lower() != "q":
            print("Press q then Enter to stop all servers.", flush=True)
    except EOFError:
        return
    QUIT.set()


QUIT = threading.Event()


def main() -> int:
    env = read_dotenv(ROOT / ".env")
    try:
        frontend_port = port(env, "FRONTEND_PORT", 5173)
        backend_port = port(env, "BACKEND_PORT", 8000)
        mcp_port = port(env, "MCP_PORT", 8001)
        if len({frontend_port, backend_port, mcp_port}) != 3:
            raise ValueError("FRONTEND_PORT, BACKEND_PORT, and MCP_PORT must be different")
        if env.get("MCP_TRANSPORT", "streamable-http") != "streamable-http":
            raise ValueError("MCP_TRANSPORT must be streamable-http")
        for configured_port in (mcp_port, backend_port, frontend_port):
            check_port_free(configured_port)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Startup configuration error: {error}", file=sys.stderr)
        return 1

    env.update({
        "FRONTEND_PORT": str(frontend_port),
        "BACKEND_PORT": str(backend_port),
        "MCP_PORT": str(mcp_port),
        "UV_CACHE_DIR": str(ROOT / ".uv-cache"),
    })
    mcp_env = env.copy()
    mcp_env.pop("LLM_API_KEY", None)
    frontend_env = env.copy()
    frontend_env.pop("LLM_API_KEY", None)
    frontend_env.pop("MCP_CONFIRMATION_SECRET", None)
    node = shutil.which("node")
    if node is None:
        print("Startup configuration error: Node.js is not installed or is not on PATH", file=sys.stderr)
        return 1
    services: list[tuple[str, subprocess.Popen[bytes]]] = []
    commands = [
        ("MCP server", python_command("mcp", str(ROOT / "mcp" / "main.py")), ROOT, mcp_port, mcp_env),
        ("Backend / LLM gateway", python_command("llm", "main.py"), ROOT / "llm", backend_port, env),
        ("Frontend", [node, "node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", str(frontend_port), "--strictPort"], ROOT / "webapp", frontend_port, frontend_env),
    ]
    exit_code = 0
    try:
        for name, command, cwd, service_port, service_env in commands:
            server = start(name, command, cwd, service_env)
            services.append((name, server))
            wait_for_port(name, server, service_port)
    except (OSError, RuntimeError) as error:
        print(f"Startup failed: {error}", file=sys.stderr)
        QUIT.set()
        exit_code = 1
    except KeyboardInterrupt:
        QUIT.set()
        exit_code = 130
    else:
        print("All local servers are running. Press Ctrl+C or q then Enter to stop.", flush=True)
        threading.Thread(target=wait_for_quit, daemon=True).start()

    try:
        while not QUIT.wait(0.25):
            for name, server in services:
                if server.poll() is not None:
                    print(f"{name} exited unexpectedly (code {server.returncode}); shutting down the rest.", file=sys.stderr)
                    QUIT.set()
                    exit_code = 1
                    break
    except KeyboardInterrupt:
        QUIT.set()
    finally:
        for name, server in reversed(services):
            stop(server, name)
        print("All managed servers stopped.", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
