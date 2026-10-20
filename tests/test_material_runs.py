"""Material-run state machine tests against a fake SD plugin (no SD needed)."""
import json
import os
import threading

import pytest

import sd_material_runs as mr

GRAPH = "G"
USAGES = ["height", "normal", "ambientOcclusion", "baseColor",
          "roughness", "metallic"]
LABELS = {
    "height": "Height", "normal": "Normal",
    "ambientOcclusion": "Ambient Occlusion", "baseColor": "Base Color",
    "roughness": "Roughness", "metallic": "Metallic",
}


class FakeDesigner:
    """Minimal in-memory SD plugin: package file, graph state, checkpoints.

    Checkpoints are JSON snapshots of the graph state, so verify_checkpoint
    really re-reads what checkpoint_package wrote. Knobs let tests inject
    corruption, command failures and channel regressions.
    """

    def __init__(self, root):
        self.root = root
        self.package_path = os.path.join(root, "pack.sbs")
        with open(self.package_path, "w", encoding="utf-8") as fh:
            fh.write("fake package")
        self.nodes = {
            "noise": {"definition": "sbs::compositing::perlin_noise",
                      "params": {"disorder": [0.5, 3.0]}},
            "blur": {"definition": "sbs::compositing::blur",
                     "params": {"intensity": [5.0, 4.0]}},
        }
        self.outputsize = [10, 10]
        self.calls = []
        self.checkpoints = {}
        self.corrupt_checkpoints = False
        self.fail_node = None
        self.fail_restore = False
        self.fail_verify = False
        self.drop_usage = None
        self.effect = 0.5
        self.export_bits = 8
        self.export_size = 2048
        self.export_skip = None
        self.compose_supported = True
        self.active = 0
        self.max_active = 0
        self._active_lock = threading.Lock()

    # ── helpers ────────────────────────────────────────────────────────
    def snapshot(self):
        return {"nodes": json.loads(json.dumps(self.nodes)),
                "outputsize": list(self.outputsize)}

    def load(self, snapshot):
        self.nodes = json.loads(json.dumps(snapshot["nodes"]))
        self.outputsize = list(snapshot["outputsize"])

    def fingerprint(self):
        nodes = []
        for nid, node in sorted(self.nodes.items()):
            inputs = [{"id": pid, "value": val[0], "connected": False}
                      for pid, val in sorted(node["params"].items())]
            nodes.append({"id": nid, "definition": node["definition"],
                          "position": [100.0, 200.0], "is_output": False,
                          "is_library": False, "inputs": inputs,
                          "annotations": []})
        connections = [{"from_node": "noise",
                        "from_output": "unique_filter_output",
                        "to_node": "blur", "to_input": "input1"}]
        for usage in USAGES:
            nodes.append({"id": "out_" + usage,
                          "definition": "sbs::compositing::output",
                          "position": [400.0, 200.0], "is_output": True,
                          "is_library": False, "inputs": [],
                          "annotations": [{"id": "label", "value": LABELS[usage]}]})
            connections.append({"from_node": "blur",
                                "from_output": "unique_filter_output",
                                "to_node": "out_" + usage, "to_input": "inputNodeOutput"})
        return {
            "graph_identifier": GRAPH,
            "nodes": nodes,
            "connections": connections,
            "graph_inputs": [{
                "id": "$outputsize",
                "value": {"x": self.outputsize[0], "y": self.outputsize[1]},
            }],
            "output_node_ids": ["out_" + u for u in USAGES],
        }

    # ── command dispatch ───────────────────────────────────────────────
    def __call__(self, cmd, params):
        with self._active_lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.calls.append((cmd, dict(params)))
            return self._dispatch(cmd, params)
        finally:
            with self._active_lock:
                self.active -= 1

    def _dispatch(self, cmd, params):
        if cmd == "get_graph_info":
            return {"identifier": GRAPH, "node_count": len(self.nodes)}
        if cmd == "get_scene_info":
            return {"packages": [{
                "index": 0, "file_path": self.package_path,
                "graphs": [{"identifier": GRAPH}],
            }]}
        if cmd == "graph_fingerprint":
            return self.fingerprint()
        if cmd == "checkpoint_package":
            path = os.path.abspath(params["checkpoint_path"])
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fingerprint = self.fingerprint()
            if self.corrupt_checkpoints:
                fingerprint["nodes"][1]["inputs"][0]["value"] += 1.0
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"fingerprint": fingerprint,
                           "state": self.snapshot()}, fh)
            self.checkpoints[path] = True
            return {"checkpoint_path": path,
                    "package_path": self.package_path,
                    "graph_identifier": GRAPH,
                    "fingerprint": self.fingerprint(),
                    "size": os.path.getsize(path)}
        if cmd == "verify_checkpoint":
            path = os.path.abspath(params["checkpoint_path"])
            if self.fail_verify:
                raise mr.DesignerError("checkpoint unreadable")
            with open(path, encoding="utf-8") as fh:
                stored = json.load(fh)
            return {"checkpoint_path": path,
                    "graph_identifier": GRAPH,
                    "fingerprint": stored["fingerprint"],
                    "already_loaded": False}
        if cmd == "restore_package":
            if self.fail_restore:
                raise mr.DesignerError("restore failed", )
            path = os.path.abspath(params["checkpoint_path"])
            with open(path, encoding="utf-8") as fh:
                stored = json.load(fh)
            self.load(stored["state"])
            return {"restored_from": path,
                    "restored_to": self.package_path,
                    "package_path": self.package_path,
                    "graph_identifier": GRAPH,
                    "fingerprint": self.fingerprint()}
        if cmd == "get_node_info":
            node = self.nodes.get(params["node_id"])
            if node is None:
                raise mr.DesignerError(
                    "Node '{}' not found.".format(params["node_id"]))
            return {
                "node_id": params["node_id"],
                "definition": node["definition"],
                "inputs": [{"id": pid} for pid in sorted(node["params"])],
                "annotations": [],
                "outputs": [{"id": "unique_filter_output"}],
            }
        if cmd == "set_parameters":
            results = []
            for update in params["updates"]:
                nid, pid = update["node_id"], update["parameter_id"]
                node = self.nodes.get(nid)
                if self.fail_node == nid or node is None or pid not in node["params"]:
                    results.append({"node_id": nid, "parameter_id": pid,
                                    "status": "error", "error": "unknown"})
                    continue
                node["params"][pid][0] = float(update["value"])
                results.append({"node_id": nid, "parameter_id": pid,
                                "status": "ok"})
            applied = sum(1 for r in results if r["status"] == "ok")
            return {"graph": GRAPH, "applied": applied,
                    "failed": len(results) - applied, "results": results}
        if cmd == "set_graph_output_size":
            self.outputsize = [params["width_log2"], params["height_log2"]]
            return {"graph": GRAPH}
        if cmd == "inspect_outputs":
            path = params["output_path"]
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(b"PNG")
            intensity = self.nodes["blur"]["params"]["intensity"][0]
            mean = 0.5 + self.effect * (intensity / 10.0)
            outputs, skipped = [], []
            for usage in params.get("output_usages") or USAGES:
                if self.drop_usage == usage:
                    skipped.append({"usage": usage, "label": LABELS[usage]})
                    continue
                outputs.append({
                    "usage": usage, "label": LABELS[usage],
                    "node": "out_" + usage,
                    "port_id": "unique_filter_output",
                    "width": 16, "height": 16, "pixel_format": "u8",
                    "value_domain": "0..1",
                    "channels": {"L": {"min": 0.01, "max": 0.99,
                                       "mean": mean, "count": 256,
                                       "non_finite": 0}},
                })
            return {"graph": GRAPH, "path": path, "width": 16, "height": 16,
                    "outputs": outputs, "skipped": skipped}
        if cmd == "send_to_3d_view":
            return {"job_id": 1, "graph": GRAPH, "targets": [], "skipped": []}
        if cmd == "send_to_3d_view_status":
            return {"job_id": 1, "graph": GRAPH, "done": True, "bound": [],
                    "failed": [], "pending": [], "skipped": []}
        if cmd == "capture_3d_view":
            path = params["output_path"]
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(b"PNG3D")
            return {"path": path, "width": 8, "height": 8,
                    "method": "printwindow"}
        if cmd == "export_outputs":
            outputs, skipped = [], []
            for usage in params["output_usages"]:
                if usage == self.export_skip:
                    skipped.append({"usage": usage, "reason": "not found"})
                    continue
                path = os.path.join(params["output_dir"], "G_{}.png".format(usage))
                os.makedirs(params["output_dir"], exist_ok=True)
                bits = 16 if (usage == "height" and self.export_bits >= 16) else self.export_bits
                with open(path, "wb") as fh:
                    fh.write(png_header(self.export_size, self.export_size, bits))
                outputs.append({"usage": usage, "label": LABELS.get(usage, usage),
                                "path": path, "width": self.export_size,
                                "height": self.export_size,
                                "pixel_format": "RGBA16" if bits == 16 else "RGBA8"})
            return {"graph": GRAPH, "output_dir": params["output_dir"],
                    "outputs": outputs, "skipped": skipped}
        if cmd == "compose_comparison" and self.compose_supported:
            with open(params["output_path"], "wb") as fh:
                fh.write(b"PNGCMP")
            return {"path": params["output_path"], "width": 64, "height": 32}
        raise mr.DesignerError("Unknown command '{}'".format(cmd))


