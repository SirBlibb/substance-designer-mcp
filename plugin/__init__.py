"""
Substance Designer MCP Plugin v3.4.0
=====================================
ARCHITECTURE
============
LLM -> stdio -> sd_mcp_bridge.py (FastMCP) -> TCP -> this plugin -> sd.api
PORT: 9881 (all clients: Claude Code, Claude Desktop, Cursor)
PROTOCOL: Length-prefix framing [4-byte big-endian length][JSON payload]
CONNECTION MODEL: Fresh TCP socket per command (no persistent connections)
THREADING: ALL sd.api calls on Qt main thread via Signal/Slot queued dispatch

═══════════════════════════════════════════════════════════════════════════════
NEW IN v3.0.0
═══════════════════════════════════════════════════════════════════════════════
  - RECIPE ENGINE: 34 complete PBR material recipes built-in
  - SMART GRAPH BUILDER: validates ALL nodes/ports before touching SD
  - ATOMIC PORT REGISTRY: no more wrong port names crashing SD
  - LIBRARY NODE CACHE: caches discovered pkg:// URLs per session
  - BATCH GRAPH with library node support (resolves URLs internally)
  - HEIGHTMAP BUILDER: specialized tool for cliff/rock/terrain heightmaps
  - MATERIAL BUILDER: one-call full PBR graph (diffuse+normal+rough+metal+AO)
  - WARP CHAIN BUILDER: complex warp networks in one call
  - SUB-GRAPH MERGE: create partial graphs then merge them
  - COLOR PALETTE TOOL: set baseColor with specific HSL values
  - DIAGNOSTIC TOOL: full scene health check
  - GRAPH SNAPSHOT: export node list + connections as JSON
  - RECIPE PREVIEW: list available recipes with descriptions

═══════════════════════════════════════════════════════════════════════════════
CRITICAL RULES - DO NOT VIOLATE
═══════════════════════════════════════════════════════════════════════════════
  - ONE CALL AT A TIME. NEVER parallel. NEVER batch from outside.
  - newNode(unknown_def) HANGS SD 15 permanently
  - SDUsage.sNew() HANGS SD 15 permanently
  - arrange_nodes() DESTROYS ALL connections
  - Library node outputs are NEVER "unique_filter_output"
  - directionalwarp warp map port = "inputintensity" (NOT inputgradient)
  - connect_nodes with wrong port = crash. Plugin validates before calling SD.
═══════════════════════════════════════════════════════════════════════════════

SD API NOTES (confirmed from live SD 15.0.3 introspection)
===========================================================
  SDPackage has NO deleteResource() — use resource.delete() on the SDResource itself
  SDPackage methods: findResourceFromUrl, getChildrenResources, getFilePath, isModified
  SDResource.delete() — removes graph from package (confirmed working, 156 graphs deleted)

VERSION HISTORY
===============
  1.x.x - Initial releases
  2.x.x - PySide6 injection, Signal/Slot dispatcher, recipe stubs
  3.0.0 - Full recipe engine, 34 materials, smart builder, all tools
  3.1.0 - SD crash fixes (SDValueInt2 on float, SDValueFloat on enum), build_heightmap bug fix
  3.2.x - Reporting bug fix (library node definition field), recipes v5.0 (78 recipes, JP architecture)
  3.3.0 - list_documentation tool: embedded knowledge base for all SD nodes, ports, params, workflows
  3.4.0 - Port errors list valid port IDs (closest first); reload_plugin hot reload
"""

import sd
import os
import re
import json
import struct
import socket
import select
import threading
import traceback
import sys
import io
import time
import queue
import importlib as _importlib
from contextlib import redirect_stdout, redirect_stderr

# ── SD API imports ───────────────────────────────────────────────────────────
from sd.api.sdproperty import SDPropertyCategory
from sd.api.sdbasetypes import float2, float3, float4, ColorRGBA, int2, int3, int4
from sd.api.sdvaluefloat   import SDValueFloat
from sd.api.sdvalueint     import SDValueInt
from sd.api.sdvalueint2    import SDValueInt2
from sd.api.sdvalueint3    import SDValueInt3
from sd.api.sdvalueint4    import SDValueInt4
from sd.api.sdvaluebool    import SDValueBool
from sd.api.sdvaluestring  import SDValueString
from sd.api.sdvaluefloat2  import SDValueFloat2
from sd.api.sdvaluefloat3  import SDValueFloat3
from sd.api.sdvaluefloat4  import SDValueFloat4
from sd.api.sdvaluecolorrgba import SDValueColorRGBA
from sd.api.sbs.sdsbscompgraph import SDSBSCompGraph

# ── Configuration ────────────────────────────────────────────────────────────
DEFAULT_PORTS     = [9881]
PLUGIN_VERSION    = (3, 4, 0)
HEADER_SIZE       = 4
COMMAND_TIMEOUT   = 120
CLIENT_TIMEOUT    = 130
REQUEST_TIMEOUT   = 10    # connected clients send their request at once; never let one stall the serial accept loop
ACCEPT_BACKLOG    = 5
MAX_MSG_SIZE      = 100 * 1024 * 1024   # 100 MB

# System properties not valid as connection targets
_SYSTEM_PARAMS = frozenset({
    "$outputsize", "$format", "$pixelsize", "$pixelratio",
    "$tiling", "$randomseed", "$time"
})

# ── Known atomic node port registry ─────────────────────────────────────────
# Source: Adobe SD docs + empirical testing. Prevents wrong-port crashes.
ATOMIC_PORTS = {
    "sbs::compositing::blend": {
        "inputs":  ["source", "destination", "opacity"],
        "outputs": ["unique_filter_output"],
    },
    "sbs::compositing::levels": {
        "inputs":  ["input1"],
        "outputs": ["unique_filter_output"],
    },
    "sbs::compositing::curve": {
        "inputs":  ["input1"],
        "outputs": ["unique_filter_output"],
    },
    "sbs::compositing::hsl": {
        "inputs":  ["input1"],
        "outputs": ["unique_filter_output"],
    },
    "sbs::compositing::blur": {
        "inputs":  ["input1"],
        "outputs": ["unique_filter_output"],
    },
    "sbs::compositing::sharpen": {
        "inputs":  ["input1"],
        "outputs": ["unique_filter_output"],
    },
    "sbs::compositing::warp": {
        "inputs":  ["input1", "inputgradient"],
        "outputs": ["unique_filter_output"],
    },
    "sbs::compositing::directionalwarp": {
        "inputs":  ["input1", "inputintensity"],   # NOT inputgradient !
        "outputs": ["unique_filter_output"],
    },
    "sbs::compositing::normal": {
        "inputs":  ["input1"],
        "outputs": ["unique_filter_output"],
    },
    "sbs::compositing::transformation": {
        "inputs":  ["input1"],
        "outputs": ["unique_filter_output"],
    },
    "sbs::compositing::distance": {
        "inputs":  ["input1"],
        "outputs": ["unique_filter_output"],
    },
    "sbs::compositing::grayscaleconversion": {
        "inputs":  ["input1"],
        "outputs": ["unique_filter_output"],
    },
    "sbs::compositing::shuffle": {
        "inputs":  ["input1"],
        "outputs": ["unique_filter_output"],
    },
    "sbs::compositing::emboss": {
        "inputs":  ["input1"],
        "outputs": ["unique_filter_output"],
    },
    "sbs::compositing::passthrough": {
        "inputs":  ["input1"],
        "outputs": ["unique_filter_output"],
    },
    "sbs::compositing::uniform": {
        "inputs":  [],
        "outputs": ["unique_filter_output"],
    },
    "sbs::compositing::output": {
        "inputs":  ["inputNodeOutput"],
        "outputs": [],
    },
    "sbs::compositing::input_color": {
        "inputs":  [],
        "outputs": ["unique_filter_output"],
    },
    "sbs::compositing::input_grayscale": {
        "inputs":  [],
        "outputs": ["unique_filter_output"],
    },
    "sbs::compositing::gradient": {
        "inputs":  ["input1", "gradient"],
        "outputs": ["unique_filter_output"],
    },
    "sbs::compositing::pixelprocessor": {
        "inputs":  ["input1"],
        "outputs": ["unique_filter_output"],
    },
    "sbs::compositing::fxmaps": {
        "inputs":  ["input1"],
        "outputs": ["unique_filter_output"],
    },
}

# ── Blend mode constants ─────────────────────────────────────────────────────
BLEND_MODES = {
    "copy": 0, "normal": 0,
    "add": 1, "linear_dodge": 1,
    "subtract": 2,
    "multiply": 3,
    "max": 4, "lighten": 4,
    "min": 5, "darken": 5,
    "overlay": 9,
    "screen": 10,
    "soft_light": 11,
    "hard_light": 12,
    "divide": 13,
    "difference": 14,
}

# ── Globals ──────────────────────────────────────────────────────────────────
_server = None
# Submodules reloaded by the reload_plugin command (pure Python, no SD state).
_RELOADABLE_SUBMODULES = ("recipes", "sd_documentation", "port_suggest")
_library_cache = {}   # filter_text -> list of {identifier, url, package}

# ── Logging ──────────────────────────────────────────────────────────────────
def _log(msg):
    print("[SD-MCP] {}".format(msg))


# ── PySide6 path injection ───────────────────────────────────────────────────
def _inject_sd_pyside_path():
    candidates = []
    try:
        import psutil
        for proc in psutil.process_iter(['name', 'exe']):
            name = (proc.info.get('name') or '').lower()
            if 'substance' in name and 'designer' in name:
                exe = proc.info.get('exe') or ''
                if exe:
                    candidates.append(os.path.dirname(exe))
    except Exception:
        pass

    try:
        exe_dir = os.path.dirname(sys.executable)
        for _ in range(4):
            site_pkg = os.path.join(exe_dir, 'Lib', 'site-packages')
            if os.path.isdir(os.path.join(site_pkg, 'PySide6')):
                candidates.append(exe_dir)
                break
            exe_dir = os.path.dirname(exe_dir)
    except Exception:
        pass

    try:
        for drive in ('C:', 'D:', 'E:', 'F:'):
            for base in (
                drive + '\\Program Files\\Adobe',
                drive + '\\Program Files (x86)\\Adobe',
                drive + '\\Create\\Build\\DCC\\SubstanceDesigner',
            ):
                if not os.path.isdir(base):
                    continue
                for item in os.listdir(base):
                    if 'substance' in item.lower() and 'designer' in item.lower():
                        candidates.append(os.path.join(base, item))
    except Exception:
        pass

    for sd_root in candidates:
        site_pkg = os.path.join(sd_root, 'plugins', 'pythonsdk', 'Lib', 'site-packages')
        if os.path.isdir(os.path.join(site_pkg, 'PySide6')):
            if site_pkg not in sys.path:
                sys.path.insert(0, site_pkg)
            return site_pkg
    return None


_pyside_path = _inject_sd_pyside_path()


# ── SD library package auto-loading ─────────────────────────────────────────
# SD ships its library node sources as loose .sbs packages under
# '<SD install>/resources/packages'. They are not loaded into the package
# manager until something instantiates them, so recipe pkg:/// URLs fail on a
# fresh session (and their ?dependency= hashes drift between SD versions).
# We load the packages the recipes need once, then resolve by identifier.
_LIBRARY_SOURCE_FILES = (
    "perlin_noise_1.sbs",
    "cells_1.sbs", "cells_2.sbs", "cells_4.sbs",
    "clouds_1.sbs", "clouds_2.sbs",
    "crystal_1.sbs", "polygon_2.sbs",
    "gradient_linear_1.sbs", "gradient_axial.sbs",
    "blur_hq.sbs", "slope_blur.sbs", "non_uniform_blur.sbs",
    "multi_directional_warp.sbs", "invert.sbs", "highpass.sbs",
    "histogram_scan.sbs", "edge_detect.sbs",
    "flood_fill_2.sbs", "tile_random.sbs",
)


def _find_sd_library_dir():
    """Locate '<SD install>/resources/packages'."""
    candidates = []
    try:
        exe_dir = os.path.dirname(sys.executable)
        for _ in range(5):
            candidates.append(exe_dir)
            exe_dir = os.path.dirname(exe_dir)
    except Exception:
        pass
    try:
        import psutil
        for proc in psutil.process_iter(['name', 'exe']):
            name = (proc.info.get('name') or '').lower()
            if 'substance' in name and 'designer' in name:
                exe = proc.info.get('exe') or ''
                if exe:
                    candidates.append(os.path.dirname(exe))
    except Exception:
        pass
    try:
        for drive in ('C:', 'D:', 'E:', 'F:'):
            for base in (drive + '\\Program Files\\Adobe',
                         drive + '\\Program Files (x86)\\Adobe'):
                if not os.path.isdir(base):
                    continue
                for item in os.listdir(base):
                    if 'substance' in item.lower() and 'designer' in item.lower():
                        candidates.append(os.path.join(base, item))
    except Exception:
        pass
    for root in candidates:
        lib_dir = os.path.join(root, 'resources', 'packages')
        if os.path.isdir(lib_dir):
            return lib_dir
    return None


_LIB_DIR_CACHE = []


