"""Material-aware evaluation, controlled comparisons, graph roles and delivery
validation: pure helpers plus the controller paths (fake SD plugin)."""
import os
import struct

import pytest

import sd_material_quality as q
import sd_material_runs as mr
from test_material_runs import (BLUR_UP, FakeDesigner, _baseline, _hypothesis,
                                _start, png_header)


def _stats(**means):
    return {usage: {"channels": {"L": {"min": 0.0, "max": 1.0, "mean": mean}},
                    "width": 1024, "height": 1024, "pixel_format": "LUM16"}
            for usage, mean in means.items()}


# ── appearance targets ─────────────────────────────────────────────────────

def test_profile_merges_with_explicit_targets():
    appearance, errors = q.normalize_appearance(
        "Metal", {"Base Color": {"mean": [0.6, None]},
                  "roughness": {"mean": {"min": 0.2, "max": 0.4}}})
    assert errors == []
    assert appearance["profile"] == "metal"
    targets = appearance["targets"]
    assert targets["metallic"] == {"mean": [0.8, 1.0]}
    assert targets["Base Color"] == {"mean": [0.6, None]}
    assert "baseColor" not in targets
    assert targets["roughness"] == {"mean": [0.2, 0.4]}


@pytest.mark.parametrize("profile, targets, message", [
    ("velvet", None, "Unknown material_profile"),
    (None, {"roughness": {"mean": [0.8, 0.2]}}, "max below min"),
    (None, {"roughness": {"mean": [None, None]}}, "at least one bound"),
    (None, {"roughness": {"median": [0, 1]}}, "unknown key"),
    (None, {"roughness": 0.5}, "must be an object"),
])
def test_bad_appearance_is_reported(profile, targets, message):
    _, errors = q.normalize_appearance(profile, targets)
    assert any(message in e for e in errors), errors


def test_appearance_check_statuses():
    appearance = {"targets": {"roughness": {"mean": [0.4, 0.6]}}}
    cases = [(0.5, 0.55, "within"), (0.9, 0.7, "toward"), (0.7, 0.9, "away"),
             (0.5, 0.8, "left_range"), (0.7, 0.7, "outside")]
    for before, after, status in cases:
        [check] = q.appearance_checks(appearance, _stats(roughness=before),
                                      _stats(roughness=after))
        assert check["status"] == status, (before, after)
        assert check["regression"] is (status in ("away", "left_range"))
    [missing] = q.appearance_checks(appearance, None, _stats(height=0.5))
    assert missing["status"] == "missing"


def test_legacy_classification_is_sign_based():
    result = q.classify_changes(_stats(height=0.8), _stats(height=0.5), 0.1)
    assert result["mode"] == "legacy"
    assert result["threshold_failures"][0]["classification"] == "regression"


def test_material_aware_classification():
    appearance = {"targets": {"roughness": {"mean": [0.6, 0.8]},
                              "metallic": {"mean": [0.0, 0.1]}}}
    hypothesis = {"predictions": [{"output": "baseColor", "metric": "mean",
                                   "direction": "increase"}]}
    before = _stats(roughness=0.3, metallic=0.05, height=0.8, baseColor=0.3)
    after = _stats(roughness=0.5, metallic=0.3, height=0.5, baseColor=0.5)
    result = q.classify_changes(before, after, 0.1, appearance, hypothesis)
    by_output = {(c["output"], c["metric"]): c["classification"]
                 for c in result["changes"]}
    assert by_output[("roughness", "mean")] == "toward_target"
    assert by_output[("metallic", "mean")] == "away_from_target"
    assert by_output[("height", "mean")] == "side_effect"
    assert by_output[("baseColor", "mean")] == "intended"
    # A height drop no-one targeted is a side effect, not a failure.
    assert [f["output"] for f in result["threshold_failures"]] == ["metallic"]


# ── controlled comparisons ─────────────────────────────────────────────────