def png_header(width, height, bits, colour=6):
    import struct
    import zlib
    ihdr = struct.pack(">IIBBBBB", width, height, bits, colour, 0, 0, 0)
    chunk = b"IHDR" + ihdr
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(ihdr)) + chunk
            + struct.pack(">I", zlib.crc32(chunk)))


@pytest.fixture
def env(tmp_path):
    designer = FakeDesigner(str(tmp_path))
    runs_dir = str(tmp_path / "runs")
    store = mr.MaterialRunStore(os.path.join(runs_dir, "runs.sqlite3"))
    controller = mr.MaterialRunController(store, designer, runs_dir)
    return designer, store, controller


def _start(controller, **kwargs):
    return controller.start_run(GRAPH, **kwargs)["run"]["run_id"]


def _baseline(controller, run_id):
    return controller.checkpoint_revision(run_id, label="baseline")


def _propose_and_apply(controller, run_id, operations):
    controller.propose_patch(run_id, operations)
    return controller.apply_patch(run_id)


def _cycle(controller, run_id, value=9.0):
    """apply a blur patch, capture, evaluate (accepted by default)."""
    _propose_and_apply(controller, run_id, [{
        "op": "set_parameter", "node_id": "blur",
        "parameter_id": "intensity", "value": value}])
    preview = controller.capture_preview(run_id)
    evaluation = controller.record_evaluation(
        run_id, verdict="better", summary="test")
    return preview, evaluation


