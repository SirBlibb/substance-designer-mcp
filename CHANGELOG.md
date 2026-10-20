# Changelog

All notable changes to the Substance Designer MCP Plugin are documented here.

---

## [Unreleased]

### Bridge (v2.5.0)
- **Material runs**: a persistent, versioned iteration workflow built on the
  existing tools. Runs, revisions, candidates, evaluations and artifacts live in
  SQLite under `%LOCALAPPDATA%/sd_mcp/runs` (override with `--runs-dir` or
  `SD_MCP_RUNS_DIR`). Every revision is an immutable package copy plus a graph
  fingerprint, so a run can return to any recorded state.
  - `start_material_run`, `get_material_run`, `list_material_runs`
  - `checkpoint_material_revision` (baseline/extra checkpoints),
    `verify_material_checkpoint`, `restore_material_revision`
  - `propose_material_patch` (validated `set_parameter` / `set_output_size`
    operations), `apply_material_patch` (re-validates, refuses on drift,
    auto-checkpoints the applied state and rolls back on failure)
  - `capture_material_preview` (multi-output contact sheet + 3D render and
    per-channel stats in one call; returned as images when requested)
  - `record_material_evaluation` (rejects stale/missing previews, compares
    against the base revision, technical checks + regression threshold),
    `decide_material_candidate`, `finish_material_run` (restores the best
    accepted revision)
  - The controller is Qt-free, serialises every SD operation on one lock and
    resumes runs after a bridge restart. Restores re-check the checkpoint
    file's recorded sha256 before loading it, so a modified or corrupted
    checkpoint is refused instead of silently restoring.
- Added `--runs-dir` for the material-run database and checkpoints.
- `checkpoint_package`, `restore_package`, `verify_checkpoint` and
  `graph_fingerprint` use the heavy timeout profile.

### Bridge (v2.4.0)
- New `capture_node_preview` tool: captures each node's graph-editor tile with
  SD's live thumbnail for one node or a labeled contact sheet of `node_ids`
  (max 32). Unlike `capture_2d_view(node_id=...)`, it does not call
  `graph.compute()`, so it is fast and works on nodes that are not connected to
  a graph output. Each node is focused briefly (`SDUIMgr.focusGraphNode`), fitted
  into view, and its rectangle (`getGraphNodeBBox`) is cropped from the graph
  view; the original zoom/scroll position is restored afterward. SD 15 renders
  node previews in the scene, so this tile crop is the normal path, with a
  graph-owned preview widget used when present. Also available as a staged long
  job (`start_long_job(job_type="capture_node_preview")`)
- **Security**: `execute_sd_code` is disabled by default. Restart the bridge with
  `--allow-arbitrary-code` (or `SD_MCP_ALLOW_EXECUTE_CODE=1`) to arm it; the plugin has
  its own independent gate (`SD_MCP_ALLOW_EXECUTE_CODE=1` in SD's environment or an
  `allow_execute_code` marker file in the plugin folder), so both sides must agree.
  `--token SECRET` sends a shared secret with every command; the plugin refuses
  unauthorized commands while it runs with `SD_MCP_TOKEN` set
- **Typed tool results**: create/get graph, create node, parameter updates
  (`set_parameter(s)`), batch + recipe builds (`create_batch_graph`,
  `build_material_graph`, `build_heightmap_graph`, `apply_recipe`) and `send_to_3d_view`
  now return MCP structured content validated against an output schema alongside the
  unchanged readable JSON text
- **Batch recovery**: `create_batch_graph(preflight=True)` validates definitions, library
  URLs/keywords, parameter shapes and atomic port IDs without creating anything;
  `cleanup_on_failure=True` deletes the new graph and raises with the report when the
  build fails (recipe builders default to `cleanup_on_failure=True`)
- **Long jobs**: `start_long_job` / `wait_long_job` / `get_job_status` / `cancel_long_job`
  run `inspect_outputs`, `get_map_stats`, `capture_2d_view` and `capture_node_preview`
  as stage-tracked jobs on the plugin; `wait_long_job` emits MCP progress notifications per stage and returns the
  usual tool result
- New `diagnostic` and `graph_snapshot` tools: the plugin's health check and full
  node/connection export were implemented but not registered as MCP tools
