#!/usr/bin/env python
"""
sd_mcp_bridge.py - Substance Designer MCP Bridge v2.5.0

Relay between Claude (stdio/FastMCP) and the SD plugin (TCP).

Architecture: Claude -> stdio -> this bridge -> TCP localhost:<port> -> SD plugin

Protocol: Length-prefix framing [4-byte big-endian length][JSON payload]
Connection: Fresh TCP socket per command (no persistent connection)

Added in v2.5.0:
  Material runs: a versioned, checkpoint-based iteration workflow on top of the
  existing tools. Runs and revisions are persisted in SQLite (default
  %LOCALAPPDATA%/sd_mcp/runs, override with --runs-dir or SD_MCP_RUNS_DIR);
  each revision is an immutable package copy saved via saveCopyOfPackageAs and
  verified by reloading it. New tools: start_material_run, get_material_run,
  list_material_runs, checkpoint_material_revision, verify_material_checkpoint,
  restore_material_revision, propose_material_patch, apply_material_patch,
  capture_material_preview (contact sheet + 3D render images), and
  record_material_evaluation, decide_material_candidate, finish_material_run.
  The plugin gained checkpoint_package, restore_package, verify_checkpoint and
  graph_fingerprint (plugin v3.9.0). The controller is independent of Qt and
  runs every SD operation through the same one-at-a-time TCP transport.
  Hypothesis-driven refinement: propose_material_patch takes a hypothesis
  (visual defect, expected effect, measurable predictions) that
  record_material_evaluation checks against the preview stats.
  Material-aware evaluation (material_profile / appearance_targets), locked
  comparison contexts with before|after sheets, analyze_material_graph (node
  roles) and validate_material_delivery (export + PBR delivery checks; plugin
  export_outputs / compose_comparison, v3.10.0).

Added in v2.4.0:
  capture_node_preview: grabs each node's graph-editor tile (with SD's live
  thumbnail, no graph.compute()) for one node or a labeled multi-node contact
  sheet. Also available as a staged long job.
  execute_sd_code is disabled by default; enable with --allow-arbitrary-code
  (bridge) and SD_MCP_ALLOW_EXECUTE_CODE=1 or an `allow_execute_code` marker
  file in the plugin folder (plugin). --token adds a shared secret required
  by the plugin when it runs with SD_MCP_TOKEN set. reload_plugin is likewise
  disabled unless --allow-dev-tools (bridge) and SD_MCP_ALLOW_DEV_TOOLS=1 or
  the execute-code opt-in (plugin) are set.
  Timeouts are layered: --timeout (default 150s) and --heavy-timeout
  (default 300s) are sent with each command; the plugin stops its main-thread
  wait just before them. Commands carry a request_id, so the plugin can replay
  a cached response instead of executing a duplicate. SD_MCP_PORT selects the
  default plugin/bridge port. The plugin runs one worker per client, keeping
  job polling/cancel responsive while sd.api blocks the main thread.
  Several JSON tools now return MCP structured content (typed output schema):
  create/get graph, create node, parameter updates, batch/recipe builds and
  send_to_3d_view; the readable JSON text content is unchanged.
  create_batch_graph gained preflight (validate without building) and
  cleanup_on_failure (delete the new graph when the build fails).
  New tools: diagnostic (health check), graph_snapshot (nodes + connections),
  start_long_job / wait_long_job / get_job_status / cancel_long_job for
  stage-tracked, cancellable heavy computes (inspect_outputs, get_map_stats,
  capture_2d_view) with MCP progress notifications.

Added in v2.3.0:
  set_parameters: batch parameter edits across nodes in one call (the plugin
  resolves the graph and each node once and reports per-update status)
  inspect_outputs: one graph compute for both a labeled preview contact sheet
  and per-channel statistics of several outputs

Added in v2.2.0:
  capture_2d_view tool: renders a single node/map (e.g. the Height output) as a
  PNG at native resolution via the SD API, or grabs the 2D view widget
  output_usages: captures multiple labeled outputs into one contact sheet
  get_map_stats: returns per-channel pixel statistics and histograms (exact
  min/max/mean; histograms of >1M-pixel 16-bit/float maps are sample-counted,
  see histogram.sample_stride)

Fixes in v2.0.0:
  BUG-B01: _send_lock no longer held across full retry loop (prevents 360s deadlock)
  BUG-B03: Default port corrected to 9881 (was 9880)
  BUG-B04: directionalwarp warp input corrected to "inputintensity" (was "inputgradient")
  BUG-B05: FastMCP lifespan set at constructor, not post-construction attribute
  BUG-B06: ctx: Context kept for FastMCP compatibility (injection works in 1.4.1+)
  BUG-B07: None result returns "{}" not "null"; float nan/inf handled via SD plugin _json_safe
  BUG-B08: Retry logic documented correctly (retries connection errors only, not timeouts)

Usage:
    uv run --directory E:/Create/Build/DCC/MCP/SubstanceDesignerMCP/server \\
           python sd_mcp_bridge.py --port 9881
"""
import sys
import os
import json
import struct
import socket
import logging
import argparse
import asyncio
import time
import threading
import uuid
from typing import Any, Dict, List, Optional
from contextlib import asynccontextmanager

# Ensure venv site-packages on path
script_dir = os.path.dirname(os.path.abspath(__file__))
venv_site = os.path.join(script_dir, '.venv', 'Lib', 'site-packages')
if os.path.exists(venv_site):
    sys.path.insert(0, venv_site)

from mcp.server.fastmcp import FastMCP, Context, Image
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import (AliasChoices, BaseModel, ConfigDict, Field, ValidationError,
                      model_validator)

import sd_material_runs

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SD_MCP_Bridge")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Single source of truth for the bridge version; kept in sync with the
# `version` field in server/pyproject.toml.
BRIDGE_VERSION = "2.5.0"

# The bridge must outwait the plugin's main-thread dispatch budget (120 s by
# default); the plugin additionally stops RESPONSE_GRACE seconds earlier.
TIMEOUT = 150          # default socket timeout for ordinary commands
HEAVY_TIMEOUT = 300    # builds/computes/captures that legitimately run longer
CONNECT_TIMEOUT = 5    # timeout for initial TCP connection
HEADER_SIZE = 4
MAX_RETRIES = 2        # only for connection failures, not SD operation timeouts
RETRY_DELAY = 1.0      # seconds between retry attempts

# Tools that may run longer than TIMEOUT. The plugin receives the chosen
# timeout in the command envelope and sets its dispatch wait just below it.
_HEAVY_TOOLS = frozenset({
    "create_batch_graph", "build_material_graph", "build_heightmap_graph",
    "apply_recipe", "inspect_outputs", "get_map_stats", "capture_2d_view",
    "capture_node_preview", "capture_3d_view", "send_to_3d_view",
    "execute_code", "save_package",
    "checkpoint_package", "restore_package", "verify_checkpoint",
    "graph_fingerprint", "export_outputs", "compose_comparison",
})


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name, "")).strip()
    for part in raw.split(","):
        try:
            return int(part.strip())
        except (TypeError, ValueError):
            continue
    return default


def _tool_timeout(cmd_type: str) -> float:
    return HEAVY_TIMEOUT if cmd_type in _HEAVY_TOOLS else TIMEOUT


# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
_sd_port: int = _env_int("SD_MCP_PORT", 9881)   # BUG-B03: matches plugin default
# BUG-B01 fix: lock is NOT held across the retry loop — only during the actual
# socket operation. This prevents a single timeout from blocking the bridge for
# 3 x 120 = 360 seconds. Each _send_command call acquires its own lock window.
_send_lock = threading.Lock()

# ── Security ────────────────────────────────────────────────────────────────
# execute_sd_code is opt-in: set --allow-arbitrary-code (or the env var below).
# The plugin enforces its own matching gate before running anything.
_allow_arbitrary_code: bool = False
# reload_plugin re-executes plugin code from disk; disabled by default.
_allow_dev_tools: bool = False
# Shared secret: when set, every command carries a "token" field. The plugin
# rejects commands while SD_MCP_TOKEN is set and the token does not match.
_auth_token: Optional[str] = None

# ── Material runs ───────────────────────────────────────────────────────────
# Runs database + revision checkpoints. Defaults to %LOCALAPPDATA%/sd_mcp/runs;
# override with --runs-dir or SD_MCP_RUNS_DIR. The controller is created
# lazily on first use so unrelated tool calls do not touch the filesystem.
_runs_dir: Optional[str] = None
_material_controller: Optional["sd_material_runs.MaterialRunController"] = None
_material_lock = threading.Lock()


def _default_runs_dir() -> str:
    env = os.environ.get("SD_MCP_RUNS_DIR", "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(
        "~/.local/share")
    return os.path.join(base, "sd_mcp", "runs")


def _material_designer(cmd_type: str, params: dict) -> Any:
    """Designer callable for the run controller: one TCP command to the plugin."""
    return _request(cmd_type, params)


def _material_runs() -> "sd_material_runs.MaterialRunController":
    global _material_controller
    with _material_lock:
        if _material_controller is None:
            runs_dir = _runs_dir or _default_runs_dir()
            store = sd_material_runs.MaterialRunStore(
                os.path.join(runs_dir, "runs.sqlite3"))
            _material_controller = sd_material_runs.MaterialRunController(
                store=store, designer=_material_designer, runs_dir=runs_dir)
            logger.info(f"Material runs: {runs_dir}")
        return _material_controller


def _reset_material_runs() -> None:
    """Drop the cached controller (tests, or after changing the runs dir)."""
    global _material_controller
    with _material_lock:
        _material_controller = None


async def _material_op(fn, *args, **kwargs) -> dict:
    """Run a controller method off the event loop; surface errors as ToolError."""
    def work():
        try:
            return fn(*args, **kwargs)
        except (sd_material_runs.RunError,
                sd_material_runs.DesignerError) as exc:
            raise ToolError(str(exc))
    return await asyncio.to_thread(work)


def _material_json(result: dict) -> str:
    return json.dumps(result, indent=2)



# ---------------------------------------------------------------------------
# Length-prefix protocol
# ---------------------------------------------------------------------------
def _send_framed(sock: socket.socket, data: bytes) -> None:
    sock.sendall(struct.pack(">I", len(data)) + data)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes. Returns b"" on clean disconnect."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return b""
        buf += chunk
    return buf


def _recv_framed(sock: socket.socket, timeout: float) -> bytes:
    sock.settimeout(timeout)
    header = _recv_exact(sock, HEADER_SIZE)
    if not header:
        raise ConnectionAbortedError("Connection closed while reading header.")
    msg_len = struct.unpack(">I", header)[0]
    if msg_len == 0:
        return b""
    if msg_len > 100 * 1024 * 1024:
        raise ValueError(f"Message too large: {msg_len} bytes")
    payload = _recv_exact(sock, msg_len)
    if not payload:
        raise ConnectionAbortedError("Connection closed while reading payload.")
    return payload