# ── start / checkpoint ─────────────────────────────────────────────────────

def test_start_run_creates_created_state(env):
    designer, store, controller = env
    result = controller.start_run(GRAPH, goal="increase cavity",
                                  max_iterations=3)
    run = result["run"]
    assert run["state"] == "created"
    assert run["graph_identifier"] == GRAPH
    assert run["max_iterations"] == 3
    assert run["preview_preset"]["output_usages"] == USAGES
    assert store.get_run(run["run_id"]) is not None


def test_checkpoint_baseline_is_verified_and_recorded(env):
    designer, store, controller = env
    run_id = _start(controller)
    result = _baseline(controller, run_id)
    revision = result["revision"]
    assert revision["verified"] is True
    assert revision["seq"] == 1
    assert revision["structure_sha256"]
    assert os.path.isfile(revision["checkpoint_path"])
    assert os.path.getsize(revision["checkpoint_path"]) > 0
    assert result["run"]["state"] == "baseline_ready"
    assert result["run"]["current_revision_id"] == revision["revision_id"]
    assert result["run"]["baseline_revision_id"] == revision["revision_id"]
    assert len(store.list_revisions(run_id)) == 1


def test_checkpoint_verification_failure_raises_and_records_nothing(env):
    designer, store, controller = env
    designer.corrupt_checkpoints = True
    run_id = _start(controller)
    with pytest.raises(mr.DesignerError, match="verification failed"):
        _baseline(controller, run_id)
    assert store.list_revisions(run_id) == []
    assert store.get_run(run_id).state == "created"


def test_verify_checkpoint_detects_tampering(env):
    designer, store, controller = env
    run_id = _start(controller)
    revision = _baseline(controller, run_id)["revision"]
    assert controller.verify_checkpoint(run_id)["verified"] is True
    # Rewrite the checkpoint with a different graph state.
    with open(revision["checkpoint_path"], encoding="utf-8") as fh:
        stored = json.load(fh)
    stored["fingerprint"]["nodes"][1]["inputs"][0]["value"] = 1.0
    with open(revision["checkpoint_path"], "w", encoding="utf-8") as fh:
        json.dump(stored, fh)
    result = controller.verify_checkpoint(run_id)
    assert result["verified"] is False
    assert store.get_revision(revision["revision_id"]).verified == 0


def test_pending_candidate_blocks_checkpoint(env):
    designer, store, controller = env
    run_id = _start(controller)
    _baseline(controller, run_id)
    controller.propose_patch(run_id, [{
        "op": "set_parameter", "node_id": "blur",
        "parameter_id": "intensity", "value": 8.0}])
    with pytest.raises(mr.RunError, match="candidate is pending"):
        controller.checkpoint_revision(run_id)


# ── patch validation / application ─────────────────────────────────────────

