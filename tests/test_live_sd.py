"""Smoke tests against a running Substance Designer (skipped when port 9881 is closed).

Read-only: they do not create or modify graphs.
"""
import json

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


async def test_live_unknown_node_is_tool_error(client):
    result = await client.call_tool("connect_nodes", {
        "from_node_id": "does_not_exist", "to_node_id": "does_not_exist_either"})
    assert result.isError


async def test_live_documentation_categories(client):
    result = await client.call_tool("list_documentation", {"action": "categories"})
    assert not result.isError
    assert "port_reference" in result.content[0].text