def test_context_diff_reports_each_field():
    cfg = {"output_usages": ["height", "Base Color"], "max_size": 1024,
           "histogram_bins": 16}
    render = {"width": 800, "height": 600, "wait_ms": 600, "refresh_first": True}
    ref = q.comparison_context(cfg, _stats(height=0.5),
                               {"width_log2": 11, "height_log2": 11}, render)
    same = q.diff_contexts(ref, ref)
    assert same == {"controlled": True, "render_controlled": True, "mismatches": []}

    stats = _stats(height=0.5)
    stats["height"]["width"] = 2048
    changed = q.comparison_context(dict(cfg, max_size=512), stats,
                                   {"width_log2": 11, "height_log2": 11},
                                   dict(render, width=640))
    diff = q.diff_contexts(ref, changed)
    fields = {m["field"] for m in diff["mismatches"]}
    assert fields == {"max_size", "outputs.height.width", "render.width"}
    assert diff["controlled"] is False and diff["render_controlled"] is False
    assert q.diff_contexts(None, ref)["controlled"] is False


# ── graph roles ────────────────────────────────────────────────────────────

def _node(nid, definition, inputs=(), label=None, output=False):
    return {"id": nid, "definition": definition, "is_output": output,
            "inputs": [{"id": k, "value": v, "connected": False} for k, v in inputs],
            "annotations": [{"id": "label", "value": label}] if label else []}


def _role_fingerprint():
    nodes = [
        _node("tiles", "sbs::library::tile_generator", [("x_amount", 4), ("scale", 1.0)]),
        _node("slope", "sbs::library::slope_blur_grayscale", [("intensity", 8.0)]),
        _node("grain", "sbs::library::perlin_noise", [("scale", 32)]),
        _node("blob", "sbs::library::clouds_2", [("scale", 2)]),
        _node("blend", "sbs::compositing::blend", [("opacity", 0.5)]),
        _node("gmap", "sbs::library::gradient_map", [("$randomseed", 3)]),
        _node("rlev", "sbs::compositing::levels", [("levelinlow", 0.1)]),
        _node("orphan", "sbs::library::perlin_noise", [("scale", 8)]),
        _node("out_h", "sbs::compositing::output", label="Height", output=True),
        _node("out_c", "sbs::compositing::output", label="Base Color", output=True),
        _node("out_r", "sbs::compositing::output", label="Roughness", output=True),
    ]
    edges = [("tiles", "slope"), ("slope", "blend"), ("grain", "blend"),
             ("blob", "blend"), ("blend", "out_h"), ("gmap", "out_c"),
             ("rlev", "out_r")]
    return {"graph_identifier": "G", "nodes": nodes,
            "connections": [{"from_node": a, "from_output": "o", "to_node": b,
                             "to_input": "i"} for a, b in edges],
            "output_node_ids": ["out_h", "out_c", "out_r"]}


def test_graph_roles_are_classified():
    analysis = q.analyze_graph_roles(_role_fingerprint())
    roles = {nid: info["role"] for nid, info in analysis["nodes"].items()}
    assert roles["tiles"] == "macro_shape"
    assert roles["slope"] == "edge_breakup"
    assert roles["grain"] == "surface_grain"
    assert roles["blob"] == "macro_shape"
    assert roles["blend"] == "height_processing"
    assert roles["gmap"] == "color"
    assert roles["rlev"] == "roughness"
    assert roles["orphan"] is None
    assert roles["out_h"] == "output"
    assert analysis["nodes"]["tiles"]["influences"] == {"Height": 3}
    assert analysis["outputs"]["Height"]["upstream"][0] == "blend"
    assert [u["node_id"] for u in analysis["unclassified"]] == ["orphan"]
    assert {"id": "x_amount", "value": 4} in analysis["nodes"]["tiles"]["tunable_parameters"]
    assert analysis["nodes"]["gmap"]["tunable_parameters"] == []


# ── file headers / delivery ────────────────────────────────────────────────

def _write(path, data):
    with open(path, "wb") as fh:
        fh.write(data)
    return str(path)