# ---------------------------------------------------------------------------
# Send command — BUG-B01/B10: lock held only for one connect+send+recv
# ---------------------------------------------------------------------------
def _send_command_locked(cmd_type: str, params: dict = None,
                         request_id: str = None, timeout: float = None) -> dict:
    """
    Open fresh TCP connection, send one command, receive one response.
    Lock is acquired INSIDE this function for the duration of the socket op.
    This prevents a single timeout from holding the lock for the full wait.

    The command carries a request_id (the plugin caches the response and
    replays it for a duplicate) and its timeout, so the plugin can stop its
    main-thread wait just before the bridge gives up.

    BUG-B10 fix: the lock covers connect as well as send/recv, so a waiting
    call cannot connect and occupy the plugin's accept queue ahead of the lock
    holder. With per-client plugin workers this is no longer a deadlock, but
    keeping one in-flight command at a time still matches SD's contract.
    """
    timeout = float(timeout if timeout is not None else _tool_timeout(cmd_type))
    request_id = request_id or uuid.uuid4().hex
    command = {"type": cmd_type, "params": params or {},
               "request_id": request_id, "timeout": timeout}
    if _auth_token:
        command["token"] = _auth_token
    data_out = json.dumps(command).encode("utf-8")

    with _send_lock:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT)
        try:
            sock.connect(("localhost", _sd_port))
        except Exception as e:
            sock.close()
            raise ConnectionError(
                f"Cannot connect to Substance Designer on localhost:{_sd_port}. "
                f"Is SD running with the MCP plugin loaded? ({e})")

        try:
            _send_framed(sock, data_out)
            response_bytes = _recv_framed(sock, timeout)
            if not response_bytes:
                return {"status": "error",
                        "message": f"Empty response from SD on '{cmd_type}' "
                                   f"(request {request_id})."}
            return json.loads(response_bytes.decode("utf-8"))
        except socket.timeout:
            return {"status": "error",
                    "message": f"Timeout ({timeout}s) waiting for SD on '{cmd_type}' "
                               f"(request {request_id}). SD may be busy — try again."}
        except json.JSONDecodeError as e:
            return {"status": "error", "message": f"Invalid JSON from SD: {e}"}
        except Exception as e:
            return {"status": "error", "message": f"Communication error: {e}"}
        finally:
            try:
                sock.close()
            except Exception:
                pass


def _request(cmd_type: str, params: dict = None, timeout: float = None,
             request_id: str = None) -> Any:
    """
    Send one command and return its result.

    request_id is generated once and reused across internal retries so the
    plugin can deduplicate a command that was already executed. timeout is in
    seconds; omitted means _tool_timeout(cmd_type) (default vs heavy profile).

    BUG-B01 fix: retry loop is OUTSIDE the lock (lock is inside _send_command_locked).
    BUG-B08 clarification: timeouts do NOT retry (would just re-queue a stuck SD op).
    BUG-B09 fix: retry ONLY when the TCP connect itself failed (ConnectionError raised
    before anything was sent). Previously any error message containing "connect"
    (e.g. a failed connect_nodes) was retried, re-running commands SD had already run.
    Raises ToolError on failure so MCP clients receive isError=true.
    """
    request_id = request_id or uuid.uuid4().hex
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = _send_command_locked(
                cmd_type, params, request_id=request_id, timeout=timeout)
            break
        except ConnectionError as e:
            if attempt < MAX_RETRIES:
                logger.warning(f"Attempt {attempt+1} failed (connection): {e}. Retrying in {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
                continue
            raise ToolError(f"Connection Error: {e}") from e
    if response.get("status") == "error":
        raise ToolError(response.get("message", "Unknown error"))
    result = response.get("result")
    # BUG-B07 fix: None result returns "{}" not "null"
    result = {} if result is None else result

    # Non-finite floats were replaced by the plugin with nulls plus explicit
    # path flags; surface those flags so clients do not mistake them for data.
    flags = response.get("non_finite_values")
    if flags:
        logger.warning(f"SD returned non-finite values on '{cmd_type}' (request "
                       f"{request_id}): {flags}")
        if isinstance(result, dict):
            result.setdefault("non_finite_values", flags)
        else:
            result = {"result": result, "non_finite_values": flags}
    return result


def _send(cmd_type: str, params: dict = None) -> str:
    """Send a command and format its result as JSON text for the MCP tool response."""
    return json.dumps(_request(cmd_type, params), indent=2)


async def _async_send(cmd_type: str, params: dict = None) -> str:
    return await asyncio.to_thread(_send, cmd_type, params)


async def _async_data(cmd_type: str, params: dict = None) -> dict:
    """Send a command and return its parsed result dict (for typed tools)."""
    return await asyncio.to_thread(_request, cmd_type, params)


# ---------------------------------------------------------------------------
# FastMCP Server — BUG-B05 fix: lifespan passed at constructor
# ---------------------------------------------------------------------------
@asynccontextmanager
async def _lifespan(app: FastMCP):
    logger.info(f"Substance Designer MCP Bridge v{BRIDGE_VERSION} -> SD plugin on port {_sd_port}")
    logger.info("Ensure Substance Designer is running with the MCP plugin loaded.")
    yield {}
    logger.info("Substance Designer MCP bridge shutting down.")


SERVER_INSTRUCTIONS = """\
Tools for driving a running Adobe Substance 3D Designer (SD) instance.
SD must be running with the MCP plugin loaded (TCP port 9881); if a tool reports
a connection error, ask the user to start SD rather than retrying.

Recommended workflow:
1. Call get_scene_info first to see loaded packages, graphs and the active graph.
2. Prefer high-level tools over node-by-node building: list_recipes / apply_recipe,
   build_material_graph, build_heightmap_graph, or create_batch_graph (one call
   for a whole graph). Fall back to create_node + connect_nodes for small edits.
3. Port IDs must match exactly. Atomic nodes output "unique_filter_output";
   library nodes (create_instance_node) have custom port names, so call
   get_node_info before connecting them. A wrong port ID fails with the node's valid
   port IDs (closest match first), so read the error and retry with the suggested ID.
   list_documentation(category="port_reference") lists known ports, and
   list_documentation(action="search", query=...) searches all docs.
4. To check the result visually, capture_2d_view renders a single map straight
   from the graph at native resolution - ideal for reviewing individual channels
   (e.g. output_usage="height" or "normal"); without a node it grabs the 2D view
   widget (open with F2). inspect_outputs combines a multi-output contact sheet
   with numeric per-channel statistics from ONE graph compute - prefer it over
   several capture_2d_view/get_map_stats calls. To review node thumbnails without
   a compute, capture_node_preview grabs each node's tile from the graph editor
   (one node or a labeled sheet of node_ids). For a full material preview,
   open the 3D view in SD (F3), call send_to_3d_view(graph) to show that graph's
   material there (no mouse control needed), then capture_3d_view, which returns
   the rendered preview as an image. Iterate with set_parameters (several
   properties in one call) or set_parameter, then capture again
   (refresh_first=True if the render looks stale).
5. Changes are in memory until save_package is called.

For a reproducible iteration loop, use the material-run tools:
start_material_run -> checkpoint_material_revision (baseline; verifies the
package copy) -> propose_material_patch -> apply_material_patch ->
capture_material_preview (review the images/stats) ->
record_material_evaluation -> decide_material_candidate (accept or restore the
base), then repeat. finish_material_run restores the best accepted revision and
closes the run. restore_material_revision returns to any checkpointed revision;
verify_material_checkpoint proves a checkpoint file still matches.

Refine hypothesis-first: each iteration should name ONE specific visual defect
seen in the latest preview, propose a targeted change for it, and predict the
effect. Pass that as propose_material_patch(hypothesis={defect, observed_in,
rationale, expected_effect, predictions:[{output, metric, direction,
min_change}]}); record_material_evaluation checks the predictions against the
stats and reports confirmed/partial/refuted (pass hypothesis_outcome and
observed_effect from your own look at the images). Read get_material_run's
hypothesis_log before the next proposal so refuted ideas are not repeated.
start_material_run(require_hypothesis=True) enforces this.
Before choosing what to edit, call analyze_material_graph to see which nodes
drive macro shape, edge breakup, surface grain, colour and roughness, and put
the intended role in hypothesis.target_role. Give the run its intended look
(material_profile and/or appearance_targets) so evaluation judges changes
against it rather than by sign; keep the 3D view (camera, lighting, tiling)
untouched during a run, because only the capture settings are verifiable.
Pass delivery={...} to start_material_run when the material must ship; then
validate_material_delivery exports and checks outputs, resolution and bit
depth, and finish_material_run refuses until that passes.

When a build fails, retry with preflight=True on create_batch_graph first; for
already-created partial graphs prefer cleanup_on_failure=True next time.

execute_sd_code and reload_plugin are disabled by default - ask the user to
restart the bridge with --allow-arbitrary-code or --allow-dev-tools and to set
the matching plugin-side gate before relying on them. diagnostic is a read-only
health check useful after unexpected failures.
For very large graphs, heavy inspection tools can run as cancellable, stage-
tracked jobs: start_long_job -> wait_long_job (progress notifications) /
cancel_long_job instead of the direct blocking inspect_outputs/get_map_stats/
capture_2d_view/capture_node_preview calls.
"""

# BUG-B05 fix: pass lifespan at construction (not post-construction attribute)
mcp = FastMCP("SubstanceDesignerMCP", instructions=SERVER_INSTRUCTIONS, lifespan=_lifespan)

# Tool annotations (MCP hints; all tools act only on the local SD instance).
_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
_ADDITIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                            idempotentHint=False, openWorldHint=False)
_UPDATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                          idempotentHint=True, openWorldHint=False)
_DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                               idempotentHint=True, openWorldHint=False)
_ARBITRARY = ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                             idempotentHint=False, openWorldHint=False)


# ---------------------------------------------------------------------------
# Typed tool inputs (documented in the MCP input schema). They are dumped back
# to the plain dicts the plugin expects, so the TCP wire format is unchanged.
# ---------------------------------------------------------------------------
Position = List[float]
ParamOverrides = Dict[str, Any]

_PARAMS_DESC = (
    "Parameter overrides keyed by parameter ID. A bare value is type-inferred "
    "(numbers -> float, 2/3/4-element lists -> float2/3/4, bools, strings); use "
    "{\"value\": ..., \"type\": \"int2\"} to force a type, e.g. "
    "{\"intensity\": 10.0, \"blendingmode\": {\"value\": 3, \"type\": \"int\"}}.")


class BatchNode(BaseModel):
    """One node to create in create_batch_graph."""
    model_config = ConfigDict(extra="allow")

    id_alias: Optional[str] = Field(
        None, description="Name used to reference this node in connections "
                          "(default: last part of definition_id). Duplicates get _1, _2 suffixes.")
    definition_id: Optional[str] = Field(
        None, description="Atomic node definition, e.g. 'sbs::compositing::blur' or "
                          "'sbs::compositing::output' (default: sbs::compositing::uniform). "
                          "Omit for library nodes.")
    resource_url: Optional[str] = Field(
        None, description="Library node URL from get_library_nodes, "
                          "e.g. 'pkg:///cells_1?dependency=...'.")
    library_keyword: Optional[str] = Field(
        None, description="Library node identifier resolved by the plugin, e.g. 'perlin_noise', "
                          "'cells_1' (alternative to resource_url).")
    position: Optional[Position] = Field(
        None, min_length=2, max_length=2,
        description="[x, y] in the graph editor (default [0, 0]).")
    usage: Optional[str] = Field(
        None, description="Output nodes only: baseColor, normal, height, roughness, metallic, "
                          "ambientOcclusion, emissive, opacity.")
    label: Optional[str] = Field(
        None, description="Output nodes only: display label (default: usage).")
    parameters: Optional[ParamOverrides] = Field(None, description=_PARAMS_DESC)


class BatchParameter(BaseModel):
    """One parameter update in set_parameters."""
    model_config = ConfigDict(extra="allow")

    node_id: str = Field(
        ..., description="Node identifier or node label to update.")
    parameter_id: str = Field(
        ..., description="Parameter/property ID on that node (same IDs and "
                         "aliases as set_parameter).")
    value: Any = Field(
        ..., description="Value to set; type inferred per value_type.")
    value_type: str = Field(
        "float", description="'float', 'int', 'bool', 'string', 'float2', "
                             "'float3', 'float4', 'color' (RGBA 0-1), 'int2', "
                             "'int3', 'int4'.")


