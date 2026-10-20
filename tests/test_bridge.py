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

    def counting(cmd_type, params=None, **kwargs):
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


def test_tool_timeouts_split_default_and_heavy():
    assert bridge._tool_timeout("get_scene_info") == bridge.TIMEOUT
    assert bridge._tool_timeout("create_batch_graph") == bridge.HEAVY_TIMEOUT
    assert bridge.HEAVY_TIMEOUT > bridge.TIMEOUT


async def test_command_envelope_has_request_id_and_timeout(fake_sd, client):
    await client.call_tool("get_scene_info", {})
    command = fake_sd.raw_received[0]
    assert command["request_id"]
    assert command["timeout"] == bridge.TIMEOUT
    # Envelope metadata stays out of the documented params payload.
    assert fake_sd.received[0] == {
        "type": "get_scene_info", "params": {"include_library": False}}


def test_heavy_tool_uses_heavy_timeout(fake_sd):
    bridge._request("create_batch_graph", {})
    assert fake_sd.raw_received[-1]["timeout"] == bridge.HEAVY_TIMEOUT


def test_request_id_reused_across_retries(monkeypatch):
    ids = []

    def fake(cmd_type, params=None, request_id=None, timeout=None):
        ids.append(request_id)
        if len(ids) == 1:
            raise ConnectionError("connect failed")
        return {"status": "success", "result": {"ok": True}}

    monkeypatch.setattr(bridge, "_send_command_locked", fake)
    monkeypatch.setattr(bridge, "RETRY_DELAY", 0)
    assert bridge._request("get_scene_info") == {"ok": True}
    assert len(ids) == 2
    assert ids[0] == ids[1]


def test_non_finite_flags_surface_in_result(fake_sd):
    flags = [{"path": "$.value", "value": "nan"}]
    fake_sd.handler = lambda t, p: {
        "status": "success", "result": {"value": None},
        "non_finite_values": flags}
    result = bridge._request("get_scene_info")
    assert result["value"] is None
    assert result["non_finite_values"] == flags


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


# ── capture_2d_view ────────────────────────────────────────────────────────

async def test_capture_2d_view_texture_mode_returns_text_and_image(fake_sd, client, tmp_path):
    png = tmp_path / "map.png"
    png.write_bytes(PNG_1X1)
    info = {"path": str(png), "width": 1, "height": 1, "method": "texture",
            "node": "n1", "port_id": "output", "label": "Height"}
    fake_sd.handler = lambda t, p: {"status": "success", "result": info}

    result = await client.call_tool("capture_2d_view", {
        "graph_identifier": "G", "output_usage": "height", "max_size": 512})
    assert not result.isError
    assert [c.type for c in result.content] == ["text", "image"]
    assert json.loads(result.content[0].text) == info
    assert result.content[1].mimeType == "image/png"
    assert result.content[1].data  # base64 payload
    sent = fake_sd.received[0]
    assert sent["type"] == "capture_2d_view"
    assert sent["params"]["graph_identifier"] == "G"
    assert sent["params"]["output_usage"] == "height"
    assert sent["params"]["max_size"] == 512


async def test_capture_2d_view_widget_mode_without_image(fake_sd, client, tmp_path):
    png = tmp_path / "view.png"
    png.write_bytes(PNG_1X1)
    fake_sd.handler = lambda t, p: {
        "status": "success", "result": {"path": str(png), "method": "widget_grab"}}
    result = await client.call_tool("capture_2d_view", {"return_image": False})
    assert not result.isError
    assert [c.type for c in result.content] == ["text"]
    assert fake_sd.received[0]["params"]["node_id"] is None
    assert fake_sd.received[0]["params"]["output_usage"] is None


# ── capture_node_preview ───────────────────────────────────────────────────

