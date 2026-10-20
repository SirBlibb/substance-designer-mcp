"""Smoke tests against a running Substance Designer (skipped when port 9881 is closed).

Read-only: they do not create or modify graphs.
"""
import json
import os

import pytest

import sd_mcp_bridge as bridge
from conftest import SD_LIVE_PORT

pytestmark = [pytest.mark.live, pytest.mark.anyio]


@pytest.fixture(autouse=True)
def live_port(monkeypatch):
    monkeypatch.setattr(bridge, "_sd_port", SD_LIVE_PORT)


async def test_live_scene_info(client):
    result = await client.call_tool("get_scene_info", {})
    assert not result.isError, result.content[0].text
    assert isinstance(json.loads(result.content[0].text), dict)


async def test_live_capture_node_preview(client):
    scene = json.loads((await client.call_tool("get_scene_info", {})).content[0].text)
    graph = scene["current_graph"]
    info = json.loads((await client.call_tool(
        "get_graph_info", {"graph_identifier": graph})).content[0].text)
    node_id = info["nodes"][0]["identifier"]

    result = await client.call_tool("capture_node_preview", {
        "graph_identifier": graph,
        "node_id": node_id,
        "max_size": 128,
        "return_image": False,
    })
    assert not result.isError, result.content[0].text
    payload = json.loads(result.content[0].text)
    assert payload["method"] == "node_preview"
    assert payload["node"] == node_id
    assert payload["width"] > 0 and payload["height"] > 0
    assert os.path.isfile(payload["path"])
    assert payload["capture"] in ("preview_widget", "graph_view_crop")


async def test_live_unknown_node_is_tool_error(client):
    result = await client.call_tool("connect_nodes", {
        "from_node_id": "does_not_exist", "to_node_id": "does_not_exist_either"})
    assert result.isError


async def test_live_documentation_categories(client):
    result = await client.call_tool("list_documentation", {"action": "categories"})
    assert not result.isError
    assert "port_reference" in result.content[0].text
