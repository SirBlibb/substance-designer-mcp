#!/usr/bin/env python
"""
sd_material_quality.py - Material-aware judgement helpers for material runs.

Pure, dependency-free functions used by ``sd_material_runs``:

* **Appearance targets** - what the material is *meant* to look like, as
  per-output metric ranges (optionally seeded from a material profile such as
  ``metal`` or ``dielectric``). A change is judged by whether it moves the map
  toward or away from its intended range, not by its sign.
* **Change classification** - every per-channel change beyond the regression
  threshold is labelled ``toward_target`` / ``within_target`` /
  ``away_from_target`` / ``intended`` (predicted by the hypothesis) /
  ``side_effect`` (nobody asked for it).
* **Comparison context** - the capture settings that must stay fixed for a
  before/after comparison to mean anything (outputs, sheet size, histogram
  bins, map resolution and pixel format, 3D render size and timing).
* **Graph roles** - which nodes drive macro shape, edge breakup, surface
  grain, colour, roughness and metallic, derived from a graph fingerprint.
* **Delivery checks** - required PBR outputs, resolution, bit depth and file
  integrity of an export, with stdlib PNG/TIFF/EXR header parsing.

Nothing here talks to Designer; the controller feeds in fingerprints, stats
and export results.
"""
import os
import re
import struct
from collections import deque
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = [
    "MATERIAL_PROFILES", "ROLES", "DEFAULT_DELIVERY_SPEC",
    "normalize_appearance", "appearance_checks", "classify_changes",
    "comparison_context", "diff_contexts",
    "analyze_graph_roles", "read_image_header", "pixel_format_bits",
    "normalize_delivery_spec", "delivery_checks",
]

_METRICS = ("min", "max", "mean")


def norm_usage(value: Any) -> str:
    text = "".join(c for c in str(value or "").lower() if c.isalnum())
    return {"ao": "ambientocclusion", "albedo": "basecolor",
            "diffuse": "basecolor", "color": "basecolor",
            "displacement": "height", "metalness": "metallic"}.get(text, text)


# ---------------------------------------------------------------------------
# Appearance targets
# ---------------------------------------------------------------------------

#: Loose PBR sanity bands per material class, in the maps' stored 0..1 domain
#: (baseColor is sRGB-encoded). They are starting points that the run's own
#: appearance_targets override, not a validator of artistic intent.
MATERIAL_PROFILES: Dict[str, dict] = {
    "dielectric": {
        "description": "Non-metal (stone, wood, plastic, fabric): metallic ~0, "
                       "albedo within the sRGB 30..240 band.",
        "targets": {
            "metallic": {"mean": [0.0, 0.1]},
            "baseColor": {"mean": [0.12, 0.94]},
        },
    },
    "metal": {
        "description": "Raw metal: metallic ~1, bright albedo (sRGB >= ~130).",
        "targets": {
            "metallic": {"mean": [0.8, 1.0]},
            "baseColor": {"mean": [0.5, 1.0]},
        },
    },
    "rough_dielectric": {
        "description": "Matte non-metal (concrete, plaster, raw stone).",
        "targets": {
            "metallic": {"mean": [0.0, 0.1]},
            "baseColor": {"mean": [0.12, 0.94]},
            "roughness": {"mean": [0.6, 1.0]},
        },
    },
    "glossy_dielectric": {
        "description": "Polished non-metal (lacquer, polished stone, ceramic).",
        "targets": {
            "metallic": {"mean": [0.0, 0.1]},
            "baseColor": {"mean": [0.12, 0.94]},
            "roughness": {"mean": [0.0, 0.35]},
        },
    },
}


def _range(value: Any, where: str, errors: List[str]) -> Optional[List[Optional[float]]]:
    if isinstance(value, dict):
        value = [value.get("min"), value.get("max")]
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        errors.append("{} must be [min, max] (either end may be null)".format(where))
        return None
    lo, hi = value
    for end in (lo, hi):
        if end is not None and (isinstance(end, bool) or not isinstance(end, (int, float))):
            errors.append("{} bounds must be numbers or null".format(where))
            return None
    if lo is None and hi is None:
        errors.append("{} needs at least one bound".format(where))
        return None
    if lo is not None and hi is not None and hi < lo:
        errors.append("{} has max below min".format(where))
        return None
    return [None if lo is None else float(lo), None if hi is None else float(hi)]