async def test_capture_node_preview_returns_text_and_image(fake_sd, client, tmp_path):
    png = tmp_path / "node.png"
    png.write_bytes(PNG_1X1)
    info = {"path": str(png), "width": 1, "height": 1,
            "method": "node_preview", "capture": "preview_widget",
            "graph": "G", "node": "Base_Noise", "label": "Base Noise",
            "source_width": 128, "source_height": 128}
    fake_sd.handler = lambda t, p: {"status": "success", "result": info}

    result = await client.call_tool("capture_node_preview", {
        "graph_identifier": "G", "node_id": "Base_Noise", "max_size": 256})
    assert not result.isError
    assert [c.type for c in result.content] == ["text", "image"]
    assert json.loads(result.content[0].text) == info
    assert result.content[1].mimeType == "image/png"
    sent = fake_sd.received[0]
    assert sent["type"] == "capture_node_preview"
    assert sent["params"] == {"graph_identifier": "G", "node_id": "Base_Noise",
                              "node_ids": None, "max_size": 256,
                              "output_path": None, "wait_ms": 250}


async def test_capture_node_preview_sheet_without_image(fake_sd, client, tmp_path):
    png = tmp_path / "sheet.png"
    png.write_bytes(PNG_1X1)
    fake_sd.handler = lambda t, p: {
        "status": "success", "result": {"path": str(png),
                                        "method": "node_preview_sheet"}}
    result = await client.call_tool("capture_node_preview", {
        "node_ids": ["n1", "n2"], "return_image": False})
    assert not result.isError
    assert [c.type for c in result.content] == ["text"]
    assert fake_sd.received[0]["params"]["node_ids"] == ["n1", "n2"]


async def test_capture_node_preview_is_a_long_job_op(fake_sd, client):
    result = await client.call_tool("start_long_job", {
        "job_type": "capture_node_preview", "params": {"node_id": "n1"}})
    assert not result.isError
    sent = fake_sd.received[0]
    assert sent["type"] == "start_job"
    assert sent["params"]["op"] == "capture_node_preview"


# ── inspect_outputs ────────────────────────────────────────────────────────

async def test_inspect_outputs_returns_text_and_image(fake_sd, client, tmp_path):
    png = tmp_path / "sheet.png"
    png.write_bytes(PNG_1X1)
    info = {"graph": "G", "path": str(png), "width": 1, "height": 1,
            "method": "inspect_outputs", "histogram_bins": 8,
            "outputs": [{"usage": "height", "pixel_format": "LUM16"}],
            "skipped": []}
    fake_sd.handler = lambda t, p: {"status": "success", "result": info}

    result = await client.call_tool("inspect_outputs", {
        "graph_identifier": "G", "output_usages": ["height", "normal"],
        "max_size": 512, "histogram_bins": 8})
    assert not result.isError
    assert [c.type for c in result.content] == ["text", "image"]
    assert json.loads(result.content[0].text) == info
    assert result.content[1].mimeType == "image/png"
    assert result.content[1].data  # base64 payload
    sent = fake_sd.received[0]
    assert sent["type"] == "inspect_outputs"
    assert sent["params"] == {"graph_identifier": "G",
                              "output_usages": ["height", "normal"],
                              "max_size": 512, "histogram_bins": 8,
                              "output_path": None}


async def test_inspect_outputs_defaults_and_without_image(fake_sd, client, tmp_path):
    png = tmp_path / "sheet.png"
    png.write_bytes(PNG_1X1)
    fake_sd.handler = lambda t, p: {
        "status": "success", "result": {"path": str(png)}}
    result = await client.call_tool("inspect_outputs", {"return_image": False})
    assert not result.isError
    assert [c.type for c in result.content] == ["text"]
    sent = fake_sd.received[0]["params"]
    assert sent["output_usages"] is None
    assert sent["histogram_bins"] == 16


# ── set_parameters ─────────────────────────────────────────────────────────

async def test_set_parameters_wire_format(fake_sd, client):
    result = await client.call_tool("set_parameters", {
        "graph_identifier": "G",
        "updates": [
            {"node_id": "n1", "parameter_id": "intensity", "value": 4.0},
            {"node_id": "n2", "parameter_id": "blendingmode",
             "value": 3, "value_type": "int"},
        ]})
    assert not result.isError
    sent = fake_sd.received[0]
    assert sent["type"] == "set_parameters"
    assert sent["params"]["graph_identifier"] == "G"
    assert sent["params"]["updates"] == [
        {"node_id": "n1", "parameter_id": "intensity", "value": 4.0,
         "value_type": "float"},
        {"node_id": "n2", "parameter_id": "blendingmode", "value": 3,
         "value_type": "int"}]


