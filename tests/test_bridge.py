"""Bridge tests against a fake SD plugin (no Substance Designer needed)."""
import json
import socket
import struct
import time

import pytest

import sd_mcp_bridge as bridge

pytestmark = pytest.mark.anyio

# Smallest valid PNG (1x1, transparent).
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6300010000000500010d0a2db40000000049454e44ae426082")


def _text(result):
    return result.content[0].text


def _closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── Framing ────────────────────────────────────────────────────────────────

async def test_framing_roundtrip_large_payload():
    a, b = socket.socketpair()
    try:
        payload = json.dumps({"blob": "x" * 300_000}).encode()
        bridge._send_framed(a, payload)
        assert bridge._recv_framed(b, timeout=5) == payload
    finally:
        a.close()
        b.close()


async def test_framing_header_is_big_endian_length():
    a, b = socket.socketpair()
    try:
        bridge._send_framed(a, b'{"k": 1}')
        assert b.recv(4) == struct.pack(">I", 8)
    finally:
        a.close()
        b.close()


async def test_tool_call_sends_framed_command_and_returns_json(fake_sd, client):
    fake_sd.handler = lambda t, p: {"status": "success", "result": {"packages": [], "version": "15.1.2"}}
    result = await client.call_tool("get_scene_info", {})
    assert not result.isError
    assert fake_sd.received == [{"type": "get_scene_info", "params": {"include_library": False}}]
    assert json.loads(_text(result)) == {"packages": [], "version": "15.1.2"}


async def test_none_result_becomes_empty_object(fake_sd, client):
    fake_sd.handler = lambda t, p: {"status": "success", "result": None}
    result = await client.call_tool("create_package", {})
    assert not result.isError
    assert json.loads(_text(result)) == {}


async def test_empty_response_is_tool_error(fake_sd, client):
    fake_sd.handler = lambda t, p: b""
    result = await client.call_tool("get_scene_info", {})
    assert result.isError
    assert "Empty response" in _text(result)


# ── Errors and retries ─────────────────────────────────────────────────────

async def test_failed_command_is_tool_error_and_reaches_sd_once(fake_sd, client):
    # The message mentions "connect"; the old bridge retried on that substring.
    fake_sd.handler = lambda t, p: {
        "status": "error",
        "message": "Cannot connect: Input port 'imput1' not found. Did you mean 'input1'?"}
    result = await client.call_tool("connect_nodes", {"from_node_id": "1", "to_node_id": "2",
                                                      "to_input": "imput1"})
    assert result.isError
    assert "Did you mean 'input1'" in _text(result)
    assert len(fake_sd.received) == 1
    assert fake_sd.received[0]["type"] == "connect_nodes"


async def test_connect_failure_is_retried_then_tool_error(monkeypatch, client):
    monkeypatch.setattr(bridge, "_sd_port", _closed_port())
    monkeypatch.setattr(bridge, "RETRY_DELAY", 0)
    attempts = []
    original = bridge._send_command_locked

    def counting(cmd_type, params=None):
        attempts.append(cmd_type)
        return original(cmd_type, params)

    monkeypatch.setattr(bridge, "_send_command_locked", counting)
    result = await client.call_tool("get_scene_info", {})
    assert result.isError
    assert "Connection Error" in _text(result)
    assert len(attempts) == bridge.MAX_RETRIES + 1


async def test_timeout_is_not_retried(fake_sd, client, monkeypatch):
    monkeypatch.setattr(bridge, "TIMEOUT", 0.3)

    def slow(t, p):
        time.sleep(1.0)
        return {"status": "success", "result": {}}

    fake_sd.handler = slow
    result = await client.call_tool("get_scene_info", {})
    assert result.isError
    assert "Timeout" in _text(result)
    time.sleep(0.8)  # let the fake server finish the slow request
    assert len(fake_sd.received) == 1


# ── capture_3d_view ────────────────────────────────────────────────────────

async def test_capture_3d_view_returns_text_and_image(fake_sd, client, tmp_path):
    png = tmp_path / "view.png"
    png.write_bytes(PNG_1X1)
    info = {"path": str(png), "width": 1, "height": 1, "method": "PrintWindow"}
    fake_sd.handler = lambda t, p: {"status": "success", "result": info}

    result = await client.call_tool("capture_3d_view", {"refresh_first": True})
    assert not result.isError
    assert [c.type for c in result.content] == ["text", "image"]
    assert json.loads(result.content[0].text) == info
    assert result.content[1].mimeType == "image/png"
    assert result.content[1].data  # base64 payload
    assert fake_sd.received[0]["params"]["refresh_first"] is True


async def test_capture_3d_view_without_image(fake_sd, client, tmp_path):
    png = tmp_path / "view.png"
    png.write_bytes(PNG_1X1)
    fake_sd.handler = lambda t, p: {"status": "success", "result": {"path": str(png)}}
    result = await client.call_tool("capture_3d_view", {"return_image": False})
    assert [c.type for c in result.content] == ["text"]


# ── Tool metadata ──────────────────────────────────────────────────────────