class BatchConnection(BaseModel):
    """One connection to make in create_batch_graph."""
    model_config = ConfigDict(extra="allow")

    from_: str = Field(
        validation_alias=AliasChoices("from", "from_alias"), serialization_alias="from",
        description="id_alias of the source node.")
    to: str = Field(
        validation_alias=AliasChoices("to", "to_alias"), serialization_alias="to",
        description="id_alias of the destination node.")
    from_output: str = Field(
        "unique_filter_output",
        description="Output port ID on the source. Atomic nodes use 'unique_filter_output'; "
                    "library nodes have custom names (single-output nodes are auto-resolved).")
    to_input: str = Field(
        "input1",
        description="Input port ID on the destination, e.g. 'input1', blend 'source'/"
                    "'destination'/'opacity', output nodes 'inputNodeOutput'. Invalid IDs fail "
                    "with the node's valid port IDs, closest match first.")


def _dump(items) -> list:
    """Pydantic models -> plain dicts for the plugin (aliased keys, unset Nones dropped)."""
    return [i.model_dump(by_alias=True, exclude_none=True) for i in items or []]


# ---------------------------------------------------------------------------
# Typed tool outputs. The plugin returns plain dicts; these lenient models
# (extra="allow", everything optional unless the plugin guarantees it) give
# MCP clients a validated structuredContent alongside the JSON text content.
# ---------------------------------------------------------------------------
_JsonObj = Dict[str, Any]


def _typed(model_cls: type, data: dict) -> Any:
    """Validate a plugin result into a typed output model.

    Raises ToolError carrying the raw JSON so nothing is lost when the plugin
    returns an unexpected shape.
    """
    try:
        return model_cls.model_validate(data)
    except ValidationError as exc:
        raise ToolError(
            "Structured result validation failed for {}. "
            "Raw result:\n{}".format(model_cls.__name__, json.dumps(data, indent=2))
        ) from exc


class GraphInfoResult(BaseModel):
    """Output of get_graph_info."""
    model_config = ConfigDict(extra="allow")

    identifier: str = Field(..., description="Graph identifier.")
    node_count: int = 0
    truncated: Optional[bool] = None
    node_limit: Optional[int] = None
    nodes: List[_JsonObj] = Field(default_factory=list)


class NodeCreatedResult(BaseModel):
    """Output of create_node."""
    model_config = ConfigDict(extra="allow")

    node_id: str
    definition: Optional[str] = None
    position: Optional[List[float]] = None


class CreateGraphResult(BaseModel):
    """Output of create_graph."""
    model_config = ConfigDict(extra="allow")

    identifier: str = Field(..., description="Sanitized graph identifier.")
    requested_name: Optional[str] = None
    sanitized_name: Optional[str] = None
    type: Optional[str] = None
    package: Optional[str] = None


class ParameterUpdateResult(BaseModel):
    """One update inside set_parameter/set_parameters results.

    Only keys the plugin always sends are declared; conditional keys
    (status/error on failures, renamed_from on renames) flow through as
    extras so the structured dump mirrors the plugin's JSON exactly.
    """
    model_config = ConfigDict(extra="allow")

    node_id: str
    parameter_id: str
    value: Any = None
    value_type: Optional[str] = None


class SetParametersResult(BaseModel):
    """Output of set_parameters."""
    model_config = ConfigDict(extra="allow")

    graph: str
    applied: int = 0
    failed: int = 0
    results: List[ParameterUpdateResult] = Field(default_factory=list)


class BatchGraphResult(BaseModel):
    """Output of create_batch_graph / build_material_graph /
    build_heightmap_graph for an executed build. Only keys the plugin always
    sends are declared; conditional keys (param_warnings, future additions)
    stay extras so the structured dump mirrors the plugin's JSON exactly.
    """
    model_config = ConfigDict(extra="allow")

    graph_identifier: str
    requested_name: str
    nodes_created: int = 0
    nodes_failed: int = 0
    connections_ok: int = 0
    connections_failed: int = 0
    node_map: Dict[str, str] = Field(default_factory=dict)
    nodes: List[_JsonObj] = Field(default_factory=list)
    failed_nodes: List[_JsonObj] = Field(default_factory=list)
    connections: List[_JsonObj] = Field(default_factory=list)


class BatchPreflightResult(BaseModel):
    """Output of create_batch_graph(preflight=True) — nothing is created."""
    model_config = ConfigDict(extra="allow")

    preflight: bool = True
    requested_name: str
    graph_name: str
    valid: bool
    errors: List[_JsonObj] = Field(default_factory=list)
    warnings: List[_JsonObj] = Field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0
    nodes_checked: int = 0
    connections_checked: int = 0


class BatchBuildResult(BaseModel):
    """Return annotation of create_batch_graph / build_*.

    Union return annotations get wrapped as {"result": ...} by FastMCP, so
    the tools validate against the concrete class (BatchGraphResult or
    BatchPreflightResult) and return the plugin's raw dict; this permissive
    pass-through model then produces structuredContent byte-equal to it.
    """
    model_config = ConfigDict(extra="allow")


class ApplyRecipeResult(BaseModel):
    """Output of apply_recipe (nodes are added to an existing graph)."""
    model_config = ConfigDict(extra="allow")

    graph_identifier: str
    recipe: str
    nodes_added: int = 0
    nodes_failed: int = 0
    connections_ok: int = 0
    connections_failed: int = 0
    node_map: Dict[str, str] = Field(default_factory=dict)


class Send3DStatusResult(BaseModel):
    """Output of send_to_3d_view (and its status calls)."""
    model_config = ConfigDict(extra="allow")

    job_id: int
    graph: Optional[str] = None
    done: bool = False
    bound: List[_JsonObj] = Field(default_factory=list)
    failed: List[_JsonObj] = Field(default_factory=list)
    pending: List[_JsonObj] = Field(default_factory=list)
    skipped: List[_JsonObj] = Field(default_factory=list)


# ======================================================================
# MCP TOOLS
# BUG-B06 note: ctx: Context kept — FastMCP 1.4.1+ injects it correctly.
# If a future version drops injection, remove ctx parameter from all tools.
# ======================================================================

@mcp.tool(annotations=_READ_ONLY, structured_output=False)
async def get_scene_info(ctx: Context, include_library: bool = False) -> str:
    """
    Get info about loaded packages and graphs in Substance Designer.
    Returns packages, graphs, node counts, current graph, and SD version.
    Each package has an "index" usable as package_index in other tools.

    - include_library: also list SD's built-in library packages (resources/packages),
      which SD loads as dependencies of library nodes. Hidden by default; their
      count is reported as hidden_library_packages.
    """
    return await _async_send("get_scene_info", {"include_library": include_library})


@mcp.tool(annotations=_ADDITIVE, structured_output=False)
async def create_package(ctx: Context) -> str:
    """
    Create a new empty package in Substance Designer.
    Use save_package to save it to disk afterward.
    """
    return await _async_send("create_package")


@mcp.tool(annotations=_ADDITIVE)
async def create_graph(ctx: Context,
                       package_index: int = 0,
                       graph_name: str = "MCP_Graph",
                       package_path: Optional[str] = None) -> CreateGraphResult:
    """
    Create a new SBS Compositing Graph in Substance Designer.
    - package_index: which loaded package (0 = first/current)
    - graph_name: identifier for the new graph
    - package_path: optional full path to specific .sbs file

    IMPORTANT: graph_name should use only letters, digits, underscores.
    Spaces and special characters are automatically sanitized.
    """
    data = await _async_data("create_graph", {
        "package_index": package_index,
        "graph_name": graph_name,
        "package_path": package_path,
    })
    return _typed(CreateGraphResult, data)


@mcp.tool(annotations=_DESTRUCTIVE, structured_output=False)
async def delete_graph(ctx: Context,
                       graph_identifier: str,
                       package_index: int = 0) -> str:
    """Delete a graph from a package."""
    return await _async_send("delete_graph", {
        "graph_identifier": graph_identifier,
        "package_index": package_index,
    })


@mcp.tool(annotations=_UPDATE, structured_output=False)
async def open_graph(ctx: Context, graph_identifier: str) -> str:
    """Open a graph in the Substance Designer UI editor."""
    return await _async_send("open_graph", {"graph_identifier": graph_identifier})


@mcp.tool(annotations=_READ_ONLY)
async def get_graph_info(ctx: Context,
                         graph_identifier: Optional[str] = None,
                         node_limit: int = 100,
                         include_connections: bool = True) -> GraphInfoResult:
    """
    Get detailed info about a graph including all nodes and connections.
    - graph_identifier: graph identifier (None = current active graph)
    - node_limit: max nodes to return in detail (default 100; use 0 for summary/count only)
    - include_connections: whether to include connection data per node (default True)

    For large graphs (100+ nodes), use node_limit=0 to get just the count,
    then query specific nodes with get_node_info.
    """
    data = await _async_data("get_graph_info", {
        "graph_identifier": graph_identifier,
        "node_limit": node_limit,
        "include_connections": include_connections,
    })
    return _typed(GraphInfoResult, data)


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
async def list_node_definitions(ctx: Context,
                                filter_text: str = "",
                                graph_identifier: Optional[str] = None,
                                limit: int = 500) -> str:
    """
    List available node definitions in Substance Designer.
    - filter_text: search filter (e.g. 'blur', 'cells', 'perlin', 'blend')
    - graph_identifier: optional graph to query from
    - limit: max results (default 500)

    Common sbs::compositing:: definitions:
      uniform, blend, levels, normal, curve, hsl, gradient, blur, sharpen,
      warp, directionalwarp, emboss, transformation, distance, grayscaleconversion,
      shuffle, pixelprocessor, fxmaps, bitmap, output, input_color, input_grayscale
    """
    return await _async_send("list_node_definitions", {
        "filter_text": filter_text,
        "graph_identifier": graph_identifier,
        "limit": limit,
    })


@mcp.tool(annotations=_ADDITIVE)
async def create_node(ctx: Context,
                      definition_id: str,
                      graph_identifier: Optional[str] = None,
                      position: Optional[List[float]] = None) -> NodeCreatedResult:
    """
    Create an atomic node in a Substance Designer graph.
    - definition_id: e.g. 'sbs::compositing::blend', 'sbs::compositing::levels'
    - graph_identifier: target graph (None = current active graph)
    - position: [x, y] position in graph editor

    Common definition_ids:
      sbs::compositing::uniform        - Solid color/grayscale
      sbs::compositing::blend          - Blend two inputs (modes: Copy=0, Add=1, Subtract=2, Multiply=3, etc.)
      sbs::compositing::levels         - Levels adjustment
      sbs::compositing::normal         - Height to Normal map
      sbs::compositing::curve          - Curve adjustment
      sbs::compositing::hsl            - Hue/Saturation/Luminosity
      sbs::compositing::blur           - Gaussian blur
      sbs::compositing::sharpen        - Sharpen
      sbs::compositing::warp           - Warp/distortion
      sbs::compositing::directionalwarp - Directional warp
      sbs::compositing::emboss         - Emboss
      sbs::compositing::transformation - 2D transform
      sbs::compositing::distance       - Distance field
      sbs::compositing::grayscaleconversion - RGB to grayscale
      sbs::compositing::shuffle        - Channel shuffle
      sbs::compositing::bitmap         - Bitmap input
      sbs::compositing::fxmaps         - FX-Map (pattern generator)
      sbs::compositing::pixelprocessor - Per-pixel math
      sbs::compositing::passthrough    - Pass-through
      sbs::compositing::input_color    - Color input parameter
      sbs::compositing::input_grayscale - Grayscale input parameter

    Returns node_id which is used in connect_nodes, set_parameter, etc.
    """
    data = await _async_data("create_node", {
        "definition_id": definition_id,
        "graph_identifier": graph_identifier,
        "position": position,
    })
    return _typed(NodeCreatedResult, data)


@mcp.tool(annotations=_ADDITIVE, structured_output=False)
async def create_instance_node(ctx: Context,
                               resource_url: str,
                               graph_identifier: Optional[str] = None,
                               position: Optional[List[float]] = None) -> str:
    """
    Create an instance of a library node (Cells, Perlin Noise, etc.).
    First use get_library_nodes to find the resource_url.
    - resource_url: URL like 'pkg:///cells_1?dependency=1563150890'
    - graph_identifier: target graph (None = current)
    - position: [x, y]

    IMPORTANT: After creating, call get_node_info(node_id) to find the exact
    output/input port IDs before connecting. Library nodes do NOT use
    'unique_filter_output' — they have custom output names.
    """
    return await _async_send("create_instance_node", {
        "resource_url": resource_url,
        "graph_identifier": graph_identifier,
        "position": position,
    })