async def test_set_parameters_schema_documents_fields(client):
    tools = {t.name: t for t in (await client.list_tools()).tools}
    defs = tools["set_parameters"].inputSchema["$defs"]
    props = defs["BatchParameter"]["properties"]
    for field in ("node_id", "parameter_id", "value", "value_type"):
        assert field in props and props[field].get("description"), field
    assert set(defs["BatchParameter"]["required"]) == {
        "node_id", "parameter_id", "value"}


async def test_set_parameters_invalid_update_rejected_before_sd(fake_sd, client):
    result = await client.call_tool("set_parameters", {
        "updates": [{"node_id": "n1"}]})
    assert result.isError
    assert fake_sd.received == []


async def test_set_parameters_reports_partial_failures(fake_sd, client):
    reply = {"graph": "G", "applied": 1, "failed": 1,
             "results": [
                 {"node_id": "n1", "parameter_id": "intensity",
                  "value": 1.0, "value_type": "float", "status": "ok"},
                 {"node_id": "n2", "parameter_id": "nope",
                  "value": 1.0, "value_type": "float", "status": "error",
                  "error": "Property 'nope' not found on node 'n2'."}]}
    fake_sd.handler = lambda t, p: {"status": "success", "result": reply}
    result = await client.call_tool("set_parameters", {
        "updates": [{"node_id": "n1", "parameter_id": "intensity", "value": 1.0},
                    {"node_id": "n2", "parameter_id": "nope", "value": 1.0}]})
    assert not result.isError
    assert json.loads(_text(result)) == reply


# ── Tool metadata ──────────────────────────────────────────────────────────

_TYPED_TOOLS = {
    "create_graph", "get_graph_info", "create_node", "set_parameter",
    "set_parameters", "create_batch_graph", "build_material_graph",
    "build_heightmap_graph", "apply_recipe", "send_to_3d_view",
}


async def test_every_tool_has_annotations(client):
    tools = (await client.list_tools()).tools
    assert len(tools) >= 40
    for tool in tools:
        assert tool.annotations is not None, tool.name
        assert tool.annotations.readOnlyHint is not None, tool.name
        assert tool.annotations.openWorldHint is False, tool.name
    by_name = {t.name: t for t in tools}
    assert by_name["get_scene_info"].annotations.readOnlyHint is True
    assert by_name["delete_node"].annotations.destructiveHint is True
    assert by_name["execute_sd_code"].annotations.destructiveHint is True
    assert "reload_plugin" in by_name
    assert by_name["set_parameters"].annotations.readOnlyHint is False
    assert by_name["inspect_outputs"].annotations.readOnlyHint is True


async def test_typed_tools_declare_output_schemas(client):
    """Structured-output tools carry a client-validatable schema; the rest opt
    out (JSON text only)."""
    tools = (await client.list_tools()).tools
    for tool in tools:
        if tool.name in _TYPED_TOOLS:
            assert tool.outputSchema is not None, tool.name
        else:
            assert tool.outputSchema is None, tool.name
    by_name = {t.name: t for t in tools}
    schema = by_name["set_parameters"].outputSchema
    assert schema["properties"]["applied"]["type"] == "integer"
    assert schema["properties"]["graph"]["type"] == "string"


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


async def test_reload_plugin_disabled_by_default(fake_sd, client, monkeypatch):
    monkeypatch.setattr(bridge, "_allow_dev_tools", False)
    result = await client.call_tool("reload_plugin", {"handlers": False})
    assert result.isError
    assert "disabled by default" in _text(result)
    assert fake_sd.received == []


async def test_reload_plugin_command(fake_sd, client, monkeypatch):
    monkeypatch.setattr(bridge, "_allow_dev_tools", True)
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


# ── send_to_3d_view ────────────────────────────────────────────────────────