- **Layered timeouts**: `--timeout` (default 150 s) and `--heavy-timeout`
  (default 300 s) replace the single socket wait; heavy tools (batch/recipe
  builds, captures, `execute_code`, `save_package`) get the longer one. Each
  command carries its `timeout` to the plugin, which stops its main-thread wait
  just before it, so clients never time out on a live request
- **Idempotent commands**: every command carries a `request_id` (stable across
  the bridge's reconnect retries); the plugin replays the cached response for a
  duplicate id instead of executing twice
- `reload_plugin` is disabled by default like `execute_sd_code`; arm it with
  `--allow-dev-tools` (bridge) plus `SD_MCP_ALLOW_DEV_TOOLS=1` or the
  execute-code opt-in (plugin)
- `SD_MCP_PORT` (and `--port`) now seeds the default plugin port
- Version alignment: `server/pyproject.toml` was still `0.1.0` while the bridge
  logged v2.4.0; both are now `2.4.0`, with `BRIDGE_VERSION` as the single
  source of truth in the bridge code

### Plugin (v3.9.0)
- Material-run checkpoint commands:
  - `checkpoint_package` saves an immutable package copy with
    `SDPackageMgr.saveCopyOfPackageAs` (the working package path is unchanged),
    creating parent directories; explains the save-first requirement for
    never-saved packages.
  - `verify_checkpoint` loads a checkpoint package, fingerprints the graph
    inside it and unloads it again (skipping both when already loaded).
  - `restore_package` unloads the working package, loads the checkpoint and
    optionally saves it back over the working path; refuses to use the working
    path or an already-loaded checkpoint as the source.
  - `graph_fingerprint` returns a canonical JSON description of a graph: nodes
    (ids, definitions, positions, output role, inputs with serialized values and
    connection flags, annotations), the full connection list, graph inputs and
    output node ids - used for drift detection and restore verification.
- `PLUGIN_VERSION` is now 3.9.0; the bridge logs the loaded plugin version.
- Package lookups by path normalise case and separators: SD reports stored
  paths with forward slashes (`C:/...`) while callers may pass Windows-style
  paths, which previously made checkpointing a saved package fail.

### Plugin (v3.8.0)
- `capture_node_preview` command: grabs each node's graph-editor tile (which
  contains SD's live thumbnail) without a `graph.compute()`, for one node or a
  labeled multi-node contact sheet. SD 15 draws previews in the graph scene, so
  the node is briefly fitted into view and its rectangle is cropped; the
  original zoom/scroll position is restored afterward. A graph-owned preview
  widget is used when present. Registered as a staged long job
  (`start_job(op="capture_node_preview")`)
- Fixed the direct (synchronous) `capture_2d_view` / `get_map_stats` /
  `inspect_outputs` paths raising `KeyError: 'done'` before running; `_drive_sync`
  now initializes the job flag. Added plugin handler regression tests with a fake
  `sd` package (direct jobs, preflight failures, fail-closed validation)
- `create_batch_graph(preflight=True)` no longer raises `IndexError` for an
  unknown `library_keyword`; library lookup failures produce one
  `library_not_found` error
- Node and port validation fails closed: `create_node`, batch/recipe builds and
  `_safe_connect` refuse to call SD when definitions or port lists cannot be
  introspected, instead of risking an SD 15 hang with an unknown definition
- `set_parameters` command: applies several parameter updates across nodes in one
  request, resolving the graph and each node once; per-update status, so one bad
  property does not abort the batch (`set_parameter` now shares the same core)
- `inspect_outputs` command: one `graph.compute()` for both a labeled preview
  contact sheet and per-channel statistics (min/max/mean/histogram) of several
  outputs (default: height, normal, ambientOcclusion, baseColor); unmatched or
  unreadable outputs are reported under `skipped`
- `get_map_stats` / `inspect_outputs` share one texture-stats reader, and the
  contact-sheet renderer is shared with `capture_2d_view`; fixes a NameError in
  the row-pitch check (`row_size` was used before assignment)
- Fast stats in the new pure-Python `texture_stats` module (unit-tested outside
  SD): 8-bit formats run min/max/sum over byte slices and histogram via
  `bytes.translate` + `bytes.count`; 16-bit/float formats unpack once per row;
  ~10x faster on 2048² maps, ~20x on 8192² (a 4-output inspect of 8192² PBR
  maps went from ~8 min to ~28 s)
- Histograms of 16-bit/float maps larger than ~1M pixels are counted from
  evenly strided samples (new `histogram.sample_stride` field; min/max/mean/
  count stay exact) so reading multi-megapixel outputs stays far below the
  bridge timeout
- `set_parameter` / `set_parameters` pick the input vs annotation setter by
  property category and coerce the value type to the property's actual SD type;
  previously an annotation-only property (e.g. `label`) died in SD 15's silent
  execution crash
- **Per-client workers**: the TCP server accepts and serves each client on its
  own thread (max 16, then a `busy` error) instead of serializing on one accept
  loop, so `job_status` / `job_cancel` stay responsive while an earlier client's
  `sd.api` compute blocks the main thread
- **Command timeouts**: the plugin derives its main-thread wait from the
  command's `timeout` (`_command_budget`, capped by `SD_MCP_COMMAND_TIMEOUT`,
  max `SD_MCP_COMMAND_TIMEOUT_MAX`); a timed-out command is cancelled before it
  can run, so queued work does not execute after the caller gave up. Response
  sends reset the socket timeout instead of inheriting the receive timeout
