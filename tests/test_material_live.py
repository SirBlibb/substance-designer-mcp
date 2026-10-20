"""Opt-in live test for the material-run workflow (needs SD + plugin >= 3.9.0).

Skipped when SD is not listening (conftest marks `live` tests) and when the
loaded plugin predates the checkpoint commands. Runs against a scratch package
saved under tmp_path so the user's open graphs are never targeted; the scratch
package stays loaded in SD afterwards (there is no unload-only command).
"""
import json
import os
import uuid

import pytest

import sd_mcp_bridge as bridge
from conftest import SD_LIVE_PORT

pytestmark = [pytest.mark.live, pytest.mark.anyio]


@pytest.fixture(autouse=True)
def live_port(monkeypatch):
    monkeypatch.setattr(bridge, "_sd_port", SD_LIVE_PORT)


def _json(result):
    assert not result.isError, result.content[0].text
    return json.loads(result.content[0].text)


async def _scene(client):
    return _json(await client.call_tool("get_scene_info", {}))


def _plugin_version(scene):
    try:
        return tuple(int(part) for part in scene["plugin_version"].split("."))
    except (KeyError, ValueError):
        return (0, 0, 0)


async def _new_scratch_package(client, tmp_path):
    await client.call_tool("create_package", {})
    scene = await _scene(client)
    candidates = [entry for entry in scene["packages"]
                  if not entry.get("file_path") and not entry.get("graphs")]
    assert candidates, "could not identify the new scratch package"
    package_index = candidates[-1]["index"]
    return package_index


@pytest.fixture
async def scratch(client, tmp_path, monkeypatch):
    scene = await _scene(client)
    if _plugin_version(scene) < (3, 9, 0):
        pytest.skip(
            "The loaded SD plugin ({}) predates the material-run commands; "
            "restart Substance Designer to load plugin 3.9.0.".format(
                scene.get("plugin_version", "unknown")))
    monkeypatch.setattr(bridge, "_runs_dir", str(tmp_path / "runs"))
    bridge._reset_material_runs()
    package_index = await _new_scratch_package(client, tmp_path)

    graph_name = "MR_Live_{}".format(uuid.uuid4().hex[:6])
    graph = _json(await client.call_tool("create_graph", {
        "graph_name": graph_name, "package_index": package_index}))
    package_path = str(tmp_path / "scratch.sbs")
    await client.call_tool("save_package", {
        "package_index": package_index, "file_path": package_path})
    scene = await _scene(client)
    for entry in scene["packages"]:
        if entry.get("file_path") and \
                os.path.normcase(os.path.abspath(entry["file_path"])) == \
                os.path.normcase(os.path.abspath(package_path)):
            package_index = entry["index"]
            break

    await client.call_tool("create_batch_graph", {
        "graph_name": graph["identifier"],
        "package_index": package_index,
        "output_size_log2": 9,
        "nodes": [
            {"id_alias": "color", "definition_id": "sbs::compositing::uniform"},
            {"id_alias": "height", "definition_id": "sbs::compositing::output",
             "usage": "height", "label": "Height"},
        ],
        "connections": [
            {"from": "color", "to": "height", "to_input": "inputNodeOutput"},
        ],
    })
    try:
        yield {"graph": graph["identifier"], "package_index": package_index,
               "package_path": package_path}
    finally:
        await client.call_tool("delete_graph", {
            "graph_identifier": graph["identifier"]})
        await client.call_tool("save_package", {
            "package_index": package_index})


async def test_live_material_run_workflow(client, scratch):
    graph = scratch["graph"]
    started = _json(await client.call_tool("start_material_run", {
        "graph_identifier": graph,
        "goal": "live workflow test",
        "max_iterations": 1,
        "preview_preset": {"include_3d": False, "max_size": 512},
        "package_index": scratch["package_index"],
        "package_path": scratch["package_path"],
    }))
    run_id = started["run"]["run_id"]

    baseline = _json(await client.call_tool(
        "checkpoint_material_revision", {"run_id": run_id, "label": "baseline"}))
    revision = baseline["revision"]
    assert revision["verified"] is True
    assert os.path.isfile(revision["checkpoint_path"])

    verified = _json(await client.call_tool(
        "verify_material_checkpoint", {"run_id": run_id}))
    assert verified["verified"] is True

    node_id = _json(await client.call_tool(
        "get_graph_info", {"graph_identifier": graph}))["nodes"][0]["identifier"]

    proposed = _json(await client.call_tool("propose_material_patch", {
        "run_id": run_id,
        "operations": [{
            "op": "set_parameter", "node_id": node_id,
            "parameter_id": "outputcolor", "value": [0.8, 0.2, 0.1],
            "value_type": "color",
        }],
    }))
    candidate = proposed["candidate"]
    assert candidate["validation"]["valid"] is True, candidate["validation"]

    applied = _json(await client.call_tool(
        "apply_material_patch", {"run_id": run_id}))
    assert applied["status"] == "applied", applied
    assert applied["run"]["state"] == "awaiting_evaluation"

    preview = _json(await client.call_tool(
        "capture_material_preview", {"run_id": run_id, "return_image": False}))
    assert preview["technical_passed"] is True, preview["technical_checks"]
    assert any(a["kind"] == "outputs_sheet" for a in preview["artifacts"])

    evaluation = _json(await client.call_tool(
        "record_material_evaluation",
        {"run_id": run_id, "verdict": "live smoke", "accepted": True}))
    assert evaluation["evaluation"]["technical_passed"] is True

    decision = _json(await client.call_tool(
        "decide_material_candidate", {"run_id": run_id}))
    assert decision["decision"] == "accepted"
    best_id = decision["run"]["best_revision_id"]

    restored = _json(await client.call_tool("restore_material_revision", {
        "run_id": run_id, "revision_id": revision["revision_id"],
        "force": True}))
    assert restored["revision"]["revision_id"] == revision["revision_id"]

    finished = _json(await client.call_tool(
        "finish_material_run", {"run_id": run_id, "notes": "live"}))
    assert finished["run"]["state"] == "finished"
    assert finished["report"]["best_revision_id"] == best_id