@mcp.tool(annotations=_ADDITIVE, structured_output=False)
async def create_output_node(ctx: Context,
                             usage: str = "baseColor",
                             label: Optional[str] = None,
                             graph_identifier: Optional[str] = None,
                             position: Optional[List[float]] = None) -> str:
    """
    Create an output node with a specific PBR usage in Substance Designer.
    - usage: baseColor, normal, height, roughness, metallic, ambientOcclusion, emissive, opacity
    - label: display label (defaults to usage name)
    - graph_identifier: target graph (None = current)
    - position: [x, y]
    """
    return await _async_send("create_output_node", {
        "usage": usage,
        "label": label,
        "graph_identifier": graph_identifier,
        "position": position,
    })


@mcp.tool(annotations=_ADDITIVE, structured_output=False)
async def connect_nodes(ctx: Context,
                        from_node_id: str,
                        to_node_id: str,
                        from_output: str = "unique_filter_output",
                        to_input: str = "input1",
                        graph_identifier: Optional[str] = None) -> str:
    """
    Connect two nodes in a Substance Designer graph.
    - from_node_id: source node identifier (from create_node result)
    - to_node_id: destination node identifier
    - from_output: output slot on source (always 'unique_filter_output' for atomic nodes)
    - to_input: input slot - CRITICAL, must match target node type exactly:

    NODE TYPE              to_input
    blur               ->  "input1"
    levels             ->  "input1"
    normal             ->  "input1"
    curve              ->  "input1"
    hsl                ->  "input1"
    sharpen            ->  "input1"
    grayscaleconversion->  "input1"
    transformation     ->  "input1"
    emboss             ->  "input1"
    warp               ->  "input1" (image), "inputgradient" (warp map)
    directionalwarp    ->  "input1" (image), "inputintensity" (warp map — NOT inputgradient!)
    distance           ->  "input1"
    blend              ->  "source" (fg), "destination" (bg), "opacity" (mask)
    output             ->  "inputNodeOutput"

    For library nodes (Cells, Perlin, Polygon, etc.): ALWAYS run get_node_info
    first to discover exact input/output IDs. Never guess.

    - graph_identifier: target graph (None = current)
    """
    return await _async_send("connect_nodes", {
        "from_node_id": from_node_id,
        "to_node_id": to_node_id,
        "from_output": from_output,
        "to_input": to_input,
        "graph_identifier": graph_identifier,
    })


@mcp.tool(annotations=_DESTRUCTIVE, structured_output=False)
async def disconnect_nodes(ctx: Context,
                           node_id: str,
                           input_id: str,
                           graph_identifier: Optional[str] = None) -> str:
    """
    Disconnect all connections to a specific input of a node.
    - node_id: target node
    - input_id: input property to disconnect (e.g. 'input1', 'source')
    - graph_identifier: target graph (None = current)
    """
    return await _async_send("disconnect_nodes", {
        "node_id": node_id,
        "input_id": input_id,
        "graph_identifier": graph_identifier,
    })


@mcp.tool(annotations=_UPDATE)
async def set_parameter(ctx: Context,
                        node_id: str,
                        parameter_id: str,
                        value: Any,
                        value_type: str = "float",
                        graph_identifier: Optional[str] = None) -> ParameterUpdateResult:
    """
    Set a parameter on a node in Substance Designer.
    - node_id: target node identifier
    - parameter_id: parameter name

    Common parameter IDs:
      '$outputsize'  - Resolution as int2, e.g. [11,11] for 2048x2048
      'intensity'    - Blur/sharpen/normal intensity (float)
      'blendingmode' - Blend mode (int): 0=Copy, 1=Add, 2=Subtract, 3=Multiply, 9=Overlay
      'opacitymult'  - Blend opacity multiplier (float 0-1)
      'outputcolor'  - Uniform color output (color [r,g,b,a])
      'levelinlow'   - Levels input low (float4)
      'levelinhigh'  - Levels input high (float4)
      'leveloutlow'  - Levels output low (float4)
      'levelouthigh' - Levels output high (float4)
      'hue'          - HSL hue shift (float 0-1, 0.5=no shift)
      'saturation'   - HSL saturation (float 0-1, 0.5=no change)
      'luminosity'   - HSL luminosity (float 0-1, 0.5=no change)
      'matrix22'     - Transformation matrix (float4)
      'offset'       - Transformation offset (float2)
      'channelsweights' - Grayscale channel weights (float4)

    - value: the value to set
    - value_type: 'float', 'int', 'bool', 'string', 'float2', 'float3', 'float4',
                  'color' (RGBA 0-1), 'int2', 'int3', 'int4'
    - graph_identifier: target graph (None = current)
    """
    data = await _async_data("set_parameter", {
        "node_id": node_id,
        "parameter_id": parameter_id,
        "value": value,
        "value_type": value_type,
        "graph_identifier": graph_identifier,
    })
    return _typed(ParameterUpdateResult, data)


@mcp.tool(annotations=_UPDATE)
async def set_parameters(ctx: Context,
                         updates: List[BatchParameter],
                         graph_identifier: Optional[str] = None) -> SetParametersResult:
    """
    Set several parameters across one or more nodes in ONE call.

    The plugin resolves the graph and each node once and applies every update,
    so an edit -> inspect -> adjust loop needs one call instead of one per
    property. Failures are reported per update (status "ok"/"error") and do
    not abort the batch.

    updates: list of {node_id, parameter_id, value, value_type?} objects using
    the same parameter IDs, aliases and value types as set_parameter. Example:
      [{"node_id": "Base_Noise", "parameter_id": "disorder", "value": 0.4},
       {"node_id": "Controls", "parameter_id": "color",
        "value": [0.8, 0.2, 0.1], "value_type": "color"}]

    Returns {graph, applied, failed, results: [{node_id, parameter_id, value,
    value_type, status, error?, renamed_from?}...]}.
    """
    data = await _async_data("set_parameters", {
        "updates": _dump(updates),
        "graph_identifier": graph_identifier,
    })
    return _typed(SetParametersResult, data)


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
async def get_node_info(ctx: Context,
                        node_id: str,
                        graph_identifier: Optional[str] = None) -> str:
    """
    Get detailed info about a node (properties, connections, position).
    - node_id: node identifier
    - graph_identifier: target graph (None = current)

    IMPORTANT: Call this for ANY library node before connecting it.
    The response includes exact input/output port IDs and whether it's a library node.
    """
    return await _async_send("get_node_info", {
        "node_id": node_id,
        "graph_identifier": graph_identifier,
    })


@mcp.tool(annotations=_DESTRUCTIVE, structured_output=False)
async def delete_node(ctx: Context,
                      node_id: str,
                      graph_identifier: Optional[str] = None) -> str:
    """
    Delete a node from a Substance Designer graph.
    - node_id: node identifier to delete
    - graph_identifier: target graph (None = current)
    """
    return await _async_send("delete_node", {
        "node_id": node_id,
        "graph_identifier": graph_identifier,
    })


@mcp.tool(annotations=_UPDATE, structured_output=False)
async def move_node(ctx: Context,
                    node_id: str,
                    position: List[float],
                    graph_identifier: Optional[str] = None) -> str:
    """
    Move a node to a new position in the graph.
    - node_id: node to move
    - position: [x, y] new position
    - graph_identifier: target graph (None = current)

    PREFER move_node over arrange_nodes — arrange_nodes DESTROYS all connections in SD 15.
    """
    return await _async_send("move_node", {
        "node_id": node_id,
        "position": position,
        "graph_identifier": graph_identifier,
    })


@mcp.tool(annotations=_ADDITIVE, structured_output=False)
async def duplicate_node(ctx: Context,
                         node_id: str,
                         offset: Optional[List[float]] = None,
                         graph_identifier: Optional[str] = None) -> str:
    """
    Duplicate an ATOMIC node (creates a new node of the same type).
    - node_id: node to duplicate (must be atomic, not a library instance node)
    - offset: [dx, dy] position offset from original (default [100, 0])
    - graph_identifier: target graph (None = current)

    WARNING: Library nodes (Cells, Perlin, Polygon, etc.) CANNOT be duplicated
    via this method. Use create_instance_node with the same resource_url instead.
    """
    return await _async_send("duplicate_node", {
        "node_id": node_id,
        "offset": offset,
        "graph_identifier": graph_identifier,
    })


@mcp.tool(annotations=_UPDATE, structured_output=False)
async def set_graph_output_size(ctx: Context,
                                width_log2: int = 11,
                                height_log2: int = 11,
                                graph_identifier: Optional[str] = None) -> str:
    """
    Set the output resolution of a graph.
    - width_log2: log2 of width (9=512, 10=1024, 11=2048, 12=4096)
    - height_log2: log2 of height
    - graph_identifier: target graph (None = current)
    """
    return await _async_send("set_graph_output_size", {
        "width_log2": width_log2,
        "height_log2": height_log2,
        "graph_identifier": graph_identifier,
    })


@mcp.tool(annotations=_DESTRUCTIVE, structured_output=False)
async def save_package(ctx: Context,
                       package_index: int = 0,
                       file_path: Optional[str] = None,
                       package_path: Optional[str] = None) -> str:
    """
    Save a package to disk.
    - package_index: which user package (0 = first)
    - file_path: if given, Save As to this path (.sbs) — directories created automatically
    - package_path: identify package by existing file path
    """
    return await _async_send("save_package", {
        "package_index": package_index,
        "file_path": file_path,
        "package_path": package_path,
    })


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
async def get_library_nodes(ctx: Context,
                            filter_text: str = "",
                            limit: int = 200) -> str:
    """
    Get available library nodes from Substance Designer's built-in packages.
    Returns resource URLs for use with create_instance_node.
    - filter_text: search filter (e.g. 'cell', 'perlin', 'noise', 'grunge', 'polygon')
    - limit: max results

    After getting a URL, use create_instance_node(resource_url=url) to add it,
    then get_node_info(node_id) to discover its port IDs before connecting.
    """
    return await _async_send("get_library_nodes", {
        "filter_text": filter_text,
        "limit": limit,
    })


@mcp.tool(annotations=_UPDATE, structured_output=False)
async def arrange_nodes(ctx: Context,
                        graph_identifier: Optional[str] = None,
                        start_x: float = -1000,
                        start_y: float = 0,
                        node_spacing_x: float = 200,
                        node_spacing_y: float = 150) -> str:
    """
    Auto-arrange all nodes in a graph in a grid layout.
    - graph_identifier: target graph (None = current)
    - start_x, start_y: starting position
    - node_spacing_x, node_spacing_y: spacing between nodes

    WARNING: In SD 15, arrange_nodes DESTROYS all node connections.
    Use move_node() instead to reposition nodes without losing connections.
    Only use this tool when you don't mind reconnecting all nodes manually.
    """
    return await _async_send("arrange_nodes", {
        "graph_identifier": graph_identifier,
        "start_x": start_x,
        "start_y": start_y,
        "node_spacing_x": node_spacing_x,
        "node_spacing_y": node_spacing_y,
    })


