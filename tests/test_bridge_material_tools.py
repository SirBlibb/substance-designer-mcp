"""Bridge tests for the material-run tools against a fake SD plugin."""
import json
import os

import pytest

import sd_mcp_bridge as bridge
from test_material_runs import FakeDesigner

pytestmark = pytest.mark.anyio


def _json(result):
    return json.loads(result.content[0].text)


@pytest.fixture
def material_env(fake_sd, tmp_path, monkeypatch):
    fake = FakeDesigner(str(tmp_path))

    def handler(cmd_type, params):
        try:
            return {"status": "success", "result": fake(cmd_type, params)}
        except Exception as exc:  # surface fake failures like the real plugin
            return {"status": "error", "message": str(exc)}

    fake_sd.handler = handler
    monkeypatch.setattr(bridge, "_runs_dir", str(tmp_path / "runs"))
    bridge._reset_material_runs()
    yield fake
    bridge._reset_material_runs()


async def _start(client, **kwargs):
    payload = {"graph_identifier": "G"}
    payload.update(kwargs)
    return _json(await client.call_tool("start_material_run", payload))["run"]


async def test_start_and_read_material_run(client, material_env):
    run = await _start(client, goal="more cavity", max_iterations=2)
    assert run["state"] == "created"
    assert run["goal"] == "more cavity"

    bundle = _json(await client.call_tool("get_material_run", {}))
    assert bundle["run"]["run_id"] == run["run_id"]
    assert bundle["revisions"] == []
    assert bundle["pending_candidate"] is None

    listed = _json(await client.call_tool("list_material_runs", {}))
    assert listed["count"] == 1
    assert listed["runs"][0]["run_id"] == run["run_id"]


async def test_checkpoint_and_verify_tools(client, material_env):
    run = await _start(client)
    checkpoint = _json(await client.call_tool(
        "checkpoint_material_revision",
        {"run_id": run["run_id"], "label": "baseline"}))
    revision = checkpoint["revision"]
    assert checkpoint["run"]["state"] == "baseline_ready"
    assert revision["verified"] is True
    assert os.path.isfile(revision["checkpoint_path"])

    verified = _json(await client.call_tool(
        "verify_material_checkpoint", {"run_id": run["run_id"]}))
    assert verified["verified"] is True
    assert verified["revision_id"] == revision["revision_id"]


async def test_full_material_cycle_tools(client, material_env):
    run = await _start(client)
    run_id = run["run_id"]
    await client.call_tool("checkpoint_material_revision", {"run_id": run_id})
    await client.call_tool("capture_material_preview", {"run_id": run_id})

    proposed = _json(await client.call_tool(
        "propose_material_patch",
        {"run_id": run_id, "operations": [{
            "op": "set_parameter", "node_id": "blur",
            "parameter_id": "intensity", "value": 9.0}]}))
    candidate = proposed["candidate"]
    assert candidate["validation"]["valid"] is True

    applied = _json(await client.call_tool(
        "apply_material_patch", {"run_id": run_id}))
    assert applied["status"] == "applied"
    assert applied["run"]["state"] == "awaiting_evaluation"

    preview = await client.call_tool(
        "capture_material_preview", {"run_id": run_id})
    assert not preview.isError
    assert len(preview.content) == 4
    payload = _json(preview)
    assert payload["technical_passed"] is True
    assert [a["kind"] for a in payload["artifacts"]] == [
        "outputs_sheet", "render_3d", "comparison_sheet"]
    assert payload["comparison_control"]["controlled"] is True
    assert all(item.type == "image" for item in preview.content[1:])

    evaluation = _json(await client.call_tool(
        "record_material_evaluation",
        {"run_id": run_id, "verdict": "better", "summary": "works"}))
    assert evaluation["evaluation"]["accepted"] is True

    decision = _json(await client.call_tool(
        "decide_material_candidate", {"run_id": run_id}))
    assert decision["decision"] == "accepted"
    assert decision["run"]["state"] == "baseline_ready"

    finished = _json(await client.call_tool(
        "finish_material_run", {"run_id": run_id, "notes": "done"}))
    assert finished["run"]["state"] == "finished"
    assert finished["report"]["iterations_used"] == 1