async def test_send_to_3d_view_polls_until_done(fake_sd, client):
    polls = []

    def handler(cmd_type, params):
        if cmd_type == "send_to_3d_view":
            return {"status": "success",
                    "result": {"job_id": 7, "graph": "G", "targets": [], "skipped": []}}
        polls.append(params)
        done = len(polls) >= 2
        return {"status": "success",
                "result": {"job_id": 7, "graph": "G", "done": done,
                           "bound": [{"node": "1", "channel": "Base Color"}] if done else [],
                           "failed": [], "skipped": [], "pending": []}}

    fake_sd.handler = handler
    result = await client.call_tool("send_to_3d_view", {
        "graph_identifier": "G", "channels": {"1": "Base Color"}})
    assert not result.isError
    status = json.loads(result.content[0].text)
    assert status["done"] is True
    assert status["bound"] == [{"node": "1", "channel": "Base Color"}]
    assert fake_sd.received[0]["params"] == {
        "graph_identifier": "G", "channels": {"1": "Base Color"}}
    assert all(p == {"job_id": 7} for p in polls)


# ── Security gate: execute_sd_code ─────────────────────────────────────────

async def test_execute_sd_code_disabled_by_default(fake_sd, client, monkeypatch):
    monkeypatch.delenv("SD_MCP_ALLOW_EXECUTE_CODE", raising=False)
    monkeypatch.setattr(bridge, "_allow_arbitrary_code", False)
    result = await client.call_tool("execute_sd_code", {"code": "print('hi')"})
    assert result.isError
    assert "disabled by default" in _text(result)
    assert fake_sd.received == []  # nothing reaches the plugin


async def test_execute_sd_code_enabled_when_opted_in(fake_sd, client, monkeypatch):
    monkeypatch.setattr(bridge, "_allow_arbitrary_code", True)
    fake_sd.handler = lambda t, p: {"status": "success",
                                    "result": {"executed": True, "stdout": "hi"}}
    result = await client.call_tool("execute_sd_code", {"code": "print('hi')"})
    assert not result.isError
    assert fake_sd.received == [{"type": "execute_code",
                                 "params": {"code": "print('hi')"}}]


async def test_token_attached_when_configured(fake_sd, client, monkeypatch):
    monkeypatch.setattr(bridge, "_auth_token", "s3cret")
    await client.call_tool("get_scene_info", {})
    received = fake_sd.received[0]
    assert received["token"] == "s3cret"
    # The extra key must not leak into the documented params payload.
    assert received["params"] == {"include_library": False}


async def test_no_token_field_by_default(fake_sd, client, monkeypatch):
    monkeypatch.setattr(bridge, "_auth_token", None)
    await client.call_tool("get_scene_info", {})
    assert "token" not in fake_sd.received[0]


# ── Typed (structured) outputs ─────────────────────────────────────────────

async def test_set_parameters_returns_structured_content(fake_sd, client):
    reply = {"graph": "G", "applied": 2, "failed": 0,
             "results": [
                 {"node_id": "n1", "parameter_id": "intensity",
                  "value": 4.0, "value_type": "float", "status": "ok"},
                 {"node_id": "n2", "parameter_id": "blendingmode",
                  "value": 3, "value_type": "int", "status": "ok"}]}
    fake_sd.handler = lambda t, p: {"status": "success", "result": reply}
    result = await client.call_tool("set_parameters", {
        "updates": [{"node_id": "n1", "parameter_id": "intensity", "value": 4.0},
                    {"node_id": "n2", "parameter_id": "blendingmode",
                     "value": 3, "value_type": "int"}]})
    assert not result.isError
    assert result.structuredContent == reply
    # Unstructured text stays valid JSON for text-only clients.
    assert json.loads(_text(result)) == reply