- **Idempotency cache**: duplicate `request_id`s replay the cached response
  (last 128), including in-flight waiters, so bridge retries cannot execute a
  command twice
- **Security**: token comparison uses `hmac.compare_digest` on UTF-8 bytes
  (constant-time, non-ASCII safe); `reload_plugin` is gated by
  `SD_MCP_ALLOW_DEV_TOOLS=1` or the execute-code opt-in; `SD_MCP_PORT` accepts a
  comma-separated port list
- Non-finite floats in results (NaN/±Inf) are replaced with `null` and reported
  as `non_finite_values` flags instead of breaking JSON, with explicit markers
  in the encoded text
- `_resolve_lib_url` prefers exact identifier matches over substrings, dedupes
  by URL, and raises a listing error when a partial name is ambiguous

### Plugin (v3.6.0)
- `capture_2d_view` command: renders one node's output map to PNG through the SD
  API (`graph.compute()` + output-texture read + Shiboken QImage conversion) -
  e.g. the Height output via `output_usage="height"`, or any mid-graph node via
  `node_id`; exact pixels at native resolution (optional `max_size` downscale),
  no UI interaction. Without a node/usage it grabs the visible 2D view widget
  (selection preview, tiling, zoom, checkerboard)
- Contact sheets render several labeled outputs in one capture call and one graph compute.
- `get_map_stats` reads the SDTexture pixel buffer and returns per-channel min/max/mean,
  finite and non-finite counts, and a configurable histogram. Integer formats are
  normalized to 0..1; float values retain their native range.

### Plugin (v3.4.0)
- Invalid port IDs in `connect_nodes` / batch connections now fail with the node's valid
  connectable port IDs, closest match first (`port_suggest.py`); input IDs are matched
  case-insensitively; unknown batch aliases list the known aliases
- `reload_plugin` command: hot-reloads recipes, documentation and the command handlers
  without restarting SD (keeps the TCP server and Qt invoker)
- `send_to_3d_view` drives SD's "View In 3D View" menus with synthetic Qt mouse events
  instead of OS-level input: the cursor no longer moves and SD is not raised. Progress
  is reported by the new `send_to_3d_view_status` command; a failing step marks that
  channel failed instead of stalling the job
- Brick recipes (`brick_red`, `brick_old`, `brick_white`) build their shape from a
  `brick_generator` instance (4x8 running bond, bevelled) instead of a stretched
  `polygon_2`, which gave a few oversized tiles; mortar levels retuned and params the
  SD 15 library nodes don't expose (`Tiling`, `non_square_expansion`, clouds_2
  `scale`/`disorder`) removed, so they no longer show up as skipped
- Library packages from `<SD install>/resources/packages` load on demand
  (`brick_generator.sbs` and `tile_generator.sbs` added; `pkg:///<name>` without a
  `?dependency=` suffix loads `<name>.sbs` directly). After each command the plugin
  unloads the user-package copies again, so the ones a graph instances stay loaded only
  as its dependencies, the way SD's own library loads them, and no longer show as user packages

### Bridge
- New `set_parameters` tool (v2.3.0): batch parameter edits, typed update list,
  per-update ok/error results
- New `inspect_outputs` tool (v2.3.0): returns contact sheet + per-output channel
  statistics from a single graph compute, as JSON text plus the image