async def test_every_tool_has_annotations_and_no_output_schema(client):
    tools = (await client.list_tools()).tools
    assert len(tools) >= 30
    for tool in tools:
        assert tool.annotations is not None, tool.name
        assert tool.annotations.readOnlyHint is not None, tool.name
        assert tool.annotations.openWorldHint is False, tool.name
        assert tool.outputSchema is None, tool.name
    by_name = {t.name: t for t in tools}
    assert by_name["get_scene_info"].annotations.readOnlyHint is True
    assert by_name["delete_node"].annotations.destructiveHint is True
    assert by_name["execute_sd_code"].annotations.destructiveHint is True
    assert "reload_plugin" in by_name


async def test_batch_schema_documents_fields(client):
    tools = {t.name: t for t in (await client.list_tools()).tools}
    defs = tools["create_batch_graph"].inputSchema["$defs"]
    node_props = defs["BatchNode"]["properties"]
    for field in ("id_alias", "definition_id", "resource_url", "position",
                  "usage", "label", "parameters"):
        assert field in node_props and node_props[field].get("description"), field
    conn = defs["BatchConnection"]
    assert set(conn["properties"]) >= {"from", "to", "from_output", "to_input"}
    assert set(conn["required"]) == {"from", "to"}


# ── Wire format of typed inputs ────────────────────────────────────────────

async def test_batch_wire_format_unchanged(fake_sd, client):
    nodes = [
        {"id_alias": "noise", "library_keyword": "perlin_noise", "position": [0, 0]},
        {"id_alias": "blur", "definition_id": "sbs::compositing::blur",
         "parameters": {"intensity": 10.0, "$outputsize": {"value": [11, 11], "type": "int2"}}},
        {"id_alias": "out", "definition_id": "sbs::compositing::output", "usage": "height",
         "label": "Height", "position": [400, 0]},
    ]
    connections = [
        {"from": "noise", "to": "blur"},
        {"from": "blur", "to": "out", "to_input": "inputNodeOutput"},
    ]
    result = await client.call_tool("create_batch_graph", {
        "graph_name": "G", "nodes": nodes, "connections": connections})
    assert not result.isError
    sent = fake_sd.received[0]["params"]
    assert sent["nodes"] == nodes  # unset fields are not sent as nulls
    assert sent["connections"] == [
        {"from": "noise", "to": "blur", "from_output": "unique_filter_output", "to_input": "input1"},
        {"from": "blur", "to": "out", "from_output": "unique_filter_output",
         "to_input": "inputNodeOutput"},
    ]


async def test_batch_accepts_legacy_alias_keys(fake_sd, client):
    result = await client.call_tool("create_batch_graph", {
        "graph_name": "G",
        "nodes": [{"id_alias": "a"}, {"id_alias": "b"}],
        "connections": [{"from_alias": "a", "to_alias": "b"}]})
    assert not result.isError
    conn = fake_sd.received[0]["params"]["connections"][0]
    assert conn["from"] == "a" and conn["to"] == "b"


async def test_batch_invalid_input_rejected_before_sd(fake_sd, client):
    result = await client.call_tool("create_batch_graph", {
        "graph_name": "G", "nodes": [{"id_alias": "a", "position": [1, 2, 3]}]})
    assert result.isError
    assert fake_sd.received == []


async def test_recipe_overrides_pass_through(fake_sd, client):
    overrides = {"base": {"intensity": 4.0, "blendingmode": {"value": 3, "type": "int"}}}
    result = await client.call_tool("apply_recipe", {"recipe_name": "steel", "overrides": overrides})
    assert not result.isError
    assert fake_sd.received[0]["params"]["overrides"] == overrides


async def test_reload_plugin_command(fake_sd, client):
    result = await client.call_tool("reload_plugin", {"handlers": False})
    assert not result.isError
    assert fake_sd.received == [{"type": "reload_plugin", "params": {"handlers": False}}]


async def test_waiting_call_does_not_hold_a_plugin_connection(fake_sd):
    """Regression: parallel calls deadlocked until the 120s timeout.

    The plugin serves connections one at a time in connect order. The bridge used
    to connect *before* taking _send_lock, so a call waiting on the lock already
    sat in the plugin's accept queue. If a call that connected later got the lock
    first, the plugin waited for the earlier call's request, which waited for the
    lock, while the lock holder waited for its reply.
    """
    import asyncio
    import threading

    with bridge._send_lock:
        # Call A must wait for the lock; it must not connect to SD while waiting.
        waiting = threading.Thread(target=bridge._request, args=("get_scene_info",), daemon=True)
        waiting.start()
        await asyncio.sleep(0.3)

        # Meanwhile the lock holder (this test) talks to SD and must get a reply promptly.
        def lock_holder_round_trip():
            with socket.create_connection(("127.0.0.1", fake_sd.port), timeout=2) as s:
                payload = json.dumps({"type": "get_node_info", "params": {}}).encode()
                s.sendall(struct.pack(">I", len(payload)) + payload)
                (length,) = struct.unpack(">I", s.recv(4))
                return json.loads(s.recv(length))

        reply = await asyncio.to_thread(lock_holder_round_trip)
        assert reply["status"] == "success"

    await asyncio.to_thread(waiting.join, 5)
    assert not waiting.is_alive()
    assert [c["type"] for c in fake_sd.received] == ["get_node_info", "get_scene_info"]