def _tiff(width, height, bits):
    entries = [(256, 3, 1, width), (257, 3, 1, height), (258, 3, 1, bits),
               (277, 3, 1, 1)]
    body = struct.pack("<H", len(entries))
    for tag, typ, n, value in entries:
        body += struct.pack("<HHIHH", tag, typ, n, value, 0)
    return b"II" + struct.pack("<HI", 42, 8) + body + struct.pack("<I", 0)


def _exr(width, height, pixel_type):
    def attr(name, typ, value):
        return name + b"\0" + typ + b"\0" + struct.pack("<I", len(value)) + value
    channels = b"Y\0" + struct.pack("<iBBBBii", pixel_type, 0, 0, 0, 0, 1, 1) + b"\0"
    window = struct.pack("<iiii", 0, 0, width - 1, height - 1)
    return (b"\x76\x2f\x31\x01" + struct.pack("<I", 2) + attr(b"channels", b"chlist", channels)
            + attr(b"dataWindow", b"box2i", window) + b"\0")


def test_image_headers(tmp_path):
    png = q.read_image_header(_write(tmp_path / "a.png", png_header(512, 256, 16)))
    assert png["width"] == 512 and png["height"] == 256 and png["bits_per_channel"] == 16
    tif = q.read_image_header(_write(tmp_path / "a.tif", _tiff(64, 32, 16)))
    assert (tif["width"], tif["height"], tif["bits_per_channel"]) == (64, 32, 16)
    exr = q.read_image_header(_write(tmp_path / "a.exr", _exr(128, 128, 1)))
    assert (exr["width"], exr["bits_per_channel"]) == (128, 16)
    assert q.read_image_header(_write(tmp_path / "x.bin", b"nope")) is None
    assert q.pixel_format_bits("RGBA16F") == 16
    assert q.pixel_format_bits("LUM32F") == 32
    assert q.pixel_format_bits("RGBA8") == 8


def _export(tmp_path, usage, size=1024, bits=8, ext="png"):
    folder = tmp_path / "{}_{}_{}".format(usage, size, bits)
    folder.mkdir(exist_ok=True)
    path = _write(folder / "{}.{}".format(usage, ext), png_header(size, size, bits))
    return {"usage": usage, "path": path, "width": size, "height": size,
            "pixel_format": "RGBA16" if bits == 16 else "RGBA8"}


def test_delivery_checks(tmp_path):
    spec, errors = q.normalize_delivery_spec(
        {"required_outputs": ["baseColor", "height"], "bit_depth": {"height": 16}})
    assert errors == []
    good = [_export(tmp_path, "baseColor"), _export(tmp_path, "height", bits=16)]
    assert q.delivery_checks(spec, good)["passed"] is True

    low = [_export(tmp_path, "baseColor"), _export(tmp_path, "height", bits=8)]
    result = q.delivery_checks(spec, low)
    assert [c["name"] for c in result["failed"]] == ["bit_depth:height"]

    missing = q.delivery_checks(spec, good[:1])
    assert "exported:height" in [c["name"] for c in missing["failed"]]

    exact, _ = q.normalize_delivery_spec(dict(spec, resolution=[11, 11]))
    failed = {c["name"] for c in q.delivery_checks(exact, good)["failed"]}
    assert failed == {"resolution:baseColor", "resolution:height"}

    mixed = [_export(tmp_path, "baseColor", size=512),
             _export(tmp_path, "height", size=1024, bits=16)]
    assert "uniform_size" in {c["name"] for c in q.delivery_checks(spec, mixed)["failed"]}

    flat = {"baseColor": {"channels": {"r": {"min": 0.5, "max": 0.5}}}}
    assert q.delivery_checks(spec, good, flat)["warnings"]


@pytest.mark.parametrize("spec, message", [
    ({"file_format": "gif"}, "file_format"),
    ({"bit_depth": 12}, "bit_depth"),
    ({"resolution": [11]}, "resolution"),
    ({"required_outputs": []}, "required_outputs"),
    ({"colour_space": "srgb"}, "Unknown delivery field"),
])
def test_bad_delivery_spec(spec, message):
    _, errors = q.normalize_delivery_spec(spec)
    assert any(message in e for e in errors), errors