def test_invalid_patch_is_rejected_and_marked_invalid(env):
    designer, store, controller = env
    run_id = _start(controller)
    _baseline(controller, run_id)
    with pytest.raises(mr.RunError, match="no parameter 'nope'"):
        controller.propose_patch(run_id, [{
            "op": "set_parameter", "node_id": "blur",
            "parameter_id": "nope", "value": 3.0}])
    candidates = store.list_candidates(run_id)
    assert candidates[0].status == "invalid"
    assert store.get_run(run_id).state == "baseline_ready"


def test_invalid_patch_shapes_are_reported(env):
    designer, store, controller = env
    run_id = _start(controller)
    _baseline(controller, run_id)
    with pytest.raises(mr.RunError, match="[Oo]nly one set_output_size"):
        controller.propose_patch(run_id, [
            {"op": "set_output_size", "width_log2": 11, "height_log2": 11},
            {"op": "set_output_size", "width_log2": 12, "height_log2": 12},
        ])
    with pytest.raises(mr.RunError, match="outside"):
        controller.propose_patch(run_id, [
            {"op": "set_output_size", "width_log2": 20, "height_log2": 11},
        ])


def test_apply_creates_second_revision_and_binds_candidate(env):
    designer, store, controller = env
    run_id = _start(controller)
    base = _baseline(controller, run_id)["revision"]
    applied = _propose_and_apply(controller, run_id, [{
        "op": "set_parameter", "node_id": "blur",
        "parameter_id": "intensity", "value": 9.0}])
    assert applied["status"] == "applied"
    assert applied["run"]["state"] == "awaiting_evaluation"
    assert applied["run"]["iteration"] == 1
    candidate = applied["candidate"]
    assert candidate["status"] == "applied"
    result_revision = candidate["apply_result"]["result_revision_id"]
    assert result_revision != base["revision_id"]
    assert applied["apply_result"]["effective_change"] is True
    assert store.get_run(run_id).current_revision_id == result_revision
    assert len(store.list_revisions(run_id)) == 2


def test_apply_failure_rolls_back_and_keeps_run_usable(env):
    designer, store, controller = env
    run_id = _start(controller)
    _baseline(controller, run_id)
    designer.fail_node = "blur"
    result = _propose_and_apply(controller, run_id, [{
        "op": "set_parameter", "node_id": "blur",
        "parameter_id": "intensity", "value": 9.0}])
    assert result["status"] == "failed"
    assert result["rollback"]["restored"] is True
    assert designer.nodes["blur"]["params"]["intensity"][0] == 5.0
    assert store.get_run(run_id).state == "baseline_ready"
    assert store.list_candidates(run_id)[0].status == "apply_failed"


def test_apply_refuses_after_manual_drift(env):
    designer, store, controller = env
    run_id = _start(controller)
    _baseline(controller, run_id)
    controller.propose_patch(run_id, [{
        "op": "set_parameter", "node_id": "blur",
        "parameter_id": "intensity", "value": 9.0}])
    designer.nodes["blur"]["params"]["intensity"][0] = 7.0
    with pytest.raises(mr.RunError, match="graph changed since the checkpoint"):
        controller.apply_patch(run_id)
    assert store.get_run(run_id).state == "baseline_ready"


# ── preview / evaluation / decision ────────────────────────────────────────

def test_cycle_capture_and_evaluation(env):
    designer, store, controller = env
    run_id = _start(controller)
    base = _baseline(controller, run_id)["revision"]
    controller.capture_preview(run_id)
    preview, evaluation = _cycle(controller, run_id)
    assert preview["technical_passed"] is True
    kinds = [a["kind"] for a in preview["artifacts"]]
    assert kinds == ["outputs_sheet", "render_3d", "comparison_sheet"]
    assert all(os.path.isfile(a["path"]) for a in preview["artifacts"])
    assert preview["source_fingerprint"]
    assert evaluation["evaluation"]["technical_passed"] is True
    assert evaluation["evaluation"]["baseline_revision_id"] == base["revision_id"]
    assert evaluation["evaluation"]["comparisons"]["skipped"] is False


def test_evaluation_accepts_by_default_and_advances(env):
    designer, store, controller = env
    run_id = _start(controller)
    _baseline(controller, run_id)
    controller.capture_preview(run_id)
    _cycle(controller, run_id)
    run = store.get_run(run_id)
    applied_id = run.current_revision_id
    decision = controller.decide_candidate(run_id)
    assert decision["decision"] == "accepted"
    assert decision["run"]["state"] == "baseline_ready"
    assert decision["run"]["best_revision_id"] == applied_id
    assert decision["run"]["current_revision_id"] == applied_id