def normalize_appearance(profile: Optional[str],
                         targets: Optional[dict],
                         description: Optional[str] = None) -> Tuple[dict, List[str]]:
    """Merge a named profile with explicit targets.

    ``targets`` maps an output usage to ``{metric: [min, max], "channel": c,
    "note": "..."}``. Explicit targets replace the profile's for the same
    output/metric. Returns (appearance, errors).
    """
    errors: List[str] = []
    merged: Dict[str, dict] = {}
    if profile:
        key = str(profile).strip().lower()
        if key not in MATERIAL_PROFILES:
            errors.append("Unknown material_profile '{}'. Known: {}.".format(
                profile, ", ".join(sorted(MATERIAL_PROFILES))))
        else:
            for usage, spec in MATERIAL_PROFILES[key]["targets"].items():
                merged[usage] = {m: list(r) for m, r in spec.items()}
            profile = key
    if targets is not None and not isinstance(targets, dict):
        errors.append("appearance_targets must be an object keyed by output usage.")
        targets = {}
    for usage, spec in (targets or {}).items():
        where = "appearance_targets.{}".format(usage)
        if not isinstance(spec, dict):
            errors.append("{} must be an object like {{\"mean\": [0.4, 0.7]}}".format(where))
            continue
        unknown = sorted(set(spec) - set(_METRICS) - {"channel", "note"})
        if unknown:
            errors.append("{} has unknown key(s): {}".format(where, ", ".join(unknown)))
            continue
        entry: Dict[str, Any] = {}
        # Merge onto a profile entry for the same output (by normalised name).
        for existing in list(merged):
            if norm_usage(existing) == norm_usage(usage):
                entry = merged.pop(existing)
        for metric in _METRICS:
            if metric in spec:
                rng = _range(spec[metric], "{}.{}".format(where, metric), errors)
                if rng is not None:
                    entry[metric] = rng
        if spec.get("channel") is not None:
            entry["channel"] = str(spec["channel"])
        if spec.get("note"):
            entry["note"] = str(spec["note"])
        if not any(m in entry for m in _METRICS):
            errors.append("{} needs at least one of min/max/mean".format(where))
            continue
        merged[usage] = entry
    appearance = {"profile": profile or None, "targets": merged,
                  "description": description or ""}
    if profile and profile in MATERIAL_PROFILES:
        appearance["profile_description"] = MATERIAL_PROFILES[profile]["description"]
    return appearance, errors


def _find_output(stats: Optional[dict], usage: str) -> Optional[dict]:
    for key, info in (stats or {}).items():
        if norm_usage(key) == norm_usage(usage):
            return info
    return None


def _channels_for(info: Optional[dict], channel: Optional[str]) -> List[str]:
    channels = list(((info or {}).get("channels") or {}).keys())
    if channel:
        return [channel] if channel in channels else []
    colour = [c for c in channels if c.lower() not in ("alpha", "a")]
    return colour or channels


def _distance(value: float, rng: List[Optional[float]]) -> float:
    lo, hi = rng
    if lo is not None and value < lo:
        return lo - value
    if hi is not None and value > hi:
        return value - hi
    return 0.0


def _target_for(appearance: Optional[dict], usage: str) -> Optional[dict]:
    for key, spec in ((appearance or {}).get("targets") or {}).items():
        if norm_usage(key) == norm_usage(usage):
            return spec
    return None


def appearance_checks(appearance: Optional[dict], before: Optional[dict],
                      after: dict, epsilon: float = 1e-4) -> List[dict]:
    """Judge each target against the candidate (and base, when known).

    status: ``within`` (candidate in range), ``toward`` (outside, but closer
    than the base), ``away`` (further than the base), ``left_range`` (base was
    in range, candidate is not), ``outside`` (no base to compare against),
    ``missing`` (output/channel not captured).
    """
    results: List[dict] = []
    for usage, spec in ((appearance or {}).get("targets") or {}).items():
        a_info = _find_output(after, usage)
        b_info = _find_output(before, usage)
        channels = _channels_for(a_info, spec.get("channel"))
        for metric in _METRICS:
            rng = spec.get(metric)
            if rng is None:
                continue
            if not channels:
                results.append({"output": usage, "metric": metric, "range": rng,
                                "channel": spec.get("channel"), "status": "missing",
                                "regression": False})
                continue
            for channel in channels:
                aval = ((a_info.get("channels") or {}).get(channel) or {}).get(metric)
                bval = None
                if b_info:
                    bval = ((b_info.get("channels") or {}).get(channel) or {}).get(metric)
                entry = {"output": usage, "channel": channel, "metric": metric,
                         "range": rng, "before": bval, "after": aval}
                if aval is None:
                    entry.update(status="missing", regression=False)
                    results.append(entry)
                    continue
                d_after = _distance(float(aval), rng)
                entry["distance_after"] = round(d_after, 6)
                if bval is not None:
                    d_before = _distance(float(bval), rng)
                    entry["distance_before"] = round(d_before, 6)
                if d_after <= 0.0:
                    status = "within"
                elif bval is None:
                    status = "outside"
                elif entry["distance_before"] <= 0.0:
                    status = "left_range"
                elif d_after < entry["distance_before"] - epsilon:
                    status = "toward"
                elif d_after > entry["distance_before"] + epsilon:
                    status = "away"
                else:
                    status = "outside"
                entry["status"] = status
                entry["regression"] = status in ("away", "left_range")
                results.append(entry)
    return results


