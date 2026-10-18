"""Shared fixtures: a fake SD plugin TCP server and an in-memory MCP client session."""
import json
import socket
import struct
import threading

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

import sd_mcp_bridge as bridge

SD_LIVE_PORT = 9881


def _port_open(port, host="127.0.0.1"):
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def pytest_collection_modifyitems(config, items):
    live = [i for i in items if "live" in i.keywords]
    if live and not _port_open(SD_LIVE_PORT):
        skip = pytest.mark.skip(reason="Substance Designer not listening on localhost:{}".format(SD_LIVE_PORT))
        for item in live:
            item.add_marker(skip)


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("client closed after {} of {} bytes".format(len(buf), n))
        buf += chunk
    return buf


class FakeSD:
    """Minimal stand-in for the SD plugin: one framed JSON command per connection.

    `handler(cmd_type, params)` returns the response dict ({"status": ..., ...}),
    or raw bytes to send as the payload as-is. Every command received is
    recorded in `received` as {"type", "params"}.
    """

    def __init__(self):
        self.received = []
        self.handler = lambda cmd_type, params: {"status": "success", "result": {"ok": cmd_type}}
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while self._running:
            try:
                client, _ = self._sock.accept()
            except (socket.timeout, OSError):
                continue
            with client:
                try:
                    client.settimeout(10)
                    (length,) = struct.unpack(">I", _recv_exact(client, 4))
                    command = json.loads(_recv_exact(client, length).decode("utf-8"))
                    self.received.append(command)
                    response = self.handler(command["type"], command.get("params", {}))
                    payload = response if isinstance(response, bytes) else json.dumps(response).encode()
                    client.sendall(struct.pack(">I", len(payload)) + payload)
                except (ConnectionError, OSError):
                    pass

    def close(self):
        self._running = False
        self._thread.join(timeout=2)
        self._sock.close()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def fake_sd(monkeypatch):
    sd = FakeSD()
    monkeypatch.setattr(bridge, "_sd_port", sd.port)
    monkeypatch.setattr(bridge, "RETRY_DELAY", 0)
    yield sd
    sd.close()


@pytest.fixture
async def client():
    async with create_connected_server_and_client_session(bridge.mcp) as session:
        yield session