def _is_builtin_package(file_path):
    """True if file_path is one of SD's own library packages (<SD install>/resources/packages)."""
    if not file_path:
        return False
    if not _LIB_DIR_CACHE:
        _LIB_DIR_CACHE.append(_find_sd_library_dir())
    path = os.path.normcase(os.path.normpath(file_path))
    lib_dir = _LIB_DIR_CACHE[0]
    if lib_dir:
        return path.startswith(os.path.normcase(os.path.normpath(lib_dir)) + os.sep)
    return os.path.normcase(os.path.join('resources', 'packages') + os.sep) in path


# ── Recipe param-name compatibility ─────────────────────────────────────────
# Recipes are authored against upstream library parameter names but SD
# 15.1.x instance nodes expose renamed properties (perlin: "Distance" +
# "Disorder" instead of "scale" + "disorder"; Disorder is 0-100 not 0-1).
# _set_node_params and set_parameter resolve ids via: exact id ->
# case-insensitive id -> these aliases (value_factor rescales 0-1 values).
_PARAM_ID_ALIASES = {
    "scale":    ("Distance",),
    "disorder": ("Disorder",),
}
_PARAM_VALUE_SCALE = {
    ("disorder", "Disorder"): 100.0,
}


_QTimer = None
_QT_BINDING_USED = None
for _qt_binding in ("PySide6.QtCore", "PySide2.QtCore"):
    try:
        _qtcore_mod = _importlib.import_module(_qt_binding)
        _QTimer = _qtcore_mod.QTimer
        _QT_BINDING_USED = _qt_binding
        break
    except Exception:
        pass


# ── Qt main-thread invoker ───────────────────────────────────────────────────
class _Invoker:
    _instance = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls._build()
        return cls._instance

    @staticmethod
    def _build():
        if not _QT_BINDING_USED:
            return None
        try:
            qtcore = _importlib.import_module(_QT_BINDING_USED)

            class Invoker(qtcore.QObject):
                invoke_signal = qtcore.Signal()

                def __init__(self):
                    super().__init__()
                    self._queue = queue.Queue()
                    self.invoke_signal.connect(self._execute, qtcore.Qt.QueuedConnection)

                def schedule(self, fn):
                    self._queue.put(fn)
                    self.invoke_signal.emit()

                def _execute(self):
                    try:
                        fn = self._queue.get_nowait()
                        fn()
                    except queue.Empty:
                        pass
                    except Exception as e:
                        _log("Invoker._execute error: {}".format(e))

            return Invoker()
        except Exception as e:
            _log("Warning: _Invoker build failed: {}".format(e))
            return None


class MainThreadDispatcher:
    def dispatch(self, fn, *args, **kwargs):
        if threading.current_thread() is threading.main_thread():
            try:
                return fn(*args, **kwargs), None
            except Exception as e:
                return None, e

        invoker = _Invoker.instance()
        if invoker is None:
            hint = (" injected: {}".format(_pyside_path)
                    if _pyside_path else " (pythonsdk not found)")
            return None, RuntimeError(
                "Qt invoker unavailable — cannot dispatch to main thread." + hint)

        result_holder = [None, None]
        done = threading.Event()

        def _call():
            try:
                result_holder[0] = fn(*args, **kwargs)
            except Exception as e:
                result_holder[1] = e
            finally:
                done.set()

        invoker.schedule(_call)

        if not done.wait(timeout=COMMAND_TIMEOUT):
            return None, TimeoutError(
                "Main thread dispatch timed out after {}s".format(COMMAND_TIMEOUT))

        return result_holder[0], result_holder[1]


_dispatcher = MainThreadDispatcher()


def _run_on_main(fn, *args, **kwargs):
    result, exc = _dispatcher.dispatch(fn, *args, **kwargs)
    if exc is not None:
        raise exc
    return result


# ── Length-prefix protocol ───────────────────────────────────────────────────
def _recv_exact(sock, n, timeout=COMMAND_TIMEOUT):
    sock.settimeout(timeout)
    buf = b""
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except socket.timeout:
            raise socket.timeout("Timed out reading {} bytes (got {})".format(n, len(buf)))
        if not chunk:
            return None
        buf += chunk
    return buf


def _recv_framed(sock, timeout=COMMAND_TIMEOUT):
    header = _recv_exact(sock, HEADER_SIZE, timeout)
    if not header:
        return None
    msg_len = struct.unpack(">I", header)[0]
    if msg_len == 0:
        return None
    if msg_len > MAX_MSG_SIZE:
        raise ValueError("Message too large: {} bytes".format(msg_len))
    payload = _recv_exact(sock, msg_len, timeout)
    if not payload:
        return None
    return payload


def _send_framed(sock, data):
    sock.sendall(struct.pack(">I", len(data)) + data)


# ── TCP Server ───────────────────────────────────────────────────────────────
class SDMCPServer:
    def __init__(self, ports=None):
        self.host = 'localhost'
        self.ports = ports or DEFAULT_PORTS
        self.running = False
        self.listeners = {}
        self._thread = None
        self._handler = CommandHandler()

    def start(self):
        self.running = True
        for port in self.ports:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((self.host, port))
                sock.listen(ACCEPT_BACKLOG)
                sock.setblocking(False)
                self.listeners[port] = sock
                _log("Listening on {}:{}".format(self.host, port))
            except Exception as e:
                _log("Failed to bind port {}: {}".format(port, e))

        if not self.listeners:
            _log("ERROR: No ports could be opened!")
            return

        self._thread = threading.Thread(
            target=self._serve_loop, daemon=True, name="SD-MCP-Serve")
        self._thread.start()
        _log("v{} running on ports: {}".format(
            ".".join(map(str, PLUGIN_VERSION)), list(self.listeners.keys())))

    def stop(self):
        self.running = False
        for sock in self.listeners.values():
            try:
                sock.close()
            except Exception:
                pass
        self.listeners.clear()
        _log("Server stopped")

    def _serve_loop(self):
        while self.running:
            readable = list(self.listeners.values())
            if not readable:
                time.sleep(0.1)
                continue
            try:
                ready, _, _ = select.select(readable, [], [], 0.1)
            except (OSError, ValueError):
                if not self.running:
                    break
                time.sleep(0.1)
                continue

            for listener in ready:
                port = next((p for p, s in self.listeners.items() if s is listener), None)
                if port is None:
                    continue
                try:
                    client, addr = listener.accept()
                except (BlockingIOError, OSError):
                    continue
                try:
                    self._handle_client(client, addr, port)
                except Exception as e:
                    _log("Unexpected error: {}".format(e))

    def _handle_client(self, client, addr, port):
        try:
            client.setblocking(True)
            client.settimeout(CLIENT_TIMEOUT)
            _log("Client on port {}: {}".format(port, addr))

            payload = _recv_framed(client, timeout=REQUEST_TIMEOUT)
            if payload is None:
                return

            try:
                command = json.loads(payload.decode('utf-8'))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                _send_framed(client, json.dumps(
                    {"status": "error", "message": "Invalid JSON: {}".format(e)}
                ).encode("utf-8"))
                return

            response = self._execute_safe(command)
            _send_framed(client, json.dumps(response, default=_json_safe).encode("utf-8"))

        except socket.timeout:
            _log("Timeout from client on port {}".format(port))
        except ConnectionResetError:
            pass
        except Exception as e:
            _log("Error handling client on port {}: {}".format(port, e))
        finally:
            try:
                client.close()
            except Exception:
                pass

    def _execute_safe(self, command):
        try:
            cmd_type = command.get("type")
            params   = command.get("params", {})
            _log("Executing: {}".format(cmd_type))
            result = _run_on_main(self._handler.dispatch, cmd_type, params)
            _log("Done: {}".format(cmd_type))
            return {"status": "success", "result": result if result is not None else {}}
        except Exception as e:
            _log("Error in {}: {}".format(command.get("type", "?"), e))
            try:
                traceback.print_exc()
            except Exception:
                pass
            return {"status": "error", "message": str(e)}


def _json_safe(obj):
    if isinstance(obj, float):
        if obj != obj:
            return None
        if obj == float('inf'):
            return 1e308
        if obj == float('-inf'):
            return -1e308
    return str(obj)