def classify_changes(before: Optional[dict], after: dict, threshold: float,
                     appearance: Optional[dict] = None,
                     hypothesis: Optional[dict] = None) -> dict:
    """Material-aware replacement for a sign-based regression check.

    Legacy behaviour (no targets and no hypothesis): a mean drop beyond the
    threshold is a failure, a rise an improvement. Otherwise changes are
    judged against the intended appearance and the hypothesis' predictions,
    and unexplained changes are reported as side effects, not failures.
    """
    result: Dict[str, Any] = {
        "threshold": threshold, "changes": [], "regressions": [],
        "improvements": [], "side_effects": [], "threshold_failures": [],
        "skipped": False,
        "mode": "legacy",
    }
    if not before:
        result["skipped"] = True
        return result
    targets = (appearance or {}).get("targets") or {}
    predictions = (hypothesis or {}).get("predictions") or []
    material_aware = bool(targets) or bool(hypothesis)
    if material_aware:
        result["mode"] = "material_aware"

    def predicted(usage: str, channel: str, metric: str, relative: float) -> Optional[bool]:
        for pred in predictions:
            if norm_usage(pred.get("output")) != norm_usage(usage):
                continue
            if pred.get("metric", "mean") != metric:
                continue
            if pred.get("channel") and pred["channel"] != channel:
                continue
            direction = pred.get("direction")
            if direction == "increase":
                return relative > 0
            if direction == "decrease":
                return relative < 0
            return False  # predicted "unchanged" but it moved
        return None

    for usage, info in (after or {}).items():
        b_info = _find_output(before, usage)
        if b_info is None:
            continue
        target = _target_for(appearance, usage)
        for channel, stats in (info.get("channels") or {}).items():
            b = (b_info.get("channels") or {}).get(channel)
            if not b:
                continue
            for metric in ("mean", "min", "max"):
                bval, aval = b.get(metric), stats.get(metric)
                if bval is None or aval is None:
                    continue
                denom = max(abs(float(bval)), 1e-6)
                relative = (float(aval) - float(bval)) / denom
                if abs(relative) <= threshold:
                    continue
                entry = {"output": usage, "channel": channel, "metric": metric,
                         "before": bval, "after": aval, "relative": round(relative, 6)}
                if not material_aware:
                    if relative < 0:
                        entry["classification"] = "regression"
                        result["regressions"].append(entry)
                        if metric == "mean":
                            result["threshold_failures"].append(entry)
                    else:
                        entry["classification"] = "improvement"
                        result["improvements"].append(entry)
                    result["changes"].append(entry)
                    continue
                rng = (target or {}).get(metric)
                if rng is not None and (not target.get("channel")
                                        or target["channel"] == channel):
                    d_before = _distance(float(bval), rng)
                    d_after = _distance(float(aval), rng)
                    if d_after <= 0.0:
                        entry["classification"] = "within_target"
                        result["improvements"].append(entry)
                    elif d_after < d_before:
                        entry["classification"] = "toward_target"
                        result["improvements"].append(entry)
                    else:
                        entry["classification"] = "away_from_target"
                        entry["range"] = rng
                        result["regressions"].append(entry)
                        result["threshold_failures"].append(entry)
                else:
                    match = predicted(usage, channel, metric, relative)
                    if match is True:
                        entry["classification"] = "intended"
                        result["improvements"].append(entry)
                    else:
                        entry["classification"] = "side_effect"
                        if match is False:
                            entry["note"] = "contradicts the hypothesis prediction"
                        result["side_effects"].append(entry)
                result["changes"].append(entry)
    return result


# ---------------------------------------------------------------------------
# Controlled comparisons
# ---------------------------------------------------------------------------

