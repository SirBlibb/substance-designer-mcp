# Substance Designer MCP

**The first MCP (Model Context Protocol) integration for Adobe Substance 3D Designer.**

Control Substance Designer from any MCP-compatible AI client (Claude Code, Claude Desktop, Cursor) — create graphs, build nodes, connect them, set parameters, generate full PBR material graphs from a single prompt.

---

## What it does

- **Natural language → SD graph**: "Create a cracked concrete material" → 44-node PBR graph with height, normal, roughness, AO, baseColor, metallic outputs
- **79 built-in material recipes**: pro-grade materials using the same node architecture as professional SD artists
- **57 MCP tools**: get scene info, create/delete graphs, create nodes, connect nodes, set parameters, build full materials in one call, health check, snapshot, stage-tracked cancellable long jobs, and versioned material runs with package checkpoints and rollback
- **Library node support**: Clouds, Cells, Perlin Noise, Polygon, Edge Detect, Flood Fill, Multi-Directional Warp, etc.
- **Thread-safe**: All SD API calls dispatched to Qt main thread via Signal/Slot queued connection

---

## Architecture

```
AI Client (Claude / Cursor)
        │  stdio (MCP protocol)
        ▼
sd_mcp_bridge.py  (FastMCP server, Python 3.12)
        │  TCP localhost:9881 (length-prefix framing)
        ▼
SD Plugin __init__.py  (Python 3.11, runs inside SD)
        │  sd.api
        ▼
Adobe Substance 3D Designer 15.x
```

The bridge and plugin communicate with a simple length-prefix framing protocol: `[4-byte big-endian length][JSON payload]`. Each command uses a fresh TCP socket — no persistent connection, no stale state.

---

## Requirements