async def test_hypothesis_tools_round_trip(client, material_env):
    run = await _start(client, require_hypothesis=True)
    run_id = run["run_id"]
    assert run["require_hypothesis"] is True
    await client.call_tool("checkpoint_material_revision", {"run_id": run_id})
    await client.call_tool("capture_material_preview", {"run_id": run_id})
    operations = [{"op": "set_parameter", "node_id": "blur",
                   "parameter_id": "intensity", "value": 9.0}]

    bare = await client.call_tool(
        "propose_material_patch", {"run_id": run_id, "operations": operations})
    assert bare.isError
    assert "requires a hypothesis" in bare.content[0].text

    await client.call_tool("propose_material_patch", {
        "run_id": run_id, "operations": operations,
        "hypothesis": {
            "defect": "roughness too dark on peaks",
            "expected_effect": "roughness brightens",
            "predictions": [{"output": "roughness", "direction": "increase",
                             "min_change": 0.1}]}})
    await client.call_tool("apply_material_patch", {"run_id": run_id})
    await client.call_tool("capture_material_preview", {"run_id": run_id})
    evaluation = _json(await client.call_tool("record_material_evaluation", {
        "run_id": run_id, "observed_effect": "peaks lighter",
        "hypothesis_outcome": "confirmed"}))["evaluation"]
    assert evaluation["hypothesis_check"]["measured_outcome"] == "confirmed"
    assert evaluation["hypothesis_check"]["observed_effect"] == "peaks lighter"

    bundle = _json(await client.call_tool("get_material_run", {"run_id": run_id}))
    assert [e["defect"] for e in bundle["hypothesis_log"]] == [
        "roughness too dark on peaks"]


async def test_graph_analysis_and_delivery_tools(client, material_env):
    material_env.export_bits = 16
    run = await _start(client, material_profile="dielectric", delivery={})
    run_id = run["run_id"]
    assert run["appearance"]["profile"] == "dielectric"
    assert run["delivery_required"] is True

    analysis = _json(await client.call_tool(
        "analyze_material_graph", {"graph_identifier": "G"}))
    assert analysis["nodes"]["noise"]["role"] == "surface_grain"
    assert "Roughness" in analysis["outputs"]

    await client.call_tool("checkpoint_material_revision", {"run_id": run_id})
    delivery = _json(await client.call_tool(
        "validate_material_delivery", {"run_id": run_id}))["delivery"]
    assert delivery["passed"] is True
    assert all(os.path.isfile(f["path"]) for f in delivery["files"])

    finished = _json(await client.call_tool(
        "finish_material_run", {"run_id": run_id}))
    assert finished["report"]["delivery"]["passed"] is True


async def test_rejected_candidate_tool_restores_base(client, material_env):
    run = await _start(client)
    run_id = run["run_id"]
    await client.call_tool("checkpoint_material_revision", {"run_id": run_id})
    await client.call_tool("capture_material_preview", {"run_id": run_id})
    await client.call_tool("propose_material_patch", {
        "run_id": run_id, "operations": [{
            "op": "set_parameter", "node_id": "blur",
            "parameter_id": "intensity", "value": 9.0}]})
    await client.call_tool("apply_material_patch", {"run_id": run_id})
    await client.call_tool("capture_material_preview", {"run_id": run_id})
    await client.call_tool("record_material_evaluation", {
        "run_id": run_id, "verdict": "worse", "accepted": False})

    decision = _json(await client.call_tool(
        "decide_material_candidate", {"run_id": run_id}))
    assert decision["decision"] == "rejected"
    assert decision["rollback"]["restored"] is True
    assert material_env.nodes["blur"]["params"]["intensity"][0] == 5.0


async def test_material_tool_reports_domain_error(client, material_env):
    run = await _start(client)
    result = await client.call_tool(
        "apply_material_patch", {"run_id": run["run_id"]})
    assert result.isError
    assert "requires state" in result.content[0].text


async def test_restore_material_revision_tool(client, material_env):
    run = await _start(client)
    run_id = run["run_id"]
    await client.call_tool("checkpoint_material_revision", {"run_id": run_id})
    material_env.nodes["blur"]["params"]["intensity"][0] = 2.0

    refused = await client.call_tool(
        "restore_material_revision", {"run_id": run_id})
    assert refused.isError
    assert "un-checkpointed changes" in refused.content[0].text

    restored = _json(await client.call_tool(
        "restore_material_revision", {"run_id": run_id, "force": True}))
    assert restored["run"]["state"] == "baseline_ready"
    assert material_env.nodes["blur"]["params"]["intensity"][0] == 5.0


async def test_preview_tool_can_skip_images(client, material_env):
    run = await _start(client)
    await client.call_tool("checkpoint_material_revision",
                           {"run_id": run["run_id"]})
    result = await client.call_tool(
        "capture_material_preview",
        {"run_id": run["run_id"], "return_image": False})
    assert not result.isError
    assert len(result.content) == 1
    assert _json(result)["artifacts"]