# ── controller integration ─────────────────────────────────────────────────

@pytest.fixture
def env(tmp_path):
    designer = FakeDesigner(str(tmp_path))
    runs_dir = str(tmp_path / "runs")
    store = mr.MaterialRunStore(os.path.join(runs_dir, "runs.sqlite3"))
    return designer, store, mr.MaterialRunController(store, designer, runs_dir)


def _ready(controller, **kwargs):
    run_id = _start(controller, **kwargs)
    _baseline(controller, run_id)
    controller.capture_preview(run_id)
    return run_id


def _evaluate(controller, run_id, operations=BLUR_UP, hypothesis=None, **capture):
    controller.propose_patch(run_id, operations, hypothesis=hypothesis)
    controller.apply_patch(run_id)
    preview = controller.capture_preview(run_id, **capture)
    return preview, controller.record_evaluation(run_id)["evaluation"]


def test_change_toward_intended_look_is_accepted(env):
    designer, store, controller = env
    # Blur 5 -> 9 lifts every mean 0.75 -> 0.95; roughness is meant to be high.
    run_id = _ready(controller, appearance_targets={"roughness": {"mean": [0.9, 1.0]}})
    _, evaluation = _evaluate(controller, run_id)
    comparisons = evaluation["comparisons"]
    assert comparisons["mode"] == "material_aware"
    roughness = [c for c in comparisons["changes"] if c["output"] == "roughness"
                 and c["metric"] == "mean"]
    assert roughness[0]["classification"] == "within_target"
    assert comparisons["side_effects"]  # the untargeted outputs moved too
    assert not comparisons["threshold_failures"]
    assert evaluation["accepted"] is True
    assert any("side effects" in w for w in evaluation["warnings"])


def test_change_away_from_intended_look_is_rejected(env):
    designer, store, controller = env
    run_id = _ready(controller, material_profile="dielectric",
                    appearance_targets={"roughness": {"mean": [0.0, 0.8]}})
    _, evaluation = _evaluate(controller, run_id)
    statuses = {c["output"]: c["status"]
                for c in evaluation["comparisons"]["appearance_checks"]}
    assert statuses["roughness"] == "left_range"
    assert statuses["baseColor"] == "left_range"  # 0.75 -> 0.95 leaves 0.12..0.94
    assert evaluation["accepted"] is False


def test_invalid_appearance_and_delivery_fail_at_start(env):
    designer, store, controller = env
    with pytest.raises(mr.RunError, match="Invalid appearance"):
        controller.start_run("G", material_profile="velvet")
    with pytest.raises(mr.RunError, match="Invalid delivery spec"):
        controller.start_run("G", delivery={"file_format": "gif"})


def test_first_capture_locks_the_comparison_context(env):
    designer, store, controller = env
    run_id = _ready(controller)
    lock = store.get_run(run_id).comparison_context
    assert lock["max_size"] == 1024
    assert lock["render"]["width"] == 8
    preview, evaluation = _evaluate(controller, run_id, max_size=512)
    assert preview["comparison_control"]["controlled"] is False
    assert evaluation["comparisons"]["control"]["controlled"] is False
    assert evaluation["accepted"] is False
    assert store.get_run(run_id).comparison_context == lock


def test_resolution_change_is_an_uncontrolled_comparison(env):
    designer, store, controller = env
    run_id = _ready(controller)
    _, evaluation = _evaluate(controller, run_id, operations=[
        {"op": "set_output_size", "width_log2": 11, "height_log2": 11}])
    fields = {m["field"] for m in evaluation["comparisons"]["control"]["mismatches"]}
    assert "graph_output_size" in fields
    assert evaluation["accepted"] is False