def test_regression_marks_evaluation_unaccepted(env):
    designer, store, controller = env
    run_id = _start(controller)
    _baseline(controller, run_id)
    controller.capture_preview(run_id)
    designer.effect = -1.0  # intensity increase lowers the mean now
    preview, evaluation = _cycle(controller, run_id)
    assert preview["technical_passed"] is True
    assert evaluation["evaluation"]["accepted"] is False
    assert evaluation["evaluation"]["comparisons"]["threshold_failures"]


def test_rejection_restores_base_revision(env):
    designer, store, controller = env
    run_id = _start(controller)
    base = _baseline(controller, run_id)["revision"]
    controller.capture_preview(run_id)
    _cycle(controller, run_id, value=9.0)
    assert designer.nodes["blur"]["params"]["intensity"][0] == 9.0
    decision = controller.decide_candidate(run_id, accepted=False)
    assert decision["decision"] == "rejected"
    assert decision["rollback"]["restored"] is True
    assert designer.nodes["blur"]["params"]["intensity"][0] == 5.0
    assert decision["run"]["current_revision_id"] == base["revision_id"]
    assert decision["run"]["state"] == "baseline_ready"


def test_reject_without_restore_keeps_edits(env):
    designer, store, controller = env
    run_id = _start(controller)
    _baseline(controller, run_id)
    controller.capture_preview(run_id)
    _cycle(controller, run_id)
    decision = controller.decide_candidate(run_id, accepted=False,
                                           restore=False)
    assert decision["rollback"]["attempted"] is False
    assert designer.nodes["blur"]["params"]["intensity"][0] == 9.0


def test_evaluation_without_preview_is_rejected(env):
    designer, store, controller = env
    run_id = _start(controller)
    _baseline(controller, run_id)
    _propose_and_apply(controller, run_id, [{
        "op": "set_parameter", "node_id": "blur",
        "parameter_id": "intensity", "value": 9.0}])
    with pytest.raises(mr.RunError, match="No preview artifact"):
        controller.record_evaluation(run_id, verdict="better")


def test_stale_preview_is_rejected(env):
    designer, store, controller = env
    run_id = _start(controller)
    _baseline(controller, run_id)
    controller.capture_preview(run_id)
    _propose_and_apply(controller, run_id, [{
        "op": "set_parameter", "node_id": "blur",
        "parameter_id": "intensity", "value": 9.0}])
    run = store.get_run(run_id)
    revision = store.get_revision(run.current_revision_id)
    store.insert_artifact(
        run_id=run_id, revision_id=revision.revision_id,
        kind="outputs_sheet", path=None, sha256=None,
        config={"skipped": [], "output_usages": USAGES},
        stats={}, source_fingerprint=revision.full_sha256,
        created_at=1.0)
    with pytest.raises(mr.RunError, match="predates the patch"):
        controller.record_evaluation(run_id, verdict="better")


def test_capture_rejects_manual_drift(env):
    designer, store, controller = env
    run_id = _start(controller)
    _baseline(controller, run_id)
    designer.nodes["blur"]["params"]["intensity"][0] = 2.0
    with pytest.raises(mr.RunError, match="does not match revision"):
        controller.capture_preview(run_id)


def test_missing_output_fails_technical_checks(env):
    designer, store, controller = env
    designer.drop_usage = "normal"
    run_id = _start(controller)
    _baseline(controller, run_id)
    preview = controller.capture_preview(run_id)
    assert preview["technical_passed"] is False
    assert any(s.get("usage") == "normal" for s in preview["skipped_outputs"])
    names = [c["name"] for c in preview["technical_checks"]
             if not c["passed"]]
    assert "output_present:normal" in names


# ── restore / finish ───────────────────────────────────────────────────────

def test_restore_requires_force_when_graph_has_uncheckpointed_changes(env):
    designer, store, controller = env
    run_id = _start(controller)
    _baseline(controller, run_id)
    designer.nodes["blur"]["params"]["intensity"][0] = 2.0
    with pytest.raises(mr.RunError, match="un-checkpointed changes"):
        controller.restore_revision(run_id)
    result = controller.restore_revision(run_id, force=True)
    assert result["run"]["state"] == "baseline_ready"
    assert designer.nodes["blur"]["params"]["intensity"][0] == 5.0


def test_restore_refuses_tampered_checkpoint_file(env):
    designer, store, controller = env
    run_id = _start(controller)
    baseline = _baseline(controller, run_id)["revision"]
    with open(baseline["checkpoint_path"], "ab") as fh:
        fh.write(b"tampered")
    with pytest.raises(mr.RunError, match="no longer matches"):
        controller.restore_revision(run_id)
    assert store.get_run(run_id).state == "baseline_ready"