async def test_batch_graph_build_returns_structured_content(fake_sd, client):
    reply = {"graph_identifier": "G", "requested_name": "G",
             "nodes_created": 2, "nodes_failed": 0,
             "connections_ok": 1, "connections_failed": 0,
             "node_map": {"a": "101", "out": "102"},
             "nodes": [{"alias": "a", "node_id": "101",
                        "definition": "sbs::compositing::blur"}],
             "failed_nodes": [],
             "connections": [{"from": "a", "to": "out", "success": True}]}
    fake_sd.handler = lambda t, p: {"status": "success", "result": reply}
    result = await client.call_tool("create_batch_graph", {
        "graph_name": "G",
        "nodes": [{"id_alias": "a", "definition_id": "sbs::compositing::blur"}],
        "connections": []})
    assert not result.isError
    assert result.structuredContent == reply
    assert result.structuredContent["node_map"]["a"] == "101"


async def test_batch_preflight_gets_its_own_shape(fake_sd, client):
    reply = {"preflight": True, "requested_name": "G", "graph_name": "G",
             "valid": False,
             "errors": [{"kind": "builtin", "alias": "b", "error": "unknown"}],
             "warnings": [], "error_count": 1, "warning_count": 0,
             "nodes_checked": 1, "connections_checked": 0}
    fake_sd.handler = lambda t, p: {"status": "success", "result": reply}
    result = await client.call_tool("create_batch_graph", {
        "graph_name": "G", "preflight": True})
    assert not result.isError
    assert result.structuredContent == reply
    assert result.structuredContent["valid"] is False


async def test_typed_output_reports_unexpected_shape(fake_sd, client):
    """A plugin reply that violates the declared schema comes back as a
    protocol error containing the raw payload, not silently coerced data."""
    fake_sd.handler = lambda t, p: {"status": "success", "result": {"surprise": True}}
    result = await client.call_tool("set_parameters", {
        "updates": [{"node_id": "n1", "parameter_id": "intensity", "value": 1.0}]})
    assert result.isError
    text = _text(result)
    assert "Validation" in text or "validation" in text
    assert "surprise" in text  # raw JSON is kept for debugging


async def test_typed_output_accepts_extra_fields(fake_sd, client):
    """Plugin versions may add fields; extra keys must survive, not be dropped."""
    reply = {"graph_identifier": "G", "requested_name": "G",
             "nodes_created": 0, "nodes_failed": 0,
             "connections_ok": 0, "connections_failed": 0,
             "node_map": {}, "nodes": [], "failed_nodes": [],
             "connections": [], "future_field": {"x": 1}}
    fake_sd.handler = lambda t, p: {"status": "success", "result": reply}
    result = await client.call_tool("create_batch_graph", {"graph_name": "G"})
    assert not result.isError
    assert result.structuredContent["future_field"] == {"x": 1}


# ── diagnostic / graph_snapshot ────────────────────────────────────────────

async def test_diagnostic_is_forwarded_read_only(fake_sd, client):
    reply = {"sd_version": "15.1.2", "version_result": True, "main_thread_pid": 1,
             "running_thread_count": 2, "main_thread_stack": [],
             "python_version": "3.11.9", "qt_binding": "PySide6"}
    fake_sd.handler = lambda t, p: {"status": "success", "result": reply}
    result = await client.call_tool("diagnostic", {})
    assert not result.isError
    assert fake_sd.received == [{"type": "diagnostic", "params": {}}]
    assert json.loads(_text(result)) == reply
    assert fake_sd.received[0]["params"] == {}  # truly read-only


async def test_graph_snapshot_forwards_graph_identifier(fake_sd, client):
    reply = {"graph": "G", "nodes": [], "connections": []}
    fake_sd.handler = lambda t, p: {"status": "success", "result": reply}
    result = await client.call_tool("graph_snapshot", {"graph_identifier": "G"})
    assert not result.isError
    assert fake_sd.received == [{"type": "graph_snapshot",
                                 "params": {"graph_identifier": "G"}}]
    assert json.loads(_text(result)) == reply


# ── Long jobs ──────────────────────────────────────────────────────────────

