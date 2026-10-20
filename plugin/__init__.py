"""
Substance Designer MCP Plugin v3.9.0
=====================================
ARCHITECTURE
============
LLM -> stdio -> sd_mcp_bridge.py (FastMCP) -> TCP -> this plugin -> sd.api
PORT: 9881 (override with SD_MCP_PORT; all clients: Claude Code, Claude Desktop, Cursor)
PROTOCOL: Length-prefix framing [4-byte big-endian length][JSON payload]
CONNECTION MODEL: Fresh TCP socket per command, one worker thread per client
THREADING: ALL sd.api calls on Qt main thread via Signal/Slot queued dispatch;
           job_status/job_cancel read in-memory job records on worker threads

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
  3.5.0 - Library package loading from <SD>/resources/packages; recipe fixes
  3.6.0 - capture_2d_view (texture + widget modes, contact sheets), get_map_stats
  3.7.0 - set_parameters (batch edits, one node resolve each); inspect_outputs
          (contact sheet + per-channel stats from a single graph compute)
  3.8.0 - SECURITY: optional shared token (SD_MCP_TOKEN; commands must carry a
          matching "token" field), constant-time token comparison, execute_code
          opt-in gate (SD_MCP_ALLOW_EXECUTE_CODE=1 or an allow_execute_code
          marker file), reload_plugin dev-tool gate. Batch builder: preflight
          validation without creating a graph, cleanup_on_failure removes a
          partially built graph. Long jobs: capture_2d_view, get_map_stats and
          inspect_outputs run as staged, cancellable jobs (start_job /
          job_status / job_cancel) with progress reporting.
          capture_node_preview grabs the node tile (with SD's live thumbnail)
          from the graph view without a graph.compute(); single node or a
          labeled multi-node contact sheet, using a graph-view crop (or a
          graph-owned preview widget when the SD version exposes one).
           RELIABILITY: one worker per client so job polls and cancels are not
           queued behind computes; command dispatch waits are bounded below the
           bridge timeout, timed-out queued calls are cancelled, request ids
           replay cached responses, SD_MCP_PORT selects the plugin port, and
           non-finite floats are replaced with explicit flags.
  3.9.0 - MATERIAL RUN CHECKPOINTS (used by the bridge's material-run
          workflow): checkpoint_package saves an immutable copy of a package
          via SDPackageMgr.saveCopyOfPackageAs without changing the working
          package; verify_checkpoint loads a checkpoint package, fingerprints
          the graph inside it and unloads it again; restore_package unloads
          the working package, loads the checkpoint and (optionally) saves it
          back over the working path; graph_fingerprint returns a canonical
          JSON description of a graph (nodes, properties, connections, graph
          inputs) for drift detection and restore verification.
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
import collections
import hmac
import math
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
def _env_int(name, default):
    try:
        return int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default


def _parse_ports(raw):
    """Parse SD_MCP_PORT ("9881" or "9881,9882") into a list of valid ports."""
    ports = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            port = int(part)
        except ValueError:
            continue
        if 1 <= port <= 65535 and port not in ports:
            ports.append(port)
    return ports


DEFAULT_PORTS     = _parse_ports(os.environ.get("SD_MCP_PORT")) or [9881]
PLUGIN_VERSION    = (3, 9, 0)
HEADER_SIZE       = 4
COMMAND_TIMEOUT   = _env_int("SD_MCP_COMMAND_TIMEOUT", 120)
COMMAND_TIMEOUT_MAX = _env_int("SD_MCP_COMMAND_TIMEOUT_MAX", 600)
RESPONSE_GRACE    = 10.0  # send the reply before the bridge's socket timeout
REQUEST_TIMEOUT   = 10    # clients send their request at once; never stall a worker on it
SEND_TIMEOUT      = 120   # response sends reset the socket timeout (large contact sheets)
CLIENT_TIMEOUT    = 130
ACCEPT_BACKLOG    = 5
MAX_CLIENT_WORKERS = 16
MAX_MSG_SIZE      = 100 * 1024 * 1024   # 100 MB
IDEMPOTENCY_CACHE_SIZE = 128
REQUEST_ID_MAX_LEN = 128


def _command_budget(requested):
    """Main-thread wait budget for one command.

    The bridge sends its own socket timeout with each request; the plugin stops
    waiting RESPONSE_GRACE seconds earlier so it can send a clean timeout error
    before the bridge gives up. Never wait longer than COMMAND_TIMEOUT_MAX.
    """
    if requested is None:
        return COMMAND_TIMEOUT
    try:
        requested = float(requested)
    except (TypeError, ValueError):
        return COMMAND_TIMEOUT
    if requested <= 0:
        return COMMAND_TIMEOUT
    budget = min(requested - RESPONSE_GRACE, COMMAND_TIMEOUT_MAX)
    if budget <= 0:
        budget = max(0.1, requested * 0.8)
    return budget


# Commands that only read/write in-memory job records; they never touch sd.api,
# so they stay responsive while the main thread is blocked by a compute.
_THREADSAFE_COMMANDS = frozenset({"job_status", "job_cancel"})

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
_RELOADABLE_SUBMODULES = ("recipes", "sd_documentation", "port_suggest", "texture_stats")
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
# The SD API can only load packages as user packages, so we load the ones the
# recipes need on demand, resolve by identifier, and after each command unload
# the user-package copies again (_release_library_packages). Packages a graph
# instances stay loaded as dependencies, the way SD's own library loads them.
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
    "brick_generator.sbs", "tile_generator.sbs",
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
class _DispatchCall:
    """One queued main-thread call, cancellable until it starts running.

    A timed-out dispatcher cancels the call before it is popped from the Qt
    queue, so a command that the caller has already given up on is not executed
    later. Once the main thread starts the call it cannot be interrupted.
    """

    def __init__(self, fn, args, kwargs):
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.result = None
        self.error = None
        self.done = threading.Event()
        self._lock = threading.Lock()
        self._started = False
        self._cancelled = False

    def start(self):
        with self._lock:
            if self._cancelled:
                return False
            self._started = True
            return True

    def cancel(self):
        """Cancel if still queued; False once the call has started."""
        with self._lock:
            if self._started:
                return False
            self._cancelled = True
            return True

    def run(self):
        if not self.start():
            self.done.set()
            return
        try:
            self.result = self.fn(*self.args, **self.kwargs)
        except Exception as e:
            self.error = e
        finally:
            self.done.set()


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

                def schedule(self, call):
                    self._queue.put(call)
                    self.invoke_signal.emit()

                def _execute(self):
                    try:
                        call = self._queue.get_nowait()
                    except queue.Empty:
                        return
                    call.run()

            return Invoker()
        except Exception as e:
            _log("Warning: _Invoker build failed: {}".format(e))
            return None


class MainThreadDispatcher:
    def dispatch(self, fn, *args, timeout=None, **kwargs):
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

        budget = COMMAND_TIMEOUT if timeout is None else float(timeout)
        call = _DispatchCall(fn, args, kwargs)
        invoker.schedule(call)

        if not call.done.wait(timeout=max(0.1, budget)):
            call.cancel()
            return None, TimeoutError(
                "Main thread dispatch timed out after {}s".format(budget))

        return call.result, call.error


_dispatcher = MainThreadDispatcher()


def _run_on_main(fn, *args, timeout=None, **kwargs):
    result, exc = _dispatcher.dispatch(fn, *args, timeout=timeout, **kwargs)
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


def _send_framed(sock, data, timeout=SEND_TIMEOUT):
    """Send one framed payload, resetting the socket timeout first.

    _recv_framed leaves the socket at REQUEST_TIMEOUT; a large response (up to
    MAX_MSG_SIZE) could exceed that while draining into the kernel buffer.
    """
    sock.settimeout(timeout)
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
        # Optional shared secret: when SD_MCP_TOKEN is set in SD's environment,
        # every command must carry a matching "token" field or it is refused
        # before dispatch. The bridge sends it when started with --token.
        self.token = os.environ.get("SD_MCP_TOKEN")
        # Per-client workers keep job_status/job_cancel (and other clients)
        # serviceable while one command waits on the SD main thread.
        self._workers = set()
        self._clients = set()
        self._workers_lock = threading.Lock()
        self._client_slots = threading.BoundedSemaphore(MAX_CLIENT_WORKERS)
        # Request idempotency: a retried command with the same request_id gets
        # the first response instead of running twice.
        self._response_cache = collections.OrderedDict()
        self._inflight = {}
        self._idempotency_lock = threading.Lock()

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
        with self._workers_lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                client.close()
            except OSError:
                pass
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

                if not self._client_slots.acquire(blocking=False):
                    try:
                        _send_framed(client, json.dumps({
                            "status": "error",
                            "message": "Server busy: too many concurrent clients."
                        }).encode("utf-8"), timeout=5)
                    except Exception:
                        pass
                    try:
                        client.close()
                    except Exception:
                        pass
                    continue

                worker = threading.Thread(
                    target=self._client_worker, args=(client, addr, port),
                    daemon=True, name="SD-MCP-Client")
                with self._workers_lock:
                    self._workers.add(worker)
                    self._clients.add(client)
                try:
                    worker.start()
                except Exception as e:
                    _log("Failed to start client worker: {}".format(e))
                    with self._workers_lock:
                        self._workers.discard(worker)
                        self._clients.discard(client)
                    self._client_slots.release()
                    try:
                        client.close()
                    except Exception:
                        pass

    def _client_worker(self, client, addr, port):
        try:
            self._handle_client(client, addr, port)
        except Exception as e:
            _log("Unexpected error in client worker: {}".format(e))
        finally:
            with self._workers_lock:
                self._workers.discard(threading.current_thread())
                self._clients.discard(client)
            self._client_slots.release()

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

    def _claim_request(self, request_id):
        """Return (cached_response, wait_event, is_owner) for a request id."""
        with self._idempotency_lock:
            if request_id in self._response_cache:
                return self._response_cache[request_id], None, False
            event = self._inflight.get(request_id)
            if event is not None:
                return None, event, False
            self._inflight[request_id] = threading.Event()
            return None, None, True

    def _finish_request(self, request_id, response):
        with self._idempotency_lock:
            self._response_cache[request_id] = response
            while len(self._response_cache) > IDEMPOTENCY_CACHE_SIZE:
                self._response_cache.popitem(last=False)
            event = self._inflight.pop(request_id, None)
        if event is not None:
            event.set()

    def _execute_safe(self, command):
        cmd_type = command.get("type")

        # Authenticate before touching the idempotency cache, so unauthorized
        # callers can never replay or poison another caller's request id.
        if self.token:
            provided = command.get("token")
            try:
                authorized = (isinstance(provided, str) and hmac.compare_digest(
                    provided.encode("utf-8"), self.token.encode("utf-8")))
            except Exception:
                authorized = False
            if not authorized:
                _log("Rejected {}: token missing or incorrect.".format(cmd_type))
                return {"status": "error",
                        "message": "Unauthorized: token missing or incorrect. "
                                   "The bridge must be started with --token "
                                   "matching the SD_MCP_TOKEN set for SD."}

        request_id = command.get("request_id")
        rid = None
        if request_id not in (None, ""):
            rid = str(request_id)
            if len(rid) > REQUEST_ID_MAX_LEN:
                rid = None

        owner = False
        if rid:
            cached, event, owner = self._claim_request(rid)
            if cached is not None:
                _log("Idempotent replay: {} ({})".format(cmd_type, rid))
                return cached
            if event is not None:
                budget = _command_budget(command.get("timeout"))
                if not event.wait(timeout=max(0.1, budget)):
                    return {"status": "error",
                            "message": "Duplicate request '{}' is still in progress; "
                                       "retry with a new request_id.".format(rid)}
                with self._idempotency_lock:
                    cached = self._response_cache.get(rid)
                if cached is not None:
                    return cached
                return {"status": "error",
                        "message": "Duplicate request '{}' failed before producing "
                                   "a response.".format(rid)}

        response = None
        try:
            params = command.get("params", {})
            budget = _command_budget(command.get("timeout"))
            label = "{} ({})".format(cmd_type, rid) if rid else cmd_type
            _log("Executing: {}".format(label))
            if cmd_type in _THREADSAFE_COMMANDS:
                result = self._handler.dispatch(cmd_type, params)
            else:
                result = _run_on_main(self._handler.dispatch, cmd_type, params,
                                      timeout=budget)
            _log("Done: {}".format(label))

            flags = []
            result = _mark_nonfinite(result, flags)
            response = {"status": "success",
                        "result": result if result is not None else {}}
            if flags:
                response["non_finite_values"] = flags
            return response
        except Exception as e:
            _log("Error in {}: {}".format(cmd_type or "?", e))
            try:
                traceback.print_exc()
            except Exception:
                pass
            response = {"status": "error", "message": str(e)}
            return response
        finally:
            if owner:
                self._finish_request(
                    rid, response if response is not None else
                    {"status": "error", "message": "Request did not complete."})


def _mark_nonfinite(obj, flags, path="$"):
    """Replace non-finite floats with None and record their paths.

    JSON has no NaN/Infinity. Silently substituting values (or emitting
    non-standard literals) hides data problems, so the response carries an
    explicit `non_finite_values` list alongside the sanitized result.
    """
    if isinstance(obj, float):
        if math.isnan(obj):
            flags.append({"path": path, "value": "nan"})
            return None
        if math.isinf(obj):
            flags.append({"path": path, "value": "inf" if obj > 0 else "-inf"})
            return None
        return obj
    if isinstance(obj, dict):
        return {key: _mark_nonfinite(value, flags, "{}.{}".format(path, key))
                for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_mark_nonfinite(value, flags, "{}[{}]".format(path, idx))
                for idx, value in enumerate(obj)]
    return obj


def _json_safe(obj):
    """json.dumps fallback for values JSON cannot encode.

    Non-finite floats are normally handled by _mark_nonfinite; if one reaches
    this fallback it becomes an explicit marker rather than a silent value.
    """
    if isinstance(obj, float):
        if obj != obj:
            return {"__non_finite__": "nan"}
        if obj == float('inf'):
            return {"__non_finite__": "inf"}
        if obj == float('-inf'):
            return {"__non_finite__": "-inf"}
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
            "set_parameters":         self.set_parameters,
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
            "capture_2d_view":        self.capture_2d_view,
            "capture_node_preview":   self.capture_node_preview,
            "get_map_stats":          self.get_map_stats,
            "inspect_outputs":        self.inspect_outputs,
            "start_job":              self.start_job,
            "job_status":             self.job_status,
            "job_cancel":             self.job_cancel,
            "send_to_3d_view":        self.send_to_3d_view,
            "send_to_3d_view_status": self.send_to_3d_view_status,
            # ── Material-run checkpoints ──
            "graph_fingerprint":      self.graph_fingerprint,
            "checkpoint_package":     self.checkpoint_package,
            "verify_checkpoint":      self.verify_checkpoint,
            "restore_package":        self.restore_package,
            # ── Documentation ──
            "list_documentation":     self.list_documentation,
            # ── Development ──
            "reload_plugin":          self.reload_plugin,
        }
        # Library node URL cache (populated lazily, session-lifetime)
        self._lib_url_cache = {}  # lower-case identifier -> url string
        # Long-job registry: job_id -> (public job dict, stage generator)
        self._jobs = {}
        self._job_seq = 0
        # Guards the registry and the cancelled flag; job_status/job_cancel run
        # on client worker threads while stages run on the main thread.
        self._jobs_lock = threading.Lock()

    def dispatch(self, cmd_type, params):
        handler = self.HANDLERS.get(cmd_type)
        if not handler:
            raise ValueError("Unknown command: '{}'. Available: {}".format(
                cmd_type, sorted(self.HANDLERS.keys())))
        # Job control reads/writes in-memory records only. Running it directly
        # on the worker thread keeps it responsive while sd.api blocks the main
        # thread, and avoids the library-package release (an sd.api call).
        if cmd_type in _THREADSAFE_COMMANDS:
            return handler(**params)
        try:
            return handler(**params)
        finally:
            self._release_library_packages()

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
        """Find a library node URL by identifier (case-insensitive). Cached.

        Exact identifier matches always win over substring matches. If several
        resources match equally (e.g. the same short name in two loaded
        packages, or a broad substring), raise instead of silently returning
        whichever package happens to be enumerated first.
        """
        key = str(keyword).strip().lower()
        if not key:
            return None
        if key in self._lib_url_cache:
            return self._lib_url_cache[key]

        self._ensure_library_loaded()
        exact, partial = [], []
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
                                rkey = rid.lower()
                                if key != rkey and key not in rkey:
                                    continue
                                url = None
                                try:
                                    url = res.getUrl()
                                except Exception:
                                    pass
                                if not url:
                                    continue
                                entry = (rid, url, fp)
                                if rkey == key:
                                    exact.append(entry)
                                else:
                                    partial.append(entry)
                            except Exception:
                                pass
                        break
                    except Exception:
                        continue
            except Exception:
                pass

        matches = exact or partial
        if not matches:
            return None
        # The same resource can be reported more than once; dedupe by URL.
        unique = collections.OrderedDict()
        for rid, url, fp in matches:
            unique.setdefault(url, (rid, url, fp))
        if len(unique) > 1:
            described = ", ".join(
                "'{}' ({})".format(rid, os.path.basename(fp or "?"))
                for rid, _url, fp in unique.values())
            raise ValueError(
                "Library identifier '{}' is ambiguous; {} matches: {}. "
                "Use a more specific library_keyword or pass a pkg:/// "
                "resource_url instead.".format(keyword, len(unique), described))

        rid, url, _fp = next(iter(unique.values()))
        self._lib_url_cache[key] = url
        self._lib_url_cache[rid.lower()] = url
        return url

    def _load_library_file(self, lib_dir, fname, loaded_paths):
        """Load one SD library .sbs unless it is already loaded. True if loaded now."""
        path = os.path.join(lib_dir, fname)
        if not os.path.isfile(path):
            return False
        if os.path.normcase(os.path.normpath(path)) in loaded_paths:
            return False
        try:
            self._pkg_mgr().loadUserPackage(path)
            loaded_paths.add(os.path.normcase(os.path.normpath(path)))
            return True
        except Exception as e:
            _log("Library load failed for {}: {}".format(fname, e))
            return False

    def _loaded_package_paths(self):
        paths = set()
        for pkg in list(self._pkg_mgr().getPackages()):
            try:
                fp = pkg.getFilePath()
                if fp:
                    paths.add(os.path.normcase(os.path.normpath(fp)))
            except Exception:
                pass
        return paths

    def _ensure_library_loaded(self, identifier=None):
        """Load SD library .sbs packages so pkg:/// URLs can resolve.

        With an identifier, first try just '<identifier>.sbs' (e.g.
        brick_generator); returns True if that file exists. Otherwise load
        every package in _LIBRARY_SOURCE_FILES that is not loaded yet.
        The user-package copies are released again after the command."""
        lib_dir = _find_sd_library_dir()
        if not lib_dir:
            _log("Library dir not found - library nodes unavailable")
            return False
        loaded_paths = self._loaded_package_paths()
        if identifier:
            fname = os.path.basename(identifier) + ".sbs"
            if os.path.isfile(os.path.join(lib_dir, fname)):
                self._load_library_file(lib_dir, fname, loaded_paths)
                return True
            return False
        loaded = 0
        for fname in _LIBRARY_SOURCE_FILES:
            if self._load_library_file(lib_dir, fname, loaded_paths):
                loaded += 1
        if loaded:
            _log("Loaded {} library packages from {}".format(loaded, lib_dir))
        return False

    def _release_library_packages(self):
        """Unload SD library packages that are open as user packages.

        Those a graph instances stay loaded as its dependencies (not user
        packages); unused ones are dropped and reloaded on demand. Modified
        packages are left alone."""
        try:
            mgr = self._pkg_mgr()
            for pkg in list(mgr.getUserPackages()):
                try:
                    if _is_builtin_package(pkg.getFilePath()) and not pkg.isModified():
                        mgr.unloadUserPackage(pkg)
                except Exception as e:
                    _log("Library release failed: {}".format(e))
        except Exception as e:
            _log("Library release failed: {}".format(e))

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
        ident = url.split("pkg:///")[-1].split("?")[0].strip("/")
        # 'pkg:///brick_generator' -> load just brick_generator.sbs first
        if not (ident and self._ensure_library_loaded(ident)):
            self._ensure_library_loaded()
        res = self._find_resource_by_url(url)
        if res is not None:
            return res, url
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

        # Property type maps: id -> SDType id string
        input_type_map, annot_type_map = self._node_property_ids(node)

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

        if not out_ids:
            raise RuntimeError(
                "Cannot validate output port '{}' on node {}: SD reported no "
                "output ports. Refusing to connect unvalidated.".format(
                    from_out, self._node_label(from_node)))
        if not in_ids:
            raise RuntimeError(
                "Cannot validate input port '{}' on node {}: SD reported no "
                "input ports. Refusing to connect unvalidated.".format(
                    to_in, self._node_label(to_node)))

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
    # ── MATERIAL-RUN CHECKPOINTS ────────────────────────────────────────
    # ═══════════════════════════════════════════════════════════════════════
    # These commands support the bridge's versioned material-iteration
    # workflow. A checkpoint is an *immutable copy* of the whole package on
    # disk; restore unloads the working package, loads the checkpoint and
    # optionally saves it back over the working path. The checkpoint file is
    # never modified, and a graph fingerprint (canonical node/property/
    # connection description) lets the bridge verify restores and detect
    # drift in the working graph.

    def _find_graph_in_package(self, pkg, identifier):
        try:
            for res in list(pkg.getChildrenResources(False)):
                try:
                    if res.getIdentifier() == identifier:
                        return res
                except Exception:
                    continue
        except Exception:
            return None
        return None

    def _fingerprint_graph(self, graph):
        """Canonical, JSON-safe description of a comp graph.

        Node ids are included so the bridge can report id drift, but its
        id-independent structure hash is what restore verification compares.
        Input/annotation values are serialized with the same converter used
        by get_node_info, so fingerprints are stable across calls.
        """
        output_ids = set()
        try:
            arr = graph.getOutputNodes()
            for i in range(arr.getSize()):
                try:
                    output_ids.add(arr.getItem(i).getIdentifier())
                except Exception:
                    pass
        except Exception:
            pass

        nodes = list(graph.getNodes())
        node_entries = []
        for node in nodes:
            try:
                nid = node.getIdentifier()
            except Exception:
                continue
            entry = {
                "id": nid,
                "definition": self._get_node_def_id(node),
                "position": self._get_node_pos(node),
                "is_output": nid in output_ids,
                "is_library": self._is_instance_node(node),
                "inputs": [],
                "annotations": [],
            }
            try:
                for prop in list(node.getProperties(SDPropertyCategory.Input)):
                    pid = prop.getId()
                    info = {"id": pid}
                    try:
                        val = node.getPropertyValue(prop)
                        if val is not None:
                            info["value"] = self._serialize_sd_value(val)
                    except Exception:
                        pass
                    try:
                        conns = node.getPropertyConnections(prop)
                        info["connected"] = bool(conns is not None and list(conns))
                    except Exception:
                        pass
                    entry["inputs"].append(info)
            except Exception:
                pass
            try:
                for prop in list(node.getProperties(SDPropertyCategory.Annotation)):
                    pid = prop.getId()
                    info = {"id": pid}
                    try:
                        val = node.getPropertyValue(prop)
                        if val is not None:
                            info["value"] = self._serialize_sd_value(val)
                    except Exception:
                        pass
                    entry["annotations"].append(info)
            except Exception:
                pass
            node_entries.append(entry)

        connections = []
        for node in nodes:
            try:
                nid = node.getIdentifier()
            except Exception:
                continue
            try:
                for prop in list(node.getProperties(SDPropertyCategory.Input)):
                    pid = prop.getId()
                    conns = node.getPropertyConnections(prop)
                    if conns is None:
                        continue
                    for conn in list(conns):
                        try:
                            sn = conn.getInputPropertyNode()
                            sp = conn.getInputProperty()
                            if sn and sp:
                                connections.append({
                                    "from_node": sn.getIdentifier(),
                                    "from_output": sp.getId(),
                                    "to_node": nid,
                                    "to_input": pid,
                                })
                        except Exception:
                            pass
            except Exception:
                pass

        graph_inputs = []
        try:
            for prop in list(graph.getProperties(SDPropertyCategory.Input)):
                pid = prop.getId()
                info = {"id": pid}
                try:
                    val = graph.getPropertyValue(prop)
                    if val is not None:
                        info["value"] = self._serialize_sd_value(val)
                except Exception:
                    pass
                graph_inputs.append(info)
        except Exception:
            pass

        return {
            "graph_identifier": graph.getIdentifier(),
            "nodes": node_entries,
            "connections": connections,
            "graph_inputs": graph_inputs,
            "output_node_ids": sorted(output_ids),
        }

    def graph_fingerprint(self, graph_identifier=None):
        """Return a canonical JSON fingerprint of a graph (no compute).

        Used by the bridge's material-run workflow for drift detection, and
        by checkpoint verification to prove a saved copy matches the working
        graph. Includes node ids, definitions, positions, property values,
        connection list and graph inputs.
        """
        graph = self._resolve_graph(graph_identifier)
        return self._fingerprint_graph(graph)

    def checkpoint_package(self, package_index=0, package_path=None,
                           checkpoint_path=None, graph_identifier=None):
        """Save an immutable copy of a user package to `checkpoint_path`.

        SDPackageMgr.saveCopyOfPackageAs writes the copy without changing
        the working package's own file path, so the working session is
        untouched. Pass graph_identifier to also embed a fingerprint of that
        graph (computed before the copy) in the response.

        The package must have been saved at least once; a never-saved
        untitled package cannot be copied (save it first with save_package).
        """
        if not checkpoint_path:
            raise ValueError("checkpoint_path is required.")
        checkpoint_path = os.path.abspath(checkpoint_path)
        pkg = self._resolve_package(package_index, package_path)
        working_path = pkg.getFilePath()
        if working_path and os.path.normcase(os.path.abspath(working_path)) == \
                os.path.normcase(checkpoint_path):
            raise ValueError(
                "checkpoint_path must differ from the working package path "
                "('{}').".format(working_path))
        parent = os.path.dirname(checkpoint_path)
        if parent and not os.path.exists(parent):
            try:
                os.makedirs(parent, exist_ok=True)
            except Exception as e:
                raise ValueError("Cannot create checkpoint directory '{}': "
                                 "{}".format(parent, e))

        fingerprint = None
        if graph_identifier:
            graph = self._resolve_graph(graph_identifier)
            fingerprint = self._fingerprint_graph(graph)

        try:
            self._pkg_mgr().saveCopyOfPackageAs(pkg, checkpoint_path)
        except Exception as e:
            if not working_path:
                raise RuntimeError(
                    "Could not copy an unsaved package to '{}'. Save the "
                    "package first with save_package. ({})".format(
                        checkpoint_path, e))
            raise
        if not os.path.isfile(checkpoint_path):
            raise RuntimeError(
                "saveCopyOfPackageAs reported success but '{}' does not "
                "exist.".format(checkpoint_path))

        return {
            "checkpoint_path": checkpoint_path,
            "package_path": working_path,
            "graph_identifier": graph_identifier,
            "fingerprint": fingerprint,
            "size": os.path.getsize(checkpoint_path),
        }

    def verify_checkpoint(self, checkpoint_path, graph_identifier):
        """Load a checkpoint package, fingerprint a graph inside it, unload.

        The checkpoint file is loaded read-only as a temporary user package
        (unloaded again afterwards, unless it was already loaded), so the
        bridge can prove that a checkpoint file really contains the expected
        graph state before relying on it for restore.
        """
        if not graph_identifier:
            raise ValueError("graph_identifier is required.")
        cp = os.path.abspath(checkpoint_path)
        if not os.path.isfile(cp):
            raise ValueError("Checkpoint file not found: '{}'".format(cp))
        mgr = self._pkg_mgr()
        already = None
        for pkg in list(mgr.getPackages()):
            try:
                fp = pkg.getFilePath()
            except Exception:
                continue
            if fp and os.path.normcase(os.path.abspath(fp)) == os.path.normcase(cp):
                already = pkg
                break
        loaded = already if already is not None else mgr.loadUserPackage(cp)
        try:
            graph = self._find_graph_in_package(loaded, graph_identifier)
            if graph is None:
                raise RuntimeError(
                    "The checkpoint '{}' does not contain graph '{}'.".format(
                        cp, graph_identifier))
            fingerprint = self._fingerprint_graph(graph)
        finally:
            if already is None:
                try:
                    mgr.unloadUserPackage(loaded)
                except Exception:
                    pass
        return {
            "checkpoint_path": cp,
            "graph_identifier": graph_identifier,
            "fingerprint": fingerprint,
            "already_loaded": already is not None,
        }

    def restore_package(self, checkpoint_path, package_index=0,
                        package_path=None, save_back=True,
                        graph_identifier=None):
        """Restore the working package from a checkpoint file.

        Unloads the current working user package, loads the checkpoint as a
        new user package and, when the working package had a file path and
        save_back is True, saves the loaded checkpoint back over that path.
        The checkpoint file itself is never modified. Unsaved changes in the
        working package are discarded; the bridge's material-run controller
        verifies fingerprints before calling this, so use it directly only
        when the working state is disposable.
        """
        mgr = self._pkg_mgr()
        cp = os.path.abspath(checkpoint_path)
        if not os.path.isfile(cp):
            raise ValueError("Checkpoint file not found: '{}'".format(cp))
        working = self._resolve_package(package_index, package_path)
        working_path = working.getFilePath()
        if working_path and os.path.normcase(os.path.abspath(working_path)) == \
                os.path.normcase(cp):
            raise ValueError(
                "checkpoint_path is the working package path; refusing to "
                "restore a package from itself.")
        for pkg in list(mgr.getUserPackages()):
            try:
                fp = pkg.getFilePath()
            except Exception:
                continue
            if fp and os.path.normcase(os.path.abspath(fp)) == os.path.normcase(cp):
                raise ValueError(
                    "The checkpoint '{}' is already loaded as a package; "
                    "unload it in SD before restoring.".format(cp))

        try:
            mgr.unloadUserPackage(working)
        except Exception as e:
            raise RuntimeError(
                "Could not unload the working package ({}). If SD is showing "
                "a save prompt, resolve it manually and retry. ({})".format(
                    working_path or "<untitled>", e))
        loaded = mgr.loadUserPackage(cp)
        restored_to = cp
        if save_back and working_path:
            mgr.savePackageAs(loaded, working_path)
            restored_to = working_path
        fingerprint = None
        if graph_identifier:
            graph = self._find_graph_in_package(loaded, graph_identifier)
            if graph is None:
                raise RuntimeError(
                    "The restored package does not contain graph "
                    "'{}'.".format(graph_identifier))
            fingerprint = self._fingerprint_graph(graph)
        return {
            "restored_from": cp,
            "restored_to": restored_to,
            "package_path": loaded.getFilePath(),
            "graph_identifier": graph_identifier,
            "fingerprint": fingerprint,
        }

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
    # ── 2D VIEW CAPTURE ─────────────────────────────────────────────────
    # ═══════════════════════════════════════════════════════════════════════

    def _map_label(self, node):
        from sd.api.sdproperty import SDPropertyCategory
        try:
            value = node.getPropertyValueFromId(
                "label", SDPropertyCategory.Annotation)
            return (value.get() or "") if value else ""
        except Exception:
            return ""

    def _find_map_node(self, graph, node_id=None, output_usage=None):
        """Resolve either a node id or a graph output label to an SD node."""
        if bool(node_id) == bool(output_usage):
            raise ValueError("Pass exactly one of node_id or output_usage.")
        if node_id:
            node = self._find_node(graph, node_id)
            return node, self._map_label(node)

        def _norm(value):
            return "".join(c for c in (value or "").lower() if c.isalnum())

        aliases = {"ao": "ambientocclusion"}
        want = aliases.get(_norm(output_usage), _norm(output_usage))

        try:
            array = graph.getOutputNodes()
            nodes = [array.getItem(i) for i in range(array.getSize())]
        except Exception:
            nodes = [n for n in graph.getNodes()
                     if self._get_node_def_id(n) == "sbs::compositing::output"]
        matches = [(self._map_label(n), n) for n in nodes
                   if aliases.get(_norm(self._map_label(n)),
                                  _norm(self._map_label(n))) == want]
        if not matches:
            raise ValueError(
                "No output node matches usage '{}'. Available outputs: {}."
                .format(output_usage, [self._map_label(n) for n in nodes]))
        return matches[0][1], matches[0][0]

    def _map_texture_outputs(self, node):
        from sd.api.sdproperty import SDPropertyCategory
        props = []
        try:
            props = list(node.getProperties(SDPropertyCategory.Output))
        except Exception:
            pass
        if not props:
            try:
                props = list(node.getDefinition().getProperties(
                    SDPropertyCategory.Output))
            except Exception:
                pass
        outputs = []
        for prop in props:
            try:
                prop_type = prop.getType()
                if (prop_type is None
                        or "Texture" not in (prop_type.getClassName() or "")):
                    continue
                value = node.getPropertyValue(prop)
                texture = value.get() if value is not None else None
                if texture is not None:
                    outputs.append((prop.getId(), texture))
            except Exception:
                continue
        return outputs

    def _select_map_texture(self, node, port_id=None):
        outputs = self._map_texture_outputs(node)
        if not outputs:
            raise ValueError(
                "Node '{}' has no computed texture output. Ensure it is "
                "connected and evaluated.".format(node.getIdentifier()))
        if port_id:
            for pid, texture in outputs:
                if pid == port_id:
                    return pid, texture
            raise ValueError(
                "Port '{}' has no computed texture on node '{}'. Available "
                "ports: {}.".format(port_id, node.getIdentifier(),
                                    [pid for pid, _ in outputs]))
        if len(outputs) == 1:
            return outputs[0]
        for preferred in ("output", "unique_filter_output"):
            for pid, texture in outputs:
                if pid == preferred:
                    return pid, texture
        raise ValueError(
            "Node '{}' has several texture outputs {}; pass port_id."
            .format(node.getIdentifier(), [pid for pid, _ in outputs]))

    def _map_texture_to_qimage(self, texture):
        from PySide6.QtGui import QImage
        try:
            import shiboken6 as shiboken
        except Exception:
            try:
                import shiboken2 as shiboken
            except Exception:
                shiboken = None
        if shiboken is None:
            raise RuntimeError("shiboken not available; cannot convert SDTexture.")
        image = QImage()
        self._ui_mgr().convertSDTextureToQImage(
            texture, shiboken.getCppPointer(image)[0])
        if image.isNull():
            raise RuntimeError("SDTexture -> QImage conversion produced an empty image.")
        return image

    def _default_output_path(self, prefix):
        """PNG path under %TEMP%/sd_mcp_preview (created on save)."""
        out_dir = os.path.join(os.environ.get("TEMP", "."), "sd_mcp_preview")
        return os.path.join(
            out_dir, "{}_{}.png".format(prefix, time.strftime("%Y%m%d_%H%M%S")))

    @staticmethod
    def _ensure_output_dir(path):
        path = os.path.abspath(path)
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        return path

    @staticmethod
    def _downscale_preview(image, max_size):
        """Scale a preview QImage so its longest side is <= max_size (0 = native)."""
        if max_size and int(max_size) > 0 and \
                max(image.width(), image.height()) > int(max_size):
            from PySide6.QtCore import Qt
            return image.scaled(int(max_size), int(max_size),
                                Qt.KeepAspectRatio, Qt.SmoothTransformation)
        return image

    def _render_contact_sheet(self, tiles, output_path=None,
                              prefix="2dview_contact_sheet"):
        """Draw labeled preview tiles into a two-column grid image.

        tiles: [{'image': QImage, 'usage', 'label', 'node', 'port_id',
                 'source_width', 'source_height'}, ...]
        Returns {path, width, height, columns, rows, items}.
        """
        from PySide6.QtGui import QColor, QFont, QImage, QPainter
        columns = min(2, len(tiles))
        rows = (len(tiles) + columns - 1) // columns
        cell_width = max(tile["image"].width() for tile in tiles)
        cell_height = max(tile["image"].height() for tile in tiles)
        label_height = 30
        sheet = QImage(columns * cell_width,
                       rows * (cell_height + label_height),
                       QImage.Format_ARGB32)
        sheet.fill(QColor(34, 36, 40))
        painter = QPainter(sheet)
        painter.setPen(QColor(245, 245, 245))
        painter.setFont(QFont("Arial", 11, QFont.Bold))
        item_info = []
        for index, tile in enumerate(tiles):
            col, row = index % columns, index // columns
            x, y = col * cell_width, row * (cell_height + label_height)
            painter.drawText(x + 8, y + 21, tile["label"])
            image = tile["image"]
            ix = x + (cell_width - image.width()) // 2
            iy = y + label_height + (cell_height - image.height()) // 2
            painter.drawImage(ix, iy, image)
            item_info.append({k: tile[k] for k in (
                "usage", "label", "node", "port_id",
                "source_width", "source_height")})
        painter.end()
        if not output_path:
            output_path = self._default_output_path(prefix)
        output_path = self._ensure_output_dir(output_path)
        if not sheet.save(output_path):
            raise RuntimeError(
                "Failed to save image to '{}'.".format(output_path))
        return {"path": output_path, "width": sheet.width(),
                "height": sheet.height(), "columns": columns,
                "rows": rows, "items": item_info}

    def _texture_stats(self, texture, histogram_bins=16):
        """Per-channel min/max/mean + histogram from one computed SDTexture.

        Copies the raw pixel buffer and delegates to texture_stats (pure
        Python, unit-tested outside SD). Shared by get_map_stats and
        inspect_outputs; see that module for the exact semantics.
        """
        import ctypes
        from . import texture_stats

        bins = int(histogram_bins)
        if bins < 1 or bins > 512:
            raise ValueError("histogram_bins must be between 1 and 512.")

        pixel_format = texture.getPixelFormat()
        format_name = pixel_format.name if pixel_format is not None else "Unknown"
        spec = texture_stats.FORMATS.get(format_name)
        if spec is None:
            raise ValueError(
                "Unsupported SDTexture pixel format '{}'."
                .format(format_name))
        expected_bytes = spec[1] * len(spec[4])
        dimensions = texture.getSize()
        if dimensions is None:
            raise RuntimeError("SDTexture did not report its dimensions.")
        width, height = int(dimensions.x), int(dimensions.y)
        bytes_per_pixel = int(texture.getBytesPerPixel())
        if (width <= 0 or height <= 0
                or bytes_per_pixel != expected_bytes):
            raise RuntimeError(
                "Unexpected SDTexture layout: {}x{}, {} bytes per pixel "
                "for {}.".format(width, height, bytes_per_pixel, format_name))
        row_size = width * bytes_per_pixel
        row_pitch = int(texture.getRowPitch())
        address = int(texture.getPixelBufferAddress() or 0)
        if not address or not row_pitch:
            raise RuntimeError("SDTexture pixel buffer is unavailable.")
        if abs(row_pitch) < row_size:
            raise RuntimeError(
                "SDTexture row pitch {} is smaller than its {}-byte row."
                .format(row_pitch, row_size))

        first_row = address + min(0, (height - 1) * row_pitch)
        span = (height - 1) * abs(row_pitch) + row_size
        buffer = ctypes.string_at(first_row, span)
        return texture_stats.stats_from_buffer(
            buffer, width, height, row_pitch, format_name, bins)

    def capture_2d_view(self, graph_identifier=None, node_id=None,
                        output_usage=None, output_usages=None,
                        port_id=None, max_size=1024,
                        output_path=None, wait_ms=600):
        """Capture one map, a labeled output contact sheet, or the 2D view widget.

        Runs the staged generator to completion synchronously; start_job
        ("capture_2d_view") runs the same stages step-by-step so progress can
        be polled and the job cancelled between stages.

        Capture modes:
        - Texture mode (node_id or output_usage given): calls graph.compute()
          and saves the target node's output texture through the SD API - exact
          pixels at native resolution, no UI interaction (works while SD is
          occluded or on another tab).
          - node_id: any node of the graph (undefined port picks the only
            texture output).
          - output_usage: selects the OUTPUT node whose label matches, e.g.
            'height', 'normal', 'Base Color', 'ao', 'ambientOcclusion'.
          - output_usages: renders those output labels in one contact sheet.
            This cannot be combined with node_id or output_usage.
          - port_id: output port to use when the node has several texture
            outputs (e.g. 'output_1', 'unique_filter_output').
          - max_size: downscale so the longest side <= max_size (0 = native).
        - Widget mode (neither node_id nor output_usage): grabs the visible
          2D view widget - exactly what SD shows (selection preview, tiling,
          zoom, checkerboard). If graph_identifier is given, that graph's tab
          is opened first.

        Returns {path, width, height, method, ...}.
        """
        job = {"stage": "resolve_graph", "progress": 0.0,
               "stages_total": 0, "result": None}
        self._drive_sync(self._gen_capture_2d_view(job, {
            "graph_identifier": graph_identifier,
            "node_id": node_id,
            "output_usage": output_usage,
            "output_usages": output_usages,
            "port_id": port_id,
            "max_size": max_size,
            "output_path": output_path,
            "wait_ms": wait_ms,
        }), job)
        return job["result"]

    def _gen_capture_2d_view(self, job, params):
        graph_identifier = params.get("graph_identifier")
        node_id          = params.get("node_id")
        output_usage     = params.get("output_usage")
        output_usages    = params.get("output_usages")
        port_id          = params.get("port_id")
        max_size         = params.get("max_size", 1024)
        output_path      = params.get("output_path")
        wait_ms          = params.get("wait_ms", 600)

        graph = self._resolve_graph(graph_identifier)
        gid = graph.getIdentifier()

        # ── texture mode: render one map or a contact sheet ──
        if output_usages is not None:
            if node_id or output_usage:
                raise ValueError(
                    "output_usages cannot be combined with node_id or output_usage.")
            usages = [str(usage) for usage in output_usages if str(usage).strip()]
            if not usages or len(usages) > 16:
                raise ValueError("Pass between 1 and 16 output_usages.")
            job["stages_total"] = 2 + len(usages)
            job["stage"] = "compute"
            yield
            try:
                graph.compute()
            except Exception as e:
                raise RuntimeError(
                    "graph.compute() failed for '{}': {}".format(gid, e))

            tiles = []
            for index, usage in enumerate(usages):
                job["stage"] = "read_output"
                job["progress"] = round(0.1 + 0.7 * index / len(usages), 3)
                yield
                node, label = self._find_map_node(graph, output_usage=usage)
                pid, texture = self._select_map_texture(node)
                image = self._map_texture_to_qimage(texture)
                src_w, src_h = image.width(), image.height()
                tiles.append({"usage": usage, "label": label or usage,
                              "node": node.getIdentifier(), "port_id": pid,
                              "source_width": src_w, "source_height": src_h,
                              "image": self._downscale_preview(image, max_size)})

            job["stage"] = "render_sheet"
            job["progress"] = 0.9
            yield
            sheet = self._render_contact_sheet(
                tiles, output_path, prefix="2dview_contact_sheet")
            job["stage"] = "done"
            job["progress"] = 1.0
            job["result"] = {"path": sheet["path"], "width": sheet["width"],
                             "height": sheet["height"], "method": "contact_sheet",
                             "graph": gid, "columns": sheet["columns"],
                             "rows": sheet["rows"], "items": sheet["items"]}
            return

        if node_id or output_usage:
            node, label = self._find_map_node(
                graph, node_id=node_id, output_usage=output_usage)
            job["stages_total"] = 4   # resolve, compute, convert, save
            job["stage"] = "compute"
            yield
            try:
                graph.compute()
            except Exception as e:
                raise RuntimeError(
                    "graph.compute() failed for '{}': {}".format(gid, e))
            pid, tex = self._select_map_texture(node, port_id)

            job["stage"] = "convert"
            job["progress"] = 0.8
            yield
            img = self._map_texture_to_qimage(tex)
            src_w, src_h = img.width(), img.height()
            img = self._downscale_preview(img, max_size)

            if not output_path:
                safe = re.sub(r'[^A-Za-z0-9]+', '_', label).strip('_').lower()
                output_path = self._default_output_path(
                    "2dview_" + safe if safe else "2dview")
            output_path = self._ensure_output_dir(output_path)
            job["stage"] = "save"
            job["progress"] = 0.95
            yield
            if not img.save(output_path):
                raise RuntimeError(
                    "Failed to save image to '{}'.".format(output_path))
            job["stage"] = "done"
            job["progress"] = 1.0
            job["result"] = {
                "path":          output_path,
                "width":         img.width(),
                "height":        img.height(),
                "method":        "texture",
                "source_width":  src_w,
                "source_height": src_h,
                "graph":         gid,
                "node":          node.getIdentifier(),
                "port_id":       pid,
                "label":         label,
            }
            return

        # ── widget mode: grab what the 2D view currently shows ──
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import QTimer, QEventLoop

        app = QApplication.instance()
        if app is None:
            raise RuntimeError("Qt application not available.")
        job["stages_total"] = 4   # resolve, open/wait, grab, save

        def _pump(ms):
            loop = QEventLoop()
            QTimer.singleShot(int(ms), loop.quit)
            loop.exec()

        if graph_identifier:
            job["stage"] = "open_editor"
            job["progress"] = 0.1
            yield
            try:
                self._ui_mgr().openResourceInEditor(graph)
            except Exception:
                pass
            _pump(max(200, int(wait_ms)))

        job["stage"] = "grab"
        job["progress"] = 0.5
        yield
        widget = None
        for w in app.allWidgets():
            try:
                if (w.metaObject().className()
                        == "Pfx::Editor::Components::View2D::GraphicsView"
                        and w.isVisible() and w.width() > 100):
                    widget = w
                    break
            except Exception:
                pass
        if widget is None:
            raise RuntimeError(
                "2D view widget not found. Open the 2D view in SD (F2) first.")

        try:
            pix = widget.grab()
        except Exception as e:
            raise RuntimeError("2D view grab failed: {}".format(e))
        if pix is None or pix.isNull():
            raise RuntimeError("2D view grab produced an empty image.")

        if not output_path:
            output_path = self._default_output_path("2dview")
        output_path = self._ensure_output_dir(output_path)
        job["stage"] = "save"
        job["progress"] = 0.95
        yield
        if not pix.save(output_path):
            raise RuntimeError(
                "Failed to save image to '{}'.".format(output_path))
        job["stage"] = "done"
        job["progress"] = 1.0
        job["result"] = {
            "path":   output_path,
            "width":  pix.width(),
            "height": pix.height(),
            "method": "widget_grab",
            "graph":  gid,
        }

    def capture_node_preview(self, graph_identifier=None, node_id=None,
                             node_ids=None, max_size=512, output_path=None,
                             wait_ms=250):
        """Capture node preview thumbnails from the graph editor.

        Grabs each node's tile (which contains SD's live thumbnail) from the
        visible graph view, so no graph.compute() is needed: fast, and works on
        nodes that are not connected to a graph output. SD 15 renders node
        previews in the scene rather than as per-node widgets; a graph-owned
        preview widget is used when the SD version exposes one. Each node is
        fitted into view for a consistent crop, then the original zoom and
        scroll position are restored.

        - node_id: one node; node_ids: several nodes (labeled contact sheet).
        - max_size: downscale so the longest side <= max_size (0 = native).
        - wait_ms: settle time after opening/focusing the node.
        Returns {path, width, height, method, graph, node, label, ...} for one
        node, or a contact-sheet result with items + skipped for several.

        Runs the staged generator to completion synchronously; start_job
        ("capture_node_preview") runs the same stages step-by-step.
        """
        job = {"stage": "resolve_graph", "progress": 0.0,
               "stages_total": 0, "result": None}
        self._drive_sync(self._gen_capture_node_preview(job, {
            "graph_identifier": graph_identifier,
            "node_id": node_id,
            "node_ids": node_ids,
            "max_size": max_size,
            "output_path": output_path,
            "wait_ms": wait_ms,
        }), job)
        return job["result"]

    def _capture_node_preview_nodes(self, graph, node_id=None, node_ids=None):
        """Resolve node_id/node_ids to a deduped [(node, label), ...] list."""
        raw = []
        if node_id:
            raw.append(str(node_id))
        if node_ids:
            if isinstance(node_ids, str):
                node_ids = [node_ids]
            raw.extend(str(value) for value in node_ids
                       if str(value).strip())
        if not raw:
            raise ValueError("Pass node_id or node_ids (at least one node).")
        unique = []
        for value in raw:
            if value not in unique:
                unique.append(value)
        if len(unique) > 32:
            raise ValueError(
                "At most 32 node previews per call (got {}).".format(
                    len(unique)))
        targets = []
        for value in unique:
            node = self._find_node(graph, value)
            targets.append((node, self._map_label(node)))
        return targets

    def _pump_qt_events(self, ms):
        from PySide6.QtCore import QEventLoop, QTimer
        loop = QEventLoop()
        QTimer.singleShot(max(0, int(ms)), loop.quit)
        loop.exec()

    def _visible_graph_view(self):
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is None:
            return None
        for widget in app.allWidgets():
            try:
                if (widget.metaObject().className()
                        == "Pfx::Editor::Components::Graph::GraphView"
                        and widget.isVisible() and widget.width() > 100):
                    return widget
            except Exception:
                pass
        return None

    def _graph_view_id(self, graph):
        uim = self._ui_mgr()
        try:
            for index in range(uim.getGraphViewIDCount()):
                view_id = uim.getGraphViewIDAt(index)
                view_graph = uim.getGraphFromGraphViewID(view_id)
                if (view_graph is not None
                        and view_graph.getIdentifier() == graph.getIdentifier()):
                    return view_id
        except Exception:
            pass
        return None

    def _open_graph_view_for_preview(self, graph, wait_ms):
        from PySide6.QtWidgets import QApplication
        if QApplication.instance() is None:
            raise RuntimeError("Qt application not available.")
        try:
            self._ui_mgr().openResourceInEditor(graph)
        except Exception:
            pass
        self._pump_qt_events(max(100, int(wait_ms)))
        graph_view = self._visible_graph_view()
        if graph_view is None:
            raise RuntimeError(
                "Graph editor view not found. Open the graph in SD and retry.")
        view_id = self._graph_view_id(graph)
        if view_id is None:
            raise RuntimeError(
                "Graph view ID not found for '{}'. Open the graph in the SD "
                "editor and retry.".format(graph.getIdentifier()))
        return graph_view, view_id

    def _graph_graphics_view(self, graph_view):
        """Return the QGraphicsView that owns the graph scene."""
        from PySide6.QtWidgets import QGraphicsView
        if (hasattr(graph_view, "mapFromScene")
                and hasattr(graph_view, "viewport")):
            return graph_view
        try:
            candidates = [widget for widget in graph_view.findChildren(
                QGraphicsView) if widget.isVisible() and widget.width() > 100]
        except Exception:
            candidates = []
        return candidates[0] if candidates else None

    def _node_view_global_rect(self, graph_view, bbox):
        """Map an SD node bbox (scene coords) to a global screen QRect."""
        from PySide6.QtCore import QPointF, QRect
        view = self._graph_graphics_view(graph_view)
        if view is None:
            return None
        x, y = float(bbox.x), float(bbox.y)
        width, height = float(bbox.z), float(bbox.w)
        top_left = view.mapFromScene(QPointF(x, y))
        bottom_right = view.mapFromScene(QPointF(x + width, y + height))
        rect = QRect(top_left, bottom_right).normalized()
        viewport = view.viewport()
        return QRect(viewport.mapToGlobal(rect.topLeft()),
                     viewport.mapToGlobal(rect.bottomRight())).normalized()

    @staticmethod
    def _widget_matches_node(widget, node_id, label):
        """True when a widget exposes the node's id/label in its Qt metadata."""
        texts = []
        for getter in ("objectName", "windowTitle", "accessibleName",
                       "accessibleDescription", "statusTip", "toolTip"):
            try:
                value = getattr(widget, getter)()
                if value:
                    texts.append(str(value))
            except Exception:
                pass
        for text in texts:
            if (text == node_id
                    or (label and text == label)
                    or (node_id and node_id in text)):
                return True
        try:
            meta = widget.metaObject()
            for index in range(meta.propertyCount()):
                try:
                    name = meta.property(index).name()
                    if name in ("objectName", "windowTitle"):
                        continue
                    value = widget.property(name)
                    if not isinstance(value, str) or not value:
                        continue
                    if (value == node_id
                            or (label and value == label)
                            or (node_id and node_id in value)):
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def _find_node_preview_widget(self, graph_view, node, global_rect):
        """Find a graph-owned preview widget for `node`, when SD exposes one.

        SD 15 renders node previews directly in the graph view scene, so this
        usually returns None and the caller crops the node tile instead. The
        ancestor check keeps widgets from other docks (for example the PSD
        exporter's layer-stack preview) from being matched by accident.
        """
        from PySide6.QtCore import QPoint, QRect
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is None:
            return None
        view_rect = QRect(graph_view.mapToGlobal(QPoint(0, 0)),
                          graph_view.size())
        node_id = node.getIdentifier()
        label = self._map_label(node)
        geometric = []
        for widget in app.allWidgets():
            try:
                class_name = widget.metaObject().className()
                if (class_name
                        != "Pfx::Editor::Components::Graph::PSD::PSDNodePreviewWidget"
                        and "NodePreview" not in class_name):
                    continue
                if not widget.isVisible() or widget.width() <= 0 \
                        or widget.height() <= 0:
                    continue
                if not graph_view.isAncestorOf(widget):
                    continue
                rect = QRect(widget.mapToGlobal(QPoint(0, 0)), widget.size())
                if not view_rect.intersects(rect):
                    continue
                if self._widget_matches_node(widget, node_id, label):
                    return widget
                if global_rect is not None and global_rect.contains(
                        rect.center()):
                    geometric.append((rect.width() * rect.height(), widget))
            except Exception:
                continue
        if geometric:
            geometric.sort(key=lambda item: item[0], reverse=True)
            return geometric[0][1]
        return None

    @staticmethod
    def _grab_widget_image(widget):
        try:
            pixmap = widget.grab()
        except Exception:
            return None
        if pixmap is None or pixmap.isNull():
            return None
        image = pixmap.toImage()
        return None if image.isNull() else image

    def _grab_node_preview(self, graph_view, view_id, node, wait_ms):
        """Focus a node and return (QImage, capture_method).

        Prefers a graph-owned preview widget when one exists; otherwise crops
        the node tile from the graph view, which is SD 15's normal rendering.
        """
        from PySide6.QtCore import QRect, QRectF, Qt
        uim = self._ui_mgr()
        try:
            uim.focusGraphNode(view_id, node)
        except Exception:
            pass
        self._pump_qt_events(max(50, int(wait_ms)))

        bbox = None
        try:
            bbox = uim.getGraphNodeBBox(view_id, node)
        except Exception:
            pass
        global_rect = None
        if bbox is not None:
            try:
                global_rect = self._node_view_global_rect(graph_view, bbox)
            except Exception:
                pass

        widget = self._find_node_preview_widget(graph_view, node, global_rect)
        if widget is not None:
            image = self._grab_widget_image(widget)
            if image is not None:
                return image, "preview_widget"

        view = self._graph_graphics_view(graph_view)
        if bbox is None or view is None:
            raise RuntimeError(
                "Could not locate '{}' in the graph view. Make sure the graph "
                "is open in the SD editor and its view is visible.".format(
                    node.getIdentifier()))
        scene_rect = QRectF(float(bbox.x), float(bbox.y),
                            float(bbox.z), float(bbox.w))
        saved_view = None
        try:
            saved_view = (
                view.transform(),
                view.horizontalScrollBar().value(),
                view.verticalScrollBar().value(),
            )
            view.fitInView(scene_rect, Qt.KeepAspectRatio)
            self._pump_qt_events(max(30, min(120, int(wait_ms) // 3)))
        except Exception:
            if saved_view is not None:
                try:
                    view.setTransform(saved_view[0])
                    view.horizontalScrollBar().setValue(saved_view[1])
                    view.verticalScrollBar().setValue(saved_view[2])
                except Exception:
                    pass
            saved_view = None
        try:
            try:
                rect = view.mapFromScene(scene_rect).boundingRect().intersected(
                    view.viewport().rect())
            except Exception as e:
                raise RuntimeError(
                    "Could not map the node bounds of '{}': {}".format(
                        node.getIdentifier(), e))
            if rect.isEmpty():
                raise RuntimeError(
                    "Node '{}' is outside the visible graph view.".format(
                        node.getIdentifier()))
            pixmap = view.viewport().grab()
            if pixmap is None or pixmap.isNull():
                raise RuntimeError("Graph view grab produced an empty image.")
            image = pixmap.toImage()
            if image.isNull():
                raise RuntimeError("Graph view grab produced an empty image.")
            ratio = float(pixmap.devicePixelRatio() or 1.0)
            crop = QRect(
                int(round(rect.x() * ratio)), int(round(rect.y() * ratio)),
                max(1, int(round(rect.width() * ratio))),
                max(1, int(round(rect.height() * ratio))))
            image = image.copy(crop)
            if image.isNull():
                raise RuntimeError("Graph view crop produced an empty image.")
            return image, "graph_view_crop"
        finally:
            if saved_view is not None:
                try:
                    view.setTransform(saved_view[0])
                    view.horizontalScrollBar().setValue(saved_view[1])
                    view.verticalScrollBar().setValue(saved_view[2])
                except Exception:
                    pass

    def _gen_capture_node_preview(self, job, params):
        graph = self._resolve_graph(params.get("graph_identifier"))
        gid = graph.getIdentifier()
        targets = self._capture_node_preview_nodes(
            graph, params.get("node_id"), params.get("node_ids"))
        max_size = params.get("max_size", 512)
        output_path = params.get("output_path")
        wait_ms = params.get("wait_ms", 250)

        job["stages_total"] = 3 + len(targets)
        job["stage"] = "open_editor"
        job["progress"] = 0.05
        yield
        graph_view, view_id = self._open_graph_view_for_preview(graph, wait_ms)

        tiles = []
        skipped = []
        for index, (node, label) in enumerate(targets):
            job["stage"] = "grab_preview"
            job["progress"] = round(
                0.1 + 0.8 * index / max(1, len(targets)), 3)
            yield
            try:
                image, capture = self._grab_node_preview(
                    graph_view, view_id, node, wait_ms)
            except Exception as e:
                if len(targets) == 1:
                    raise
                skipped.append({"node": node.getIdentifier(),
                                "label": label, "reason": str(e)})
                continue
            source_width, source_height = image.width(), image.height()
            tiles.append({
                "usage": "node_preview",
                "label": label or node.getIdentifier(),
                "node": node.getIdentifier(),
                "port_id": None,
                "source_width": source_width,
                "source_height": source_height,
                "capture": capture,
                "image": self._downscale_preview(image, max_size),
            })

        if not tiles:
            raise RuntimeError(
                "No node previews could be captured on '{}'. {}".format(
                    gid, skipped))

        job["stage"] = "save"
        job["progress"] = 0.95
        yield
        if len(targets) == 1:
            tile = tiles[0]
            if not output_path:
                safe = re.sub(r'[^A-Za-z0-9]+', '_',
                              tile["label"]).strip('_').lower()
                output_path = self._default_output_path(
                    "node_preview_" + safe if safe else "node_preview")
            output_path = self._ensure_output_dir(output_path)
            image = tile["image"]
            if not image.save(output_path):
                raise RuntimeError(
                    "Failed to save image to '{}'.".format(output_path))
            job["result"] = {
                "path":          output_path,
                "width":         image.width(),
                "height":        image.height(),
                "method":        "node_preview",
                "capture":       tile["capture"],
                "source_width":  tile["source_width"],
                "source_height": tile["source_height"],
                "graph":         gid,
                "node":          tile["node"],
                "label":         tile["label"],
            }
        else:
            sheet = self._render_contact_sheet(
                tiles, output_path, prefix="node_preview_sheet")
            for item, tile in zip(sheet["items"], tiles):
                item["capture"] = tile["capture"]
            job["result"] = {
                "path":    sheet["path"],
                "width":   sheet["width"],
                "height":  sheet["height"],
                "method":  "node_preview_sheet",
                "graph":   gid,
                "columns": sheet["columns"],
                "rows":    sheet["rows"],
                "items":   sheet["items"],
                "skipped": skipped,
            }
        job["stage"] = "done"
        job["progress"] = 1.0

    def get_map_stats(self, graph_identifier=None, node_id=None,
                      output_usage=None, port_id=None, histogram_bins=16):
        """Calculate per-channel statistics directly from an SDTexture buffer.

        Runs the staged generator to completion synchronously; start_job
        ("get_map_stats") runs the same stages step-by-step with progress and
        between-stage cancellation.

        min/max/mean/count are exact; histograms of 16-bit/float maps larger
        than ~1M pixels are counted from evenly strided samples
        (histogram.sample_stride reports the stride; 1 = every pixel counted).
        """
        job = {"stage": "resolve_graph", "progress": 0.0,
               "stages_total": 0, "result": None}
        self._drive_sync(self._gen_get_map_stats(job, {
            "graph_identifier": graph_identifier,
            "node_id": node_id,
            "output_usage": output_usage,
            "port_id": port_id,
            "histogram_bins": histogram_bins,
        }), job)
        return job["result"]

    def _gen_get_map_stats(self, job, params):
        graph_identifier = params.get("graph_identifier")
        node_id          = params.get("node_id")
        output_usage     = params.get("output_usage")
        port_id          = params.get("port_id")
        histogram_bins   = params.get("histogram_bins", 16)

        graph = self._resolve_graph(graph_identifier)
        node, label = self._find_map_node(
            graph, node_id=node_id, output_usage=output_usage)
        job["stages_total"] = 4   # resolve, compute, select, stats
        job["stage"] = "compute"
        yield
        try:
            graph.compute()
        except Exception as e:
            raise RuntimeError(
                "graph.compute() failed for '{}': {}".format(
                    graph.getIdentifier(), e))
        selected_port, texture = self._select_map_texture(node, port_id)

        job["stage"] = "stats"
        job["progress"] = 0.85
        yield
        result = {
            "graph": graph.getIdentifier(),
            "node": node.getIdentifier(),
            "port_id": selected_port,
            "label": label,
        }
        result.update(self._texture_stats(texture, histogram_bins))
        job["stage"] = "done"
        job["progress"] = 1.0
        job["result"] = result

    def inspect_outputs(self, graph_identifier=None, output_usages=None,
                        max_size=1024, histogram_bins=16, output_path=None):
        """Compute the graph once and return a contact sheet plus per-channel
        statistics for the requested outputs.

        Runs the staged generator to completion synchronously; start_job
        ("inspect_outputs") runs the same stages step-by-step with progress
        and between-stage cancellation.

        capture_2d_view(output_usages=...) and get_map_stats each compute the
        graph on their own; this combined inspection computes once and reads
        both the preview tiles and the pixel buffers from the same textures.
        Defaults to the four common PBR outputs; outputs that cannot be
        matched or read are reported in 'skipped' instead of failing the call.

        min/max/mean/count are exact; histograms of 16-bit/float maps larger
        than ~1M pixels are counted from evenly strided samples
        (histogram.sample_stride reports the stride; 1 = every pixel counted).

        Returns {graph, path, width, height, method, histogram_bins, columns,
        rows, outputs: [{usage, label, node, port_id, source_width,
        source_height, width, height, pixel_format, value_domain, channels}],
        skipped}.
        """
        job = {"stage": "resolve_graph", "progress": 0.0,
               "stages_total": 0, "result": None}
        self._drive_sync(self._gen_inspect_outputs(job, {
            "graph_identifier": graph_identifier,
            "output_usages": output_usages,
            "max_size": max_size,
            "histogram_bins": histogram_bins,
            "output_path": output_path,
        }), job)
        return job["result"]

    def _gen_inspect_outputs(self, job, params):
        graph_identifier = params.get("graph_identifier")
        output_usages    = params.get("output_usages")
        max_size         = params.get("max_size", 1024)
        histogram_bins   = params.get("histogram_bins", 16)
        output_path      = params.get("output_path")

        if output_usages is None:
            usages = ["height", "normal", "ambientOcclusion", "baseColor"]
        else:
            usages = [str(usage) for usage in output_usages
                      if str(usage).strip()]
        if not usages:
            raise ValueError("Pass at least one output usage.")
        if len(usages) > 16:
            raise ValueError("Pass between 1 and 16 output_usages.")
        bins = int(histogram_bins)
        if bins < 1 or bins > 512:
            raise ValueError("histogram_bins must be between 1 and 512.")

        graph = self._resolve_graph(graph_identifier)
        gid = graph.getIdentifier()
        job["stages_total"] = 3 + len(usages) + 1   # resolve, compute, per-output, sheet
        job["stage"] = "compute"
        yield
        try:
            graph.compute()
        except Exception as e:
            raise RuntimeError(
                "graph.compute() failed for '{}': {}".format(gid, e))

        def _norm(value):
            return "".join(c for c in (value or "").lower() if c.isalnum())

        aliases = {"ao": "ambientocclusion"}
        try:
            array = graph.getOutputNodes()
            out_nodes = [array.getItem(i) for i in range(array.getSize())]
        except Exception:
            out_nodes = [n for n in graph.getNodes()
                         if self._get_node_def_id(n)
                         == "sbs::compositing::output"]
        by_label = {}
        for out_node in out_nodes:
            label = self._map_label(out_node)
            by_label.setdefault(
                aliases.get(_norm(label), _norm(label)), out_node)

        tiles = []
        outputs = []
        skipped = []
        for index, usage in enumerate(usages):
            job["stage"] = "read_output"
            job["progress"] = round(0.1 + 0.7 * index / len(usages), 3)
            yield
            want = aliases.get(_norm(usage), _norm(usage))
            node = by_label.get(want)
            if node is None:
                skipped.append({
                    "usage": usage,
                    "reason": "no output node matches; available: {}".format(
                        sorted(label for label in by_label if label))})
                continue
            try:
                pid, texture = self._select_map_texture(node)
                stats = self._texture_stats(texture, bins)
                image = self._map_texture_to_qimage(texture)
                src_w, src_h = image.width(), image.height()
                label = self._map_label(node) or usage
                tiles.append({"usage": usage, "label": label,
                              "node": node.getIdentifier(), "port_id": pid,
                              "source_width": src_w, "source_height": src_h,
                              "image": self._downscale_preview(
                                  image, max_size)})
                item = {"usage": usage, "label": label,
                        "node": node.getIdentifier(), "port_id": pid,
                        "source_width": src_w, "source_height": src_h}
                item.update(stats)
                outputs.append(item)
            except Exception as e:
                skipped.append({"usage": usage, "reason": str(e)})

        if not outputs:
            raise ValueError(
                "None of the requested outputs could be inspected on '{}'. "
                "Available outputs: {}.".format(
                    gid, sorted(label for label in by_label if label)))

        job["stage"] = "render_sheet"
        job["progress"] = 0.9
        yield
        sheet = self._render_contact_sheet(
            tiles, output_path, prefix="inspect_outputs")
        job["stage"] = "done"
        job["progress"] = 1.0
        job["result"] = {"graph": gid,
                         "path": sheet["path"],
                         "width": sheet["width"],
                         "height": sheet["height"],
                         "method": "inspect_outputs",
                         "histogram_bins": bins,
                         "columns": sheet["columns"],
                         "rows": sheet["rows"],
                         "outputs": outputs,
                         "skipped": skipped}

    # ═══════════════════════════════════════════════════════════════════════
    # ── STAGED LONG JOBS (progress + between-stage cancellation) ────────
    # ═══════════════════════════════════════════════════════════════════════
    #
    # capture_2d_view / get_map_stats / inspect_outputs are step-through
    # generators of stages executed on the Qt main thread (the compute stage
    # still blocks it, SD's constraint). _drive_sync runs one in-line to keep
    # the direct tools' exactly previous behavior; start_job schedules the
    # same generator one stage per QTimer tick, so status/cancel commands can
    # interleave between stages and progress is user-visible.

    _JOB_FACTORIES = {
        "capture_2d_view":       "_gen_capture_2d_view",
        "capture_node_preview":  "_gen_capture_node_preview",
        "get_map_stats":         "_gen_get_map_stats",
        "inspect_outputs":       "_gen_inspect_outputs",
    }

    def _drive_sync(self, gen, job):
        """Run a staged generator to completion (one next() per stage)."""
        job.setdefault("done", False)
        tries = 0
        while not job["done"] and tries <= 64:
            tries += 1
            try:
                next(gen)
            except StopIteration:
                job["done"] = True
        if not job["done"]:
            job["done"] = True

    def start_job(self, op, params=None):
        """Start a heavy inspection op as a staged background job.

        The job steps one stage per QTimer tick on SD's main thread; while any
        single stage runs (notably graph.compute()), queued commands wait.
        job_status/job_cancel interleave between stages. Returns
        {job_id, op, started} immediately.
        """
        if _QTimer is None:
            raise RuntimeError("No Qt binding: long jobs unavailable; use the "
                               "direct blocking tools instead.")
        factory_name = self._JOB_FACTORIES.get(op)
        if factory_name is None:
            raise ValueError("Unknown job op '{}'. Available: {}".format(
                op, sorted(self._JOB_FACTORIES)))

        with self._jobs_lock:
            seq = self._job_seq + 1
            self._job_seq = seq
            jobs = self._jobs
            # Bound the registry: prune finished records when it grows large.
            if len(jobs) >= 32:
                for key in sorted(jobs, key=int):
                    if jobs[key][0]["done"]:
                        del jobs[key]
                    if len(jobs) < 32:
                        break

        job = {"job_id": seq, "op": op, "done": False, "cancelled": False,
               "stage": "queued", "stage_index": 0, "stages_total": 0,
               "progress": 0.0, "result": None, "error": None}
        params = dict(params or {})
        gen = getattr(self, factory_name)(job, params)

        def _step():
            with self._jobs_lock:
                cancelled = job["cancelled"]
                done = job["done"]
            if cancelled:
                try:
                    gen.close()
                except Exception:
                    pass
                with self._jobs_lock:
                    job["done"] = True
                return
            if done:
                return
            try:
                next(gen)
            except StopIteration:
                with self._jobs_lock:
                    job["done"] = True
                return
            except Exception as e:
                with self._jobs_lock:
                    job["done"] = True
                    job["error"] = "{}: {}".format(type(e).__name__, e)
                return
            with self._jobs_lock:
                if job["done"]:
                    return
                job["stage_index"] = int(job.get("stage_index", 0)) + 1
                if not job.get("stages_total"):
                    job["stages_total"] = job["stage_index"] + 1
            _QTimer.singleShot(0, _step)

        with self._jobs_lock:
            self._jobs[seq] = (job, gen)
        _QTimer.singleShot(0, _step)
        return {"job_id": seq, "op": op, "started": True,
                "note": "poll with job_status or the bridge's wait_long_job"}

    def job_status(self, job_id=None):
        """Progress of a staged long job (latest when job_id is None).

        Returns {job_id, op, done, cancelled, stage, stage_index, stages_total,
        progress, result?, error?}. result holds the tool's normal result once
        done without error.
        """
        with self._jobs_lock:
            jobs = self._jobs
            if not jobs:
                raise ValueError("No long jobs have been started.")
            if job_id is None:
                record = max(jobs.items(), key=lambda kv: int(kv[0]))[1]
            else:
                record = jobs.get(int(job_id))
            if record is None:
                raise ValueError(
                    "Unknown job {}. Started: {}".format(
                        job_id, sorted(int(k) for k in jobs)))
            return dict(record[0])

    def job_cancel(self, job_id):
        """Request cancellation of a staged long job. It stops between stages
        (graph.compute() itself cannot be interrupted mid-flight)."""
        with self._jobs_lock:
            record = self._jobs.get(int(job_id))
            if record is None:
                raise ValueError("Unknown job {}. Started: {}".format(
                    job_id, sorted(int(k) for k in self._jobs)))
            job = record[0]
            ret = dict(job)
            if job["done"]:
                ret["note"] = "Job already finished."
            else:
                job["cancelled"] = True
                ret["note"] = ("Cancellation requested; it takes effect between "
                               "stages, after any running stage completes.")
            return ret

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
        Occlusion, Height, ...). The public API has no call for this, so the
        handler drives those menus with synthetic Qt mouse events sent straight
        to SD's widgets: the OS cursor never moves, SD is not brought to the
        front, and the user's other windows are untouched. SD does switch its
        editor tab to the graph.

        Channel per output is derived from the output node's label (e.g.
        'Base Color', 'Normal'). Use `channels` to override, keyed by output
        node identifier or label, valued with the menu channel name.

        Once bound the link is LIVE: every set_parameter edit re-renders the
        view automatically.

        The menus run nested event loops, so the chain is timer-driven and
        this returns at once with a job id (about 1.5 s per channel). Poll
        send_to_3d_view_status(job_id) until done is True.

        - graph_identifier: graph name as shown in the Explorer (e.g. 'Test_Steel')
        - channels: optional dict {node_id_or_label: channel_text}
        Returns {job_id, graph, targets: [{node, channel}...], skipped}.
        """
        from PySide6.QtWidgets import QApplication, QMenu
        from PySide6.QtCore import QTimer, QPointF, Qt, QEvent
        from PySide6.QtGui import QMouseEvent
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

        prev = getattr(self, "_send3d_job", None)
        if prev is not None and not prev["done"]:
            raise RuntimeError(
                "send_to_3d_view job {} is still running; poll "
                "send_to_3d_view_status until it is done.".format(prev["job_id"]))

        job = {
            "job_id": (prev["job_id"] + 1) if prev else 1,
            "graph": graph_identifier,
            "done": False,
            "bound": [],
            "failed": [],
            "skipped": skipped,
            "pending": [{"node": t["node"], "channel": t["channel"]}
                        for t in targets],
        }
        self._send3d_job = job

        # ── Synthetic-input helpers (no OS input, no cursor movement) ─────
        def _visible_menus():
            out = []
            for w in app.topLevelWidgets():
                try:
                    if isinstance(w, QMenu) and w.isVisible():
                        out.append(w)
                except Exception:
                    pass
            return out

        def _find_action(text):
            for m in _visible_menus():
                for a in m.actions():
                    try:
                        if a.text() == text:
                            return m, a
                    except Exception:
                        pass
            return None, None

        def _close_menus():
            for m in _visible_menus():
                try:
                    m.close()
                except Exception:
                    pass

        def _click(widget, pt, button):
            p = QPointF(pt)
            gp = QPointF(widget.mapToGlobal(pt))
            app.sendEvent(widget, QMouseEvent(
                QEvent.MouseMove, p, gp, Qt.NoButton, Qt.NoButton, Qt.NoModifier))
            app.sendEvent(widget, QMouseEvent(
                QEvent.MouseButtonPress, p, gp, button, button, Qt.NoModifier))
            app.sendEvent(widget, QMouseEvent(
                QEvent.MouseButtonRelease, p, gp, button, Qt.NoButton, Qt.NoModifier))

        def _later(ms, fn):
            # Timer callbacks swallow exceptions; record them on the job so a
            # failure never leaves it stuck at done=False.
            def _run():
                try:
                    fn()
                except Exception as e:
                    _close_menus()
                    if queue:
                        _fail(queue[0], "{}: {}".format(type(e).__name__, e))
                    else:
                        job["done"] = True
            QTimer.singleShot(int(ms), _run)

        def _wait_for(text, then, on_timeout, tries=25):
            # Poll for a visible menu holding `text`. The context menu runs a
            # nested event loop, so these timers keep firing while it is open.
            def _poll(n=0):
                m, a = _find_action(text)
                if a is not None:
                    then(m, a)
                elif n < tries:
                    _later(120, lambda: _poll(n + 1))
                else:
                    _close_menus()
                    on_timeout("menu item '{}' never appeared".format(text))
            _later(120, _poll)

        def _graph_view():
            # openResourceInEditor fronted this graph's tab, so its view is
            # the visible one. (The Qt wrapper's getGraphView hands back
            # short-lived objects that get deleted under us.)
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
        queue = list(targets)

        def _find_view_id():
            try:
                for i in range(uim.getGraphViewIDCount()):
                    vid = uim.getGraphViewIDAt(i)
                    vg = uim.getGraphFromGraphViewID(vid)
                    if vg is not None and vg.getIdentifier() == graph.getIdentifier():
                        return vid
            except Exception:
                pass
            return None

        def _next():
            _later(200, _cycle)

        def _fail(t, reason):
            job["failed"].append({"node": t["node"], "channel": t["channel"],
                                  "reason": reason})
            job["pending"].pop(0)
            queue.pop(0)
            _next()

        # ── Per-target cycles (timer-driven; menus run nested loops) ──────
        def _cycle():
            if not queue:
                _close_menus()
                job["done"] = True
                return
            t = queue[0]
            try:
                _close_menus()
                # Bring this graph's tab to the front so its view is live.
                uim.openResourceInEditor(graph)
                view_id = _find_view_id()
                if view_id is None:
                    t["_needs_view"] = t.get("_needs_view", 0) + 1
                    if t["_needs_view"] > 6:
                        _fail(t, "graph never appeared in a graph view")
                    else:
                        _later(500, _cycle)
                    return
                uim.focusGraphNode(view_id, t["obj"])
            except Exception as e:
                _fail(t, "setup error: {}".format(e))
                return
            # focusGraphNode pans the view; let it settle before clicking.
            _later(800, lambda: _open_menu(t))

        def _open_menu(t):
            gv = _graph_view()
            if gv is None:
                _fail(t, "no visible graph view")
                return
            vp = gv.viewport()
            center = vp.rect().center()
            if gv.itemAt(center) is None:
                _fail(t, "node not under the view center after focusing")
                return
            _wait_for("View In 3D View", lambda m, a: _pick_view3d(t, m, a),
                      lambda r: _fail(t, r))
            _click(vp, center, Qt.RightButton)

        def _pick_view3d(t, m, a):
            _wait_for(t["channel"], lambda m2, a2: _pick_channel(t, m2, a2),
                      lambda r: _fail(t, r))
            _click(m, m.actionGeometry(a).center(), Qt.LeftButton)

        def _pick_channel(t, m, a):
            _click(m, m.actionGeometry(a).center(), Qt.LeftButton)

            def _done():
                _close_menus()
                job["bound"].append({"node": t["node"], "channel": t["channel"]})
                job["pending"].pop(0)
                queue.pop(0)
                _next()

            _later(300, _done)

        _later(100, _cycle)
        return {
            "job_id": job["job_id"],
            "graph": graph_identifier,
            "targets": [{"node": t["node"], "channel": t["channel"]}
                        for t in targets],
            "skipped": skipped,
        }

    def send_to_3d_view_status(self, job_id=None):
        """Progress of the last send_to_3d_view job.

        Returns {job_id, graph, done, bound, failed, pending, skipped}.
        """
        job = getattr(self, "_send3d_job", None)
        if job is None:
            raise ValueError("No send_to_3d_view job has been started.")
        if job_id is not None and int(job_id) != job["job_id"]:
            raise ValueError("Unknown job {} (latest is {}).".format(
                job_id, job["job_id"]))
        return dict(job)

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

        # Security gates and config
        results["execute_code_enabled"] = self._execute_code_allowed()
        results["dev_tools_enabled"] = self._dev_tools_allowed()
        results["token_auth_required"] = bool(
            os.environ.get("SD_MCP_TOKEN"))
        results["plugin_ports"] = sorted(getattr(_server, "listeners", {}))
        results["command_timeout"] = COMMAND_TIMEOUT
        results["command_timeout_max"] = COMMAND_TIMEOUT_MAX

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

    def _guard_atomic_definition(self, definition_id, known_defs, hint=""):
        """Refuse newNode() unless definition_id is validated against known_defs.

        Unknown definitions can hang SD 15, so an introspection failure or an
        empty definition list must fail closed instead of calling newNode().
        """
        if not known_defs:
            raise RuntimeError(
                "Cannot validate definition '{}': the graph reported no node "
                "definitions. Refusing to call newNode() with an unvalidated "
                "definition — unknown definitions can hang SD 15.".format(
                    definition_id))
        if definition_id not in known_defs:
            message = "Unknown definition '{}'.".format(definition_id)
            if hint:
                message += " " + hint
            raise ValueError(message)

    def create_node(self, definition_id, graph_identifier=None, position=None):
        graph = self._resolve_graph(graph_identifier)
        # Validate before calling newNode — unknown defs HANG SD 15
        try:
            known = {d.getId() for d in list(graph.getNodeDefinitions())}
        except Exception as e:
            raise RuntimeError(
                "Cannot validate definition '{}': graph.getNodeDefinitions() "
                "failed ({}). Refusing to call newNode() with an unvalidated "
                "definition — unknown definitions can hang SD 15.".format(
                    definition_id, e))
        if definition_id not in known:
            raise ValueError(
                "Unknown definition '{}'. "
                "Library nodes require create_instance_node with pkg:// URL.".format(
                    definition_id))

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

    def _node_property_ids(self, node):
        """(input, annotation) property type maps of a node: id -> SDType id.

        The SDType ids feed _coerce_type when setting values, because SD 15
        silently crashes (kills the whole execution) on wrong SDValue types.
        """
        input_types = {}
        annot_types = {}
        try:
            for p in list(node.getProperties(SDPropertyCategory.Input)):
                pid = p.getId()
                try:
                    t = p.getType()
                    input_types[pid] = t.getId() if t else ""
                except Exception:
                    input_types[pid] = ""
        except Exception:
            pass
        try:
            for p in list(node.getProperties(SDPropertyCategory.Annotation)):
                pid = p.getId()
                try:
                    t = p.getType()
                    annot_types[pid] = t.getId() if t else ""
                except Exception:
                    annot_types[pid] = ""
        except Exception:
            pass
        return input_types, annot_types

    def _apply_parameter(self, node, parameter_id, value, value_type="float",
                         input_types=None, annot_types=None):
        """Set one parameter on an already-resolved node.

        Shared by set_parameter and set_parameters; input_types/annot_types
        (id -> SDType id maps from _node_property_ids) may be passed in when
        one node receives several parameters in a batch.

        The setter is chosen by property category and the value type is
        coerced to the actual SD property type: SD 15 silently crashes the
        whole execution call when a property is set through the wrong setter
        or with an SDValue of the wrong type.
        Returns {parameter_id, value, value_type[, renamed_from]}.
        """
        if input_types is None or annot_types is None:
            input_types, annot_types = self._node_property_ids(node)
        node_id = node.getIdentifier()

        orig_parameter_id = parameter_id
        parameter_id, value_factor, renamed = self._resolve_param_id(
            parameter_id, input_types, annot_types)
        if value_factor != 1.0 and isinstance(value, (int, float)) \
                and abs(value) <= 1.0:
            value = value * value_factor
        if renamed:
            _log("Param '{}' -> '{}' on '{}'".format(
                orig_parameter_id, parameter_id, node_id))

        if (input_types or annot_types) and \
           parameter_id not in input_types and parameter_id not in annot_types:
            all_ids = sorted(set(input_types) | set(annot_types))
            raise ValueError(
                "Property '{}' not found on node '{}'. Available: {}".format(
                    parameter_id, node_id, all_ids))

        sd_type_id = input_types.get(parameter_id) \
            or annot_types.get(parameter_id) or ""
        value_type = _coerce_type(value_type, value, sd_type_id)
        sd_value = self._make_sd_value(value_type, value)

        set_ok = False
        last_err = None
        if parameter_id in input_types:
            try:
                node.setInputPropertyValueFromId(parameter_id, sd_value)
                set_ok = True
            except Exception as e:
                last_err = e
        if not set_ok and parameter_id in annot_types:
            try:
                node.setAnnotationPropertyValueFromId(parameter_id, sd_value)
                set_ok = True
            except Exception as e:
                last_err = e

        if not set_ok:
            raise RuntimeError("Failed to set '{}' on node '{}': {}".format(
                parameter_id, node_id, last_err))

        result = {
            "parameter_id": parameter_id,
            "value":        value,
            "value_type":   value_type,
        }
        if renamed:
            result["renamed_from"] = orig_parameter_id
        return result

    def set_parameter(self, node_id, parameter_id, value, value_type="float",
                      graph_identifier=None):
        graph = self._resolve_graph(graph_identifier)
        node  = self._find_node(graph, node_id)

        result = {"node_id": node_id}
        result.update(self._apply_parameter(
            node, parameter_id, value, value_type))
        return result

    def set_parameters(self, updates, graph_identifier=None):
        """Set several parameters in one call, resolving the graph and each
        node once. Individual failures do not abort the batch; every update
        reports its own status.

        updates: list of {node_id, parameter_id, value[, value_type]} dicts
        (same parameter IDs and value types as set_parameter).
        Returns {graph, applied, failed, results}.
        """
        graph = self._resolve_graph(graph_identifier)
        if not updates:
            raise ValueError("Pass at least one update.")
        node_cache = {}
        results = []
        applied = 0
        failed  = 0
        for update in updates:
            node_id      = update.get("node_id")
            parameter_id = update.get("parameter_id")
            value        = update.get("value")
            value_type   = update.get("value_type", "float")
            entry = {"node_id": node_id, "parameter_id": parameter_id,
                     "value": value, "value_type": value_type}
            try:
                if not node_id or not parameter_id:
                    raise ValueError(
                        "Each update needs 'node_id' and 'parameter_id'.")
                cached = node_cache.get(node_id)
                if cached is None:
                    node = self._find_node(graph, node_id)
                    cached = (node,) + self._node_property_ids(node)
                    node_cache[node_id] = cached
                node, input_types, annot_types = cached
                entry.update(self._apply_parameter(
                    node, parameter_id, value, value_type,
                    input_types=input_types, annot_types=annot_types))
                entry["status"] = "ok"
                applied += 1
            except Exception as e:
                entry["status"] = "error"
                entry["error"] = str(e)
                failed += 1
            results.append(entry)
        return {"graph": graph.getIdentifier(), "applied": applied,
                "failed": failed, "results": results}

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

    def _preflight_batch_graph(self, nodes, connections, known_defs):
        """Validate as much of a batch plan as possible WITHOUT creating a
        graph: definitions against `known_defs`, library URLs/keywords
        resolved (auto-loading library packages), parameter value shapes, and
        for endpoints in the known ATOMIC_PORTS registry, port IDs.

        Returns (errors, warnings, counts)."""
        errors   = []
        warnings = []
        alias_ports = {}    # alias -> (inputs, outputs) OR None when ports unknown
        alias_names = []
        _VEC_SIZES = {"float2": 2, "float3": 3, "float4": 4,
                      "int2": 2, "int3": 3, "int4": 4, "color": 4}

        for index, spec in enumerate(nodes):
            defn_id = spec.get("definition_id")
            alias   = spec.get("id_alias") or spec.get("alias") or (
                (defn_id.split("::")[-1]) if defn_id else "node_{}".format(index))
            if alias in alias_ports:
                warnings.append({
                    "kind": "alias_duplicate", "node": alias, "index": index,
                    "message": "duplicate alias; it will be renamed automatically."})
                suffix = 1
                while "{}_{}".format(alias, suffix) in alias_ports:
                    suffix += 1
                alias = "{}_{}".format(alias, suffix)
            alias_names.append(alias)

            res_url = spec.get("resource_url")
            lib_kw  = spec.get("library_keyword")

            if defn_id == "sbs::compositing::output":
                reg = ATOMIC_PORTS["sbs::compositing::output"]
                alias_ports[alias] = (reg["inputs"], reg["outputs"])
            elif bool(res_url) == bool(lib_kw) and (res_url or lib_kw):
                errors.append({
                    "kind": "conflicting_sources", "node": alias,
                    "message": "set either resource_url or library_keyword, not both."})
                alias_ports[alias] = None
            elif res_url:
                resource, _resolved = self._resolve_res_url(res_url)
                if not resource:
                    errors.append({
                        "kind": "resource_not_found", "node": alias,
                        "message": "resource URL '{}' not found.".format(res_url)})
                alias_ports[alias] = None     # library ports known only after instancing
            elif lib_kw:
                try:
                    url = self._resolve_lib_url(lib_kw)
                except Exception as e:
                    errors.append({
                        "kind": "library_not_found", "node": alias,
                        "message": "library keyword '{}' could not be "
                                   "resolved: {}".format(lib_kw, e)})
                else:
                    if not url:
                        errors.append({
                            "kind": "library_not_found", "node": alias,
                            "message": "library keyword '{}' not found.".format(lib_kw)})
                alias_ports[alias] = None
            else:
                defn_id = defn_id or "sbs::compositing::uniform"
                reg = ATOMIC_PORTS.get(defn_id)
                if reg is not None:
                    alias_ports[alias] = (reg["inputs"], reg["outputs"])
                elif defn_id in known_defs:
                    alias_ports[alias] = None   # known, outside our registry check
                else:
                    known = sorted(known_defs or [])
                    errors.append({
                        "kind": "unknown_definition", "node": alias,
                        "message": "definition '{}' is not available in a new "
                                   "graph (known: seen {} ids, e.g. {}). "
                                   "Library nodes need resource_url or "
                                   "library_keyword.".format(
                                       defn_id, len(known), ", ".join(known[:12]))})
                    alias_ports[alias] = None

            params = spec.get("parameters") or {}
            for param_id, pspec in params.items():
                if isinstance(pspec, dict) and "value" in pspec:
                    pval, ptype = pspec["value"], pspec.get("type")
                else:
                    pval, ptype = pspec, None
                if ptype is None:
                    ptype = _infer_type(pval)
                size = _VEC_SIZES.get(ptype)
                if size and (not isinstance(pval, (list, tuple)) or len(pval) != size):
                    errors.append({
                        "kind": "parameter_shape", "node": alias,
                        "param": param_id,
                        "message": "{} needs a {}-element list, got {!r}.".format(
                            ptype, size, pval)})

        for index, conn in enumerate(connections):
            fa = conn.get("from") or conn.get("from_alias")
            ta = conn.get("to") or conn.get("to_alias")
            fo = conn.get("from_output", "unique_filter_output")
            ti = conn.get("to_input", "input1")
            ok = True
            for role, alias in (("from", fa), ("to", ta)):
                if alias not in alias_ports:
                    ok = False
                    errors.append({
                        "kind": "alias_not_found", "connection": index,
                        "message": "{} alias '{}' not in nodes list; known: "
                                   "{}.".format(role, alias, sorted(alias_ports))})
            if not ok or fa == ta:
                if fa == ta and ok:
                    errors.append({
                        "kind": "self_connection", "connection": index,
                        "message": "'{}' connects to itself.".format(fa)})
                continue
            f_ports = alias_ports.get(fa)
            t_ports = alias_ports.get(ta)
            if f_ports and fo not in f_ports[1]:
                errors.append({
                    "kind": "unknown_port", "connection": index, "node": fa,
                    "message": "output port '{}' not in the known outputs of "
                               "'{}' ({}). Single-output library nodes "
                               "auto-resolve at build time.".format(
                                   fo, fa, f_ports[1])})
            if t_ports and ti not in t_ports[0]:
                errors.append({
                    "kind": "unknown_port", "connection": index, "node": ta,
                    "message": "input port '{}' not in the known inputs of "
                               "'{}' ({}).".format(ti, ta, t_ports[0])})

        return errors, warnings, {
            "nodes_checked": len(nodes),
            "connections_checked": len(connections),
        }

    def create_batch_graph(self, graph_name, package_index=0, package_path=None,
                           nodes=None, connections=None, output_size_log2=11,
                           open_in_editor=True, preflight=False,
                           cleanup_on_failure=False):
        """
        Create a complete graph in one call.
        Supports atomic nodes, library nodes (via resource_url or keyword),
        output nodes, and full connection wiring.
        preflight: validate the plan without creating a graph; returns
        valid/errors/warnings and never touches the editor.
        cleanup_on_failure: if any node or connection fails, delete the new
        graph and raise with the report.
        """
        if nodes is None:
            nodes = []
        if connections is None:
            connections = []

        safe_name = _sanitize_identifier(graph_name)
        pkg   = self._resolve_package(package_index, package_path)

        if preflight:
            temp_pkg = self._pkg_mgr().newUserPackage()
            try:
                temp_graph = SDSBSCompGraph.sNew(temp_pkg)
                temp_graph.setIdentifier("_TempPreflight")
                try:
                    known_defs = {d.getId() for d in list(temp_graph.getNodeDefinitions())}
                except Exception:
                    known_defs = set()
            except Exception:
                known_defs = set()
            finally:
                try:
                    self._pkg_mgr().unloadUserPackage(temp_pkg)
                except Exception:
                    pass
            errors, warnings, counts = self._preflight_batch_graph(
                nodes, connections, known_defs)
            result = {
                "preflight":        True,
                "requested_name":   graph_name,
                "graph_name":       safe_name,
                "valid":            not errors,
                "errors":           errors,
                "warnings":         warnings,
                "error_count":      len(errors),
                "warning_count":    len(warnings),
                "package":          pkg.getFilePath(),
            }
            result.update(counts)
            return result

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
                    self._guard_atomic_definition(
                        defn_id, _known_defs,
                        hint="Use library_keyword for library nodes.")
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

        if (failed or any(not c.get("success") for c in conn_results)) and cleanup_on_failure:
            removed_note = "The new graph was deleted."
            try:
                graph.delete()
            except Exception as e:
                removed_note = ("The new graph could NOT be deleted ({}: {}); "
                                "it may remain in the package."
                                .format(type(e).__name__, e))
            raise RuntimeError(
                "Batch graph '{}' failed: {} node error(s), {} connection "
                "error(s). cleanup_on_failure: {}. First errors: nodes={}, "
                "connections={}".format(
                    safe_name, len(failed),
                    sum(1 for c in conn_results if not c.get("success")),
                    removed_note, failed[:3],
                    [c for c in conn_results if not c.get("success")][:3]))

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
                              open_in_editor=True, preflight=False,
                              cleanup_on_failure=True):
        """
        Build a complete PBR material graph from a named recipe.
        overrides: dict of parameter overrides applied on top of recipe defaults.
        preflight: validate the recipe plan without creating a graph.
        cleanup_on_failure: delete the new graph if the build fails.
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
            preflight        = preflight,
            cleanup_on_failure = cleanup_on_failure,
        )

    def build_heightmap_graph(self, graph_name, style,
                               package_index=0, package_path=None,
                               output_size_log2=11, open_in_editor=True,
                               detail_level=2, scale=5.0, disorder=0.5,
                               preflight=False, cleanup_on_failure=True):
        """
        Build a heightmap graph for terrain/rock/cliff styles.
        style: 'cliff', 'rock', 'cracked', 'sand', 'mud', 'mountain', 'cobblestone'
        detail_level: 1=basic, 2=standard, 3=high detail
        preflight: validate the recipe plan without creating a graph.
        cleanup_on_failure: delete the new graph if the build fails.
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
            preflight        = preflight,
            cleanup_on_failure = cleanup_on_failure,
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
                    self._guard_atomic_definition(
                        defn_id, _known_defs,
                        hint="Use library_keyword for library nodes.")
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

    def _dev_tools_allowed(self):
        """Whether development tools (reload_plugin) are opted in.

        reload_plugin re-executes Python from disk, so it is gated like
        execute_code: SD_MCP_ALLOW_DEV_TOOLS=1 in SD's environment, or the
        execute-code opt-in (env or marker file).
        """
        if os.environ.get("SD_MCP_ALLOW_DEV_TOOLS", "").strip().lower() in (
                "1", "true", "yes", "on"):
            return True
        return self._execute_code_allowed()

    def reload_plugin(self, handlers=True):
        """Hot-reload the plugin from disk without restarting SD.

        Always reloads the pure-Python submodules (recipes, sd_documentation,
        port_suggest). With handlers=True it also re-executes this package's
        __init__.py and swaps a fresh CommandHandler into the running server.
        The TCP server, its listener sockets and the Qt invoker are kept (the
        invoker must stay the one created on the main thread), so the bridge
        connection is unaffected. Changes to SDMCPServer / the dispatcher
        themselves still need an SD restart.

        Development-only: requires SD_MCP_ALLOW_DEV_TOOLS=1 (or the
        execute_code opt-in) in SD's environment, plus --allow-dev-tools on
        the bridge.
        """
        if not self._dev_tools_allowed():
            raise ValueError(
                "reload_plugin is disabled. Set SD_MCP_ALLOW_DEV_TOOLS=1 in "
                "SD's environment (or enable the execute_code opt-in) and "
                "restart the bridge with --allow-dev-tools; reload_plugin "
                "re-executes Python from disk.")
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
        if server is not None:
            server._handler = new_handler
        _log("Plugin reloaded from {}".format(src_path))
        result.update({
            "handlers_reloaded": True,
            "plugin_version":    ".".join(map(str, pkg.PLUGIN_VERSION)),
            "commands":          sorted(new_handler.HANDLERS),
        })
        return result

    def _execute_code_allowed(self):
        """Whether arbitrary-code execution is opted in on the plugin side.

        Two opt-ins (separate from the bridge's flag, which only arms the MCP
        tool): SD_MCP_ALLOW_EXECUTE_CODE=1 in SD's environment, or an
        'allow_execute_code' file (content ignored) placed in the plugin
        folder.
        """
        if os.environ.get("SD_MCP_ALLOW_EXECUTE_CODE", "").strip().lower() in (
                "1", "true", "yes", "on"):
            return True
        try:
            plugin_dir = os.path.dirname(os.path.abspath(__file__))
            if os.path.isfile(os.path.join(plugin_dir, "allow_execute_code")):
                return True
        except Exception:
            pass
        return False

    def execute_code(self, code):
        """Execute arbitrary Python on the SD main thread. Opt-in only: the
        bridge must run with --allow-arbitrary-code AND the plugin must be
        opted in (SD_MCP_ALLOW_EXECUTE_CODE=1 or an allow_execute_code marker
        file in the plugin folder). Keep the code short: it blocks SD."""
        if not self._execute_code_allowed():
            raise ValueError(
                "execute_code is disabled. Ask the user to opt in on BOTH "
                "sides: restart the bridge with --allow-arbitrary-code, and "
                "either set SD_MCP_ALLOW_EXECUTE_CODE=1 in SD's environment "
                "or create an 'allow_execute_code' file in the plugin folder "
                "({}).".format(
                    os.path.dirname(os.path.abspath(__file__))))
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