def test_iteration_budget_finishes_run(env):
    designer, store, controller = env
    run_id = _start(controller, max_iterations=1)
    _baseline(controller, run_id)
    controller.capture_preview(run_id)
    _cycle(controller, run_id)
    decision = controller.decide_candidate(run_id)
    assert decision["run"]["state"] == "finished"
    assert decision["run"]["error"] is None
    with pytest.raises(mr.RunError, match="finished|budget exhausted"):
        controller.propose_patch(run_id, [{
            "op": "set_parameter", "node_id": "blur",
            "parameter_id": "intensity", "value": 1.0}])


def test_finish_restores_best_revision(env):
    designer, store, controller = env
    run_id = _start(controller)
    _baseline(controller, run_id)
    controller.capture_preview(run_id)
    _cycle(controller, run_id)
    controller.decide_candidate(run_id)
    best_id = store.get_run(run_id).best_revision_id
    designer.nodes["blur"]["params"]["intensity"][0] = 0.5
    result = controller.finish_run(run_id, notes="done")
    assert result["run"]["state"] == "finished"
    assert result["report"]["best_revision_id"] == best_id
    assert result["report"]["restored_to_path"]
    assert designer.nodes["blur"]["params"]["intensity"][0] == 9.0


def test_finish_without_restore_leaves_graph(env):
    designer, store, controller = env
    run_id = _start(controller)
    _baseline(controller, run_id)
    controller.capture_preview(run_id)
    _cycle(controller, run_id)
    controller.decide_candidate(run_id)
    designer.nodes["blur"]["params"]["intensity"][0] = 0.5
    controller.finish_run(run_id, restore_best=False)
    assert designer.nodes["blur"]["params"]["intensity"][0] == 0.5


def test_output_size_patch_is_recorded(env):
    designer, store, controller = env
    run_id = _start(controller)
    _baseline(controller, run_id)
    applied = _propose_and_apply(controller, run_id, [
        {"op": "set_output_size", "width_log2": 12, "height_log2": 12}])
    assert applied["status"] == "applied"
    revision = store.get_revision(
        applied["candidate"]["apply_result"]["result_revision_id"])
    assert revision.output_config["width_log2"] == 12
    assert revision.output_config["height_log2"] == 12
    assert designer.outputsize == [12, 12]


# ── persistence / APIs ─────────────────────────────────────────────────────

def test_runs_are_persisted_across_controller_instances(env, tmp_path):
    designer, store, controller = env
    run_id = _start(controller, goal="persist me")
    _baseline(controller, run_id)
    runs_dir = str(tmp_path / "runs")
    reopened = mr.MaterialRunController(
        mr.MaterialRunStore(os.path.join(runs_dir, "runs.sqlite3")),
        designer, runs_dir)
    bundle = reopened.get_run(run_id)
    assert bundle["run"]["goal"] == "persist me"
    assert len(bundle["revisions"]) == 1
    assert reopened.list_runs()["count"] == 1


def test_get_run_defaults_to_latest(env):
    designer, store, controller = env
    first = _start(controller)
    second = _start(controller)
    assert controller.get_run()["run"]["run_id"] == second
    assert controller.get_run(first)["run"]["run_id"] == first
    assert controller.list_runs(limit=1)["count"] == 1


def test_capture_binds_artifacts_to_current_revision(env):
    designer, store, controller = env
    run_id = _start(controller)
    revision = _baseline(controller, run_id)["revision"]
    preview = controller.capture_preview(run_id)
    artifacts = store.list_artifacts(run_id)
    assert {a["kind"] for a in artifacts} == {"outputs_sheet", "render_3d"}
    assert all(a["revision_id"] == revision["revision_id"] for a in artifacts)
    assert all(a["source_fingerprint"] == revision["full_sha256"]
               for a in artifacts)
    assert preview["source_fingerprint"] == revision["full_sha256"]


def test_operations_are_serialized(env):
    designer, store, controller = env
    run_id = _start(controller)
    _baseline(controller, run_id)
    errors = []

    def work():
        try:
            controller.capture_preview(run_id)
        except Exception as exc:  # noqa: BLE001 - test records any failure
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert designer.max_active == 1


# ── hypothesis-driven refinement ───────────────────────────────────────────

BLUR_UP = [{"op": "set_parameter", "node_id": "blur",
            "parameter_id": "intensity", "value": 9.0}]


def _hypothesis(*predictions, **extra):
    data = {"defect": "roughness reads flat across cavities",
            "observed_in": "roughness",
            "expected_effect": "roughness brightens overall",
            "predictions": list(predictions)}
    data.update(extra)
    return data