def comparison_context(sheet_config: dict, stats: dict,
                       output_config: Optional[dict] = None,
                       render: Optional[dict] = None) -> dict:
    """The settings a before/after pair must share to be comparable."""
    outputs = {}
    for usage, info in (stats or {}).items():
        outputs[norm_usage(usage)] = {
            "width": info.get("width"), "height": info.get("height"),
            "pixel_format": info.get("pixel_format"),
        }
    context = {
        "output_usages": sorted(norm_usage(u) for u in
                                sheet_config.get("output_usages") or []),
        "max_size": sheet_config.get("max_size"),
        "histogram_bins": sheet_config.get("histogram_bins"),
        "outputs": outputs,
        "graph_output_size": [(output_config or {}).get("width_log2"),
                              (output_config or {}).get("height_log2")],
        "render": None,
    }
    if render is not None:
        context["render"] = {
            "width": render.get("width"), "height": render.get("height"),
            "wait_ms": render.get("wait_ms"),
            "refresh_first": render.get("refresh_first"),
        }
    return context


def diff_contexts(reference: Optional[dict], current: Optional[dict]) -> dict:
    """List what differs between two comparison contexts.

    ``controlled`` covers the 2D maps (the numeric evidence); ``render_controlled``
    covers the 3D render. Camera, lighting and environment of SD's 3D view are
    not readable through the API, so a matching render size is necessary but
    not sufficient - keep the 3D view untouched during a run.
    """
    if not reference or not current:
        return {"controlled": False, "render_controlled": False,
                "mismatches": [{"field": "context",
                                "detail": "no reference capture to compare against"}]}
    mismatches: List[dict] = []
    for field in ("output_usages", "max_size", "histogram_bins", "graph_output_size"):
        if reference.get(field) != current.get(field):
            mismatches.append({"field": field, "reference": reference.get(field),
                               "current": current.get(field)})
    ref_out, cur_out = reference.get("outputs") or {}, current.get("outputs") or {}
    for usage in sorted(set(ref_out) & set(cur_out)):
        for key in ("width", "height", "pixel_format"):
            if ref_out[usage].get(key) != cur_out[usage].get(key):
                mismatches.append({"field": "outputs.{}.{}".format(usage, key),
                                   "reference": ref_out[usage].get(key),
                                   "current": cur_out[usage].get(key)})
    render_mismatches: List[dict] = []
    ref_r, cur_r = reference.get("render"), current.get("render")
    if ref_r is None or cur_r is None:
        if ref_r is not None or cur_r is not None:
            render_mismatches.append({"field": "render", "reference": ref_r,
                                      "current": cur_r})
    else:
        for key in ("width", "height", "wait_ms", "refresh_first"):
            if ref_r.get(key) != cur_r.get(key):
                render_mismatches.append({"field": "render.{}".format(key),
                                          "reference": ref_r.get(key),
                                          "current": cur_r.get(key)})
    return {"controlled": not mismatches,
            "render_controlled": not mismatches and not render_mismatches
            and ref_r is not None and cur_r is not None,
            "mismatches": mismatches + render_mismatches}


# ---------------------------------------------------------------------------
# Semantic graph roles
# ---------------------------------------------------------------------------

ROLES = ("macro_shape", "edge_breakup", "surface_grain", "height_processing",
         "color", "roughness", "metallic", "normal_processing", "output")

_SHAPE_USAGES = {"height", "normal", "ambientocclusion", "curvature"}

_EDGE_KEYWORDS = ("edge_detect", "slope_blur", "warp", "erosion", "erode",
                  "chipping", "damage", "edge_damage", "bevel", "distance",
                  "non_uniform_blur", "flood_fill", "crack", "weathering",
                  "dirt", "edge_wear", "histogram_scan", "bnw_spots")
_MACRO_KEYWORDS = ("tile_generator", "tile_sampler", "tile_random", "shape",
                   "brick", "pattern", "splatter", "polygon", "cells",
                   "voronoi", "stripes", "weave", "cube_3d", "gradient_linear",
                   "gradient_radial", "mesh", "fractal_sum_base")
_GRAIN_KEYWORDS = ("grain", "grunge", "fibers", "fur", "noise", "clouds",
                   "perlin", "gaussian", "moisture", "fractal", "dust",
                   "scratches", "crystal", "messy", "creased", "liquid",
                   "anisotropic", "white_noise", "plasma", "micro")
_NORMAL_KEYWORDS = ("normal", "height_to_normal")
_FREQUENCY_PARAMS = ("scale", "tiling", "pattern_amount", "x_amount", "y_amount",
                     "amount", "frequency", "density", "cell_size", "size")


def _definition_key(definition: Optional[str]) -> str:
    text = (definition or "").lower()
    text = text.rsplit("::", 1)[-1]
    text = text.split("?", 1)[0]
    text = text.rsplit("/", 1)[-1]
    return re.sub(r"\.sbs$", "", text)