def test_capture_adds_side_by_side_sheet_or_warns(env):
    designer, store, controller = env
    run_id = _ready(controller)
    preview, _ = _evaluate(controller, run_id)
    sheet = [a for a in preview["artifacts"] if a["kind"] == "comparison_sheet"]
    assert len(sheet) == 1 and os.path.isfile(sheet[0]["path"])
    call = [p for c, p in designer.calls if c == "compose_comparison"][-1]
    assert call["left_label"].startswith("before: baseline")

    designer.compose_supported = False
    controller.decide_candidate(run_id, accepted=False)
    controller.propose_patch(run_id, BLUR_UP)
    controller.apply_patch(run_id)
    preview = controller.capture_preview(run_id)
    assert any("Side-by-side" in w for w in preview["warnings"])


def test_analyze_graph_reports_roles(env):
    designer, store, controller = env
    run_id = _start(controller)
    analysis = controller.analyze_graph(run_id)
    assert set(analysis["nodes"]["blur"]["influences"]) == set(
        ["Height", "Normal", "Ambient Occlusion", "Base Color", "Roughness", "Metallic"])
    assert analysis["nodes"]["noise"]["role"] == "surface_grain"
    assert controller.analyze_graph(graph_identifier="G")["outputs"]


def test_edit_outside_target_path_is_flagged(env):
    designer, store, controller = env
    designer.nodes["orphan"] = {"definition": "sbs::compositing::levels",
                                "params": {"level": [0.5, 0.5]}}
    run_id = _ready(controller)
    result = controller.propose_patch(
        run_id, [{"op": "set_parameter", "node_id": "orphan",
                  "parameter_id": "level", "value": 0.7}],
        hypothesis=_hypothesis({"output": "roughness", "direction": "increase"}))
    codes = [w["code"] for w in result["candidate"]["validation"]["warnings"]]
    assert "node_not_upstream" in codes


def test_target_role_mismatch_is_flagged(env):
    designer, store, controller = env
    run_id = _ready(controller)
    result = controller.propose_patch(run_id, BLUR_UP, hypothesis=_hypothesis(
        {"output": "roughness", "direction": "increase"}, target_role="color"))
    warnings = result["candidate"]["validation"]["warnings"]
    assert [w["code"] for w in warnings] == ["role_mismatch"]
    with pytest.raises(mr.RunError, match="target_role"):
        controller.decide_candidate(run_id)  # clear the pending candidate
        controller.propose_patch(run_id, BLUR_UP, hypothesis=_hypothesis(
            target_role="sparkle"))


def test_delivery_gates_finish(env):
    designer, store, controller = env
    designer.export_bits = 8  # height exports at 8 bit; the spec wants 16
    run_id = _ready(controller, delivery={})
    _evaluate(controller, run_id)
    controller.decide_candidate(run_id)
    with pytest.raises(mr.RunError, match="bit_depth:height"):
        controller.finish_run(run_id)
    run = store.get_run(run_id)
    assert run.state == "baseline_ready"
    assert run.delivery["passed"] is False

    designer.export_bits = 16
    finished = controller.finish_run(run_id)
    assert finished["run"]["state"] == "finished"
    delivery = finished["report"]["delivery"]
    assert delivery["passed"] is True
    assert delivery["revision_id"] == run.best_revision_id
    assert len(delivery["files"]) == 6
    kinds = {a["kind"] for a in store.list_artifacts(run_id)}
    assert "delivery_export" in kinds


def test_validate_delivery_reports_missing_outputs(env):
    designer, store, controller = env
    designer.export_skip = "metallic"
    run_id = _ready(controller)
    result = controller.validate_delivery(run_id, delivery={"bit_depth": 8})
    delivery = result["delivery"]
    assert delivery["passed"] is False
    assert [c["name"] for c in delivery["failed"]] == ["exported:metallic"]
    assert any("metallic" in w for w in delivery["warnings"])


def test_budget_exhaustion_waits_for_delivery(env):
    designer, store, controller = env
    designer.export_bits = 16
    run_id = _ready(controller, max_iterations=1, delivery={})
    _evaluate(controller, run_id)
    decision = controller.decide_candidate(run_id)
    assert decision["run"]["state"] == "baseline_ready"
    with pytest.raises(mr.RunError, match="budget exhausted"):
        controller.propose_patch(run_id, BLUR_UP)
    assert controller.finish_run(run_id)["report"]["delivery"]["passed"] is True