def _hypothesis_cycle(controller, run_id, hypothesis, operations=BLUR_UP, **evaluation):
    controller.propose_patch(run_id, operations, hypothesis=hypothesis)
    controller.apply_patch(run_id)
    controller.capture_preview(run_id)
    return controller.record_evaluation(run_id, **evaluation)["evaluation"]


def _ready(controller, **kwargs):
    run_id = _start(controller, **kwargs)
    _baseline(controller, run_id)
    controller.capture_preview(run_id)
    return run_id


def test_hypothesis_is_normalised_onto_the_candidate(env):
    designer, store, controller = env
    run_id = _ready(controller)
    result = controller.propose_patch(run_id, BLUR_UP, hypothesis=_hypothesis(
        {"output": "roughness", "direction": "increase", "min_change": 0.1}))
    hypothesis = result["candidate"]["patch"]["hypothesis"]
    assert hypothesis["observed_in"] == ["roughness"]
    assert hypothesis["predictions"] == [{"output": "roughness", "metric": "mean",
                                          "direction": "increase", "min_change": 0.1}]
    codes = [w["code"] for w in result["candidate"]["validation"]["warnings"]]
    assert "no_hypothesis" not in codes


def test_patch_without_hypothesis_warns(env):
    designer, store, controller = env
    run_id = _ready(controller)
    result = controller.propose_patch(run_id, BLUR_UP)
    codes = [w["code"] for w in result["candidate"]["validation"]["warnings"]]
    assert codes == ["no_hypothesis"]
    assert "hypothesis" not in result["candidate"]["patch"]


def test_require_hypothesis_rejects_bare_patch(env):
    designer, store, controller = env
    run_id = _ready(controller, require_hypothesis=True)
    assert store.get_run(run_id).require_hypothesis is True
    with pytest.raises(mr.RunError, match="requires a hypothesis"):
        controller.propose_patch(run_id, BLUR_UP)
    assert store.list_candidates(run_id)[-1].status == mr.CAND_INVALID
    assert store.get_run(run_id).state == "baseline_ready"


@pytest.mark.parametrize("hypothesis, message", [
    ({"expected_effect": "x"}, "defect is required"),
    (_hypothesis({"output": "roughness", "direction": "sideways"}), "direction"),
    (_hypothesis({"output": "roughness", "direction": "increase",
                  "metric": "median"}), "metric"),
    (_hypothesis({"output": "roughness", "direction": "unchanged",
                  "min_change": 0.1}), "tolerance"),
    (_hypothesis({"output": "roughness", "direction": "increase",
                  "min_change": 0.3, "max_change": 0.1}), "below min_change"),
    (_hypothesis(guess="x"), "Unknown hypothesis field"),
])
def test_invalid_hypotheses_are_rejected(env, hypothesis, message):
    designer, store, controller = env
    run_id = _ready(controller)
    with pytest.raises(mr.RunError, match=message):
        controller.propose_patch(run_id, BLUR_UP, hypothesis=hypothesis)


def test_confirmed_prediction_is_accepted(env):
    designer, store, controller = env
    run_id = _ready(controller)
    evaluation = _hypothesis_cycle(controller, run_id, _hypothesis(
        {"output": "roughness", "direction": "increase", "min_change": 0.1}))
    check = evaluation["hypothesis_check"]
    assert check["measured_outcome"] == "confirmed"
    assert check["outcome"] == "confirmed"
    assert check["predictions"][0]["before"] == pytest.approx(0.75)
    assert check["predictions"][0]["after"] == pytest.approx(0.95)
    assert evaluation["accepted"] is True


def test_refuted_prediction_is_not_auto_accepted(env):
    designer, store, controller = env
    run_id = _ready(controller)
    evaluation = _hypothesis_cycle(controller, run_id, _hypothesis(
        {"output": "roughness", "direction": "decrease"}))
    assert evaluation["technical_passed"] is True
    assert not evaluation["comparisons"]["threshold_failures"]
    assert evaluation["hypothesis_check"]["outcome"] == "refuted"
    assert evaluation["accepted"] is False


def test_overshoot_and_unchanged_give_partial(env):
    designer, store, controller = env
    run_id = _ready(controller)
    evaluation = _hypothesis_cycle(controller, run_id, _hypothesis(
        {"output": "roughness", "direction": "increase", "max_change": 0.1},
        {"output": "baseColor", "direction": "increase"},
        {"output": "metallic", "direction": "unchanged", "tolerance": 0.5}))
    outcomes = [p["outcome"] for p in evaluation["hypothesis_check"]["predictions"]]
    assert outcomes == ["overshot", "confirmed", "confirmed"]
    assert evaluation["hypothesis_check"]["outcome"] == "partial"