def _output_usage(node: dict) -> Optional[str]:
    for prop in (node.get("annotations") or []):
        if prop.get("id") in ("usages", "usage") and prop.get("value"):
            value = prop["value"]
            if isinstance(value, list) and value:
                value = value[0]
            if isinstance(value, str):
                return value
    for key in ("label", "identifier"):
        for prop in (node.get("annotations") or []):
            if prop.get("id") == key and isinstance(prop.get("value"), str) \
                    and prop["value"]:
                return prop["value"]
    return node.get("id")


def _tunables(node: dict) -> List[dict]:
    out = []
    for prop in node.get("inputs") or []:
        pid = prop.get("id") or ""
        if pid.startswith("$") or prop.get("connected"):
            continue
        value = prop.get("value")
        if isinstance(value, bool) or isinstance(value, (int, float)) or (
                isinstance(value, (list, tuple)) and value
                and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                        for v in value)):
            out.append({"id": pid, "value": value})
    return out


def _frequency(node: dict) -> Optional[float]:
    best = None
    for prop in node.get("inputs") or []:
        pid = (prop.get("id") or "").lower()
        if prop.get("connected") or not any(pid == p or pid.startswith(p)
                                            for p in _FREQUENCY_PARAMS):
            continue
        value = prop.get("value")
        if isinstance(value, (list, tuple)) and value:
            value = max(v for v in value if isinstance(v, (int, float))) \
                if any(isinstance(v, (int, float)) for v in value) else None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        best = float(value) if best is None else max(best, float(value))
    return best


def analyze_graph_roles(fingerprint: dict, macro_max_scale: float = 4.0,
                        grain_min_scale: float = 12.0) -> dict:
    """Classify nodes by what they control, from a graph fingerprint.

    Each node gets the outputs it reaches (upstream walk from every output
    node), its distance to the nearest output, a primary role with a short
    reason and confidence, and its tunable (unconnected numeric) parameters.
    Heuristics use the node definition name and frequency-like parameters
    (scale/tiling/amount) - review low-confidence roles before relying on them.
    """
    nodes = {n.get("id"): n for n in fingerprint.get("nodes") or [] if n.get("id")}
    upstream: Dict[str, List[str]] = {}
    for conn in fingerprint.get("connections") or []:
        upstream.setdefault(conn.get("to_node"), []).append(conn.get("from_node"))

    output_ids = list(fingerprint.get("output_node_ids") or [])
    if not output_ids:
        output_ids = [nid for nid, n in nodes.items()
                      if n.get("is_output") or
                      _definition_key(n.get("definition")) == "output"]
    output_usage = {oid: _output_usage(nodes.get(oid, {"id": oid})) for oid in output_ids}

    reach: Dict[str, Dict[str, int]] = {}
    for oid, usage in output_usage.items():
        seen = {oid: 0}
        queue = deque([oid])
        while queue:
            current = queue.popleft()
            for parent in upstream.get(current, []):
                if parent not in seen:
                    seen[parent] = seen[current] + 1
                    queue.append(parent)
        for nid, dist in seen.items():
            slot = reach.setdefault(nid, {})
            slot[usage] = min(dist, slot.get(usage, dist))

    node_info: Dict[str, dict] = {}
    roles: Dict[str, List[dict]] = {role: [] for role in ROLES}
    unclassified: List[dict] = []
    for nid, node in nodes.items():
        influences = reach.get(nid, {})
        usages = {norm_usage(u) for u in influences}
        key = _definition_key(node.get("definition"))
        freq = _frequency(node)
        role, reason, confidence = None, "", 0.0
        if nid in output_usage:
            role, reason, confidence = "output", "graph output", 1.0
        elif not usages:
            role, reason = None, "does not reach any output"
        elif usages & _SHAPE_USAGES:
            if any(k in key for k in _NORMAL_KEYWORDS) and "height" not in usages:
                role, reason, confidence = "normal_processing", "normal-only path", 0.7
            elif any(k in key for k in _EDGE_KEYWORDS):
                role, reason, confidence = "edge_breakup", \
                    "edge/warp/erosion operator '{}'".format(key), 0.75
            elif any(k in key for k in _MACRO_KEYWORDS) or \
                    (freq is not None and freq <= macro_max_scale and
                     any(k in key for k in _GRAIN_KEYWORDS)):
                role, confidence = "macro_shape", 0.7
                reason = "pattern/shape generator '{}'".format(key) \
                    if any(k in key for k in _MACRO_KEYWORDS) \
                    else "low-frequency noise (scale {:g})".format(freq)
            elif any(k in key for k in _GRAIN_KEYWORDS):
                if freq is not None and freq >= grain_min_scale:
                    role, reason, confidence = "surface_grain", \
                        "high-frequency noise (scale {:g})".format(freq), 0.75
                elif freq is None:
                    role, reason, confidence = "surface_grain", \
                        "noise generator '{}' (no scale parameter)".format(key), 0.45
                else:
                    role, reason, confidence = "surface_grain", \
                        "mid-frequency noise (scale {:g})".format(freq), 0.5
            else:
                role, reason, confidence = "height_processing", \
                    "filter on the height/normal path ('{}')".format(key), 0.4
        elif usages == {"basecolor"}:
            role, reason, confidence = "color", "only reaches baseColor", 0.9
        elif usages == {"roughness"}:
            role, reason, confidence = "roughness", "only reaches roughness", 0.9
        elif usages == {"metallic"}:
            role, reason, confidence = "metallic", "only reaches metallic", 0.9
        elif "basecolor" in usages:
            role, reason, confidence = "color", \
                "reaches baseColor and {}".format(", ".join(sorted(usages - {"basecolor"}))), 0.5
        elif "roughness" in usages:
            role, reason, confidence = "roughness", "reaches roughness", 0.5
        else:
            role, reason, confidence = None, "reaches {}".format(", ".join(sorted(usages)))
        info = {
            "node_id": nid,
            "definition": node.get("definition"),
            "role": role,
            "reason": reason,
            "confidence": confidence,
            "influences": dict(sorted(influences.items())),
            "distance_to_output": min(influences.values()) if influences else None,
            "frequency": freq,
            "tunable_parameters": _tunables(node),
        }
        node_info[nid] = info
        (roles[role] if role else unclassified).append(info)
    for items in roles.values():
        items.sort(key=lambda i: (-i["confidence"], i["distance_to_output"] or 0))
    outputs = {}
    for oid, usage in output_usage.items():
        chain = sorted(((d, nid) for nid, r in reach.items()
                        for u, d in r.items() if u == usage and nid != oid))
        outputs[usage] = {"node_id": oid, "upstream": [nid for _, nid in chain]}
    return {
        "graph_identifier": fingerprint.get("graph_identifier"),
        "outputs": outputs,
        "roles": {role: items for role, items in roles.items() if items},
        "nodes": node_info,
        "unclassified": unclassified,
        "heuristics": {"macro_max_scale": macro_max_scale,
                       "grain_min_scale": grain_min_scale},
    }