- `capture_2d_view` supports labeled output contact sheets in one tool call
- New `get_map_stats` tool returns numeric per-channel output-map diagnostics
- New `capture_2d_view` tool (v2.1.0): renders an individual map (e.g. the Height
  output) straight from the graph as an image, or grabs the 2D view widget
- `create_batch_graph` nodes/connections and recipe `overrides` are typed (Pydantic), so
  the MCP input schema documents every field; the wire format to the plugin is unchanged
- New `reload_plugin` tool
- New `send_to_3d_view` tool: binds a graph's outputs to the 3D view and waits for the result
- Pytest suite (`cd server && uv run pytest`) against a fake SD server; `live` tests run
  only when SD listens on 9881. Replaces the old hardcoded-path test scripts

---

## [1.0.0] — 2026-02-19 — First Public Release

### Plugin (v3.1.0)
- Complete MCP integration for Adobe Substance 3D Designer 15.x
- 16 MCP tools (11 core + 5 recipe/builder tools)
- Thread-safe Qt dispatch via Signal/Slot queued connection
- PySide6 path injection (SD ships its own PySide6, not in sys.path)
- Length-prefix TCP framing protocol (4-byte big-endian + JSON)
- Fresh-socket-per-command connection model (no stale state)

### Recipe System (v5.0 — 79 recipes)
- **Pro architecture family**: pro_granite, pro_limestone, pro_sandstone, pro_basalt, pro_slate, pro_steel, pro_iron, pro_copper, pro_concrete, pro_concrete_aged, pro_concrete_smooth (37–44 nodes each)
- **Core materials**: wood×5, rock×6, metal×7, organic×9, soil×5, water_ice×4, gems×5
- **Specialty**: concrete, brick, lava, asphalt, plaster, fabric, tile, specialty
- **Heightmap styles** (8): cliff, rock, sand, cracked, mud, mountain, cobblestone, terrain
- **MainShape** reconstruction (pro architecture, 11 nodes from live data)
- Pro node chain: clouds_2 → slope_blur×2 → edge_detect → flood_fill → flood_fill_to_gradient_2 → multi_directional_warp×2 → directionalwarp×3 → highpass → histogram_scan
- All 79 recipes validated: 0 port failures, 0 crashes

### Bridge Server (v2.0.0)
- BUG-B01: `_send_lock` no longer held across retry loop — prevents 360-second deadlock
- BUG-B03: Default port corrected to 9881 (was 9880)
- BUG-B04: `directionalwarp` warp-map port corrected to `inputintensity` (was `inputgradient`)
- BUG-B05: FastMCP lifespan set at constructor, not post-construction
- BUG-B06: `ctx: Context` kept for FastMCP 1.4.1+ compatibility
- BUG-B07: `None` results return `"{}"` not `"null"`; NaN/Inf handled via `_json_safe`
- BUG-B08: Retry logic applies to connection failures only, not SD operation timeouts
- Async serialization via `threading.Lock` + `asyncio.to_thread`

### Critical Bug Fixes (SD 15.x)
- `SDValueInt2/3/4` on float params crashes SD silently → `_infer_type` always returns float2/3/4 for lists
- `SDValueFloat` on enum/int SD properties crashes SD silently → `_coerce_type()` reads actual SD type
- `_set_node_params` reads property type via `getType().getId()` and coerces accordingly
- `build_heightmap_graph`: builders return dict, not tuple — fixed unpacking
- `SDPackage.deleteResource()` does not exist — correct API is `resource.delete()`

### Confirmed SD 15.0.3 API Facts
- Library node outputs ≠ `"unique_filter_output"` (each has its own ID)
- `SDUsage.sNew()` hangs SD 15 permanently — removed
- `newNode(unknown_def)` hangs SD 15 permanently — validated before call
- `arrange_nodes()` destroys all connections — use `move_node()` instead
- `directionalwarp` warp-map input = `inputintensity` (NOT `inputgradient`)

---

## Internal Versions (not publicly released)

- **v3.1.0** (2026-02-18): SD crash fixes, build_heightmap bug fix, SD API notes
- **v3.0.0** (2026-02-15): Full recipe engine, smart builder, 34 initial materials
- **v2.x.x** (2026-02-10): PySide6 injection, Signal/Slot dispatcher
- **v1.x.x** (2026-02-01): Initial prototype