def test_evaluator_outcome_overrides_measurement(env):
    designer, store, controller = env
    run_id = _ready(controller)
    evaluation = _hypothesis_cycle(
        controller, run_id,
        _hypothesis({"output": "roughness", "direction": "increase"}),
        hypothesis_outcome="Refuted", observed_effect="speckles appeared instead")
    check = evaluation["hypothesis_check"]
    assert check["measured_outcome"] == "confirmed"
    assert check["assessed_outcome"] == "refuted"
    assert check["outcome"] == "refuted"
    assert check["observed_effect"] == "speckles appeared instead"
    assert evaluation["accepted"] is False


def test_hypothesis_outcome_validation(env):
    designer, store, controller = env
    run_id = _ready(controller)
    _propose_and_apply(controller, run_id, BLUR_UP)
    controller.capture_preview(run_id)
    with pytest.raises(mr.RunError, match="no hypothesis"):
        controller.record_evaluation(run_id, hypothesis_outcome="confirmed")
    with pytest.raises(mr.RunError, match="must be one of"):
        controller.record_evaluation(run_id, hypothesis_outcome="maybe")


def test_predictions_without_base_stats_are_inconclusive(env):
    designer, store, controller = env
    run_id = _start(controller)
    _baseline(controller, run_id)  # no baseline preview captured
    evaluation = _hypothesis_cycle(controller, run_id, _hypothesis(
        {"output": "roughness", "direction": "increase"}))
    check = evaluation["hypothesis_check"]
    assert check["predictions"][0]["outcome"] == "untestable"
    assert check["outcome"] == "inconclusive"


def test_uncaptured_prediction_output_warns(env):
    designer, store, controller = env
    run_id = _ready(controller, preview_preset={"output_usages": ["roughness"]})
    result = controller.propose_patch(run_id, BLUR_UP, hypothesis=_hypothesis(
        {"output": "height", "direction": "increase"}))
    codes = [w["code"] for w in result["candidate"]["validation"]["warnings"]]
    assert "prediction_not_captured" in codes


def test_repeating_a_refuted_change_warns(env):
    designer, store, controller = env
    run_id = _ready(controller)
    first = _hypothesis_cycle(controller, run_id, _hypothesis(
        {"output": "roughness", "direction": "decrease"}))
    controller.decide_candidate(run_id)
    assert store.get_candidate(first["candidate_id"]).status == mr.CAND_REJECTED
    result = controller.propose_patch(run_id, BLUR_UP, hypothesis=_hypothesis(
        {"output": "roughness", "direction": "increase"}))
    warnings = result["candidate"]["validation"]["warnings"]
    assert [w["code"] for w in warnings] == ["repeats_failed_candidate"]
    assert first["candidate_id"] in warnings[0]["message"]


def test_hypothesis_log_in_bundle_and_report(env):
    designer, store, controller = env
    run_id = _ready(controller, max_iterations=2)
    _hypothesis_cycle(controller, run_id, _hypothesis(
        {"output": "roughness", "direction": "decrease"}),
        observed_effect="got brighter, not darker")
    controller.decide_candidate(run_id)
    _hypothesis_cycle(controller, run_id, _hypothesis(
        {"output": "roughness", "direction": "increase"},
        defect="peaks too dark"))
    controller.decide_candidate(run_id)

    log = controller.get_run(run_id)["hypothesis_log"]
    assert [e["outcome"] for e in log] == ["refuted", "confirmed"]
    assert [e["accepted"] for e in log] == [False, True]
    assert log[0]["observed_effect"] == "got brighter, not darker"
    assert log[1]["defect"] == "peaks too dark"
    assert log[1]["operations"] == BLUR_UP

    report = store.get_run(run_id).report
    assert [e["outcome"] for e in report["hypothesis_log"]] == ["refuted", "confirmed"]
    assert report["candidates"][1]["hypothesis"]["defect"] == "peaks too dark"


def test_store_migrates_pre_hypothesis_databases(tmp_path):
    import sqlite3
    db = str(tmp_path / "old.sqlite3")
    old_schema = mr._SCHEMA.replace(
        ",\n    require_hypothesis INTEGER NOT NULL DEFAULT 0", "").replace(
        ",\n    hypothesis_check TEXT", "")
    assert "require_hypothesis" not in old_schema
    assert "hypothesis_check" not in old_schema
    conn = sqlite3.connect(db)
    conn.executescript(old_schema)
    conn.close()
    mr.MaterialRunStore(db)
    conn = sqlite3.connect(db)
    run_cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    eval_cols = {row[1] for row in conn.execute("PRAGMA table_info(evaluations)")}
    conn.close()
    assert "require_hypothesis" in run_cols
    assert "hypothesis_check" in eval_cols