async def test_start_long_job_validates_and_forwards(fake_sd, client):
    job = {"job_id": 3, "op": "inspect_outputs", "graph": "G", "started": True}
    fake_sd.handler = lambda t, p: {"status": "success", "result": job}

    bad = await client.call_tool("start_long_job", {"job_type": "nonsense"})
    assert bad.isError
    assert fake_sd.received == []  # unknown job type rejected before SD

    result = await client.call_tool("start_long_job", {
        "job_type": "inspect_outputs", "params": {"graph_identifier": "G"}})
    assert not result.isError
    assert fake_sd.received[-1] == {"type": "start_job",
                                    "params": {"op": "inspect_outputs",
                                               "params": {"graph_identifier": "G"}}}
    assert json.loads(_text(result))["job_id"] == 3


async def test_wait_long_job_polls_until_done(fake_sd, client):
    polls = []

    def handler(cmd_type, params):
        assert cmd_type == "job_status"
        polls.append(params)
        done = len(polls) >= 2
        return {"status": "success",
                "result": {"job_id": 9, "op": "get_map_stats", "done": done,
                           "stage": "compute", "stage_index": 0 if not done else 1,
                           "progress": 0.25 if not done else 1.0,
                           "stages_total": 4, "cancelled": False,
                           "result": {"node": "n1", "channels": {}},
                           "error": None}}

    fake_sd.handler = handler
    result = await client.call_tool("wait_long_job", {
        "job_id": 9, "timeout_s": 10, "poll_s": 0.2})
    assert not result.isError
    status = json.loads(_text(result))
    assert status["done"] is True
    assert status["result"]["node"] == "n1"  # final result flattened
    assert status["progress"] == 1.0
    assert all(p == {"job_id": 9} for p in polls)


async def test_wait_long_job_timeout_note(fake_sd, client):
    fake_sd.handler = lambda t, p: {"status": "success",
                                    "result": {"job_id": 5, "done": False,
                                               "stage": "compute"}}
    result = await client.call_tool("wait_long_job", {
        "job_id": 5, "timeout_s": 0.5, "poll_s": 0.2})
    assert not result.isError  # never hard-fails; the note says so
    status = json.loads(_text(result))
    assert status["done"] is False
    assert "Timed out" in status.get("note", "")
    assert status["job_id"] == 5


async def test_get_job_status_and_cancel_forwarded(fake_sd, client):
    fake_sd.handler = lambda t, p: {"status": "success", "result": {"job_id": 4}}

    await client.call_tool("get_job_status", {"job_id": 4})
    assert fake_sd.received[-1] == {"type": "job_status", "params": {"job_id": 4}}

    await client.call_tool("get_job_status", {})  # None = latest job
    assert fake_sd.received[-1] == {"type": "job_status", "params": {"job_id": None}}

    await client.call_tool("cancel_long_job", {"job_id": 4})
    assert fake_sd.received[-1] == {"type": "job_cancel", "params": {"job_id": 4}}


# ── Batch recovery flags ───────────────────────────────────────────────────

async def test_batch_recovery_flags_forwarded(fake_sd, client):
    fake_sd.handler = lambda t, p: {
        "status": "success",
        "result": {"preflight": True, "requested_name": "G", "graph_name": "G",
                   "valid": True, "errors": [], "warnings": [],
                   "error_count": 0, "warning_count": 0,
                   "nodes_checked": 1, "connections_checked": 0}}

    await client.call_tool("create_batch_graph", {
        "graph_name": "G", "preflight": True, "cleanup_on_failure": False})
    sent = fake_sd.received[-1]["params"]
    assert sent["preflight"] is True
    assert sent["cleanup_on_failure"] is False


async def test_recipe_build_defaults_cleanup_on_failure(fake_sd, client):
    fake_sd.handler = lambda t, p: {
        "status": "success",
        "result": {"graph_identifier": "Steel", "requested_name": "Steel",
                   "nodes_created": 5, "nodes_failed": 0,
                   "connections_ok": 4, "connections_failed": 0,
                   "node_map": {}, "nodes": [], "failed_nodes": [],
                   "connections": []}}
    result = await client.call_tool("build_material_graph", {
        "graph_name": "Steel", "recipe_name": "steel"})
    assert not result.isError
    sent = fake_sd.received[-1]["params"]
    assert sent["cleanup_on_failure"] is True  # builders default to cleanup
    assert sent["preflight"] is False