@mcp.tool(annotations=_ARBITRARY, structured_output=False)
async def execute_sd_code(ctx: Context, code: str) -> str:
    """
    Execute arbitrary Python code in Substance Designer's main thread.

    DISABLED BY DEFAULT. Run the bridge with
      --allow-arbitrary-code
    (or env SD_MCP_ALLOW_EXECUTE_CODE=1) to enable this tool. The plugin
    enforces its own matching gate: SD_MCP_ALLOW_EXECUTE_CODE=1 in SD's
    environment, or an 'allow_execute_code' marker file in the plugin folder
    (%USERPROFILE%\\Documents\\Adobe\\Adobe Substance 3D Designer\\python\\
    sduserplugins\\sd_mcp_plugin\\allow_execute_code). Both sides must agree.

    Variables available: sd, app, pkg_mgr, ui_mgr, open_in_editor
    Returns stdout, stderr, and any error.
    WARNING: No timeout protection. Blocking code will hang SD indefinitely;
    keep code short, simple, and non-blocking.
    """
    if not _allow_arbitrary_code:
        raise ToolError(
            "execute_sd_code is disabled by default. Restart the bridge with "
            "--allow-arbitrary-code (or set SD_MCP_ALLOW_EXECUTE_CODE=1) and "
            "enable the plugin-side gate (SD_MCP_ALLOW_EXECUTE_CODE=1 in "
            "SD's environment, or an 'allow_execute_code' file in the plugin "
            "folder), then retry. Prefer a dedicated tool when one exists.")
    return await _async_send("execute_code", {"code": code})


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
async def capture_3d_view(ctx: Context,
                          output_path: Optional[str] = None,
                          refresh_first: bool = False,
                          wait_ms: int = 600,
                          return_image: bool = True) -> list:
    """
    Capture the Substance Designer 3D view (material preview) and return it as an image.

    Lets the agent SEE the rendered material and iterate on parameters:
    capture -> judge the image -> set_parameter -> capture again.
    Works even when the SD window is behind other windows (PrintWindow); falls
    back to briefly raising SD and doing a screen capture.

    - output_path: where to save the PNG (default: %TEMP%/sd_mcp_preview/3dview_<time>.png)
    - refresh_first: post Space to the 3D viewport before capture; use this if
      the render looks stale after parameter edits
    - wait_ms: settle time in ms before capturing (default 600)
    - return_image: include the PNG in the response (default True); set False to
      only save the file and get its path

    Returns JSON {path, width, height, method} followed by the image.
    The 3D view must be open in SD (F3).
    """
    result = await asyncio.to_thread(_request, "capture_3d_view", {
        "output_path": output_path,
        "refresh_first": refresh_first,
        "wait_ms": wait_ms,
    })
    content: list = [json.dumps(result, indent=2)]
    path = result.get("path") if isinstance(result, dict) else None
    if return_image and path and os.path.isfile(path):
        content.append(Image(path=path))
    return content


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
async def capture_2d_view(ctx: Context,
                          graph_identifier: Optional[str] = None,
                          node_id: Optional[str] = None,
                          output_usage: Optional[str] = None,
                          output_usages: Optional[List[str]] = None,
                          port_id: Optional[str] = None,
                          max_size: int = 1024,
                          output_path: Optional[str] = None,
                          wait_ms: int = 600,
                          return_image: bool = True) -> list:
    """
    Capture a single map of a graph (e.g. the Height output) as an image.

    Two modes:
    - Texture mode (node_id or output_usage given): the plugin calls
      graph.compute(), reads that node's output texture through the SD API and
      returns it as a PNG - exact pixels, downscaled to max_size. No UI
      interaction; works while SD is occluded. Ideal to review one map:
      capture_2d_view(graph_identifier="X", output_usage="height").
    - Widget mode (neither given): grabs the visible 2D view widget - what SD
      shows, including tiling/zoom/selection preview. Open the 2D view (F2)
      first; if graph_identifier is given, that graph's tab is opened first.

    - graph_identifier: graph name as shown in the Explorer (None = current)
    - node_id: any node (e.g. a mid-graph node) whose output map to render
    - output_usage: OUTPUT node label to render, e.g. "height", "normal",
      "Base Color", "ambientOcclusion", "ao" (case/space-insensitive)
    - output_usages: render several OUTPUT labels into one labeled contact
      sheet, e.g. ["height", "normal", "ambientOcclusion", "baseColor"].
      Cannot be combined with node_id or output_usage.
    - port_id: disambiguates nodes with several texture outputs
    - max_size: longest side of the returned PNG (0 = native resolution)
    - output_path: where to save the PNG
      (default: %TEMP%/sd_mcp_preview/2dview_*.png)
    - wait_ms: settle time in ms after switching the editor tab (widget mode)
    - return_image: include the PNG in the response (default True)

    Returns JSON {path, width, height, method, ...} followed by the image.
    """
    result = await asyncio.to_thread(_request, "capture_2d_view", {
        "graph_identifier": graph_identifier,
        "node_id": node_id,
        "output_usage": output_usage,
        "output_usages": output_usages,
        "port_id": port_id,
        "max_size": max_size,
        "output_path": output_path,
        "wait_ms": wait_ms,
    })
    content: list = [json.dumps(result, indent=2)]
    path = result.get("path") if isinstance(result, dict) else None
    if return_image and path and os.path.isfile(path):
        content.append(Image(path=path))
    return content


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
async def capture_node_preview(ctx: Context,
                               graph_identifier: Optional[str] = None,
                               node_id: Optional[str] = None,
                               node_ids: Optional[List[str]] = None,
                               max_size: int = 512,
                               output_path: Optional[str] = None,
                               wait_ms: int = 250,
                               return_image: bool = True) -> list:
    """
    Capture the graph editor's node preview thumbnail (no graph.compute()).

    Grabs each node's tile from the visible graph view, so this is much faster
    than capture_2d_view(node_id=...) and also works for nodes that are not
    connected to a graph output. The graph tab must be open in SD; each node is
    focused briefly so its tile is in view. SD 15 renders previews in the graph
    scene, so the tile crop is the normal path; a graph-owned preview widget is
    used when present. Each node is fitted into view for a consistent crop, then
    the original zoom and scroll position are restored.

    - graph_identifier: graph name as shown in the Explorer (None = current)
    - node_id: one node to capture
    - node_ids: several nodes captured into one labeled contact sheet (max 32)
    - max_size: longest side of each preview image (0 = native)
    - output_path: where to save the PNG
      (default: %TEMP%/sd_mcp_preview/node_preview_*.png)
    - wait_ms: settle time in ms after opening/focusing each node
    - return_image: include the PNG in the response (default True)

    Returns JSON {path, width, height, method, capture, graph, node, label, ...}
    for one node, or a contact-sheet result with items + skipped for several.
    """
    result = await asyncio.to_thread(_request, "capture_node_preview", {
        "graph_identifier": graph_identifier,
        "node_id": node_id,
        "node_ids": node_ids,
        "max_size": max_size,
        "output_path": output_path,
        "wait_ms": wait_ms,
    })
    content: list = [json.dumps(result, indent=2)]
    path = result.get("path") if isinstance(result, dict) else None
    if return_image and path and os.path.isfile(path):
        content.append(Image(path=path))
    return content


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
async def get_map_stats(ctx: Context,
                        graph_identifier: Optional[str] = None,
                        node_id: Optional[str] = None,
                        output_usage: Optional[str] = None,
                        port_id: Optional[str] = None,
                        histogram_bins: int = 16) -> str:
    """
    Return per-channel min, max, mean and histogram values for a computed
    node output texture.

    Pass exactly one of node_id or output_usage. Integer formats are normalized
    to 0..1; floating-point formats retain their stored values. Histograms use
    each channel's observed finite min/max range; min/max/mean/count are always
    exact, while histograms of 16-bit/float maps larger than ~1M pixels are
    counted from evenly strided samples (histogram.sample_stride reports the
    stride; 1 = every pixel counted).
    """
    return await _async_send("get_map_stats", {
        "graph_identifier": graph_identifier,
        "node_id": node_id,
        "output_usage": output_usage,
        "port_id": port_id,
        "histogram_bins": histogram_bins,
    })


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
async def inspect_outputs(ctx: Context,
                          graph_identifier: Optional[str] = None,
                          output_usages: Optional[List[str]] = None,
                          max_size: int = 1024,
                          histogram_bins: int = 16,
                          output_path: Optional[str] = None,
                          return_image: bool = True) -> list:
    """
    Inspect several graph outputs in ONE call: a labeled preview contact sheet
    plus per-channel numeric statistics, both from a single graph compute.

    capture_2d_view(output_usages=...) and get_map_stats each compute the graph
    on their own; this combined tool computes once and reads the preview tiles
    and the pixel buffers from the same computed textures. Outputs that cannot
    be matched or read are listed under "skipped" instead of failing the call.

    - graph_identifier: graph name as shown in the Explorer (None = current)
    - output_usages: OUTPUT node labels, e.g. ["height", "normal", "ao",
      "baseColor"] (case/space-insensitive; "ao" maps to ambientOcclusion).
      Default: height, normal, ambientOcclusion, baseColor.
    - max_size: longest side of each preview tile (0 = native resolution)
    - histogram_bins: histogram bucket count per channel (1..512)
    - output_path: where to save the contact sheet PNG
      (default: %TEMP%/sd_mcp_preview/inspect_outputs_*.png)
    - return_image: include the PNG in the response (default True)

    min/max/mean/count are exact; histograms of 16-bit/float maps larger than
    ~1M pixels are counted from evenly strided samples (histogram.sample_stride
    reports the stride; 1 = every pixel counted).

    Returns JSON {graph, path, width, height, histogram_bins, columns, rows,
    outputs: [{usage, label, node, port_id, source_width, source_height,
    width, height, pixel_format, value_domain, channels: {<channel>: {min,
    max, mean, count, non_finite, histogram}}}], skipped} followed by the
    image.
    """
    result = await asyncio.to_thread(_request, "inspect_outputs", {
        "graph_identifier": graph_identifier,
        "output_usages": output_usages,
        "max_size": max_size,
        "histogram_bins": histogram_bins,
        "output_path": output_path,
    })
    content: list = [json.dumps(result, indent=2)]
    path = result.get("path") if isinstance(result, dict) else None
    if return_image and path and os.path.isfile(path):
        content.append(Image(path=path))
    return content


@mcp.tool(annotations=_UPDATE)
async def send_to_3d_view(ctx: Context,
                          graph_identifier: str,
                          channels: Optional[Dict[str, str]] = None,
                          timeout_s: float = 90.0) -> Send3DStatusResult:
    """
    Show a graph's material in the SD 3D view by binding its output nodes to
    the 3D view material channels (what right-click -> "View In 3D View" does).

    Runs inside SD with synthetic Qt events: the mouse cursor is not moved and
    SD is not brought to the front. SD switches its editor tab to the graph.
    Takes about 2-3 s per channel; this call waits until all are done.

    - graph_identifier: graph name as shown in the Explorer
    - channels: optional {output node id or label: channel name} overrides.
      By default each output's label picks the channel ("Base Color",
      "Normal", "Roughness", "Metallic", "Ambient Occlusion", "Height", ...)
    - timeout_s: give up waiting after this many seconds

    Returns JSON {job_id, graph, done, bound, failed, skipped, pending}.
    The binding is live: later set_parameter edits re-render the 3D view, so
    call capture_3d_view to see the result.
    """
    start = await asyncio.to_thread(_request, "send_to_3d_view", {
        "graph_identifier": graph_identifier,
        "channels": channels,
    })
    deadline = time.monotonic() + timeout_s
    status = start
    while time.monotonic() < deadline:
        await asyncio.sleep(0.5)
        status = await asyncio.to_thread(
            _request, "send_to_3d_view_status", {"job_id": start["job_id"]})
        if status.get("done"):
            break
    else:
        status = dict(status, note="Timed out waiting; the job may still be "
                                   "running inside SD.")
    return _typed(Send3DStatusResult, status)