# ---------------------------------------------------------------------------
# Export / delivery validation
# ---------------------------------------------------------------------------

DEFAULT_DELIVERY_SPEC: Dict[str, Any] = {
    "required_outputs": ["baseColor", "normal", "roughness", "metallic",
                         "height", "ambientOcclusion"],
    "file_format": "png",
    "resolution": None,          # [width_log2, height_log2] exact, or None
    "min_resolution_log2": 9,    # 512 px
    "bit_depth": {"default": 8, "height": 16},
    "require_square": True,
    "require_uniform_size": True,
}
_FILE_FORMATS = ("png", "tga", "tif", "tiff", "exr", "jpg", "jpeg", "bmp", "dds", "psd")


def pixel_format_bits(pixel_format: Optional[str]) -> Optional[int]:
    """Bits per channel implied by an SDTexture pixel format name."""
    name = (pixel_format or "").upper()
    if name.endswith("32F"):
        return 32
    if name.endswith("16F") or name.endswith("16"):
        return 16
    if name.endswith("8"):
        return 8
    return None


def normalize_delivery_spec(spec: Optional[dict]) -> Tuple[dict, List[str]]:
    errors: List[str] = []
    merged = dict(DEFAULT_DELIVERY_SPEC)
    merged["bit_depth"] = dict(DEFAULT_DELIVERY_SPEC["bit_depth"])
    if spec is None:
        return merged, errors
    if not isinstance(spec, dict):
        return merged, ["delivery spec must be an object"]
    unknown = sorted(set(spec) - set(DEFAULT_DELIVERY_SPEC))
    if unknown:
        errors.append("Unknown delivery field(s): {}. Allowed: {}.".format(
            ", ".join(unknown), ", ".join(sorted(DEFAULT_DELIVERY_SPEC))))
    if "required_outputs" in spec:
        req = spec["required_outputs"]
        if not isinstance(req, list) or not req or \
                not all(isinstance(u, str) and u for u in req):
            errors.append("required_outputs must be a non-empty list of usages")
        else:
            merged["required_outputs"] = list(req)
    if "file_format" in spec:
        fmt = str(spec["file_format"] or "").lower().lstrip(".")
        if fmt not in _FILE_FORMATS:
            errors.append("file_format must be one of {}".format(", ".join(_FILE_FORMATS)))
        else:
            merged["file_format"] = fmt
    if spec.get("resolution") is not None:
        res = spec["resolution"]
        if not (isinstance(res, (list, tuple)) and len(res) == 2 and
                all(isinstance(v, int) and not isinstance(v, bool) and 4 <= v <= 14
                    for v in res)):
            errors.append("resolution must be [width_log2, height_log2] (4..14)")
        else:
            merged["resolution"] = [int(res[0]), int(res[1])]
    elif "resolution" in spec:
        merged["resolution"] = None
    if "min_resolution_log2" in spec:
        value = spec["min_resolution_log2"]
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)
                                  or not 4 <= value <= 14):
            errors.append("min_resolution_log2 must be an integer 4..14 or null")
        else:
            merged["min_resolution_log2"] = value
    if "bit_depth" in spec:
        depth = spec["bit_depth"]
        if isinstance(depth, int) and not isinstance(depth, bool):
            # A single depth applies to every output, per-output defaults included.
            merged["bit_depth"] = {}
            depth = {"default": depth}
        if not isinstance(depth, dict) or not all(
                isinstance(v, int) and not isinstance(v, bool) and v in (8, 16, 32)
                for v in depth.values()):
            errors.append("bit_depth must be 8/16/32 or {usage: 8|16|32, "
                          "\"default\": ...}")
        else:
            merged["bit_depth"].update(depth)
    for flag in ("require_square", "require_uniform_size"):
        if flag in spec:
            merged[flag] = bool(spec[flag])
    return merged, errors