- **Adobe Substance 3D Designer 15.x** (tested on 15.0.3)
- **Python 3.12+** for the bridge (managed via [uv](https://github.com/astral-sh/uv))
- **uv** package manager: `pip install uv` or `winget install astral-sh.uv`
- An MCP-compatible client: [Claude Code](https://claude.ai/code), [Claude Desktop](https://claude.ai/desktop), or [Cursor](https://cursor.sh)

---

## Installation

### Step 1 — Install the SD Plugin

Copy the `plugin/` folder contents to your SD user scripts directory:

**Windows:**
```
%USERPROFILE%\Documents\Adobe\Adobe Substance 3D Designer\python\sduserplugins\sd_mcp_plugin\
```

The directory must contain:
```
sd_mcp_plugin/
├── __init__.py          ← main plugin (TCP listener, all command handlers)
├── recipes.py           ← 79 material recipes
├── sd_documentation.py  ← embedded docs served by list_documentation
├── texture_stats.py     ← pixel statistics reader (unit-tested outside SD)
└── port_suggest.py      ← port-ID suggestions for connection errors
```

SD auto-loads all plugins from `sduserplugins/` on startup. No additional configuration needed.

**For plugin development**, link the folder instead of copying it so edits in the repo are
live on disk, then call the `reload_plugin` tool to pick them up without restarting SD:
```
mklink /J "%USERPROFILE%\Documents\Adobe\Adobe Substance 3D Designer\python\sduserplugins\sd_mcp_plugin" "C:\PATH\TO\substance-designer-mcp\plugin"
```

### Step 2 — Install the Bridge Server

```bash
cd server/
uv sync   # installs mcp[cli] into .venv
```

Or with pip:
```bash
pip install "mcp[cli]>=1.26.0,<2.0.0"
```

### Step 3 — Configure Your MCP Client

**Claude Code** (`~/.claude/settings.json`):
```json
{
  "mcpServers": {
    "substance_designer": {
      "command": "C:\\Users\\YOUR_USERNAME\\.local\\bin\\uv.exe",
      "args": [
        "run",
        "--directory",
        "C:\\PATH\\TO\\substance-designer-mcp\\server",
        "python",
        "sd_mcp_bridge.py",
        "--port",
        "9881"
      ]
    }
  }
}
```

See `config/` folder for Claude Desktop and Cursor examples.

### Step 4 — Start SD before your AI client

**SD must be running before the MCP bridge starts.** The plugin binds TCP port 9881 on SD startup. If SD is not running when Claude/Cursor launches, the bridge will fail to connect.

---

## Usage

Once connected, you can ask your AI client to:

```
Build a weathered steel material with surface oxidation
```
```
Create a cliff heightmap with high detail and disorder
```
```
Make a new graph called MyRock, add a clouds node and a slope blur, connect them
```
```
List all available material recipes
```

### Available MCP Tools

| Tool | Description |
|------|-------------|
| `get_scene_info` | List all open packages and graphs |
| `create_package` | Create a new empty package |
| `create_graph` | Create a new compositing graph |
| `get_graph_info` | Get all nodes and connections in a graph |
| `list_node_definitions` | Search available node types |
| `create_node` | Create an atomic node (blend, levels, blur, etc.) |
| `create_instance_node` | Create a library node (Clouds, Cells, Perlin, etc.) |
| `create_output_node` | Create a PBR output (baseColor, normal, height, etc.) |
| `connect_nodes` | Connect two nodes via their ports (wrong port IDs fail with the valid IDs, closest match first) |
| `disconnect_nodes` | Remove a connection |
| `set_parameter` | Set a node parameter |
| `set_parameters` | Apply several parameter edits across nodes in one call (per-update status) |
| `get_node_info` | Get ports and parameters of a node |
| `delete_node` | Delete a node |
| `move_node` | Reposition a node |
| `arrange_nodes` | Grid-layout all nodes (destroys connections in SD 15) |
| `duplicate_node` | Duplicate an atomic node |
| `get_library_nodes` | Search library nodes and their `pkg://` URLs |
| `set_graph_output_size` | Set graph resolution |
| `delete_graph` | Delete a graph from a package |
| `open_graph` | Open a graph in the SD editor |
| `save_package` | Save the package to disk |
| `capture_3d_view` | Return the SD 3D view as an image (to judge the material visually) |
| `capture_2d_view` | Render a map or labeled output contact sheet in one call, or grab the SD 2D view |
| `capture_node_preview` | Grab each node's graph-editor tile with SD's live thumbnail (no `graph.compute()`), for one node or a labeled multi-node contact sheet |
| `get_map_stats` | Return per-channel min/max/mean and histogram from a computed texture's pixel buffer (histograms of >1M-pixel 16-bit/float maps are sample-counted, `histogram.sample_stride`) |
| `inspect_outputs` | Contact sheet + per-channel stats for several outputs from one graph compute (~28 s for 4 outputs on 8192² maps) |
| `send_to_3d_view` | Bind a graph's outputs to the 3D view material channels, without moving the mouse |
| `list_documentation` | Browse/search the embedded SD knowledge base (nodes, ports, blend modes) |
| `diagnostic` | Read-only health check: SD version, Qt binding, packages, current graph, security gates |
| `graph_snapshot` | Export a graph's nodes and connections as JSON |
| `start_long_job` / `wait_long_job` / `get_job_status` / `cancel_long_job` | Run heavy inspection ops (inspect_outputs, get_map_stats, capture_2d_view, capture_node_preview) as stage-tracked jobs with progress notifications and between-stage cancellation |
| `execute_sd_code` | Execute arbitrary Python in SD's context (**disabled by default** — see Security) |
| `reload_plugin` | Hot-reload the plugin's Python code from disk (development) |
| **`create_batch_graph`** | **Build a whole graph (nodes + connections) in one call** |
| **`build_material_graph`** | **Build a full PBR material in one call** |
| **`build_heightmap_graph`** | **Build a heightmap-only graph in one call** |
| **`list_recipes`** | **List all 79 available material recipes** |
| **`get_recipe_info`** | **Get details about a specific recipe** |
| **`apply_recipe`** | **Apply a recipe to an existing graph** |
| `start_material_run` | Start a versioned iteration run (goal, budget, preview preset) |
| `get_material_run` / `list_material_runs` | Read run state, revisions, candidates, evaluations and artifacts |
| `checkpoint_material_revision` | Save + verify an immutable package checkpoint as a revision |
| `verify_material_checkpoint` | Reload a checkpoint and compare its graph fingerprint |
| `restore_material_revision` | Restore the working package from a checkpointed revision |
| `propose_material_patch` | Validate a `set_parameter` / `set_output_size` patch without editing |
| `apply_material_patch` | Apply the patch, auto-checkpoint it, roll back on failure |
| `capture_material_preview` | Contact sheet + 3D render + stats for the current revision |
| `record_material_evaluation` | Record verdict/score; rejects stale previews, checks regressions |
| `decide_material_candidate` | Accept the applied revision or restore the pre-patch baseline |
| `finish_material_run` | Close the run and restore the best accepted revision |

### Material Runs (versioned iteration)

A material run turns free-form parameter edits into a reproducible loop. Each
revision is a full package copy saved next to a SQLite database
(`%LOCALAPPDATA%/sd_mcp/runs` by default; override with `--runs-dir` or
`SD_MCP_RUNS_DIR`), so every step can be verified and rolled back.

```
start_material_run(graph_identifier="MyMaterial", goal="deeper cavity")
checkpoint_material_revision()            # baseline; verified by reloading the copy
propose_material_patch(operations=[
    {"op": "set_parameter", "node_id": "Blur", "parameter_id": "intensity", "value": 12.0}])
apply_material_patch()                    # applies + auto-checkpoints the result
capture_material_preview()                # images + per-channel stats for review
record_material_evaluation(verdict="better", accepted=True)
decide_material_candidate()               # accept, or restore the base revision
finish_material_run()                     # close, restoring the best accepted revision
```

Invalid patches, graph drift (manual edits since the checkpoint), stale previews
and patch failures without a working rollback are rejected with explicit errors.
`restore_material_revision` can return to any recorded revision later.

**Hypothesis-driven refinement.** Each iteration should name one specific visual
defect, make a targeted change for it, and predict the effect. Pass that with the
patch; the evaluation then scores each prediction against the base and candidate
preview stats:

```
propose_material_patch(operations=[...], hypothesis={
    "defect": "roughness is flat; cavities and peaks read the same",
    "observed_in": ["roughness"],
    "rationale": "the blur washes out the cavity mask",
    "expected_effect": "cavities glossier, peaks stay matte",
    "predictions": [
        {"output": "roughness", "metric": "mean", "direction": "decrease",
         "min_change": 0.05, "max_change": 0.3},
        {"output": "baseColor", "direction": "unchanged", "tolerance": 0.02}]})
...
record_material_evaluation(hypothesis_outcome="confirmed",
                           observed_effect="cavities read wet now")
```

Each prediction is `confirmed`, `refuted`, `overshot` (moved past `max_change`)
or `untestable`; the hypothesis as a whole is `confirmed`, `partial`, `refuted`
or `inconclusive`, and an explicit `hypothesis_outcome` from the evaluator wins
over the measured one. A refuted hypothesis is not auto-accepted, re-proposing
operations that already failed is flagged, and `get_material_run` /
the finish report carry a `hypothesis_log` of every defect, change, prediction
and outcome. `start_material_run(require_hypothesis=True)` makes the hypothesis
mandatory.

**Material-aware evaluation.** Tell the run what the material is meant to look
like and changes are judged against that, not by their sign:

```
start_material_run(graph_identifier="MyStone", material_profile="rough_dielectric",
                   appearance_targets={"height": {"mean": [0.35, 0.6]}})
```

Profiles (`dielectric`, `metal`, `rough_dielectric`, `glossy_dielectric`) seed
loose PBR bands; `appearance_targets` adds or overrides per-output ranges
(`{"mean"|"min"|"max": [lo, hi], "channel": "r"}`, `null` for an open end).
Each change beyond the regression threshold is classified as `toward_target`,
`within_target`, `away_from_target`, `intended` (predicted by the hypothesis) or
`side_effect`. Only moving away from an intended range, or leaving it, fails;
unpredicted side effects are reported as warnings. Without targets or a
hypothesis the old sign-based check still applies.

**Controlled comparisons.** The run's first capture locks the comparison
context: captured outputs, sheet size, histogram bins, each map's resolution and
pixel format, the graph output size, and the 3D render size and timing. Later
captures report `comparison_control`; an evaluation whose before/after captures
differ (including a `set_output_size` patch) is flagged and not auto-accepted.
Captures of a revision with a parent also get a `comparison_sheet`: the before
and after contact sheets side by side at the same scale. SD's API does not
expose the 3D view's camera, lighting or tiling, so leave the 3D view alone
during a run; only its render size and timing can be verified.

**Semantic graph understanding.** `analyze_material_graph` walks upstream from
every output and labels nodes as `macro_shape`, `edge_breakup`, `surface_grain`,
`height_processing`, `normal_processing`, `color`, `roughness` or `metallic`,
with the outputs each node reaches, a reason, a confidence and its tunable
parameters. The roles are heuristics (node definition names plus scale/tiling
parameters), so treat low-confidence ones as guesses. A hypothesis can name a
`target_role`. Patch validation warns when an edited node doesn't feed the
outputs the hypothesis targets (`node_not_upstream`) or looks like a different
role (`role_mismatch`).

**Export and delivery validation.** `validate_material_delivery` exports the
current revision's outputs with `SDTexture.save` and re-reads the files. It
checks that every required output is present and non-empty, the file format,
the header dimensions and bit depth (PNG, TIFF and EXR headers are parsed), the
resolution, and square, uniform sizes. Start a run with `delivery={...}`
(`{}` for the defaults: six PBR outputs, PNG, at least 512 px, 8-bit with
16-bit height) and `finish_material_run` refuses until the final revision
passes. These need plugin v3.10.0 (`export_outputs`, `compose_comparison`).

Checkpoints and restores need plugin v3.9.0, so restart Substance Designer after
updating. The opt-in `tests/test_material_live.py` exercises the whole workflow
(including a real restore) against a scratch package; run it once after the
restart to confirm the live path on your SD build.

### Material Recipes

```python
build_material_graph(graph_name="MySteel", recipe_name="pro_steel")
# → 37-node PBR graph: height + normal + roughness + AO + baseColor + metallic

build_heightmap_graph(graph_name="CliffHM", style="cliff", detail_level=3, scale=5.0, disorder=0.5)
# → 9-node heightmap graph
```

**Pro recipes (37–44 nodes, professional architecture):**
`pro_granite`, `pro_limestone`, `pro_sandstone`, `pro_basalt`, `pro_slate`,
`pro_steel`, `pro_iron`, `pro_copper`,
`pro_concrete`, `pro_concrete_aged`, `pro_concrete_smooth`

**Core materials:**
`steel`, `iron`, `copper`, `gold`, `silver`, `aluminum`, `brass`,
`granite`, `marble`, `sandstone`, `limestone`, `slate`, `basalt`,
`wood`, `wood_oak`, `moss`, `bone`, `leather`, `fabric_cotton`, `fabric_silk`, `fabric_wool`, `fabric_denim`, `fabric_burlap`,
`sand`, `mud`, `gravel`, `clay`, `soil`,
`ice`, `snow`, `frost`, `water`,
`diamond`, `ruby`, `sapphire`, `emerald`, `amethyst`,
`concrete`, `brick`, `lava`, `asphalt`, `plaster`, `tile`, `painted_metal`, `carbon_fiber`, `terracotta`, `obsidian`, ...

**Heightmap styles:**
`cliff`, `rock`, `sand`, `cracked`, `mud`, `mountain`, `cobblestone`, `terrain`

### Batch graphs

`create_batch_graph` takes typed node and connection lists (the fields are documented in
the tool's input schema):

```python
create_batch_graph(
    graph_name="NoiseHeight",
    nodes=[
        {"id_alias": "noise", "library_keyword": "perlin_noise", "position": [0, 0]},
        {"id_alias": "blur", "definition_id": "sbs::compositing::blur", "position": [200, 0],
         "parameters": {"intensity": 4.0}},
        {"id_alias": "height", "definition_id": "sbs::compositing::output",
         "usage": "height", "label": "Height", "position": [400, 0]},
    ],
    connections=[
        {"from": "noise", "to": "blur"},  # from_output defaults to unique_filter_output
        {"from": "blur", "to": "height", "to_input": "inputNodeOutput"},
    ],
)
```

Node fields: `id_alias`, `definition_id` (atomic) or `resource_url` / `library_keyword`
(library), `position`, `usage` + `label` (output nodes), `parameters`
(`{"id": value}` or `{"id": {"value": ..., "type": "int2"}}`).
Connection fields: `from`, `to`, `from_output`, `to_input`.

Every connection is validated against the node's real ports. A wrong ID fails only that
connection, and the error lists the node's valid port IDs, closest match first:

```
Input port 'input1' not found on node '1584398761' (sbs::compositing::output).
Did you mean 'inputNodeOutput'? Valid input port IDs (closest first): ['inputNodeOutput'].
```

Error recovery:

```python
create_batch_graph(..., preflight=True)
# → {"preflight": true, "valid": false, "errors": [...], "warnings": [...]}
#   Validates definitions, library URLs/keywords, parameter value shapes and
#   atomic-node port IDs WITHOUT creating a graph (library node ports are only
#   known after instancing, so those ports are skipped).

create_batch_graph(..., cleanup_on_failure=True)
# → deletes the new graph when a node or connection fails and raises with the
#   report, leaving no half-built graph in your package. build_material_graph /
#   build_heightmap_graph default to cleanup_on_failure=True.
```

---

## Security

The plugin accepts commands on a localhost TCP port and the bridge runs as an MCP stdio
server on the same machine. Two opt-ins harden that:

- **`execute_sd_code` is disabled by default.** Starting the bridge with
  `--allow-arbitrary-code` (or `SD_MCP_ALLOW_EXECUTE_CODE=1` in its environment) arms the
  MCP tool, but the plugin still refuses to run code unless SD's environment has
  `SD_MCP_ALLOW_EXECUTE_CODE=1` or an `allow_execute_code` marker file exists in the plugin
  folder. Both sides must agree. `diagnostic` reports the current state
  (`execute_code_enabled`).
- **`reload_plugin` is disabled by default.** It re-executes plugin Python from disk;
  arm it with `--allow-dev-tools` (or `SD_MCP_ALLOW_DEV_TOOLS=1` for the bridge) plus
  `SD_MCP_ALLOW_DEV_TOOLS=1` or the execute-code opt-in on the plugin side.
- **Shared token:** start the bridge with `--token SECRET` and set `SD_MCP_TOKEN=SECRET`
  in SD's environment. Every command then carries the token and unidentified localhost
  callers are refused before dispatch (`token_auth_required` shows in `diagnostic`).
  The plugin compares tokens in constant time. Without the env var set, the port stays
  localhost-trust as before.

`diagnostic` and `graph_snapshot` are read-only tools added for exactly this kind of
health/inspection work.

### Timeouts

Socket waits are layered so a live plugin request is never abandoned by the client
first:

- `--timeout` (default 150 s) applies to ordinary commands;
  `--heavy-timeout` (default 300 s) applies to builds, computes, captures,
  `execute_code`, `save_package` and the material-run checkpoint/restore/
  fingerprint commands.
- `--runs-dir` (or `SD_MCP_RUNS_DIR`) selects where material runs store their
  SQLite database and revision checkpoints (default
  `%LOCALAPPDATA%\sd_mcp\runs`).
- Each command carries its timeout to the plugin, which waits slightly less
  (`SD_MCP_COMMAND_TIMEOUT`, default 120, capped at 600 by
  `SD_MCP_COMMAND_TIMEOUT_MAX`) and cancels queued work when it expires.
- Commands are idempotent via a `request_id`, so bridge reconnect retries replay
  the plugin's cached response instead of running the command twice.
- The plugin serves each client on its own thread, so `job_status` /
  `job_cancel` respond even while another request's `sd.api` call blocks SD's
  main thread.

### Long jobs

Heavy inspection runs a `graph.compute()` that blocks SD's main thread for its whole
duration and can exceed the bridge's heavy timeout on huge graphs. The blocking tools
stay as-is; for very large graphs run the staged job variant instead:

```python
start_long_job(job_type="inspect_outputs", params={"graph_identifier": "Steel", "output_usages": ["height", "normal"]})
# → {"job_id": 4, "started": true}
wait_long_job(job_id=4)          # polls until done, emits MCP progress notifications per stage; returns the result
cancel_long_job(job_id=4)        # stops between stages if needed
```

Stages run on SD's main thread (SD's constraint), so a single `compute()` stage cannot be
interrupted mid-flight, but cancellation takes effect immediately after it, and status
polls report stages (`compute → read_output → render_sheet → done`).

---

## Critical Rules (SD 15.x)

These are hard constraints of the SD 15.x API — violating them hangs or crashes SD:

- **ONE tool call at a time** — never parallel calls to SD tools
- `SDUsage.sNew()` **hangs SD 15 permanently** — removed from plugin
- `newNode(unknown_definition)` **hangs SD 15 permanently** — plugin validates before calling
- `arrange_nodes()` **destroys all connections** — use `move_node()` instead
- Library node outputs are **never** `"unique_filter_output"` — always call `get_node_info` first
- `directionalwarp` warp-map port = `"inputintensity"` (NOT `inputgradient`)
- Wrong port ID in `connect_nodes` = crash — plugin validates before calling SD

---

## Known Library Node Ports (SD 15.0.3)

| Node | Output | Key Inputs |
|------|--------|-----------|
| `clouds_2` | `output` | `scale`, `disorder` |
| `cells_1` | `output` | `scale`, `disorder` |
| `perlin_noise` | `output` | `scale`, `disorder` |
| `polygon_2` | `output` | `Sides`, `Scale`, `Gradient` |
| `gradient_linear_1` | `Simple_Gradient` | `Tiling`, `rotation` |
| `gradient_axial` | `output` | `point_1`, `point_2` |
| `slope_blur_grayscale_2` | `Slope_Blur` | `Samples`, `Source`, `Effect` |
| `blur_hq_grayscale` | `Blur_HQ` | `Intensity`, `Source` |
| `non_uniform_blur_grayscale` | `Non_Uniform_Blur` | `Intensity`, `Anisotropy`, `Source` |
| `edge_detect` | `output` | `edge_width`, `tolerance`, `input` |
| `flood_fill` | `output` | `mask` |
| `flood_fill_to_gradient_2` | `output` | `angle`, `input`, `angle_input` |
| `flood_fill_to_grayscale` | `output` | `luminance_random`, `input` |
| `multi_directional_warp_grayscale` | `output` | `intensity`, `directions`, `input`, `intensity_input` |
| `highpass_grayscale` | `Highpass` | `Radius`, `Source` |
| `histogram_scan` | `Output` | `Position`, `Contrast`, `Input_1` |
| `invert_grayscale` | `Invert_Grayscale` | `Source` |
| `crystal_1` | `output` | `scale`, `disorder` |

---

## Troubleshooting

**Bridge can't connect to SD:**
- Make sure SD is running and fully loaded before launching your AI client
- Check SD logs: `%LOCALAPPDATA%\Adobe\Adobe Substance 3D Designer\log.txt` (look for `[SD-MCP]`)
- Verify the plugin is in the correct `sduserplugins` directory

**"Unauthorized: token missing or incorrect":**
- The bridge was started with `--token` but SD's `SD_MCP_TOKEN` differs (or the reverse).
  Both sides must share the same value; restart accordingly.

**SD hangs after a command:**
- A bad node definition was likely used. Restart SD.
- The plugin validates node definitions before calling SD, but library nodes need correct `pkg://` URLs.

**Wrong port:**
- Plugin listens on **9881** by default. Bridge must use `--port 9881`.
- Both sides also read `SD_MCP_PORT` (the plugin accepts a comma-separated list);
  `--port` overrides it on the bridge side.

**Bridge times out while SD is still working:**
- The plugin budget is derived from the command's timeout; raise `--timeout` /
  `--heavy-timeout` on the bridge and `SD_MCP_COMMAND_TIMEOUT` /
  `SD_MCP_COMMAND_TIMEOUT_MAX` in SD's environment together, keeping the
  plugin's value lower than the bridge's.

**Plugin not loading:**
- Delete `__pycache__/` in the plugin directory and restart SD.

**Plugin edits not picked up:**
- Call `reload_plugin` (reloads recipes, docs and command handlers). Changes to the TCP
  server or main-thread dispatcher still need an SD restart; bridge changes need a restart
  of your AI client.

---

## Development

Tests run the bridge in-process against a fake SD plugin (length-prefixed JSON over TCP),
so they need no Substance Designer:

```bash
cd server/
uv run pytest
```

Tests marked `live` talk to a real SD on `localhost:9881` and are skipped automatically
when that port is closed (they are read-only). Run only those with `uv run pytest -m live`.

---

## License

MIT License — see [LICENSE](LICENSE)

---

## Author

Built by **[matthieuhuguet](https://github.com/matthieuhuguet)** / **[ZenRay](https://github.com/ZenRay)** — first MCP integration for Substance Designer.

Pro recipe architecture based on deep analysis of professional Substance Designer node graph patterns: clouds_2 → slope_blur → edge_detect → flood_fill → multi_directional_warp → directionalwarp chains.

---

## Contributing

PRs welcome. Key areas for improvement:
- More material recipes
- Color graph support (currently height/grayscale focused)
- Windows + macOS path handling for plugin install
- Auto-install script for the SD plugin