# ── Helper: infer SD value type from Python ──────────────────────────────────
# CRITICAL: bare int scalars → "float" because almost all SD node params are float.
# Only explicit {"value": x, "type": "int"} should produce SDValueInt.
# Sending SDValueInt to a float param CRASHES SD 15 silently.
def _infer_type(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "float"   # int scalar → float (avoids SDValueInt crash on float params)
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        n = len(value)
        # CRITICAL: SD 15 silently crashes on SDValueInt2/3/4 applied to float params.
        # Always use float vectors — int vector params MUST use explicit {"value":..., "type":"int2"}
        if n == 2:
            return "float2"
        if n == 3:
            return "float3"
        if n == 4:
            return "float4"
    return "float"


def _coerce_type(inferred_type, value, sd_type_id):
    """Coerce the inferred Python type to match the actual SD property type.
    Prevents SD 15 silent crashes caused by wrong SDValue types.
    sd_type_id: the getId() string of the property's SDType.
    """
    if not sd_type_id:
        return inferred_type
    tid = sd_type_id.lower()

    # Exact primitive types
    if tid == "int":        return "int"
    if tid == "float":      return "float"
    if tid == "bool":       return "bool"
    if tid == "string":     return "string"
    if tid == "float2":     return "float2"
    if tid == "float3":     return "float3"
    if tid == "float4":     return "float4"
    if tid == "int2":       return "int2"
    if tid == "int3":       return "int3"
    if tid == "int4":       return "int4"
    if tid == "colorrgba":  return "color"
    if tid == "colorrgb":   return "float3"

    # SD enum types (sbs::compositing::blendingmode, sbs::compositing::format, etc.)
    # Enums are integer-based — any non-primitive sd type that accepts an int value
    # SD enum types accept SDValueInt. Coerce float → int for these.
    if tid.startswith("sbs::") or "::" in tid:
        if inferred_type in ("float",):
            # Only coerce scalar floats that represent enum/int values
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return "int"
        return inferred_type

    # Fallback: check for int/float keywords in type name
    if "int" in tid and "float" not in tid:
        if inferred_type == "float":
            return "int"
        if inferred_type == "float4" and isinstance(value, (list, tuple)):
            return "int4"
        if inferred_type == "float2" and isinstance(value, (list, tuple)):
            return "int2"

    return inferred_type


def _sanitize_identifier(name):
    if not name:
        return "MCP_Graph"
    sanitized = re.sub(r'[^A-Za-z0-9_]', '_', name)
    if sanitized and not sanitized[0].isalpha():
        sanitized = "G_" + sanitized
    return sanitized or "MCP_Graph"


# ════════════════════════════════════════════════════════════════════════════
# COMMAND HANDLER — the brain of the plugin
# ════════════════════════════════════════════════════════════════════════════
class CommandHandler:

    def __init__(self):
        self.HANDLERS = {
            # ── Core scene ──
            "get_scene_info":         self.get_scene_info,
            "create_package":         self.create_package,
            "create_graph":           self.create_graph,
            "delete_graph":           self.delete_graph,
            "open_graph":             self.open_graph,
            "get_graph_info":         self.get_graph_info,
            "save_package":           self.save_package,
            "graph_snapshot":         self.graph_snapshot,
            "diagnostic":             self.diagnostic,
            # ── Node operations ──
            "list_node_definitions":  self.list_node_definitions,
            "create_node":            self.create_node,
            "create_instance_node":   self.create_instance_node,
            "create_output_node":     self.create_output_node,
            "delete_node":            self.delete_node,
            "move_node":              self.move_node,
            "duplicate_node":         self.duplicate_node,
            "get_node_info":          self.get_node_info,
            "get_library_nodes":      self.get_library_nodes,
            # ── Connection ──
            "connect_nodes":          self.connect_nodes,
            "disconnect_nodes":       self.disconnect_nodes,
            # ── Parameters ──
            "set_parameter":          self.set_parameter,
            "set_graph_output_size":  self.set_graph_output_size,
            # ── Batch builders ──
            "create_batch_graph":     self.create_batch_graph,
            "build_material_graph":   self.build_material_graph,
            "build_heightmap_graph":  self.build_heightmap_graph,
            # ── Recipe engine ──
            "list_recipes":           self.list_recipes,
            "get_recipe_info":        self.get_recipe_info,
            "apply_recipe":           self.apply_recipe,
            # ── Utilities ──
            "smart_connect":          self.smart_connect,
            "arrange_nodes":          self.arrange_nodes,
            "execute_code":           self.execute_code,
            "capture_3d_view":        self.capture_3d_view,
            "send_to_3d_view":        self.send_to_3d_view,
            # ── Documentation ──
            "list_documentation":     self.list_documentation,
            # ── Development ──
            "reload_plugin":          self.reload_plugin,
        }
        # Library node URL cache (populated lazily, session-lifetime)
        self._lib_url_cache = {}  # lower-case identifier -> url string
        self._library_loaded = False

    def dispatch(self, cmd_type, params):
        handler = self.HANDLERS.get(cmd_type)
        if not handler:
            raise ValueError("Unknown command: '{}'. Available: {}".format(
                cmd_type, sorted(self.HANDLERS.keys())))
        return handler(**params)

    # ═══════════════════════════════════════════════════════════════════════
    # ── SD API helpers ──────────────────────────────────────────────────
    # ═══════════════════════════════════════════════════════════════════════

    def _app(self):
        return sd.getContext().getSDApplication()

    def _pkg_mgr(self):
        return self._app().getPackageMgr()

    def _ui_mgr(self):
        return self._app().getUIMgr()

    def _get_sd_version(self):
        try:
            return self._app().getVersion()
        except Exception:
            return "unknown"

    def _resolve_package(self, package_index=0, package_path=None):
        pkgs = list(self._pkg_mgr().getUserPackages())
        if package_path:
            for pkg in pkgs:
                if pkg.getFilePath() == package_path:
                    return pkg
            raise ValueError("Package '{}' not found.".format(package_path))
        if not pkgs:
            raise ValueError("No user packages loaded. Open a .sbs file first.")
        if package_index < 0:
            raise ValueError("package_index must be >= 0 (got {}).".format(package_index))
        if package_index >= len(pkgs):
            raise ValueError("Package index {} out of range (have {}).".format(
                package_index, len(pkgs)))
        return pkgs[package_index]

    def _resolve_graph(self, graph_identifier=None):
        if graph_identifier:
            for pkg in list(self._pkg_mgr().getUserPackages()):
                for res in list(pkg.getChildrenResources(False)):
                    try:
                        if res.getIdentifier() == graph_identifier:
                            return res
                    except Exception:
                        continue
            raise ValueError("Graph '{}' not found.".format(graph_identifier))

        try:
            g = self._ui_mgr().getCurrentGraph()
            if g is not None:
                return g
        except Exception:
            pass

        for pkg in list(self._pkg_mgr().getUserPackages()):
            for res in list(pkg.getChildrenResources(False)):
                try:
                    if "SDSBSCompGraph" in res.getClassName():
                        return res
                except Exception:
                    continue
        raise ValueError(
            "No graph available. Open a package/graph in SD, or pass graph_identifier.")

    def _find_node(self, graph, node_id):
        try:
            n = graph.getNodeFromId(node_id)
            if n is not None:
                return n
        except Exception:
            pass
        for node in list(graph.getNodes()):
            if node.getIdentifier() == node_id:
                return node
        raise ValueError("Node '{}' not found in graph '{}'.".format(
            node_id, graph.getIdentifier()))

    def _get_node_def_id(self, node):
        try:
            defn = node.getDefinition()
            if defn is not None:
                return defn.getId()
        except Exception:
            pass
        return "unknown"

    def _is_instance_node(self, node):
        defn_id = self._get_node_def_id(node)
        return (defn_id == "unknown" or
                defn_id.startswith("pkg://") or
                "?dependency=" in defn_id)

    def _get_node_pos(self, node):
        try:
            p = node.getPosition()
            return [p.x, p.y]
        except Exception:
            return [0.0, 0.0]

    def _make_sd_value(self, value_type, value):
        t = value_type.lower()
        if t == "float":
            return SDValueFloat.sNew(float(value))
        if t == "int":
            return SDValueInt.sNew(int(value))
        if t == "bool":
            return SDValueBool.sNew(bool(value))
        if t == "string":
            return SDValueString.sNew(str(value))
        if t == "float2":
            v = value if isinstance(value, (list, tuple)) else [value, value]
            return SDValueFloat2.sNew(float2(float(v[0]), float(v[1])))
        if t == "float3":
            v = value if isinstance(value, (list, tuple)) else [value] * 3
            return SDValueFloat3.sNew(float3(float(v[0]), float(v[1]), float(v[2])))
        if t == "float4":
            v = value if isinstance(value, (list, tuple)) else [value] * 4
            return SDValueFloat4.sNew(float4(
                float(v[0]), float(v[1]), float(v[2]), float(v[3])))
        if t in ("color", "colorrgba"):
            v = value if isinstance(value, (list, tuple)) else [value] * 3 + [1.0]
            a = float(v[3]) if len(v) > 3 else 1.0
            return SDValueColorRGBA.sNew(ColorRGBA(
                float(v[0]), float(v[1]), float(v[2]), a))
        if t == "int2":
            v = value if isinstance(value, (list, tuple)) else [value, value]
            return SDValueInt2.sNew(int2(int(v[0]), int(v[1])))
        if t == "int3":
            v = value if isinstance(value, (list, tuple)) else [value] * 3
            return SDValueInt3.sNew(int3(int(v[0]), int(v[1]), int(v[2])))
        if t == "int4":
            v = value if isinstance(value, (list, tuple)) else [value] * 4
            return SDValueInt4.sNew(int4(int(v[0]), int(v[1]), int(v[2]), int(v[3])))
        raise ValueError(
            "Unknown value_type '{}'. Valid: float, int, bool, string, "
            "float2, float3, float4, color, int2, int3, int4".format(value_type))

    def _resolve_lib_url(self, keyword):
        """Find a library node URL by keyword (case-insensitive). Cached."""
        key = keyword.lower()
        if key in self._lib_url_cache:
            return self._lib_url_cache[key]
        self._ensure_library_loaded()
        for pkg in list(self._pkg_mgr().getPackages()):
            try:
                fp = pkg.getFilePath()
                if not fp:
                    continue
                for recursive in (True, False):
                    try:
                        children = list(pkg.getChildrenResources(recursive))
                        if not children:
                            continue
                        for res in children:
                            try:
                                if "SDSBSCompGraph" not in res.getClassName():
                                    continue
                                rid = res.getIdentifier()
                                if key in rid.lower():
                                    url = None
                                    try:
                                        url = res.getUrl()
                                    except Exception:
                                        pass
                                    if url:
                                        self._lib_url_cache[key] = url
                                        # also cache by exact identifier
                                        self._lib_url_cache[rid.lower()] = url
                                        return url
                            except Exception:
                                pass
                        break
                    except Exception:
                        continue
            except Exception:
                pass
        return None

    def _ensure_library_loaded(self):
        """Load SD library .sbs packages once so pkg:/// URLs can resolve."""
        if getattr(self, "_library_loaded", False):
            return
        self._library_loaded = True
        lib_dir = _find_sd_library_dir()
        if not lib_dir:
            _log("Library dir not found - library nodes unavailable")
            return
        loaded = 0
        for fname in _LIBRARY_SOURCE_FILES:
            path = os.path.join(lib_dir, fname)
            if not os.path.isfile(path):
                continue
            try:
                self._pkg_mgr().loadUserPackage(path)
                loaded += 1
            except Exception as e:
                _log("Library load failed for {}: {}".format(fname, e))
        _log("Loaded {} library packages from {}".format(loaded, lib_dir))

    def _find_resource_by_url(self, url):
        """Return the resource matching an exact pkg:/// URL, or None."""
        for pkg in list(self._pkg_mgr().getPackages()):
            try:
                r = pkg.findResourceFromUrl(url)
                if r is not None:
                    return r
            except Exception:
                pass
        return None

    def _find_lib_resource(self, identifier):
        """Find a loaded compgraph resource by identifier (exact, then prefix)."""
        idl = identifier.lower()
        fallback = None
        for pkg in list(self._pkg_mgr().getPackages()):
            try:
                for res in list(pkg.getChildrenResources(True)):
                    try:
                        if "SDSBSCompGraph" not in res.getClassName():
                            continue
                        rid = res.getIdentifier().lower()
                        if rid == idl:
                            return res
                        if fallback is None and (idl in rid or rid in idl):
                            fallback = res
                    except Exception:
                        pass
            except Exception:
                pass
        return fallback

    def _resolve_res_url(self, url):
        """Resolve a pkg:/// URL: exact match, then auto-load library packages,
        then fall back to identifier lookup (handles stale dependency hashes)."""
        res = self._find_resource_by_url(url)
        if res is not None:
            return res, url
        self._ensure_library_loaded()
        res = self._find_resource_by_url(url)
        if res is not None:
            return res, url
        ident = url.split("pkg:///")[-1].split("?")[0].strip("/")
        if ident:
            res = self._find_lib_resource(ident)
            if res is not None:
                try:
                    new_url = res.getUrl()
                except Exception:
                    new_url = url
                _log("Resolved '{}' -> '{}'".format(url, new_url))
                return res, new_url
        return None, url

    def _create_library_node(self, graph, keyword, position=None):
        """Create a library node from a keyword, auto-resolving URL."""
        url = self._resolve_lib_url(keyword)
        if not url:
            raise ValueError(
                "Library node '{}' not found. "
                "Use get_library_nodes(filter_text='{}') to verify.".format(keyword, keyword))
        resource, _resolved = self._resolve_res_url(url)
        if not resource:
            raise ValueError("Resource URL not found: {}".format(url))
        node = graph.newInstanceNode(resource)
        if not node:
            raise RuntimeError("newInstanceNode failed for '{}'.".format(url))
        if position and len(position) >= 2:
            node.setPosition(float2(float(position[0]), float(position[1])))
        return node

    def _resolve_param_id(self, param_id, input_type_map, annot_type_map):
        """Map a recipe-style parameter id to the actual SD property id for a
        node. Returns (resolved_id, value_factor, renamed)."""
        all_ids = set(input_type_map) | set(annot_type_map)
        if param_id in all_ids:
            return param_id, 1.0, False
        lowered = str(param_id).lower()
        for pid in all_ids:
            if pid.lower() == lowered:
                factor = _PARAM_VALUE_SCALE.get((lowered, pid), 1.0)
                return pid, factor, True
        for alias in _PARAM_ID_ALIASES.get(lowered, ()):
            if alias in all_ids:
                return alias, _PARAM_VALUE_SCALE.get((lowered, alias), 1.0), True
        return param_id, 1.0, False

    def _set_node_params(self, node, params):
        """Apply parameter dict to a node. Type-safe: reads actual SD property type
        and coerces before setting to prevent SD 15 silent crashes."""
        if not params:
            return {}
        results = {}

        # Build property type maps: id -> SDType id string
        input_type_map  = {}   # param_id -> sd_type_id
        annot_type_map  = {}
        try:
            for p in list(node.getProperties(SDPropertyCategory.Input)):
                pid = p.getId()
                try:
                    t = p.getType()
                    input_type_map[pid] = t.getId() if t else ""
                except Exception:
                    input_type_map[pid] = ""
        except Exception:
            pass
        try:
            for p in list(node.getProperties(SDPropertyCategory.Annotation)):
                pid = p.getId()
                try:
                    t = p.getType()
                    annot_type_map[pid] = t.getId() if t else ""
                except Exception:
                    annot_type_map[pid] = ""
        except Exception:
            pass

        for param_id, param_spec in params.items():
            # Skip system params — they cannot be set as node inputs safely
            if param_id.startswith("$"):
                results[param_id] = "skipped_system"
                continue
            try:
                if isinstance(param_spec, dict) and "value" in param_spec:
                    pval  = param_spec["value"]
                    ptype = param_spec.get("type", _infer_type(pval))
                else:
                    pval  = param_spec
                    ptype = _infer_type(pval)

                # Resolve recipe-style ids to real SD property ids on this node
                resolved_id, value_factor, renamed = self._resolve_param_id(
                    param_id, input_type_map, annot_type_map)
                if value_factor != 1.0 and isinstance(pval, (int, float)) \
                        and abs(pval) <= 1.0:
                    pval = pval * value_factor

                # Coerce ptype to match actual SD property type (prevents silent crash)
                sd_type_id = (input_type_map.get(resolved_id)
                              or annot_type_map.get(resolved_id) or "")
                ptype = _coerce_type(ptype, pval, sd_type_id)

                sd_val = self._make_sd_value(ptype, pval)
                set_ok = False
                if resolved_id in input_type_map:
                    try:
                        node.setInputPropertyValueFromId(resolved_id, sd_val)
                        set_ok = True
                    except Exception:
                        pass
                if not set_ok and resolved_id in annot_type_map:
                    try:
                        node.setAnnotationPropertyValueFromId(resolved_id, sd_val)
                        set_ok = True
                    except Exception:
                        pass
                if set_ok and renamed:
                    _log("Param '{}' -> '{}' on '{}'".format(
                        param_id, resolved_id, node.getIdentifier()))
                results[param_id] = "ok" if set_ok else "skipped"
            except Exception as e:
                results[param_id] = "error: {}".format(e)
        return results

    def _port_ids(self, node, category):
        """Connectable port IDs of a node, in SD's declaration order.

        Inputs exclude system params ($outputsize, ...) and non-connectable
        parameters (blendingmode, intensity, ...). Falls back to all non-system
        IDs if SD reports nothing connectable.
        """
        ids, connectable = [], []
        try:
            for p in list(node.getProperties(category)):
                pid = p.getId()
                if pid in _SYSTEM_PARAMS:
                    continue
                ids.append(pid)
                try:
                    if p.isConnectable():
                        connectable.append(pid)
                except Exception:
                    pass
        except Exception:
            pass
        return connectable or ids

    def _node_label(self, node):
        try:
            return "'{}' ({})".format(node.getIdentifier(), self._get_node_def_id(node))
        except Exception:
            return "'?'"

    def _safe_connect(self, graph, from_node, from_out, to_node, to_in):
        """Connect nodes with port validation. Returns True on success.

        Invalid port IDs raise ValueError listing the node's valid port IDs,
        closest match first (see port_suggest.port_error).
        """
        from . import port_suggest
        out_list = self._port_ids(from_node, SDPropertyCategory.Output)
        in_list  = self._port_ids(to_node, SDPropertyCategory.Input)
        out_ids  = set(out_list)
        in_ids   = set(in_list)

        if out_ids and from_out not in out_ids:
            # Library instance nodes expose version-specific output port names
            # (e.g. 'Noise_Zoom' instead of 'output' in SD 15.1.x). Auto-resolve
            # when there is exactly one output or a case-insensitive match.
            resolved = None
            if len(out_ids) == 1:
                resolved = next(iter(out_ids))
            else:
                lowered = str(from_out).lower()
                for pid in out_ids:
                    if pid.lower() == lowered:
                        resolved = pid
                        break
            if resolved is not None:
                _log("Auto-resolved output port '{}' -> '{}' on '{}'".format(
                    from_out, resolved, from_node.getIdentifier()))
                from_out = resolved
            else:
                raise ValueError(port_suggest.port_error(
                    "Output", from_out, self._node_label(from_node), out_list,
                    "Input", self._port_ids(from_node, SDPropertyCategory.Input)))
        if in_ids and to_in not in in_ids:
            lowered = str(to_in).lower()
            resolved = next((pid for pid in in_list if pid.lower() == lowered), None)
            if resolved is not None:
                _log("Auto-resolved input port '{}' -> '{}' on '{}'".format(
                    to_in, resolved, to_node.getIdentifier()))
                to_in = resolved
            else:
                raise ValueError(port_suggest.port_error(
                    "Input", to_in, self._node_label(to_node), in_list,
                    "Output", self._port_ids(to_node, SDPropertyCategory.Output)))

        conn = from_node.newPropertyConnectionFromId(from_out, to_node, to_in)
        if conn is None:
            raise RuntimeError(
                "Connection failed: {}.{} -> {}.{}. Output ports of source: {}. "
                "Input ports of target: {}.".format(
                    from_node.getIdentifier(), from_out,
                    to_node.getIdentifier(), to_in, out_list, in_list))
        return from_out, to_in

    # ═══════════════════════════════════════════════════════════════════════
    # ── CORE SCENE TOOLS ────────────────────────────────────────────────
    # ═══════════════════════════════════════════════════════════════════════

    def get_scene_info(self, include_library=False):
        pkg_mgr = self._pkg_mgr()
        try:
            current_graph = self._ui_mgr().getCurrentGraph()
            current_graph_id = current_graph.getIdentifier() if current_graph else None
        except Exception:
            current_graph = None
            current_graph_id = None

        pkg_list = []
        hidden_library = 0
        for index, pkg in enumerate(list(pkg_mgr.getUserPackages())):
            try:
                file_path = pkg.getFilePath()
                if not include_library and _is_builtin_package(file_path):
                    hidden_library += 1
                    continue
                graphs = []
                for res in list(pkg.getChildrenResources(False)):
                    try:
                        cname = res.getClassName()
                        rid   = res.getIdentifier()
                        nc    = 0
                        if "CompGraph" in cname or "SDGraph" in cname:
                            try:
                                nc = len(list(res.getNodes()))
                            except Exception:
                                pass
                        graphs.append({"identifier": rid, "type": cname, "node_count": nc})
                    except Exception:
                        pass
                # index = position in getUserPackages(), i.e. the package_index other tools take
                pkg_list.append({"index": index, "file_path": file_path, "graphs": graphs})
            except Exception as e:
                pkg_list.append({"index": index, "error": str(e)})

        current_nc = 0
        if current_graph is not None:
            try:
                current_nc = len(list(current_graph.getNodes()))
            except Exception:
                pass

        return {
            "packages": pkg_list,
            "package_count": len(pkg_list),
            "hidden_library_packages": hidden_library,
            "current_graph": current_graph_id,
            "current_graph_node_count": current_nc,
            "sd_version": self._get_sd_version(),
            "plugin_version": ".".join(map(str, PLUGIN_VERSION)),
        }

    def create_package(self, file_path=None):
        pkg = self._pkg_mgr().newUserPackage()
        return {
            "file_path": pkg.getFilePath(),
            "message": "New package created. Use save_package to save it.",
        }

    def create_graph(self, package_index=0, graph_name="MCP_Graph", package_path=None):
        safe_name = _sanitize_identifier(graph_name)
        pkg = self._resolve_package(package_index, package_path)
        new_graph = SDSBSCompGraph.sNew(pkg)
        new_graph.setIdentifier(safe_name)
        actual_id = new_graph.getIdentifier()
        return {
            "identifier": actual_id,
            "requested_name": graph_name,
            "sanitized_name": safe_name,
            "type": new_graph.getClassName(),
            "package": pkg.getFilePath(),
        }

    def delete_graph(self, graph_identifier, package_index=0):
        pkg = self._resolve_package(package_index)
        for res in list(pkg.getChildrenResources(False)):
            try:
                if res.getIdentifier() == graph_identifier:
                    res.delete()
                    return {"deleted": graph_identifier}
            except Exception:
                pass
        raise ValueError("Graph '{}' not found.".format(graph_identifier))

    def open_graph(self, graph_identifier):
        graph = self._resolve_graph(graph_identifier)
        try:
            self._ui_mgr().openResourceInEditor(graph)
            return {"opened": graph_identifier, "success": True}
        except Exception as e:
            return {"opened": graph_identifier, "warning": str(e)}

    def get_graph_info(self, graph_identifier=None, node_limit=100, include_connections=True):
        graph = self._resolve_graph(graph_identifier)
        all_nodes = list(graph.getNodes())
        total = len(all_nodes)
        to_detail = all_nodes[:node_limit] if node_limit > 0 else []

        node_list = []
        for node in to_detail:
            try:
                info = {
                    "identifier": node.getIdentifier(),
                    "definition":  self._get_node_def_id(node),
                    "position":    self._get_node_pos(node),
                    "connections": [],
                }
                if include_connections:
                    try:
                        for prop in list(node.getProperties(SDPropertyCategory.Input)):
                            pid   = prop.getId()
                            conns = node.getPropertyConnections(prop)
                            if conns is None:
                                continue
                            for conn in list(conns):
                                try:
                                    sn = conn.getInputPropertyNode()
                                    sp = conn.getInputProperty()
                                    if sn and sp:
                                        info["connections"].append({
                                            "input":       pid,
                                            "from_node":   sn.getIdentifier(),
                                            "from_output": sp.getId(),
                                        })
                                except Exception:
                                    pass
                    except Exception:
                        pass
                node_list.append(info)
            except Exception:
                pass

        return {
            "identifier":  graph.getIdentifier(),
            "node_count":  total,
            "nodes":       node_list,
            "truncated":   total > node_limit,
            "node_limit":  node_limit,
        }

    def save_package(self, package_index=0, file_path=None, package_path=None):
        pkg_mgr = self._pkg_mgr()
        pkg = self._resolve_package(package_index, package_path)
        if file_path:
            target_dir = os.path.dirname(os.path.abspath(file_path))
            if not os.path.exists(target_dir):
                try:
                    os.makedirs(target_dir, exist_ok=True)
                except Exception as e:
                    raise ValueError("Cannot create directory '{}': {}".format(target_dir, e))
            pkg_mgr.savePackageAs(pkg, file_path)
            return {"saved_to": file_path}
        else:
            current_path = pkg.getFilePath()
            if not current_path:
                raise ValueError(
                    "Package has no file path. Use file_path parameter to specify.")
            pkg_mgr.savePackage(pkg)
            return {"saved_to": current_path}

    def graph_snapshot(self, graph_identifier=None):
        """Export full graph structure as JSON snapshot (nodes + connections)."""
        graph = self._resolve_graph(graph_identifier)
        all_nodes = list(graph.getNodes())
        snapshot = {
            "graph_identifier": graph.getIdentifier(),
            "node_count": len(all_nodes),
            "nodes": [],
            "connections": [],
        }
        for node in all_nodes:
            nid  = node.getIdentifier()
            defn = self._get_node_def_id(node)
            pos  = self._get_node_pos(node)
            snapshot["nodes"].append({
                "id": nid, "definition": defn, "position": pos,
                "is_library": self._is_instance_node(node),
            })
            try:
                for prop in list(node.getProperties(SDPropertyCategory.Input)):
                    pid   = prop.getId()
                    conns = node.getPropertyConnections(prop)
                    if conns is None:
                        continue
                    for conn in list(conns):
                        try:
                            sn = conn.getInputPropertyNode()
                            sp = conn.getInputProperty()
                            if sn and sp:
                                snapshot["connections"].append({
                                    "from_node": sn.getIdentifier(),
                                    "from_output": sp.getId(),
                                    "to_node": nid,
                                    "to_input": pid,
                                })
                        except Exception:
                            pass
            except Exception:
                pass
        return snapshot

    # ═══════════════════════════════════════════════════════════════════════
    # ── 3D VIEW CAPTURE ─────────────────────────────────────────────────
    # ═══════════════════════════════════════════════════════════════════════

    def capture_3d_view(self, output_path=None, refresh_first=False, wait_ms=600):
        """Capture the 3D viewport (material preview) to a PNG file.

        Lets an agent SEE the rendered material and iterate. Works even when
        the SD window is occluded (PrintWindow render); falls back to briefly
        raising SD and doing a screen grab when needed.

        - output_path: target PNG (default %TEMP%/sd_mcp_preview/3dview_<ts>.png)
        - refresh_first: post a Space key to the viewport first (re-render nudge)
        - wait_ms: settle time before capture
        Returns {path, width, height, method}.
        """
        import ctypes
        import time as _time
        from ctypes import wintypes as _wt
        from PySide6.QtWidgets import QApplication
        from PySide6.QtGui import QGuiApplication, QKeyEvent, QImage
        from PySide6.QtCore import QTimer, QPoint, Qt, QEvent, QEventLoop

        app = QApplication.instance()
        if app is None:
            raise RuntimeError("Qt application not available.")

        viewport = None
        for w in app.allWidgets():
            try:
                if "ViewportWidget" in w.metaObject().className() and w.width() > 100:
                    viewport = w
                    break
            except Exception:
                pass
        if viewport is None:
            raise RuntimeError(
                "3D viewport widget not found. Open the 3D view (F3) in SD first.")

        def _pump(ms):
            loop = QEventLoop()
            QTimer.singleShot(int(ms), loop.quit)
            loop.exec()

        if refresh_first:
            try:
                for et in (QEvent.KeyPress, QEvent.KeyRelease):
                    app.postEvent(viewport, QKeyEvent(et, Qt.Key_Space, Qt.NoModifier))
            except Exception:
                pass
        _pump(max(0, int(wait_ms)))

        if not output_path:
            out_dir = os.path.join(os.environ.get("TEMP", "."), "sd_mcp_preview")
            output_path = os.path.join(
                out_dir, "3dview_{}.png".format(_time.strftime("%Y%m%d_%H%M%S")))
        output_path = os.path.abspath(output_path)
        out_dir = os.path.dirname(output_path)
        if out_dir and not os.path.isdir(out_dir):
            os.makedirs(out_dir, exist_ok=True)

        def _nonblack(qimg):
            try:
                for gx in range(1, 8):
                    for gy in range(1, 8):
                        c = qimg.pixelColor(int(qimg.width() * gx / 8.0),
                                            int(qimg.height() * gy / 8.0))
                        if c.red() > 8 or c.green() > 8 or c.blue() > 8:
                            return True
            except Exception:
                return True
            return False

        # ── method 1: PrintWindow (occlusion-proof) ──
        try:
            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32
            mainwin = viewport.window()
            hwnd = int(mainwin.winId())
            rect = _wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            ww = rect.right - rect.left
            hh = rect.bottom - rect.top
            if ww > 0 and hh > 0:
                hdc = user32.GetWindowDC(hwnd)
                mdc = gdi32.CreateCompatibleDC(hdc)
                bmp = gdi32.CreateCompatibleBitmap(hdc, ww, hh)
                gdi32.SelectObject(mdc, bmp)
                ok = user32.PrintWindow(hwnd, mdc, 2)

                class _BMIH(ctypes.Structure):
                    _fields_ = [("biSize", _wt.DWORD), ("biWidth", _wt.LONG),
                                ("biHeight", _wt.LONG), ("biPlanes", _wt.WORD),
                                ("biBitCount", _wt.WORD),
                                ("biCompression", _wt.DWORD),
                                ("biSizeImage", _wt.DWORD),
                                ("biXPelsPerMeter", _wt.LONG),
                                ("biYPelsPerMeter", _wt.LONG),
                                ("biClrUsed", _wt.DWORD),
                                ("biClrImportant", _wt.DWORD)]
                bi = _BMIH()
                bi.biSize = ctypes.sizeof(_BMIH)
                bi.biWidth = ww
                bi.biHeight = -hh
                bi.biPlanes = 1
                bi.biBitCount = 32
                buf = ctypes.create_string_buffer(ww * hh * 4)
                rows = gdi32.GetDIBits(mdc, bmp, 0, hh, buf,
                                       ctypes.byref(bi), 0)
                gdi32.DeleteObject(bmp)
                gdi32.DeleteDC(mdc)
                user32.ReleaseDC(hwnd, hdc)

                if ok and rows:
                    img = QImage(bytes(buf), ww, hh, ww * 4,
                                 QImage.Format_RGB32)
                    gpt = viewport.mapToGlobal(QPoint(0, 0))
                    dpr = mainwin.devicePixelRatio() or 1.0
                    px = int(round(gpt.x() * dpr - rect.left))
                    py = int(round(gpt.y() * dpr - rect.top))
                    pw = int(round(viewport.width() * dpr))
                    ph = int(round(viewport.height() * dpr))
                    vp = img.copy(px, py, pw, ph)
                    if not vp.isNull() and _nonblack(vp):
                        if vp.save(output_path):
                            return {"path": output_path,
                                    "width": vp.width(),
                                    "height": vp.height(),
                                    "method": "printwindow"}
        except Exception:
            pass

        # ── method 2: screen grab (briefly raises SD) ──
        try:
            user32 = ctypes.windll.user32
            k32 = ctypes.windll.kernel32
            mainwin = viewport.window()
            hwnd = int(mainwin.winId())
            user32.ShowWindow(hwnd, 9)
            fg = user32.GetForegroundWindow()
            tid_fg = user32.GetWindowThreadProcessId(fg, None)
            tid_my = k32.GetCurrentThreadId()
            user32.AttachThreadInput(tid_my, tid_fg, True)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.AttachThreadInput(tid_my, tid_fg, False)
            _pump(400)
            gpt = viewport.mapToGlobal(QPoint(0, 0))
            scr = QGuiApplication.screenAt(gpt) or QGuiApplication.primaryScreen()
            pix = scr.grabWindow(0, gpt.x(), gpt.y(),
                                 viewport.width(), viewport.height())
            if not pix.isNull() and _nonblack(pix.toImage()):
                if pix.save(output_path):
                    return {"path": output_path,
                            "width": pix.width(),
                            "height": pix.height(),
                            "method": "screengrab",
                            "note": "SD window was briefly raised to capture"}
        except Exception:
            pass

        raise RuntimeError(
            "3D view capture failed (PrintWindow produced no content and "
            "screen grab failed). Make sure the 3D view is open and SD is visible.")

    # ═══════════════════════════════════════════════════════════════════════
    # ── 3D VIEW MATERIAL BINDING ────────────────────────────────────────
    # ═══════════════════════════════════════════════════════════════════════

    # Menu text for each material channel of the 3D view material, keyed by
    # normalized label/usage (lowercase, no spaces/underscores).
    _CHANNEL_TEXT = {
        "basecolor": "Base Color",
        "height": "Height",
        "normal": "Normal",
        "roughness": "Roughness",
        "metallic": "Metallic",
        "ao": "Ambient Occlusion",
        "ambientocclusion": "Ambient Occlusion",
        "emissive": "Emissive",
        "opacity": "Opacity",
        "specularlevel": "Specular Level",
        "ior": "IOR",
        "coatcolor": "Coat Color",
        "coatroughness": "Coat Roughness",
        "coatnormal": "Coat Normal",
        "coatopacity": "Coat Opacity",
        "coatspecularlevel": "Coat Specular Level",
        "anisotropyangle": "Anisotropy Angle",
        "anisotropylevel": "Anisotropy Level",
        "translucency": "Translucency",
        "heencolor": "Sheen Color",
        "sheencolor": "Sheen Color",
        "sheenroughness": "Sheen Roughness",
        "dispersion": "Dispersion",
        "absorptioncolor": "Absorption Color",
        "absorptiondistance": "Absorption Distance",
        "scatteringcolor": "Scattering Color",
    }

    def send_to_3d_view(self, graph_identifier, channels=None):
        """Bind a graph's outputs to the 3D view material channels.

        The 3D view does NOT follow the active editor tab; what it renders is
        a persistent material whose channels are assigned per OUTPUT NODE via
        the graph editor: right-click the node -> "View In 3D View" -> pick the
        material channel (Base Color, Normal, Roughness, Metallic, Ambient
        Occlusion, Height, ...). This handler automates that whole flow using
        real mouse input (SD's graph menus only commit on real clicks).

        Channel per output is derived from the output node's label (e.g.
        'Base Color', 'Normal'). Use `channels` to override, keyed by output
        node identifier or label, valued with the menu channel name.

        Once bound the link is LIVE: every set_parameter edit re-renders the
        view automatically. Loop: send_to_3d_view -> capture_3d_view ->
        set_parameter -> capture_3d_view ...

        The chain runs in the background via QTimer (SD's main thread is
        briefly busy between cycles -- roughly 3-6 s per channel, and the
        cursor is moved while it runs; it is restored at the end). Wait about
        (channels x 6) seconds before capturing.

        - graph_identifier: graph name as shown in the Explorer (e.g. 'Test_Steel')
        - channels: optional dict {node_id_or_label: channel_text}
        Returns {scheduled: True, graph, targets: [{node, channel}...], note}.
        """
        import os as _os
        import tempfile as _tempfile
        import time as _time
        import ctypes as _ctypes
        import ctypes.wintypes as _wt
        from PySide6.QtWidgets import QApplication, QMenu
        from PySide6.QtCore import QTimer, QPoint
        from PySide6.QtGui import QCursor
        from sd.api.sdproperty import SDPropertyCategory

        app = QApplication.instance()
        if app is None:
            raise RuntimeError("Qt application not available.")

        graph = self._resolve_graph(graph_identifier)
        if graph is None:
            raise ValueError("Graph '{}' not found.".format(graph_identifier))

        def _norm(s):
            return "".join(ch for ch in (s or "").lower() if ch.isalnum())

        # ── Build the (output node, channel) work list ────────────────────
        out_nodes = []
        try:
            arr = graph.getOutputNodes()
            for i in range(arr.getSize()):
                out_nodes.append(arr.getItem(i))
        except Exception:
            for nd in graph.getNodes():
                try:
                    if nd.getDefinition().getId() == "sbs::compositing::output":
                        out_nodes.append(nd)
                except Exception:
                    pass
        if not out_nodes:
            raise ValueError(
                "Graph '{}' has no output nodes.".format(graph_identifier))

        overrides = dict(channels or {})
        targets = []
        skipped = []
        for nd in out_nodes:
            nid = nd.getIdentifier()
            label = ""
            try:
                v = nd.getPropertyValueFromId("label", SDPropertyCategory.Annotation)
                if v is not None:
                    label = v.get() or ""
            except Exception:
                label = ""
            chan = overrides.get(nid)
            if chan is None and label:
                chan = overrides.get(label)
            if chan is None:
                chan = self._CHANNEL_TEXT.get(_norm(label))
            if not chan:
                skipped.append({"node": nid, "label": label,
                                "reason": "no channel mapping (label empty or unknown); pass 'channels' override"})
                continue
            targets.append({"node": nid, "channel": chan, "obj": nd})
        if not targets:
            raise ValueError(
                "No bindable outputs (labels: {}). Provide 'channels' override."
                .format([s.get("label") for s in skipped]))
        skipped_nodes = [s for s in skipped]

        # ── Log file for diagnostics ──────────────────────────────────────
        log_dir = _os.path.join(_tempfile.gettempdir(), "sd_mcp_preview")
        try:
            _os.makedirs(log_dir, exist_ok=True)
        except Exception:
            pass
        log_path = _os.path.join(log_dir, "send3d_log.txt")
        _logf = open(log_path, "a", encoding="utf-8")

        def _log(msg):
            try:
                _logf.write("[%s] %s\n" % (_time.strftime("%H:%M:%S"), msg))
                _logf.flush()
            except Exception:
                pass

        _log("send_to_3d_view: graph=%s targets=%s skipped=%s"
             % (graph_identifier, [(t["node"], t["channel"]) for t in targets],
                skipped_nodes))

        # ── Windows input helpers (real mouse, required by SD menus) ──────
        class MOUSEINPUT(_ctypes.Structure):
            _fields_ = [("dx", _wt.LONG), ("dy", _wt.LONG),
                        ("mouseData", _wt.DWORD), ("dwFlags", _wt.DWORD),
                        ("time", _wt.DWORD),
                        ("dwExtraInfo", _ctypes.POINTER(_wt.ULONG))]

        class INPUT(_ctypes.Structure):
            class _I(_ctypes.Union):
                _fields_ = [("mi", MOUSEINPUT)]
            _anonymous_ = ("i",)
            _fields_ = [("type", _wt.DWORD), ("i", _I)]

        user32 = _ctypes.windll.user32
        kernel32 = _ctypes.windll.kernel32

        def _send_mouse(flag):
            inp = INPUT(type=0)
            inp.mi = MOUSEINPUT(0, 0, 0, flag, 0, None)
            user32.SendInput(1, _ctypes.byref(inp), _ctypes.sizeof(INPUT))

        def _real_click():
            _send_mouse(0x0008)  # right down
            _send_mouse(0x0010)  # right up
            _send_mouse(0x0002)  # left down
            _send_mouse(0x0004)  # left up

        def _ensure_fg(hwnd):
            try:
                fg = user32.GetForegroundWindow()
                t_fg = user32.GetWindowThreadProcessId(fg, None)
                t_tgt = user32.GetWindowThreadProcessId(hwnd, None)
                t_cur = kernel32.GetCurrentThreadId()
                user32.AttachThreadInput(t_cur, t_fg, True)
                user32.AttachThreadInput(t_cur, t_tgt, True)
                user32.SetForegroundWindow(hwnd)
                user32.SetFocus(hwnd)
                user32.AttachThreadInput(t_cur, t_cur, False)
                user32.AttachThreadInput(t_cur, t_fg, False)
                user32.AttachThreadInput(t_cur, t_tgt, False)
            except Exception:
                pass

        # ── Qt helpers ─────────────────────────────────────────────────────
        def _visible_menus():
            out = []
            try:
                pop = app.activePopupWidget()
                if isinstance(pop, QMenu) and pop.isVisible():
                    out.append(pop)
            except Exception:
                pass
            for w in app.topLevelWidgets():
                try:
                    if isinstance(w, QMenu) and w.isVisible() and w not in out:
                        out.append(w)
                except Exception:
                    pass
            return out

        def _find_action(menu, text):
            for a in menu.actions():
                try:
                    if a.text() == text:
                        return a
                except Exception:
                    pass
            return None

        def _close_menus():
            for m in _visible_menus():
                try:
                    m.hide()
                    m.close()
                except Exception:
                    pass

        def _graph_view():
            for w in app.allWidgets():
                try:
                    if (w.metaObject().className()
                            == "Pfx::Editor::Components::Graph::GraphView"
                            and w.isVisible()):
                        return w
                except Exception:
                    pass
            return None

        uim = self._ui_mgr()
        saved_cursor = QCursor.pos()
        saved_fg = user32.GetForegroundWindow()
        queue = list(targets)

        def _finish():
            _close_menus()
            try:
                QCursor.setPos(saved_cursor)
            except Exception:
                pass
            try:
                if saved_fg:
                    user32.SetForegroundWindow(saved_fg)
            except Exception:
                pass
            _log("finished (remaining=%d)" % len(queue))
            try:
                _logf.close()
            except Exception:
                pass

        # ── Per-target cycles (timer-driven; menus run nested loops) ──────
        def _cycle():
            if not queue:
                _finish()
                return
            t = queue[0]
            _log("cycle start: node=%s channel=%s" % (t["node"], t["channel"]))
            try:
                _close_menus()
                # Make sure the graph is shown in a graph view.
                view_id = None
                try:
                    for i in range(uim.getGraphViewIDCount()):
                        vid = uim.getGraphViewIDAt(i)
                        vg = uim.getGraphFromGraphViewID(vid)
                        if vg is not None and vg.getIdentifier() == graph_identifier:
                            view_id = vid
                            break
                except Exception:
                    pass
                if view_id is None:
                    try:
                        uim.openResourceInEditor(graph)
                    except Exception:
                        pass
                    t["_needs_view"] = t.get("_needs_view", 0) + 1
                    if t["_needs_view"] > 6:
                        raise RuntimeError("graph never appeared in a graph view")
                    QTimer.singleShot(700, _cycle)
                    return
                uim.focusGraphNode(view_id, t["obj"])
            except Exception as e:
                _log("cycle setup error: %s" % e)
                queue.pop(0)
                QTimer.singleShot(400, _cycle)
                return

            gv = _graph_view()
            if gv is None:
                _log("no visible graph view")
                queue.pop(0)
                QTimer.singleShot(400, _cycle)
                return
            vp = gv.viewport()
            click_pt = vp.mapToGlobal(vp.rect().center())
            _ensure_fg(int(gv.window().winId()))
            QCursor.setPos(click_pt)
            QTimer.singleShot(180, lambda: (_send_mouse(0x0008), _send_mouse(0x0010)))
            _log("right-click at %d,%d" % (click_pt.x(), click_pt.y()))

            tries = [0]

            def _poll_menu1():
                tries[0] += 1
                m1 = None
                for m in _visible_menus():
                    if _find_action(m, "View In 3D View") is not None:
                        m1 = m
                        break
                if m1 is None:
                    if tries[0] < 20:
                        QTimer.singleShot(150, _poll_menu1)
                    else:
                        _log("menu1 not found for node %s" % t["node"])
                        queue.pop(0)
                        QTimer.singleShot(400, _cycle)
                    return
                act = _find_action(m1, "View In 3D View")
                g = m1.mapToGlobal(m1.actionGeometry(act).center())
                QCursor.setPos(g)
                QTimer.singleShot(200, lambda: (_send_mouse(0x0002), _send_mouse(0x0004)))
                _log("clicked 'View In 3D View' at %d,%d" % (g.x(), g.y()))
                sub_tries = [0]

                def _poll_sub():
                    sub_tries[0] += 1
                    sub = None
                    for m in _visible_menus():
                        if _find_action(m, t["channel"]) is not None:
                            sub = m
                            break
                    if sub is None:
                        if sub_tries[0] < 20:
                            QTimer.singleShot(150, _poll_sub)
                        else:
                            _log("channel submenu not found for '%s'" % t["channel"])
                            queue.pop(0)
                            QTimer.singleShot(400, _cycle)
                        return
                    ca = _find_action(sub, t["channel"])
                    cg = sub.mapToGlobal(sub.actionGeometry(ca).center())
                    QCursor.setPos(cg)
                    QTimer.singleShot(250,
                        lambda: (_send_mouse(0x0002), _send_mouse(0x0004)))
                    _log("clicked channel '%s' at %d,%d"
                         % (t["channel"], cg.x(), cg.y()))

                    def _done():
                        _close_menus()
                        _log("cycle done: node=%s channel=%s" % (t["node"], t["channel"]))
                        queue.pop(0)
                        try:
                            QCursor.setPos(click_pt)
                        except Exception:
                            pass
                        QTimer.singleShot(900, _cycle)

                    QTimer.singleShot(1400, _done)

                QTimer.singleShot(300, _poll_sub)

            QTimer.singleShot(350, _poll_menu1)

        QTimer.singleShot(300, _cycle)
        return {
            "scheduled": True,
            "graph": graph_identifier,
            "targets": [{"node": t["node"], "channel": t["channel"]}
                        for t in targets],
            "skipped": skipped_nodes,
            "note": ("Binding {} channel(s) in the background (~3-6 s each, "
                     "cursor moves during the run). Wait about {} s, then "
                     "capture_3d_view(). The link is live: set_parameter edits "
                     "auto re-render the view.").format(
                         len(targets), max(12, 6 * len(targets))),
        }

    def diagnostic(self):
        """Full health check of SD environment."""
        results = {}
        # SD running?
        try:
            ver = self._get_sd_version()
            results["sd_running"] = True
            results["sd_version"] = ver
        except Exception as e:
            results["sd_running"] = False
            results["sd_version_error"] = str(e)

        # Qt binding
        results["qt_binding"] = _QT_BINDING_USED or "NONE"
        results["pyside_path"] = _pyside_path or "NOT FOUND"

        # Invoker
        inv = _Invoker.instance()
        results["qt_invoker_ok"] = inv is not None

        # Packages
        try:
            pkgs = list(self._pkg_mgr().getUserPackages())
            results["user_packages"] = len(pkgs)
            results["package_files"] = [p.getFilePath() for p in pkgs]
        except Exception as e:
            results["packages_error"] = str(e)

        # Library cache
        results["library_cache_entries"] = len(self._lib_url_cache)

        # Current graph
        try:
            g = self._ui_mgr().getCurrentGraph()
            if g:
                results["current_graph"] = g.getIdentifier()
                results["current_graph_nodes"] = len(list(g.getNodes()))
            else:
                results["current_graph"] = None
        except Exception as e:
            results["current_graph_error"] = str(e)

        return results

    # ═══════════════════════════════════════════════════════════════════════
    # ── NODE TOOLS ──────────────────────────────────────────────────────
    # ═══════════════════════════════════════════════════════════════════════

    def list_node_definitions(self, filter_text="", graph_identifier=None, limit=500):
        temp_pkg = None
        try:
            if graph_identifier:
                graph = self._resolve_graph(graph_identifier)
            else:
                graph = None
                try:
                    graph = self._ui_mgr().getCurrentGraph()
                except Exception:
                    pass
                if not graph:
                    pkgs = list(self._pkg_mgr().getUserPackages())
                    if pkgs:
                        rs = list(pkgs[0].getChildrenResources(False))
                        for r in rs:
                            try:
                                if "SDSBSCompGraph" in r.getClassName():
                                    graph = r
                                    break
                            except Exception:
                                pass
                if not graph:
                    temp_pkg = self._pkg_mgr().newUserPackage()
                    graph = SDSBSCompGraph.sNew(temp_pkg)
                    graph.setIdentifier("_TempDefQuery")

            definitions = graph.getNodeDefinitions()
            result = []
            for defn in list(definitions):
                try:
                    did = defn.getId()
                    if filter_text and filter_text.lower() not in did.lower():
                        continue
                    result.append(did)
                    if len(result) >= limit:
                        break
                except Exception:
                    pass

            return {
                "count":       len(result),
                "definitions": result,
                "truncated":   len(result) >= limit,
                "filter":      filter_text,
            }
        finally:
            if temp_pkg is not None:
                try:
                    self._pkg_mgr().unloadUserPackage(temp_pkg)
                except Exception:
                    pass

    def create_node(self, definition_id, graph_identifier=None, position=None):
        graph = self._resolve_graph(graph_identifier)
        # Validate before calling newNode — unknown defs HANG SD 15
        try:
            known = {d.getId() for d in list(graph.getNodeDefinitions())}
            if definition_id not in known:
                raise ValueError(
                    "Unknown definition '{}'. "
                    "Library nodes require create_instance_node with pkg:// URL.".format(
                        definition_id))
        except ValueError:
            raise
        except Exception:
            pass

        node = graph.newNode(definition_id)
        if not node:
            raise RuntimeError("newNode('{}') returned None.".format(definition_id))
        if position and len(position) >= 2:
            node.setPosition(float2(float(position[0]), float(position[1])))
        return {
            "node_id":    node.getIdentifier(),
            "definition": self._get_node_def_id(node),
            "position":   list(position) if position else [0.0, 0.0],
        }

    def create_instance_node(self, resource_url, graph_identifier=None, position=None):
        graph = self._resolve_graph(graph_identifier)
        resource, resolved_url = self._resolve_res_url(resource_url)
        if not resource:
            raise ValueError("Resource '{}' not found.".format(resource_url))
        node = graph.newInstanceNode(resource)
        if not node:
            raise RuntimeError("newInstanceNode failed for '{}'.".format(resource_url))
        if position and len(position) >= 2:
            node.setPosition(float2(float(position[0]), float(position[1])))
        return {
            "node_id":      node.getIdentifier(),
            "resource_url": resolved_url,
            "position":     list(position) if position else [0.0, 0.0],
            "note":         "Call get_node_info to find exact port IDs before connecting.",
        }

    def create_output_node(self, usage="baseColor", label=None,
                           graph_identifier=None, position=None):
        if label is None:
            label = usage
        graph = self._resolve_graph(graph_identifier)
        node = graph.newNode('sbs::compositing::output')
        if not node:
            raise RuntimeError("Failed to create output node.")
        if position and len(position) >= 2:
            node.setPosition(float2(float(position[0]), float(position[1])))
        label_set = False
        try:
            node.setAnnotationPropertyValueFromId("label", SDValueString.sNew(label))
            label_set = True
        except Exception as e:
            _log("Warning: label set failed: {}".format(e))
        return {
            "node_id":    node.getIdentifier(),
            "definition": "sbs::compositing::output",
            "usage":      usage,
            "label":      label,
            "label_set":  label_set,
        }

    def delete_node(self, node_id, graph_identifier=None):
        graph = self._resolve_graph(graph_identifier)
        node  = self._find_node(graph, node_id)
        graph.deleteNode(node)
        return {"deleted": node_id}

    def move_node(self, node_id, position, graph_identifier=None):
        graph = self._resolve_graph(graph_identifier)
        node  = self._find_node(graph, node_id)
        node.setPosition(float2(float(position[0]), float(position[1])))
        return {"node_id": node_id, "position": position}

    def duplicate_node(self, node_id, offset=None, graph_identifier=None):
        graph = self._resolve_graph(graph_identifier)
        node  = self._find_node(graph, node_id)
        if self._is_instance_node(node):
            raise ValueError(
                "Cannot duplicate library node '{}' via duplicate_node. "
                "Use create_instance_node with the same resource_url.".format(node_id))
        defn_id = self._get_node_def_id(node)
        pos     = self._get_node_pos(node)
        off     = offset or [100, 0]
        new_node = graph.newNode(defn_id)
        if not new_node:
            raise RuntimeError("Failed to duplicate node '{}'.".format(node_id))
        new_pos = [pos[0] + off[0], pos[1] + off[1]]
        new_node.setPosition(float2(float(new_pos[0]), float(new_pos[1])))
        return {
            "original_node_id": node_id,
            "new_node_id":      new_node.getIdentifier(),
            "definition":       defn_id,
            "position":         new_pos,
        }

    def get_node_info(self, node_id, graph_identifier=None):
        graph = self._resolve_graph(graph_identifier)
        node  = self._find_node(graph, node_id)

        inputs = []
        try:
            for prop in list(node.getProperties(SDPropertyCategory.Input)):
                pid = prop.getId()
                if pid in _SYSTEM_PARAMS:
                    continue
                info = {"id": pid}
                try:
                    val_obj = node.getPropertyValue(prop)
                    if val_obj is not None:
                        info["value"] = self._serialize_sd_value(val_obj)
                except Exception:
                    pass
                try:
                    conns = node.getPropertyConnections(prop)
                    if conns is not None:
                        cl = list(conns)
                        if cl:
                            info["connected_from"] = []
                            for conn in cl:
                                try:
                                    sn = conn.getInputPropertyNode()
                                    sp = conn.getInputProperty()
                                    if sn and sp:
                                        info["connected_from"].append(
                                            "{}.{}".format(sn.getIdentifier(), sp.getId()))
                                except Exception:
                                    pass
                except Exception:
                    pass
                inputs.append(info)
        except Exception:
            pass

        outputs = []
        try:
            for prop in list(node.getProperties(SDPropertyCategory.Output)):
                outputs.append({"id": prop.getId()})
        except Exception:
            pass

        annotations = []
        try:
            for prop in list(node.getProperties(SDPropertyCategory.Annotation)):
                pid = prop.getId()
                info = {"id": pid}
                try:
                    val_obj = node.getPropertyValue(prop)
                    if val_obj is not None:
                        info["value"] = self._serialize_sd_value(val_obj)
                except Exception:
                    pass
                annotations.append(info)
        except Exception:
            pass

        is_lib = self._is_instance_node(node)
        return {
            "node_id":        node_id,
            "definition":     self._get_node_def_id(node),
            "is_library_node": is_lib,
            "position":       self._get_node_pos(node),
            "inputs":         inputs,
            "outputs":        outputs,
            "annotations":    annotations,
            "note": (
                "Library node: use output IDs listed above, NOT 'unique_filter_output'"
                if is_lib else ""
            ),
        }

    def _serialize_sd_value(self, val):
        if val is None:
            return None
        try:
            raw = val.get()
        except Exception:
            return str(val)
        if hasattr(raw, 'x') and hasattr(raw, 'y'):
            d = {"x": raw.x, "y": raw.y}
            if hasattr(raw, 'z'):
                d["z"] = raw.z
            if hasattr(raw, 'w'):
                d["w"] = raw.w
            return d
        if hasattr(raw, 'r') and hasattr(raw, 'g'):
            return {"r": raw.r, "g": raw.g, "b": raw.b, "a": raw.a}
        if hasattr(raw, 'getSize') and hasattr(raw, 'getItem'):
            items = []
            try:
                for i in range(raw.getSize()):
                    item = raw.getItem(i)
                    items.append(str(item.get()) if hasattr(item, 'get') else str(item))
            except Exception:
                return str(raw)
            return items
        return str(raw)

    def get_library_nodes(self, filter_text="", limit=200):
        self._ensure_library_loaded()
        pkg_mgr = self._pkg_mgr()
        results = []
        for pkg in list(pkg_mgr.getPackages()):
            try:
                fp = pkg.getFilePath()
                if not fp:
                    continue
                children = None
                for recursive in (True, False):
                    try:
                        c = list(pkg.getChildrenResources(recursive))
                        if c:
                            children = c
                            break
                    except Exception:
                        continue
                if not children:
                    continue
                for res in children:
                    try:
                        if "SDSBSCompGraph" not in res.getClassName():
                            continue
                        rid = res.getIdentifier()
                        if filter_text and filter_text.lower() not in rid.lower():
                            continue
                        url = None
                        try:
                            url = res.getUrl()
                        except Exception:
                            pass
                        if not url:
                            continue
                        # Update cache
                        self._lib_url_cache[rid.lower()] = url
                        results.append({
                            "identifier": rid,
                            "url":        url,
                            "package":    os.path.basename(fp),
                        })
                        if len(results) >= limit:
                            break
                    except Exception:
                        pass
                if len(results) >= limit:
                    break
            except Exception:
                pass
        return {
            "count":     len(results),
            "nodes":     results,
            "truncated": len(results) >= limit,
            "filter":    filter_text,
        }

    # ═══════════════════════════════════════════════════════════════════════
    # ── CONNECTION TOOLS ────────────────────────────────────────────────
    # ═══════════════════════════════════════════════════════════════════════

    def connect_nodes(self, from_node_id, to_node_id,
                      from_output="unique_filter_output",
                      to_input="input1",
                      graph_identifier=None):
        graph     = self._resolve_graph(graph_identifier)
        from_node = self._find_node(graph, from_node_id)
        to_node   = self._find_node(graph, to_node_id)
        from_output, to_input = self._safe_connect(
            graph, from_node, from_output, to_node, to_input)
        return {
            "from_node":   from_node_id,
            "from_output": from_output,
            "to_node":     to_node_id,
            "to_input":    to_input,
            "success":     True,
        }

    def disconnect_nodes(self, node_id, input_id, graph_identifier=None):
        graph = self._resolve_graph(graph_identifier)
        node  = self._find_node(graph, node_id)
        prop  = node.getPropertyFromId(input_id, SDPropertyCategory.Input)
        if not prop:
            raise ValueError(
                "Property '{}' not found on node '{}'.".format(input_id, node_id))
        node.deletePropertyConnections(prop)
        return {"disconnected": "{}:{}".format(node_id, input_id)}

    def smart_connect(self, from_node_id, to_node_id,
                      from_output=None, to_input=None,
                      graph_identifier=None):
        """
        Auto-detect port names if not specified.
        Uses ATOMIC_PORTS registry for known node types.
        Falls back to get_node_info for library nodes.
        """
        graph     = self._resolve_graph(graph_identifier)
        from_node = self._find_node(graph, from_node_id)
        to_node   = self._find_node(graph, to_node_id)

        from_defn = self._get_node_def_id(from_node)
        to_defn   = self._get_node_def_id(to_node)

        # Determine from_output
        if not from_output:
            if from_defn in ATOMIC_PORTS:
                outs = ATOMIC_PORTS[from_defn]["outputs"]
                from_output = outs[0] if outs else "unique_filter_output"
            else:
                # Library node — query
                try:
                    outs = [p.getId() for p in
                            list(from_node.getProperties(SDPropertyCategory.Output))]
                    from_output = outs[0] if outs else "unique_filter_output"
                except Exception:
                    from_output = "unique_filter_output"

        # Determine to_input
        if not to_input:
            if to_defn in ATOMIC_PORTS:
                ins = ATOMIC_PORTS[to_defn]["inputs"]
                to_input = ins[0] if ins else "input1"
            else:
                try:
                    ins = [p.getId() for p in
                           list(to_node.getProperties(SDPropertyCategory.Input))
                           if p.getId() not in _SYSTEM_PARAMS]
                    to_input = ins[0] if ins else "input1"
                except Exception:
                    to_input = "input1"

        from_output, to_input = self._safe_connect(
            graph, from_node, from_output, to_node, to_input)
        return {
            "from_node":   from_node_id,
            "from_output": from_output,
            "to_node":     to_node_id,
            "to_input":    to_input,
            "success":     True,
        }

    # ═══════════════════════════════════════════════════════════════════════
    # ── PARAMETER TOOLS ─────────────────────────────────────────────────
    # ═══════════════════════════════════════════════════════════════════════

    def set_parameter(self, node_id, parameter_id, value, value_type="float",
                      graph_identifier=None):
        graph = self._resolve_graph(graph_identifier)
        node  = self._find_node(graph, node_id)

        input_ids  = set()
        annot_ids  = set()
        try:
            for p in list(node.getProperties(SDPropertyCategory.Input)):
                input_ids.add(p.getId())
        except Exception:
            pass
        try:
            for p in list(node.getProperties(SDPropertyCategory.Annotation)):
                annot_ids.add(p.getId())
        except Exception:
            pass

        orig_parameter_id = parameter_id
        parameter_id, value_factor, renamed = self._resolve_param_id(
            parameter_id, {i: "" for i in input_ids}, {i: "" for i in annot_ids})
        if value_factor != 1.0 and isinstance(value, (int, float)) \
                and abs(value) <= 1.0:
            value = value * value_factor
        if renamed:
            _log("Param '{}' -> '{}' on '{}'".format(
                orig_parameter_id, parameter_id, node_id))

        sd_value = self._make_sd_value(value_type, value)

        if (input_ids or annot_ids) and \
           parameter_id not in input_ids and parameter_id not in annot_ids:
            all_ids = sorted(input_ids | annot_ids)
            raise ValueError(
                "Property '{}' not found on node '{}'. Available: {}".format(
                    parameter_id, node_id, all_ids))

        set_ok = False
        last_err = None
        for setter in [node.setInputPropertyValueFromId,
                       node.setAnnotationPropertyValueFromId]:
            try:
                setter(parameter_id, sd_value)
                set_ok = True
                break
            except Exception as e:
                last_err = e

        if not set_ok:
            raise RuntimeError("Failed to set '{}' on node '{}': {}".format(
                parameter_id, node_id, last_err))

        result = {
            "node_id":      node_id,
            "parameter_id": parameter_id,
            "value":        value,
            "value_type":   value_type,
        }
        if renamed:
            result["renamed_from"] = orig_parameter_id
        return result

    def set_graph_output_size(self, width_log2=11, height_log2=11, graph_identifier=None):
        graph = self._resolve_graph(graph_identifier)
        graph.setInputPropertyValueFromId(
            "$outputsize",
            SDValueInt2.sNew(int2(int(width_log2), int(height_log2)))
        )
        return {
            "graph":       graph.getIdentifier(),
            "width_log2":  width_log2,
            "height_log2": height_log2,
            "size":        "{}x{}".format(2 ** width_log2, 2 ** height_log2),
        }

    # ═══════════════════════════════════════════════════════════════════════
    # ── BATCH GRAPH BUILDER ─────────────────────────────────────────────
    # ═══════════════════════════════════════════════════════════════════════

    def create_batch_graph(self, graph_name, package_index=0, package_path=None,
                           nodes=None, connections=None, output_size_log2=11,
                           open_in_editor=True):
        """
        Create a complete graph in one call.
        Supports atomic nodes, library nodes (via resource_url or keyword),
        output nodes, and full connection wiring.
        """
        if nodes is None:
            nodes = []
        if connections is None:
            connections = []

        safe_name = _sanitize_identifier(graph_name)
        pkg   = self._resolve_package(package_index, package_path)
        graph = SDSBSCompGraph.sNew(pkg)
        graph.setIdentifier(safe_name)

        try:
            graph.setInputPropertyValueFromId(
                "$outputsize",
                SDValueInt2.sNew(int2(int(output_size_log2), int(output_size_log2))))
        except Exception as e:
            _log("Warning: Could not set graph output size: {}".format(e))

        try:
            _known_defs = {d.getId() for d in list(graph.getNodeDefinitions())}
        except Exception:
            _known_defs = set()

        alias_map = {}       # alias -> (node_obj, node_id_str)
        created   = []
        failed    = []
        param_warnings = []

        for spec in nodes:
            defn_id  = spec.get("definition_id", "sbs::compositing::uniform")
            pos      = spec.get("position", [0, 0])
            params   = spec.get("parameters", {})
            alias    = (spec.get("id_alias") or spec.get("alias")
                        or defn_id.split("::")[-1])
            orig_alias = alias
            usage    = spec.get("usage")
            label    = spec.get("label")
            res_url  = spec.get("resource_url")
            lib_kw   = spec.get("library_keyword")

            try:
                node = None

                if defn_id == "sbs::compositing::output":
                    lv = label or usage or "output"
                    node = graph.newNode("sbs::compositing::output")
                    if node:
                        try:
                            node.setAnnotationPropertyValueFromId(
                                "label", SDValueString.sNew(lv))
                        except Exception:
                            pass

                elif res_url:
                    resource, _resolved = self._resolve_res_url(res_url)
                    node = graph.newInstanceNode(resource) if resource else None
                    if not node:
                        raise ValueError("Resource '{}' not found.".format(res_url))

                elif lib_kw:
                    node = self._create_library_node(graph, lib_kw)

                else:
                    if _known_defs and defn_id not in _known_defs:
                        raise ValueError("Unknown definition '{}'. "
                                         "Use library_keyword for library nodes.".format(defn_id))
                    node = graph.newNode(defn_id)

                if not node:
                    raise RuntimeError("newNode returned None for '{}'.".format(defn_id))

                node.setPosition(float2(float(pos[0]), float(pos[1])))
                pstatus = self._set_node_params(node, params)
                for _pk, _pv in (pstatus or {}).items():
                    if _pv == "skipped" or str(_pv).startswith("error"):
                        param_warnings.append(
                            {"alias": alias, "param": _pk, "status": _pv})

                node_id = node.getIdentifier()
                # deduplicate aliases
                counter = 0
                while alias in alias_map:
                    counter += 1
                    alias = "{}_{}".format(orig_alias, counter)

                alias_map[alias] = (node, node_id)
                actual_def = self._get_node_def_id(node)
                created.append({
                    "alias": alias, "node_id": node_id, "definition": actual_def})

            except Exception as e:
                _log("Error creating node '{}': {}".format(alias, e))
                failed.append({"alias": alias, "error": str(e)})

        conn_results = []
        for conn in connections:
            fa  = conn.get("from") or conn.get("from_alias")
            ta  = conn.get("to") or conn.get("to_alias")
            fo  = conn.get("from_output", "unique_filter_output")
            ti  = conn.get("to_input", "input1")

            if fa not in alias_map:
                conn_results.append({"error": "from alias '{}' not found. Known aliases: {}".format(
                    fa, sorted(alias_map)), "conn": conn})
                continue
            if ta not in alias_map:
                conn_results.append({"error": "to alias '{}' not found. Known aliases: {}".format(
                    ta, sorted(alias_map)), "conn": conn})
                continue

            fn, _ = alias_map[fa]
            tn, _ = alias_map[ta]
            try:
                fo, ti = self._safe_connect(graph, fn, fo, tn, ti)
                conn_results.append({"from": fa, "to": ta, "from_output": fo,
                                     "to_input": ti, "success": True})
            except Exception as e:
                conn_results.append({"from": fa, "to": ta, "success": False, "error": str(e)})

        if open_in_editor:
            try:
                self._ui_mgr().openResourceInEditor(graph)
            except Exception as e:
                _log("Warning: openResourceInEditor failed: {}".format(e))

        result = {
            "graph_identifier":  graph.getIdentifier(),
            "requested_name":    graph_name,
            "nodes_created":     len(created),
            "nodes_failed":      len(failed),
            "connections_ok":    sum(1 for c in conn_results if c.get("success")),
            "connections_failed": sum(1 for c in conn_results if not c.get("success")),
            "node_map":          {a: nid for a, (_, nid) in alias_map.items()},
            "nodes":             created,
            "failed_nodes":      failed,
            "connections":       conn_results,
        }
        if param_warnings:
            result["param_warnings"] = param_warnings
        return result

    # ═══════════════════════════════════════════════════════════════════════
    # ── SMART MATERIAL GRAPH BUILDER ────────────────────────────────────
    # ═══════════════════════════════════════════════════════════════════════

    def build_material_graph(self, graph_name, recipe_name,
                              package_index=0, package_path=None,
                              overrides=None, output_size_log2=11,
                              open_in_editor=True):
        """
        Build a complete PBR material graph from a named recipe.
        overrides: dict of parameter overrides applied on top of recipe defaults.
        """
        from .recipes import RECIPE_REGISTRY
        recipe_name_key = recipe_name.lower().replace(" ", "_").replace("-", "_")
        recipe = RECIPE_REGISTRY.get(recipe_name_key)
        if not recipe:
            available = sorted(RECIPE_REGISTRY.keys())
            raise ValueError(
                "Recipe '{}' not found. Available: {}".format(recipe_name, available))

        # Apply overrides
        if overrides:
            import copy
            recipe = copy.deepcopy(recipe)
            for node_alias, node_overrides in overrides.items():
                for spec in recipe.get("nodes", []):
                    if spec.get("id_alias") == node_alias:
                        spec.setdefault("parameters", {}).update(node_overrides)

        nodes       = recipe.get("nodes", [])
        connections = recipe.get("connections", [])

        return self.create_batch_graph(
            graph_name       = graph_name,
            package_index    = package_index,
            package_path     = package_path,
            nodes            = nodes,
            connections      = connections,
            output_size_log2 = output_size_log2,
            open_in_editor   = open_in_editor,
        )

    def build_heightmap_graph(self, graph_name, style,
                               package_index=0, package_path=None,
                               output_size_log2=11, open_in_editor=True,
                               detail_level=2, scale=5.0, disorder=0.5):
        """
        Build a heightmap graph for terrain/rock/cliff styles.
        style: 'cliff', 'rock', 'cracked', 'sand', 'mud', 'mountain', 'cobblestone'
        detail_level: 1=basic, 2=standard, 3=high detail
        """
        from .recipes import HEIGHTMAP_RECIPES
        style_key = style.lower().replace(" ", "_")
        builder   = HEIGHTMAP_RECIPES.get(style_key)
        if not builder:
            available = sorted(HEIGHTMAP_RECIPES.keys())
            raise ValueError(
                "Heightmap style '{}' not found. Available: {}".format(style, available))

        recipe = builder(detail_level=detail_level, scale=scale, disorder=disorder)
        nodes       = recipe.get("nodes", [])
        connections = recipe.get("connections", [])
        return self.create_batch_graph(
            graph_name       = graph_name,
            package_index    = package_index,
            package_path     = package_path,
            nodes            = nodes,
            connections      = connections,
            output_size_log2 = output_size_log2,
            open_in_editor   = open_in_editor,
        )

    # ═══════════════════════════════════════════════════════════════════════
    # ── RECIPE ENGINE ───────────────────────────────────────────────────
    # ═══════════════════════════════════════════════════════════════════════

    def list_recipes(self):
        """List all available material recipes."""
        try:
            from .recipes import RECIPE_REGISTRY, HEIGHTMAP_RECIPES
            recipes = []
            for key, recipe in RECIPE_REGISTRY.items():
                recipes.append({
                    "key":         key,
                    "name":        recipe.get("name", key),
                    "category":    recipe.get("category", "unknown"),
                    "description": recipe.get("description", ""),
                    "node_count":  len(recipe.get("nodes", [])),
                    "outputs":     recipe.get("outputs", []),
                })
            heightmaps = sorted(HEIGHTMAP_RECIPES.keys())
            return {
                "material_recipes": recipes,
                "heightmap_styles": heightmaps,
                "total_recipes": len(recipes),
                "total_heightmap_styles": len(heightmaps),
            }
        except ImportError as e:
            return {"error": "Recipes module not loaded: {}".format(e)}

    def get_recipe_info(self, recipe_name):
        """Get detailed info about a specific recipe."""
        try:
            from .recipes import RECIPE_REGISTRY
            key = recipe_name.lower().replace(" ", "_").replace("-", "_")
            recipe = RECIPE_REGISTRY.get(key)
            if not recipe:
                return {"error": "Recipe '{}' not found.".format(recipe_name)}
            return {
                "key":          key,
                "name":         recipe.get("name", key),
                "category":     recipe.get("category", "unknown"),
                "description":  recipe.get("description", ""),
                "outputs":      recipe.get("outputs", []),
                "node_count":   len(recipe.get("nodes", [])),
                "nodes_preview": [
                    {
                        "alias": s.get("id_alias", "?"),
                        "type":  s.get("definition_id", s.get("library_keyword", "?")),
                    }
                    for s in recipe.get("nodes", [])[:20]
                ],
            }
        except ImportError as e:
            return {"error": "Recipes module not loaded: {}".format(e)}

    def apply_recipe(self, recipe_name, graph_identifier=None, position_offset=None,
                     overrides=None):
        """
        Apply a recipe to an EXISTING graph (add nodes + connections to current graph).
        Useful for combining multiple recipes or adding detail passes to existing work.
        """
        try:
            from .recipes import RECIPE_REGISTRY
        except ImportError as e:
            raise RuntimeError("Recipes module not loaded: {}".format(e))

        key    = recipe_name.lower().replace(" ", "_").replace("-", "_")
        recipe = RECIPE_REGISTRY.get(key)
        if not recipe:
            raise ValueError("Recipe '{}' not found.".format(recipe_name))

        import copy
        recipe = copy.deepcopy(recipe)

        # Apply position offset
        if position_offset:
            ox, oy = float(position_offset[0]), float(position_offset[1])
            for spec in recipe.get("nodes", []):
                p = spec.get("position", [0, 0])
                spec["position"] = [p[0] + ox, p[1] + oy]

        # Apply overrides
        if overrides:
            for node_alias, node_overrides in overrides.items():
                for spec in recipe.get("nodes", []):
                    if spec.get("id_alias") == node_alias:
                        spec.setdefault("parameters", {}).update(node_overrides)

        graph     = self._resolve_graph(graph_identifier)
        nodes     = recipe.get("nodes", [])
        conns     = recipe.get("connections", [])

        try:
            _known_defs = {d.getId() for d in list(graph.getNodeDefinitions())}
        except Exception:
            _known_defs = set()

        alias_map = {}
        created   = []
        failed    = []
        param_warnings = []

        for spec in nodes:
            defn_id    = spec.get("definition_id", "sbs::compositing::uniform")
            pos        = spec.get("position", [0, 0])
            params     = spec.get("parameters", {})
            alias      = spec.get("id_alias") or defn_id.split("::")[-1]
            orig_alias = alias
            res_url    = spec.get("resource_url")
            lib_kw     = spec.get("library_keyword")
            label      = spec.get("label")
            usage      = spec.get("usage")

            try:
                node = None
                if defn_id == "sbs::compositing::output":
                    lv = label or usage or "output"
                    node = graph.newNode("sbs::compositing::output")
                    if node:
                        try:
                            node.setAnnotationPropertyValueFromId(
                                "label", SDValueString.sNew(lv))
                        except Exception:
                            pass
                elif res_url:
                    resource, _resolved = self._resolve_res_url(res_url)
                    node = graph.newInstanceNode(resource) if resource else None
                    if not node:
                        raise ValueError("Resource '{}' not found.".format(res_url))
                elif lib_kw:
                    node = self._create_library_node(graph, lib_kw)
                else:
                    if _known_defs and defn_id not in _known_defs:
                        raise ValueError("Unknown definition '{}'.".format(defn_id))
                    node = graph.newNode(defn_id)

                if not node:
                    raise RuntimeError("newNode returned None.")

                node.setPosition(float2(float(pos[0]), float(pos[1])))
                pstatus = self._set_node_params(node, params)
                for _pk, _pv in (pstatus or {}).items():
                    if _pv == "skipped" or str(_pv).startswith("error"):
                        param_warnings.append(
                            {"alias": alias, "param": _pk, "status": _pv})

                nid = node.getIdentifier()
                counter = 0
                while alias in alias_map:
                    counter += 1
                    alias = "{}_{}".format(orig_alias, counter)
                alias_map[alias] = (node, nid)
                created.append({"alias": alias, "node_id": nid})

            except Exception as e:
                _log("Error creating node '{}': {}".format(alias, e))
                failed.append({"alias": alias, "error": str(e)})

        conn_results = []
        for conn in conns:
            fa = conn.get("from") or conn.get("from_alias")
            ta = conn.get("to") or conn.get("to_alias")
            fo = conn.get("from_output", "unique_filter_output")
            ti = conn.get("to_input", "input1")
            if fa not in alias_map or ta not in alias_map:
                conn_results.append({"error": "alias not found", "conn": conn})
                continue
            fn, _ = alias_map[fa]
            tn, _ = alias_map[ta]
            try:
                fo, ti = self._safe_connect(graph, fn, fo, tn, ti)
                conn_results.append({"from": fa, "to": ta, "from_output": fo,
                                     "to_input": ti, "success": True})
            except Exception as e:
                conn_results.append({"from": fa, "to": ta, "success": False, "error": str(e)})

        result = {
            "graph_identifier":  graph.getIdentifier(),
            "recipe":            recipe_name,
            "nodes_added":       len(created),
            "nodes_failed":      len(failed),
            "connections_ok":    sum(1 for c in conn_results if c.get("success")),
            "connections_failed": sum(1 for c in conn_results if not c.get("success")),
            "node_map":          {a: nid for a, (_, nid) in alias_map.items()},
        }
        if param_warnings:
            result["param_warnings"] = param_warnings
        return result

    # ═══════════════════════════════════════════════════════════════════════
    # ── UTILITIES ───────────────────────────────────────────────────────
    # ═══════════════════════════════════════════════════════════════════════

    def arrange_nodes(self, graph_identifier=None, start_x=-1000, start_y=0,
                      node_spacing_x=200, node_spacing_y=150):
        """
        WARNING: DESTROYS all connections in SD 15. Use move_node() instead.
        Only use when connections don't matter.
        """
        graph = self._resolve_graph(graph_identifier)
        nodes = list(graph.getNodes())
        if not nodes:
            return {"graph": graph.getIdentifier(), "arranged_nodes": 0,
                    "warning": "No nodes to arrange."}

        per_row = max(1, int(len(nodes) ** 0.5) + 1)
        x, y = float(start_x), float(start_y)
        for i, node in enumerate(nodes):
            try:
                node.setPosition(float2(x, y))
            except Exception:
                pass
            x += node_spacing_x
            if (i + 1) % per_row == 0:
                x = float(start_x)
                y += node_spacing_y

        return {
            "graph":          graph.getIdentifier(),
            "arranged_nodes": len(nodes),
            "warning":        "arrange_nodes DESTROYS all connections in SD 15.",
        }

    def list_documentation(self, category="all", filter_text="", node_name="",
                            action="", topic="", query=""):
        """
        Browse the SD MCP internal documentation knowledge base.

        Params:
          category    — one of: all, atomic_nodes, library_nodes, blend_modes,
                        port_reference, pbr_outputs, workflow, concepts, shortcuts,
                        connection_patterns, node_categories, parameters
          filter_text — substring filter applied to results (case-insensitive)
          node_name   — get detailed info for a specific node (e.g. 'blur', 'blend')
          action      — legacy compat alias for category ('categories', 'search')
          topic       — unused (kept for compat with newer schema)
          query       — alias for filter_text when action='search'
        """
        from . import sd_documentation as _doc

        # action='search' compat → use query as filter_text
        if action == "search" and query and not filter_text:
            filter_text = query

        # action='categories' → list available categories
        if action == "categories":
            return {
                "status": "ok",
                "categories": list(_doc.CATEGORIES.keys()),
                "descriptions": _doc.CATEGORIES,
            }

        try:
            result = _doc.query_documentation(
                category=category or "all",
                filter_text=filter_text or "",
                node_name=node_name or "",
            )
            return {"status": "ok", "result": result}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def reload_plugin(self, handlers=True):
        """Hot-reload the plugin from disk without restarting SD.

        Always reloads the pure-Python submodules (recipes, sd_documentation,
        port_suggest). With handlers=True it also re-executes this package's
        __init__.py and swaps a fresh CommandHandler into the running server.
        The TCP server, its listener sockets and the Qt invoker are kept (the
        invoker must stay the one created on the main thread), so the bridge
        connection is unaffected. Changes to SDMCPServer / the dispatcher
        themselves still need an SD restart.
        """
        pkg_name = __name__
        pkg = sys.modules[pkg_name]
        reloaded = []
        for sub in _RELOADABLE_SUBMODULES:
            name = pkg_name + "." + sub
            mod = sys.modules.get(name)
            if mod is None:
                _importlib.import_module(name)
            else:
                _importlib.reload(mod)
            reloaded.append(sub)

        result = {"reloaded_modules": reloaded, "handlers_reloaded": False}
        if not handlers:
            return result

        # Syntax-check first so a typo leaves the running module untouched.
        src_path = pkg.__file__
        with open(src_path, "r", encoding="utf-8") as f:
            compile(f.read(), src_path, "exec")

        server  = _server
        invoker = _Invoker._instance
        try:
            _importlib.reload(pkg)
        finally:
            # reload() re-runs module top-level code, which resets these.
            pkg._server = server
            pkg._Invoker._instance = invoker

        new_handler = pkg.CommandHandler()
        new_handler._lib_url_cache  = self._lib_url_cache
        new_handler._library_loaded = self._library_loaded
        if server is not None:
            server._handler = new_handler
        _log("Plugin reloaded from {}".format(src_path))
        result.update({
            "handlers_reloaded": True,
            "plugin_version":    ".".join(map(str, pkg.PLUGIN_VERSION)),
            "commands":          sorted(new_handler.HANDLERS),
        })
        return result

    def execute_code(self, code):
        """Execute arbitrary Python on the SD main thread. Keep it short."""
        stdout_cap = io.StringIO()
        stderr_cap = io.StringIO()
        app_obj    = sd.getContext().getSDApplication()

        def _open_in_editor_safe(g):
            try:
                app_obj.getUIMgr().openResourceInEditor(g)
            except Exception as e:
                print("[MCP] open_in_editor warning: {}".format(e))

        namespace = {
            "sd": sd, "app": app_obj,
            "pkg_mgr": app_obj.getPackageMgr(),
            "ui_mgr":  app_obj.getUIMgr(),
            "open_in_editor": _open_in_editor_safe,
        }
        error = None
        try:
            with redirect_stdout(stdout_cap), redirect_stderr(stderr_cap):
                exec(compile(code, "<mcp_execute>", "exec"), namespace)
        except Exception as e:
            error = "{}: {}".format(type(e).__name__, e)
            stderr_cap.write(traceback.format_exc())

        return {
            "executed": error is None,
            "stdout":   stdout_cap.getvalue(),
            "stderr":   stderr_cap.getvalue(),
            "error":    error,
        }


# ════════════════════════════════════════════════════════════════════════════
# Plugin entry points
# ════════════════════════════════════════════════════════════════════════════

def initializeSDPlugin():
    global _server
    _log("Initializing v{}".format(".".join(map(str, PLUGIN_VERSION))))

    if _pyside_path:
        _log("PySide path injected: {}".format(_pyside_path))
    else:
        _log("Warning: PySide6 path not found")

    if _QT_BINDING_USED:
        _log("Qt binding: {}".format(_QT_BINDING_USED))
    else:
        _log("FATAL: No Qt binding. All MCP calls will fail.")

    inv = _Invoker.instance()
    if inv is not None:
        _log("Qt invoker ready (Signal/Slot dispatch)")
    else:
        _log("FATAL: Qt invoker creation failed")

    try:
        _server = SDMCPServer(ports=DEFAULT_PORTS)
        _server.start()
        _log("Plugin v{} ready! Port: {}".format(
            ".".join(map(str, PLUGIN_VERSION)), DEFAULT_PORTS))
    except Exception as e:
        _log("FATAL: Failed to start server: {}".format(e))
        traceback.print_exc()


def uninitializeSDPlugin():
    global _server
    _log("Uninitializing")
    if _server:
        _server.stop()
        _server = None