def _png_header(data: bytes) -> Optional[dict]:
    if len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    width, height, depth, colour = struct.unpack(">IIBB", data[16:26])
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(colour)
    return {"format": "png", "width": width, "height": height,
            "bits_per_channel": depth if colour != 3 else 8, "channels": channels}


def _tiff_header(data: bytes) -> Optional[dict]:
    if len(data) < 8 or data[:2] not in (b"II", b"MM"):
        return None
    endian = "<" if data[:2] == b"II" else ">"
    if struct.unpack(endian + "H", data[2:4])[0] != 42:
        return None
    offset = struct.unpack(endian + "I", data[4:8])[0]
    if offset + 2 > len(data):
        return None
    count = struct.unpack(endian + "H", data[offset:offset + 2])[0]
    tags: Dict[int, Any] = {}
    sizes = {3: 2, 4: 4}
    for i in range(count):
        base = offset + 2 + 12 * i
        if base + 12 > len(data):
            break
        tag, typ, n = struct.unpack(endian + "HHI", data[base:base + 8])
        if typ not in sizes:
            continue
        size = sizes[typ] * n
        fmt = endian + ("H" if typ == 3 else "I") * n
        if size <= 4:
            raw = data[base + 8:base + 8 + size]
        else:
            ptr = struct.unpack(endian + "I", data[base + 8:base + 12])[0]
            raw = data[ptr:ptr + size]
        if len(raw) == size:
            tags[tag] = struct.unpack(fmt, raw)
    if 256 not in tags or 257 not in tags:
        return None
    bits = tags.get(258, (1,))
    return {"format": "tiff", "width": tags[256][0], "height": tags[257][0],
            "bits_per_channel": max(bits), "channels": tags.get(277, (len(bits),))[0]}


def _exr_header(data: bytes) -> Optional[dict]:
    if len(data) < 8 or data[:4] != b"\x76\x2f\x31\x01":
        return None
    pos, end = 8, len(data)
    info: Dict[str, Any] = {"format": "exr"}
    while pos < end:
        name_end = data.find(b"\x00", pos)
        if name_end < 0 or name_end == pos:
            break
        name = data[pos:name_end].decode("latin-1")
        type_end = data.find(b"\x00", name_end + 1)
        if type_end < 0:
            return None
        size = struct.unpack("<I", data[type_end + 1:type_end + 5])[0]
        value = data[type_end + 5:type_end + 5 + size]
        pos = type_end + 5 + size
        if name == "channels":
            bits, channels, p = [], 0, 0
            while p < len(value) and value[p] != 0:
                n_end = value.find(b"\x00", p)
                ptype = struct.unpack("<i", value[n_end + 1:n_end + 5])[0]
                bits.append({0: 32, 1: 16, 2: 32}.get(ptype, 0))
                channels += 1
                p = n_end + 17
            info["bits_per_channel"] = max(bits) if bits else None
            info["channels"] = channels
        elif name == "dataWindow" and size == 16:
            x0, y0, x1, y1 = struct.unpack("<iiii", value)
            info["width"], info["height"] = x1 - x0 + 1, y1 - y0 + 1
    return info if "width" in info else None