@mcp.tool(annotations=_ADDITIVE)
async def create_batch_graph(ctx: Context,
                             graph_name: str,
                             package_index: int = 0,
                             nodes: Optional[List[BatchNode]] = None,
                             connections: Optional[List[BatchConnection]] = None,
                             output_size_log2: int = 11,
                             open_in_editor: bool = True,
                             package_path: Optional[str] = None,
                             preflight: bool = False,
                             cleanup_on_failure: bool = False) -> BatchBuildResult:
    """
    Create a complete graph from a descriptor in ONE call. Most efficient for complex graphs.

    nodes: list of node descriptors:
      {
        "id_alias": "my_blur",           # reference name for connections
        "definition_id": "sbs::compositing::blur",
        "position": [0, 0],              # optional
        "usage": "height",               # for output nodes only
        "label": "Height Output",        # for output nodes
        "resource_url": "pkg:///...",    # for library nodes (use get_library_nodes first)
        "parameters": {                  # optional parameter overrides
          "intensity": 10.0,             # shorthand (auto-detects float)
          "$outputsize": {"value": [11,11], "type": "int2"}  # explicit type
        }
      }

    connections: list of connection descriptors:
      {
        "from": "my_blur",                  # from node alias
        "to": "height_output",              # to node alias
        "from_output": "unique_filter_output",  # optional (default for atomic nodes)
        "to_input": "inputNodeOutput"       # required for output nodes
      }

    Library nodes in batch:
      - Use "resource_url" or "library_keyword" (not "definition_id")
      - Single-output library nodes auto-resolve from_output; for multi-output
        nodes a wrong port ID fails that connection, and its error lists the
        valid port IDs (closest match first) so you can reconnect.

    Recovery options:
      - preflight: validate WITHOUT creating anything — resolves the package,
        node definitions, library URLs/keywords, connection aliases and port
        names (atomic nodes use the known port registry; library node ports
        can only be known after instancing). Returns valid/errors/warnings,
        no editor changes.
      - cleanup_on_failure: delete the newly created graph when any node or
        connection fails, leaving the package untouched, and raise an error
        with the report.

    Returns: node_map {alias -> actual_node_id} + creation stats, plus a
    per-connection list with the resolved ports or the error.
    """
    data = await _async_data("create_batch_graph", {
        "graph_name": graph_name,
        "package_index": package_index,
        "nodes": _dump(nodes),
        "connections": _dump(connections),
        "output_size_log2": output_size_log2,
        "open_in_editor": open_in_editor,
        "package_path": package_path,
        "preflight": preflight,
        "cleanup_on_failure": cleanup_on_failure,
    })
    _typed(BatchPreflightResult if preflight else BatchGraphResult, data)
    return data


# ---------------------------------------------------------------------------
# Recipe tools
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_READ_ONLY, structured_output=False)
async def list_recipes(ctx: Context) -> str:
    """
    List all built-in material recipes available in the SD MCP plugin.
    Returns material recipes (metals, rocks, organic, soils, water/ice, gems)
    and heightmap styles (cliff, rock, sand, cracked, mountain, cobblestone).
    """
    return await _async_send("list_recipes", {})


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
async def get_recipe_info(ctx: Context, recipe_name: str) -> str:
    """
    Get detailed info about a specific material recipe.
    recipe_name: e.g. 'steel', 'marble', 'gold', 'wood_oak', 'ice', 'diamond'
    """
    return await _async_send("get_recipe_info", {"recipe_name": recipe_name})


@mcp.tool(annotations=_ADDITIVE)
async def build_material_graph(ctx: Context,
                                graph_name: str,
                                recipe_name: str,
                                package_index: int = 0,
                                overrides: Optional[Dict[str, ParamOverrides]] = None,
                                output_size_log2: int = 11,
                                open_in_editor: bool = True,
                                package_path: Optional[str] = None,
                                cleanup_on_failure: bool = True,
                                 preflight: bool = False) -> BatchBuildResult:
    """
    Build a complete PBR material graph from a named recipe in ONE call.

    Available recipes: steel, iron, copper, gold, silver, aluminum,
    granite, marble, sandstone, limestone, slate,
    wood, wood_oak, oak, moss, bone,
    sand, mud, gravel, clay,
    ice, snow, frost, water,
    diamond, ruby, sapphire, emerald, amethyst.

    Each recipe generates: Height + Normal + Roughness + AO + BaseColor + Metallic outputs.

    overrides: optional parameter overrides keyed by recipe node alias, e.g.
      {"<alias>": {"intensity": 8.0}} (get_recipe_info lists the aliases).
    output_size_log2: 10=1024, 11=2048, 12=4096
    cleanup_on_failure: delete the new graph if the build fails (default True —
      a failed recipe build leaves no half-built graph behind).
    preflight: validate definitions/ports/aliases first without building.
    """
    data = await _async_data("build_material_graph", {
        "graph_name": graph_name,
        "recipe_name": recipe_name,
        "package_index": package_index,
        "overrides": overrides,
        "output_size_log2": output_size_log2,
        "open_in_editor": open_in_editor,
        "package_path": package_path,
        "cleanup_on_failure": cleanup_on_failure,
        "preflight": preflight,
    })
    _typed(BatchPreflightResult if preflight else BatchGraphResult, data)
    return data


@mcp.tool(annotations=_ADDITIVE)
async def build_heightmap_graph(ctx: Context,
                                 graph_name: str,
                                 style: str,
                                 package_index: int = 0,
                                 output_size_log2: int = 11,
                                 open_in_editor: bool = True,
                                 detail_level: int = 2,
                                 scale: float = 5.0,
                                 disorder: float = 0.5,
                                 package_path: Optional[str] = None,
                                 cleanup_on_failure: bool = True,
                                preflight: bool = False) -> BatchBuildResult:
    """
    Build a heightmap-only graph for terrain/rock/cliff.

    Available styles: cliff, rock, sand, cracked, mud, mountain, cobblestone, terrain.

    detail_level: 1=basic, 2=standard, 3=high detail
    scale: overall scale factor (1.0 to 10.0)
    disorder: amount of irregularity/warping (0.0 to 1.0)
    cleanup_on_failure: delete the new graph if the build fails (default True).
    preflight: validate definitions/ports/aliases first without building.
    """
    data = await _async_data("build_heightmap_graph", {
        "graph_name": graph_name,
        "style": style,
        "package_index": package_index,
        "output_size_log2": output_size_log2,
        "open_in_editor": open_in_editor,
        "detail_level": detail_level,
        "scale": scale,
        "disorder": disorder,
        "package_path": package_path,
        "cleanup_on_failure": cleanup_on_failure,
        "preflight": preflight,
    })
    _typed(BatchPreflightResult if preflight else BatchGraphResult, data)
    return data


@mcp.tool(annotations=_ADDITIVE)
async def apply_recipe(ctx: Context,
                        recipe_name: str,
                        graph_identifier: Optional[str] = None,
                        position_offset: Optional[List[float]] = None,
                        overrides: Optional[Dict[str, ParamOverrides]] = None) -> ApplyRecipeResult:
    """
    Apply a recipe to an EXISTING graph (adds nodes + connections to current graph).
    Useful for layering multiple materials or adding detail passes.

    recipe_name: any key from list_recipes
    graph_identifier: target graph (None = current active graph)
    position_offset: [x, y] to offset all node positions (avoid overlapping with existing nodes)
    overrides: parameter overrides keyed by recipe node alias, e.g.
      {"<alias>": {"intensity": 8.0}} (get_recipe_info lists the aliases)
    """
    data = await _async_data("apply_recipe", {
        "recipe_name": recipe_name,
        "graph_identifier": graph_identifier,
        "position_offset": position_offset,
        "overrides": overrides,
    })
    return _typed(ApplyRecipeResult, data)


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
async def list_documentation(ctx: Context,
                              category: str = "all",
                              filter_text: str = "",
                              node_name: str = "",
                              action: str = "",
                              query: str = "") -> str:
    """
    Browse the SD MCP embedded documentation knowledge base.
    No internet required — all knowledge is built into the plugin.

    Available categories:
      all                — everything (large response, use filter_text)
      atomic_nodes       — built-in nodes: blend, levels, blur, normal, warp, etc.
      library_nodes      — library nodes: cells, perlin, flood_fill, histogram_scan, etc.
      blend_modes        — all blend mode integers and descriptions
      port_reference     — input/output port names for every node type
      pbr_outputs        — PBR output usage tags and conventions
      workflow           — step-by-step usage rules and best practices
      concepts           — SD concepts: recipes, pro recipes, graph structure
      shortcuts          — SD keyboard shortcuts
      connection_patterns — common node chains (e.g. height→normal→AO)
      node_categories    — node families and groupings
      parameters         — parameter reference for common nodes

    Special actions:
      action="categories"         → list all available categories
      action="search", query="X"  → search all docs for keyword X

    Examples:
      list_documentation(category="atomic_nodes", node_name="blend")
      list_documentation(category="port_reference")
      list_documentation(action="search", query="directionalwarp")
      list_documentation(category="workflow")
      list_documentation(category="pbr_outputs")
    """
    return await _async_send("list_documentation", {
        "category": category,
        "filter_text": filter_text,
        "node_name": node_name,
        "action": action,
        "query": query,
    })


@mcp.tool(annotations=_UPDATE, structured_output=False)
async def reload_plugin(ctx: Context, handlers: bool = True) -> str:
    """
    Hot-reload the SD MCP plugin's Python code from disk without restarting SD.
    Development tool: use after editing files in the plugin folder.

    - handlers: also reload the command handlers (plugin __init__.py). False
      reloads only the recipes, documentation and port-suggestion modules.

    Disabled by default because it re-executes Python from disk. Start the
    bridge with --allow-dev-tools and set SD_MCP_ALLOW_DEV_TOOLS=1 in SD's
    environment (or use the execute_code opt-in) to enable it on both sides.

    The TCP server and main-thread dispatcher are kept, so changes to those
    still need an SD restart. Bridge (MCP server) changes need a client restart.
    """
    if not _allow_dev_tools:
        raise ToolError(
            "reload_plugin is disabled by default. Restart the bridge with "
            "--allow-dev-tools and enable SD_MCP_ALLOW_DEV_TOOLS=1 in SD's "
            "environment (or the execute_code opt-in) on the plugin side.")
    return await _async_send("reload_plugin", {"handlers": handlers})


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
async def diagnostic(ctx: Context) -> str:
    """
    Full health check of the SD environment: SD version, Qt binding and
    invoker, user packages, library cache, current graph, and whether the
    execute_code gate and token auth are required on the plugin side.

    Run this when tools report failures - it is a read-only report with no
    side effects.
    """
    return await _async_send("diagnostic", {})


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
async def graph_snapshot(ctx: Context,
                         graph_identifier: Optional[str] = None) -> str:
    """
    Export a graph's full structure as JSON: every node (id, definition,
    position, is_library) and every connection (from_node/from_output ->
    to_node/to_input). Good for diffing or re-documenting a graph.

    - graph_identifier: graph name as shown in the Explorer (None = current)
    """
    return await _async_send("graph_snapshot", {
        "graph_identifier": graph_identifier,
    })


# ---------------------------------------------------------------------------
# Material runs: versioned iteration with authoritative package checkpoints.
# A run targets one graph; every revision stores an immutable package copy plus
# a graph fingerprint. Patches are proposed, applied (with automatic
# checkpoint), evaluated from a fresh preview, then accepted or rolled back.
# ---------------------------------------------------------------------------