def read_image_header(path: str) -> Optional[dict]:
    """Width/height/bit depth from a PNG, TIFF or EXR header (else None)."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(1 << 16)
    except OSError:
        return None
    for parser in (_png_header, _tiff_header, _exr_header):
        header = parser(data)
        if header:
            return header
    return None


def _required_bits(spec: dict, usage: str) -> int:
    depth = spec.get("bit_depth") or {}
    for key, value in depth.items():
        if key != "default" and norm_usage(key) == norm_usage(usage):
            return int(value)
    return int(depth.get("default", 8))


def delivery_checks(spec: dict, exported: Iterable[dict],
                    stats: Optional[dict] = None) -> dict:
    """Validate an export result against a delivery spec.

    ``exported``: [{usage, path, width, height, pixel_format, ...}] as returned
    by the plugin's export_outputs. Files are re-read from disk: existence,
    non-empty size, header dimensions and bit depth. ``stats`` (latest preview
    stats) adds a flat-map warning for required outputs with min == max.
    """
    checks: List[dict] = []
    warnings: List[str] = []
    exported = list(exported or [])
    by_usage = {norm_usage(e.get("usage") or e.get("label")): e for e in exported}

    def add(name: str, passed: bool, detail: Any, output: Optional[str] = None) -> None:
        checks.append({"name": name, "output": output, "passed": bool(passed),
                       "detail": detail})

    sizes = set()
    for usage in spec.get("required_outputs") or []:
        entry = by_usage.get(norm_usage(usage))
        if entry is None:
            add("exported:{}".format(usage), False, "no exported file for this output",
                usage)
            continue
        path = entry.get("path")
        exists = bool(path) and os.path.isfile(path)
        size = os.path.getsize(path) if exists else 0
        add("exported:{}".format(usage), exists and size > 0,
            {"path": path, "bytes": size}, usage)
        if not exists or size == 0:
            continue
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        fmt = spec.get("file_format")
        same_fmt = ext == fmt or {ext, fmt} <= {"tif", "tiff"} or {ext, fmt} <= {"jpg", "jpeg"}
        add("file_format:{}".format(usage), same_fmt,
            {"expected": fmt, "actual": ext}, usage)
        header = read_image_header(path)
        width = (header or {}).get("width") or entry.get("width")
        height = (header or {}).get("height") or entry.get("height")
        if header and entry.get("width") and (header["width"], header["height"]) != (
                entry.get("width"), entry.get("height")):
            add("header_matches:{}".format(usage), False,
                {"header": [header["width"], header["height"]],
                 "reported": [entry.get("width"), entry.get("height")]}, usage)
        bits = (header or {}).get("bits_per_channel") or \
            pixel_format_bits(entry.get("pixel_format"))
        bits_source = "file header" if header and header.get("bits_per_channel") \
            else "pixel format"
        required = _required_bits(spec, usage)
        add("bit_depth:{}".format(usage), bits is not None and bits >= required,
            {"required": required, "actual": bits, "source": bits_source,
             "pixel_format": entry.get("pixel_format")}, usage)
        if header is None and ext in ("png", "tif", "tiff", "exr"):
            add("readable:{}".format(usage), False,
                "file header could not be parsed", usage)
        if width and height:
            sizes.add((width, height))
            res = spec.get("resolution")
            if res:
                expected = (1 << res[0], 1 << res[1])
                add("resolution:{}".format(usage), (width, height) == expected,
                    {"expected": list(expected), "actual": [width, height]}, usage)
            elif spec.get("min_resolution_log2"):
                minimum = 1 << int(spec["min_resolution_log2"])
                add("resolution:{}".format(usage), min(width, height) >= minimum,
                    {"min": minimum, "actual": [width, height]}, usage)
            if spec.get("require_square"):
                add("square:{}".format(usage), width == height,
                    {"actual": [width, height]}, usage)
        else:
            add("resolution:{}".format(usage), False, "dimensions unknown", usage)
        info = _find_output(stats, usage) if stats else None
        if info:
            flat = all(c.get("min") == c.get("max")
                       for c in (info.get("channels") or {}).values())
            if flat and norm_usage(usage) not in ("metallic",):
                warnings.append("Output '{}' is a flat map in the latest preview "
                                "(min == max); check it is connected.".format(usage))
    if spec.get("require_uniform_size") and len(sizes) > 1:
        add("uniform_size", False, {"sizes": sorted([list(s) for s in sizes])})
    elif sizes:
        add("uniform_size", True, {"sizes": sorted([list(s) for s in sizes])})
    return {"passed": bool(checks) and all(c["passed"] for c in checks),
            "checks": checks, "warnings": warnings,
            "failed": [c for c in checks if not c["passed"]]}