@mcp.tool(annotations=_ADDITIVE, structured_output=False)
async def start_material_run(ctx: Context,
                             graph_identifier: str,
                             goal: str = "",
                             max_iterations: int = 8,
                             preview_preset: Optional[Dict[str, Any]] = None,
                             constraints: Optional[List[str]] = None,
                             seeds: Optional[Dict[str, Any]] = None,
                             package_index: int = 0,
                             package_path: Optional[str] = None,
                             require_hypothesis: bool = False,
                             material_profile: Optional[str] = None,
                             appearance_targets: Optional[Dict[str, Any]] = None,
                             appearance_description: Optional[str] = None,
                             delivery: Optional[Dict[str, Any]] = None) -> str:
    """
    Start a versioned material-iteration run for one graph.

    Creates a persistent run record (SQLite, under the runs directory) and
    returns its run_id. The graph is NOT saved or snapshotted yet: call
    checkpoint_material_revision to record the baseline (and to verify that the
    package can actually be copied/restored) before proposing patches.

    - graph_identifier: graph name as shown in the Explorer
    - goal: free-text objective, kept with the run for evaluation context
    - max_iterations: patch budget (each accepted or rejected patch counts)
    - preview_preset: defaults for capture_material_preview, keys:
      output_usages (list), max_size (int), histogram_bins (int),
      include_3d (bool), wait_ms (int), refresh_first (bool),
      send_timeout_s (float, 3D-view binding wait)
    - constraints: free-text constraints for the evaluator
    - seeds: optional reproducibility data (e.g. random seeds) kept with the run
    - package_index / package_path: which loaded user package holds the graph
    - require_hypothesis: refuse patches that do not carry a hypothesis
      (defect -> targeted change -> predicted effect); see propose_material_patch
    - material_profile: intended material class seeding loose PBR bands:
      dielectric, metal, rough_dielectric, glossy_dielectric
    - appearance_targets: intended per-output ranges, overriding the profile:
      {"roughness": {"mean": [0.55, 0.8]}, "height": {"mean": [0.35, 0.6]},
       "baseColor": {"mean": [0.25, 0.45], "channel": "r"}} (null = open end).
      Evaluation then judges each change as toward/within/away from target;
      unpredicted changes on untargeted outputs are side effects, not failures.
    - appearance_description: free-text intended look, kept for the evaluator
    - delivery: require an export check before finishing. {} uses defaults;
      keys: required_outputs (default baseColor, normal, roughness, metallic,
      height, ambientOcclusion), file_format (png), resolution
      ([w_log2, h_log2] exact) or min_resolution_log2 (9), bit_depth
      ({"default": 8, "height": 16}), require_square, require_uniform_size
    """
    runs = _material_runs()
    result = await _material_op(runs.start_run, graph_identifier,
                                goal=goal, max_iterations=max_iterations,
                                preview_preset=preview_preset,
                                constraints=constraints, seeds=seeds,
                                package_index=package_index,
                                package_path=package_path,
                                require_hypothesis=require_hypothesis,
                                material_profile=material_profile,
                                appearance_targets=appearance_targets,
                                appearance_description=appearance_description,
                                delivery=delivery if delivery is not None else None)
    return _material_json(result)


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
async def get_material_run(ctx: Context,
                           run_id: Optional[str] = None,
                           include_artifacts: bool = True) -> str:
    """
    Get a run's state: goal, revision/candidate state, iteration budget,
    revisions, candidates, evaluations and artifacts (paths, hashes, trimmed
    stats), plus hypothesis_log: each hypothesis tried so far (defect, change,
    predictions, outcome, accepted). Omit run_id for the most recently updated
    run.
    """
    result = await _material_op(_material_runs().get_run, run_id,
                                include_artifacts=include_artifacts)
    return _material_json(result)


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
async def list_material_runs(ctx: Context, limit: int = 20) -> str:
    """
    List recent material runs (newest first): run_id, graph, state, iteration
    progress, current/best revision and timestamps. Omit run_id in the other
    material tools to target the most recently updated run.
    """
    result = await _material_op(_material_runs().list_runs, limit=limit)
    return _material_json(result)


@mcp.tool(annotations=_ADDITIVE, structured_output=False)
async def checkpoint_material_revision(ctx: Context,
                                       run_id: Optional[str] = None,
                                       label: Optional[str] = None,
                                       checkpoint_path: Optional[str] = None,
                                       verify: bool = True,
                                       notes: Optional[str] = None) -> str:
    """
    Save an immutable package checkpoint for the current working graph and
    record it as a new revision.

    Saves a full package copy via saveCopyOfPackageAs (the working package's
    own path is unchanged), records its fingerprint and sha256, and - when
    verify is True - reloads the checkpoint in SD and compares the graph
    structure hash, proving the copy really contains the current graph. The
    checkpoint path defaults to <runs>/<run_id>/revisions/rNNNN.sbs.

    Use this for the baseline before the first patch, and after manual edits
    you want to keep. Fails if a candidate is pending.
    """
    result = await _material_op(_material_runs().checkpoint_revision, run_id,
                                label=label, checkpoint_path=checkpoint_path,
                                verify=verify, notes=notes)
    return _material_json(result)


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
async def verify_material_checkpoint(ctx: Context,
                                     run_id: Optional[str] = None,
                                     revision_id: Optional[str] = None) -> str:
    """
    Re-verify a recorded revision: reload its checkpoint file in SD, compare
    the graph structure fingerprint with the stored one, and update the
    revision's verified flag. Omit revision_id for the run's current revision.

    Returns verified + both structure hashes. A False result means the
    checkpoint file was altered or contains a different graph.
    """
    result = await _material_op(_material_runs().verify_checkpoint, run_id,
                                revision_id=revision_id)
    return _material_json(result)


@mcp.tool(annotations=_DESTRUCTIVE, structured_output=False)
async def restore_material_revision(ctx: Context,
                                    run_id: Optional[str] = None,
                                    revision_id: Optional[str] = None,
                                    save_back: bool = True,
                                    force: bool = False) -> str:
    """
    Restore the working package from a revision checkpoint.

    Unloads the working package, loads the checkpoint and (with save_back)
    saves it back over the working package path, so the working file and the
    in-memory graph both return to the recorded state. Unsaved changes are
    discarded: by default the tool refuses when the working graph no longer
    matches the current revision (un-checkpointed edits) or when a candidate is
    pending - pass force=True to discard anyway.

    - revision_id: revision to restore (default: the run's current revision)
    - save_back: also overwrite the working .sbs path (ignored when the
      working package lives inside the runs directory)
    - force: discard un-checkpointed edits / supersede a pending candidate
    """
    result = await _material_op(_material_runs().restore_revision, run_id,
                                revision_id=revision_id, save_back=save_back,
                                force=force)
    return _material_json(result)


@mcp.tool(annotations=_ADDITIVE, structured_output=False)
async def propose_material_patch(ctx: Context,
                                 run_id: Optional[str] = None,
                                 operations: Optional[List[Dict[str, Any]]] = None,
                                 notes: Optional[str] = None,
                                 hypothesis: Optional[Dict[str, Any]] = None) -> str:
    """
    Propose a patch for the current revision. Does NOT modify the graph.

    operations: ordered list of edits, at most 32, at most one output-size op:
      {"op": "set_parameter", "node_id": "Blur", "parameter_id": "intensity",
       "value": 12.0, "value_type": "float"}
      {"op": "set_output_size", "width_log2": 11, "height_log2": 11}
    value_type is optional (float/int/bool/string/float2/float3/float4/color/
    int2/int3/int4). Node ids and parameters are validated against the live
    graph, and the working graph must still match the run's current revision.
    hypothesis (recommended; required when the run was started with
    require_hypothesis=True) states why this change is being tried:
      {"defect": "roughness is flat; wet and dry areas read the same",
       "observed_in": ["roughness"],
       "rationale": "the blur washes out the cavity mask driving roughness",
       "expected_effect": "cavities read darker/glossier, peaks stay matte",
       "predictions": [
         {"output": "roughness", "metric": "mean", "direction": "decrease",
          "min_change": 0.05, "max_change": 0.3},
         {"output": "baseColor", "direction": "unchanged", "tolerance": 0.02}]}
    defect and expected_effect are required. Each prediction names an output
    usage, optional channel, metric (mean/min/max, default mean) and direction
    (increase/decrease/unchanged); min_change/max_change/tolerance are relative
    (0.1 = 10%, default 0.02). Repeating operations that an earlier candidate
    already failed with is flagged as a warning. Optional target_role (one of
    macro_shape, edge_breakup, surface_grain, height_processing, color,
    roughness, metallic, normal_processing) is checked against
    analyze_material_graph; editing a node that does not feed the targeted
    outputs is flagged (node_not_upstream).
    Returns the candidate (id, validation result, patch digest); an invalid
    patch raises with every validation error.
    """
    result = await _material_op(_material_runs().propose_patch, run_id,
                                operations=operations, notes=notes,
                                hypothesis=hypothesis)
    return _material_json(result)


@mcp.tool(annotations=_UPDATE, structured_output=False)
async def apply_material_patch(ctx: Context,
                               run_id: Optional[str] = None,
                               candidate_id: Optional[str] = None) -> str:
    """
    Apply the pending candidate's operations to the live graph.

    Re-validates the patch, refuses if the working graph drifted from the
    candidate's base revision, applies each operation through the plugin, then
    automatically checkpoints the resulting state as a new revision (so a
    rejected candidate can be rolled back precisely). On failure the graph is
    restored from the base revision. Afterwards capture_material_preview and
    record_material_evaluation are required before a decision.

    candidate_id is optional when exactly one candidate is pending.
    """
    result = await _material_op(_material_runs().apply_patch, run_id,
                                candidate_id=candidate_id)
    return _material_json(result)


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
async def capture_material_preview(ctx: Context,
                                   run_id: Optional[str] = None,
                                   output_usages: Optional[List[str]] = None,
                                   max_size: Optional[int] = None,
                                   histogram_bins: Optional[int] = None,
                                   include_3d: Optional[bool] = None,
                                   wait_ms: Optional[int] = None,
                                   refresh_first: Optional[bool] = None,
                                   return_image: bool = True) -> list:
    """
    Capture the current revision's preview: a labeled multi-output contact
    sheet with per-channel statistics plus a 3D render, in ONE call. Files are
    stored under the run directory and recorded as artifacts bound to the
    revision's fingerprint (used to reject stale evaluations).

    Uses the run's preview_preset for omitted arguments. The 3D part is
    best-effort: it needs the 3D view open in SD and is skipped with a warning
    if binding/capture fails. The graph must still match the current revision.

    The run's first capture locks the comparison context (outputs, sheet size,
    histogram bins, map resolution/pixel format, 3D render size and timing);
    comparison_control reports any mismatch, and overriding preset arguments
    makes later comparisons uncontrolled. When the revision has a parent, a
    before|after side-by-side sheet (comparison_sheet) is added.

    Returns JSON (artifact paths, technical checks, stats) followed by the
    contact sheet and 3D render images when return_image is True.
    """
    result = await _material_op(_material_runs().capture_preview, run_id,
                                output_usages=output_usages,
                                max_size=max_size,
                                histogram_bins=histogram_bins,
                                include_3d=include_3d, wait_ms=wait_ms,
                                refresh_first=refresh_first)
    content: list = [_material_json(result)]
    if return_image:
        for artifact in result.get("artifacts") or []:
            path = artifact.get("path")
            if path and os.path.isfile(path):
                content.append(Image(path=path))
    return content


@mcp.tool(annotations=_ADDITIVE, structured_output=False)
async def record_material_evaluation(ctx: Context,
                                     run_id: Optional[str] = None,
                                     verdict: Optional[str] = None,
                                     score: Optional[float] = None,
                                     accepted: Optional[bool] = None,
                                     evaluator: Optional[str] = None,
                                     summary: Optional[str] = None,
                                     criteria: Optional[List[str]] = None,
                                     regression_threshold: float = 0.1,
                                     hypothesis_outcome: Optional[str] = None,
                                     observed_effect: Optional[str] = None) -> str:
    """
    Record the evaluation of the applied candidate from its latest preview.

    Requires a fresh preview captured after apply (stale or missing previews
    are rejected). Compares per-channel statistics against the candidate's
    base revision preview and applies a technical regression check.

    - verdict: free-text outcome (e.g. "better", "worse", "mixed")
    - score: optional 0..1 quality score
    - accepted: explicit decision; when omitted it defaults to technical_passed
      and no channel regressions beyond regression_threshold
    - criteria: checklist / notes that informed the verdict
    - regression_threshold: relative mean change treated as a regression
      (default 0.1 = 10%)
    - hypothesis_outcome: your visual judgement of the candidate's hypothesis
      (confirmed/partial/refuted/inconclusive); overrides the stats-measured
      outcome. observed_effect: what actually changed in the images.
    When the candidate has a hypothesis, hypothesis_check scores each
    prediction against the base preview; a refuted hypothesis is not accepted
    by default. With appearance targets (or a hypothesis), comparisons are
    material-aware: changes are classified toward_target / within_target /
    away_from_target / intended / side_effect, and only moving away from an
    intended range fails. comparisons.control says whether before and after
    were captured under the same settings; uncontrolled pairs and outputs that
    leave their target range are not auto-accepted.
    Returns the evaluation incl. per-channel comparisons and accepted.
    """
    result = await _material_op(_material_runs().record_evaluation, run_id,
                                verdict=verdict, score=score, accepted=accepted,
                                evaluator=evaluator, summary=summary,
                                criteria=criteria,
                                regression_threshold=regression_threshold,
                                hypothesis_outcome=hypothesis_outcome,
                                observed_effect=observed_effect)
    return _material_json(result)


@mcp.tool(annotations=_UPDATE, structured_output=False)
async def decide_material_candidate(ctx: Context,
                                    run_id: Optional[str] = None,
                                    accepted: Optional[bool] = None,
                                    restore: bool = True,
                                    notes: Optional[str] = None) -> str:
    """
    Accept or reject the evaluated candidate.

    - accepted: True keeps the applied state (it is already checkpointed as the
      revision the run advances to); False restores the candidate's base
      revision (unless restore=False, keeping the edits as a rejected state).
      Omitted uses the recorded evaluation's accepted flag.
    - notes: rationale stored with the decision.
    The run returns to baseline_ready for the next iteration.
    """
    result = await _material_op(_material_runs().decide_candidate, run_id,
                                accepted=accepted, restore=restore,
                                notes=notes)
    return _material_json(result)


@mcp.tool(annotations=_UPDATE, structured_output=False)
async def finish_material_run(ctx: Context,
                              run_id: Optional[str] = None,
                              notes: Optional[str] = None,
                              restore_best: bool = True) -> str:
    """
    Close a run. By default restores the best accepted revision (or the
    baseline when nothing was accepted) so the working package ends in the
    chosen state, then marks the run finished and returns a summary.

    - restore_best: False leaves the working graph as-is.
    A pending candidate must be decided (or restored with
    restore_material_revision) first. When the run was started with
    delivery, the final revision must pass validate_material_delivery (run
    automatically if no passing record matches it); otherwise the run stays
    open and the failed checks are returned as the error.
    """
    result = await _material_op(_material_runs().finish_run, run_id,
                                notes=notes, restore_best=restore_best)
    return _material_json(result)


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
async def analyze_material_graph(ctx: Context,
                                 graph_identifier: Optional[str] = None,
                                 run_id: Optional[str] = None) -> str:
    """
    Explain what each node controls, so edits target the right part of the
    material instead of adjusting parameters blindly. No compute.

    Walks upstream from every graph output and classifies nodes as
    macro_shape, edge_breakup, surface_grain, height_processing,
    normal_processing, color, roughness, metallic (or output), using which
    outputs they reach, their definition and frequency-like parameters
    (scale/tiling/amount). Each node lists its role, reason, confidence,
    influences ({output: distance}) and tunable_parameters; 'outputs' lists
    each output's upstream chain. Pass graph_identifier, or run_id (default:
    latest run) to analyze a run's graph. Treat confidence < 0.6 as a guess.
    """
    result = await _material_op(_material_runs().analyze_graph, run_id,
                                graph_identifier=graph_identifier)
    return _material_json(result)


@mcp.tool(annotations=_ADDITIVE, structured_output=False)
async def validate_material_delivery(ctx: Context,
                                     run_id: Optional[str] = None,
                                     delivery: Optional[Dict[str, Any]] = None,
                                     output_dir: Optional[str] = None) -> str:
    """
    Export the current revision's outputs and validate them for delivery.

    Writes each required output with SDTexture.save (default under the run
    directory, or output_dir), then re-reads the files: present and non-empty,
    expected file format, header dimensions/bit depth (PNG, TIFF, EXR headers
    are parsed; otherwise the texture's pixel format is used), resolution,
    square and uniform size. delivery overrides the run's delivery spec (see
    start_material_run). Records the result on the run; finish_material_run
    requires a passing record for runs started with delivery. Requires plugin
    v3.10.0.
    """
    result = await _material_op(_material_runs().validate_delivery, run_id,
                                delivery=delivery, output_dir=output_dir)
    return _material_json(result)


# ---------------------------------------------------------------------------
# Long jobs: stage-tracked, cancellable versions of the heavy computes
# (inspect_outputs, get_map_stats, capture_2d_view, capture_node_preview).
# The plugin steps each op
# through stages on its main thread (compute, conversions, save) so status
# polls can interleave; wait_long_job turns stage changes into MCP progress
# notifications. cancellation is checked between stages.
# ---------------------------------------------------------------------------

_JOB_OPS = ("inspect_outputs", "get_map_stats", "capture_2d_view",
            "capture_node_preview")


@mcp.tool(annotations=_UPDATE, structured_output=False)
async def start_long_job(ctx: Context,
                         job_type: str,
                         params: Optional[Dict[str, Any]] = None) -> str:
    """
    Start a heavy operation as a stage-tracked job and return {job_id, op}
    immediately instead of running it to completion synchronously.

    - job_type: one of "inspect_outputs", "get_map_stats", "capture_2d_view",
      "capture_node_preview" (same parameters as the direct tools;
      wait_long_job reports progress and returns the same result shape)
    - params: keyword arguments of that tool, e.g. {"graph_identifier": "Steel",
      "output_usages": ["height", "normal"]}

    poll wait_long_job(job_id) until done is True; cancel_long_job stops it
    between stages. Prefer the direct tools for small graphs.
    """
    if job_type not in _JOB_OPS:
        raise ToolError(
            "Unknown job_type '{}'. Available: {}".format(
                job_type, ", ".join(_JOB_OPS)))
    return await _async_send("start_job", {
        "op": job_type,
        "params": params or {},
    })


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
async def wait_long_job(ctx: Context,
                        job_id: int,
                        timeout_s: float = 120.0,
                        poll_s: float = 1.0) -> str:
    """
    Poll a long job started with start_long_job until it is done (or
    timeout_s elapses) and return its status/result. Stage changes are
    reported as MCP progress notifications when the client provided a
    progress token.

    Returns the job status dict: {job_id, op, done, cancelled, stage,
    stage_index, stages_total, progress, result?, error?}. On completion
    result holds the same shape the direct tool would have returned.
    """
    deadline = time.monotonic() + timeout_s
    status: dict = {}
    last_key = None
    while time.monotonic() < deadline:
        status = await asyncio.to_thread(_request, "job_status", {"job_id": job_id})
        key = (status.get("stage"), status.get("stage_index"),
               status.get("progress"), status.get("done"))
        if key != last_key:
            last_key = key
            try:
                await ctx.report_progress(
                    float(status.get("progress") or 0.0), 1.0,
                    message=str(status.get("stage") or ""))
            except Exception:
                pass
        if status.get("done"):
            break
        await asyncio.sleep(max(0.2, min(float(poll_s), 2.0)))
    else:
        status = dict(status, note="Timed out waiting; the job keeps running "
                                   "inside SD - poll get_job_status again.")
    return json.dumps(status, indent=2)


@mcp.tool(annotations=_READ_ONLY, structured_output=False)
async def get_job_status(ctx: Context, job_id: Optional[int] = None) -> str:
    """
    Return the current status of a long job without waiting or emitting
    progress: {job_id, op, done, cancelled, stage, stage_index, stages_total,
    progress, result?, error?}.

    job_id: id from start_long_job. Omit for the latest job (one registry;
    an error listing started job ids is raised when none is running).
    """
    return await _async_send("job_status", {"job_id": job_id})


@mcp.tool(annotations=_UPDATE, structured_output=False)
async def cancel_long_job(ctx: Context, job_id: int) -> str:
    """
    Request cancellation of a long job. It stops between stages (the single
    graph.compute() stage cannot be interrupted mid-flight) and its status
    reports done=True with cancelled=True.
    """
    return await _async_send("job_cancel", {"job_id": job_id})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def _positive_float(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be a number")
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def main():
    global _sd_port, _auth_token, _allow_arbitrary_code, _allow_dev_tools
    global TIMEOUT, HEAVY_TIMEOUT, _runs_dir

    parser = argparse.ArgumentParser(
        description=f"Substance Designer MCP Bridge v{BRIDGE_VERSION}")
    parser.add_argument("--port", type=int, default=_env_int("SD_MCP_PORT", 9881),
                        help="TCP port to connect to the SD plugin (default: "
                             "SD_MCP_PORT or 9881)")
    parser.add_argument("--timeout", type=_positive_float, default=TIMEOUT,
                        help="Socket timeout in seconds for ordinary commands "
                             "(default: %(default)s; must exceed the plugin's "
                             "SD_MCP_COMMAND_TIMEOUT)")
    parser.add_argument("--heavy-timeout", type=_positive_float,
                        default=HEAVY_TIMEOUT,
                        help="Socket timeout for builds/computes/captures "
                             "(default: %(default)s)")
    parser.add_argument("--token",
                        help="Shared secret sent with every command. Set "
                             "SD_MCP_TOKEN in SD's environment to require it "
                             "on the plugin side (localhost callers without "
                             "the token are refused).")
    parser.add_argument("--allow-arbitrary-code", action="store_true",
                        help="Enable the execute_sd_code tool (runs arbitrary "
                             "Python inside SD; the plugin has its own gate).")
    parser.add_argument("--allow-dev-tools", action="store_true",
                        help="Enable reload_plugin (re-executes plugin code "
                             "from disk; the plugin has its own gate).")
    parser.add_argument("--runs-dir",
                        help="Directory for material-run databases and "
                             "revision checkpoints (default: SD_MCP_RUNS_DIR "
                             "or %LOCALAPPDATA%%/sd_mcp/runs).")
    args = parser.parse_args()
    _sd_port = args.port
    TIMEOUT = args.timeout
    HEAVY_TIMEOUT = max(args.heavy_timeout, args.timeout)
    _runs_dir = (os.path.abspath(os.path.expanduser(args.runs_dir))
                 if args.runs_dir else None)
    _reset_material_runs()
    _auth_token = args.token if args.token else os.environ.get("SD_MCP_TOKEN")
    _allow_arbitrary_code = args.allow_arbitrary_code or os.environ.get(
        "SD_MCP_ALLOW_EXECUTE_CODE", "").strip().lower() in ("1", "true", "yes", "on")
    _allow_dev_tools = args.allow_dev_tools or os.environ.get(
        "SD_MCP_ALLOW_DEV_TOOLS", "").strip().lower() in ("1", "true", "yes", "on")

    logger.info(f"SD MCP Bridge v{BRIDGE_VERSION} -> SD plugin on port {_sd_port}")
    logger.info(f"Timeouts: default {TIMEOUT:g}s, heavy {HEAVY_TIMEOUT:g}s")
    if _auth_token:
        logger.info("Token auth: ENABLED (commands must match SD_MCP_TOKEN)")
    else:
        logger.info("Token auth: off (localhost only)")
    if _allow_arbitrary_code:
        logger.warning("execute_sd_code: ENABLED (--allow-arbitrary-code)")
    else:
        logger.info("execute_sd_code: disabled (use --allow-arbitrary-code to enable)")
    if _allow_dev_tools:
        logger.warning("reload_plugin: ENABLED (--allow-dev-tools)")
    else:
        logger.info("reload_plugin: disabled (use --allow-dev-tools to enable)")
    mcp.run()


if __name__ == "__main__":
    main()
