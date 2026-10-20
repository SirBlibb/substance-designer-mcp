"""Unit tests for plugin handlers that do not need a live Substance Designer.

The plugin imports sd.api at module import time, so a minimal fake ``sd``
package is installed before loading plugin/__init__.py. Tests monkeypatch the
handler methods that would otherwise call SD.
"""
import importlib.util
import socket
import sys
import threading
import time
import types
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugin"
PLUGIN_NAME = "sd_mcp_plugin_under_test"

_SD_MODULE_NAMES = (
    "sd",
    "sd.api",
    "sd.api.sdproperty",
    "sd.api.sdbasetypes",
    "sd.api.sdvaluefloat",
    "sd.api.sdvalueint",
    "sd.api.sdvalueint2",
    "sd.api.sdvalueint3",
    "sd.api.sdvalueint4",
    "sd.api.sdvaluebool",
    "sd.api.sdvaluestring",
    "sd.api.sdvaluefloat2",
    "sd.api.sdvaluefloat3",
    "sd.api.sdvaluefloat4",
    "sd.api.sdvaluecolorrgba",
    "sd.api.sbs",
    "sd.api.sbs.sdsbscompgraph",
)


class _SdValueStub:
    def __init__(self, *args, **kwargs):
        self.args = args

    @classmethod
    def sNew(cls, *args, **kwargs):
        return cls(*args, **kwargs)


def _module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def _build_fake_sd_modules():
    sd = types.ModuleType("sd")
    api = types.ModuleType("sd.api")
    sdproperty = _module(
        "sd.api.sdproperty",
        SDPropertyCategory=type("SDPropertyCategory", (), {
            "Input": "input", "Output": "output", "Annotation": "annotation"}),
    )
    basetypes = _module(
        "sd.api.sdbasetypes",
        float2=_SdValueStub, float3=_SdValueStub, float4=_SdValueStub,
        ColorRGBA=_SdValueStub, int2=_SdValueStub, int3=_SdValueStub,
        int4=_SdValueStub,
    )
    sbs = types.ModuleType("sd.api.sbs")
    modules = {
        "sd": sd,
        "sd.api": api,
        "sd.api.sdproperty": sdproperty,
        "sd.api.sdbasetypes": basetypes,
        "sd.api.sdvaluefloat": _module("sd.api.sdvaluefloat", SDValueFloat=_SdValueStub),
        "sd.api.sdvalueint": _module("sd.api.sdvalueint", SDValueInt=_SdValueStub),
        "sd.api.sdvalueint2": _module("sd.api.sdvalueint2", SDValueInt2=_SdValueStub),
        "sd.api.sdvalueint3": _module("sd.api.sdvalueint3", SDValueInt3=_SdValueStub),
        "sd.api.sdvalueint4": _module("sd.api.sdvalueint4", SDValueInt4=_SdValueStub),
        "sd.api.sdvaluebool": _module("sd.api.sdvaluebool", SDValueBool=_SdValueStub),
        "sd.api.sdvaluestring": _module("sd.api.sdvaluestring", SDValueString=_SdValueStub),
        "sd.api.sdvaluefloat2": _module("sd.api.sdvaluefloat2", SDValueFloat2=_SdValueStub),
        "sd.api.sdvaluefloat3": _module("sd.api.sdvaluefloat3", SDValueFloat3=_SdValueStub),
        "sd.api.sdvaluefloat4": _module("sd.api.sdvaluefloat4", SDValueFloat4=_SdValueStub),
        "sd.api.sdvaluecolorrgba": _module(
            "sd.api.sdvaluecolorrgba", SDValueColorRGBA=_SdValueStub),
        "sd.api.sbs": sbs,
        "sd.api.sbs.sdsbscompgraph": _module(
            "sd.api.sbs.sdsbscompgraph", SDSBSCompGraph=_SdValueStub),
    }

    sd.api = api
    api.sdproperty = sdproperty
    api.sdbasetypes = basetypes
    api.sbs = sbs
    sbs.sdsbscompgraph = modules["sd.api.sbs.sdsbscompgraph"]
    return modules


@pytest.fixture(scope="module")
def plugin_module():
    saved = {name: sys.modules.get(name) for name in _SD_MODULE_NAMES}
    modules = _build_fake_sd_modules()
    sys.modules.update(modules)

    spec = importlib.util.spec_from_file_location(
        PLUGIN_NAME, PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PLUGIN_NAME] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        for name in list(sys.modules):
            if name == PLUGIN_NAME or name.startswith(PLUGIN_NAME + "."):
                sys.modules.pop(name, None)
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


@pytest.fixture
def handler(plugin_module):
    return plugin_module.CommandHandler()


def test_drive_sync_tolerates_job_without_done_flag(handler):
    job = {"stage": "resolve_graph", "progress": 0.0,
           "stages_total": 0, "result": None}
    stages = []

    def work():
        stages.append("compute")
        job["result"] = {"ok": True}
        yield
        stages.append("save")

    handler._drive_sync(work(), job)

    assert stages == ["compute", "save"]
    assert job["done"] is True
    assert job["result"] == {"ok": True}


def test_preflight_unknown_library_keyword_reports_one_error(handler, monkeypatch):
    monkeypatch.setattr(handler, "_resolve_lib_url", lambda keyword: None)

    errors, warnings, counts = handler._preflight_batch_graph(
        [{"id_alias": "lib", "library_keyword": "definitely_missing"}],
        [], set())

    assert warnings == []
    assert counts == {"nodes_checked": 1, "connections_checked": 0}
    assert len(errors) == 1
    assert errors[0]["kind"] == "library_not_found"
    assert "not found" in errors[0]["message"]


def test_preflight_library_lookup_exception_reports_one_error(handler, monkeypatch):
    def fail(keyword):
        raise RuntimeError("lookup failed")

    monkeypatch.setattr(handler, "_resolve_lib_url", fail)

    errors, _, _ = handler._preflight_batch_graph(
        [{"id_alias": "lib", "library_keyword": "broken"}], [], set())

    assert len(errors) == 1
    assert errors[0]["kind"] == "library_not_found"
    assert "could not be resolved" in errors[0]["message"]


def test_guard_atomic_definition_fails_closed(handler):
    with pytest.raises(RuntimeError, match="reported no node definitions"):
        handler._guard_atomic_definition("sbs::compositing::blur", set())

    with pytest.raises(ValueError, match="Unknown definition"):
        handler._guard_atomic_definition(
            "sbs::compositing::nope", {"sbs::compositing::blur"})


def test_create_node_does_not_call_new_node_when_introspection_fails(handler, monkeypatch):
    created = []

    class Graph:
        def getNodeDefinitions(self):
            raise RuntimeError("introspection failed")

        def newNode(self, definition_id):
            created.append(definition_id)
            return object()

    monkeypatch.setattr(handler, "_resolve_graph", lambda gid: Graph())

    with pytest.raises(RuntimeError, match="Refusing to call newNode"):
        handler.create_node("sbs::compositing::blur")

    assert created == []


def test_safe_connect_refuses_unvalidated_ports(handler):
    class Node:
        def __init__(self):
            self.connected = False

        def getProperties(self, category):
            return []

        def getIdentifier(self):
            return "n1"

        def newPropertyConnectionFromId(self, *args):
            self.connected = True
            return None

    source, target = Node(), Node()

    with pytest.raises(RuntimeError, match="Refusing to connect unvalidated"):
        handler._safe_connect(
            None, source, "unique_filter_output", target, "input1")

    assert source.connected is False
    assert target.connected is False


# ── P1: timeouts / dispatch cancellation ────────────────────────────────────

def test_parse_ports_filters_invalid(plugin_module):
    assert plugin_module._parse_ports("9881, 9882,bad,0,70000,9881") == [9881, 9882]
    assert plugin_module._parse_ports("") == []


def test_command_budget_stays_below_requested_timeout(plugin_module):
    for requested in (1, 20, 150, 300, 5000):
        budget = plugin_module._command_budget(requested)
        assert 0 < budget < requested
    assert plugin_module._command_budget(None) == plugin_module.COMMAND_TIMEOUT
    assert plugin_module._command_budget("nope") == plugin_module.COMMAND_TIMEOUT


def test_send_framed_resets_socket_timeout(plugin_module):
    class Sock:
        def __init__(self):
            self.timeouts = []
            self.sent = b""

        def settimeout(self, value):
            self.timeouts.append(value)

        def sendall(self, data):
            self.sent += data

    sock = Sock()
    plugin_module._send_framed(sock, b"payload")
    assert sock.timeouts == [plugin_module.SEND_TIMEOUT]


def test_dispatch_timeout_cancels_queued_call(plugin_module, monkeypatch):
    class StubInvoker:
        def __init__(self):
            self.calls = []

        def schedule(self, call):
            self.calls.append(call)

    invoker = StubInvoker()
    monkeypatch.setattr(plugin_module._Invoker, "instance",
                        classmethod(lambda cls: invoker))
    dispatcher = plugin_module.MainThreadDispatcher()
    out = {}

    def run():
        out["result"] = dispatcher.dispatch(lambda: 1, timeout=0.05)

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(2)

    assert not worker.is_alive()
    result, exc = out["result"]
    assert result is None
    assert isinstance(exc, TimeoutError)
    assert len(invoker.calls) == 1
    # The timed-out command must not execute when the main thread gets to it.
    assert invoker.calls[0].start() is False


# ── P1: JSON non-finite handling ────────────────────────────────────────────

def test_mark_nonfinite_adds_flags(plugin_module):
    flags = []
    result = plugin_module._mark_nonfinite(
        {"a": float("nan"), "b": [float("inf"), 1.0], "c": float("-inf")},
        flags)
    assert result == {"a": None, "b": [None, 1.0], "c": None}
    assert [(f["path"], f["value"]) for f in flags] == [
        ("$.a", "nan"), ("$.b[0]", "inf"), ("$.c", "-inf")]


def test_execute_safe_carries_nonfinite_flags(plugin_module):
    server = plugin_module.SDMCPServer(ports=[1])
    server.token = None

    class Stub:
        def dispatch(self, cmd_type, params):
            return {"value": float("nan"), "ok": 1.0}

    server._handler = Stub()
    response = server._execute_safe({"type": "x", "params": {}})
    assert response["status"] == "success"
    assert response["result"]["value"] is None
    assert response["non_finite_values"] == [
        {"path": "$.value", "value": "nan"}]


# ── P1: security / idempotency ──────────────────────────────────────────────

def test_token_compare_accepts_unicode_and_rejects_wrong(plugin_module):
    server = plugin_module.SDMCPServer(ports=[1])
    server.token = "sécret"

    class Stub:
        def __init__(self):
            self.calls = 0

        def dispatch(self, cmd_type, params):
            self.calls += 1
            return {"ok": True}

    stub = Stub()
    server._handler = stub

    wrong = server._execute_safe({"type": "x", "token": "wrong", "params": {}})
    assert wrong["status"] == "error"
    assert stub.calls == 0

    correct = server._execute_safe(
        {"type": "x", "token": "sécret", "params": {}})
    assert correct["status"] == "success"
    assert stub.calls == 1


def test_request_id_replays_cached_response(plugin_module):
    server = plugin_module.SDMCPServer(ports=[1])
    server.token = None

    class Stub:
        def __init__(self):
            self.calls = 0

        def dispatch(self, cmd_type, params):
            self.calls += 1
            return {"call": self.calls}

    stub = Stub()
    server._handler = stub
    command = {"type": "x", "params": {}, "request_id": "req-1"}

    first = server._execute_safe(command)
    second = server._execute_safe(command)

    assert stub.calls == 1
    assert first == second


def test_reload_plugin_requires_dev_gate(handler, monkeypatch):
    monkeypatch.delenv("SD_MCP_ALLOW_DEV_TOOLS", raising=False)
    monkeypatch.setattr(handler, "_execute_code_allowed", lambda: False)
    with pytest.raises(ValueError, match="reload_plugin is disabled"):
        handler.reload_plugin(handlers=False)


def test_dev_tools_gate_env(handler, monkeypatch):
    monkeypatch.setenv("SD_MCP_ALLOW_DEV_TOOLS", "1")
    assert handler._dev_tools_allowed() is True


# ── P1: library identifier resolution ───────────────────────────────────────

class _Res:
    def __init__(self, identifier, url):
        self.identifier = identifier
        self.url = url

    def getClassName(self):
        return "SDSBSCompGraph"

    def getIdentifier(self):
        return self.identifier

    def getUrl(self):
        return self.url


class _Pkg:
    def __init__(self, path, resources):
        self.path = path
        self.resources = resources

    def getFilePath(self):
        return self.path

    def getChildrenResources(self, recursive):
        return self.resources


class _Mgr:
    def __init__(self, packages):
        self.packages = packages

    def getPackages(self):
        return self.packages


def _wire_library(handler, monkeypatch, packages):
    monkeypatch.setattr(handler, "_ensure_library_loaded", lambda *a, **k: None)
    monkeypatch.setattr(handler, "_pkg_mgr", lambda: _Mgr(packages))


def test_resolve_lib_url_prefers_exact_identifier(handler, monkeypatch):
    _wire_library(handler, monkeypatch, [
        _Pkg("a.sbs", [_Res("cells_10", "pkg:///cells_10")]),
        _Pkg("b.sbs", [_Res("cells_1", "pkg:///cells_1")]),
    ])
    assert handler._resolve_lib_url("cells_1") == "pkg:///cells_1"


def test_resolve_lib_url_accepts_unique_substring(handler, monkeypatch):
    _wire_library(handler, monkeypatch, [
        _Pkg("a.sbs", [_Res("perlin_noise", "pkg:///perlin_noise")]),
    ])
    assert handler._resolve_lib_url("perlin") == "pkg:///perlin_noise"


def test_resolve_lib_url_errors_on_ambiguity(handler, monkeypatch):
    _wire_library(handler, monkeypatch, [
        _Pkg("a.sbs", [_Res("cells_1", "pkg:///a/cells_1")]),
        _Pkg("b.sbs", [_Res("cells_1", "pkg:///b/cells_1")]),
    ])
    with pytest.raises(ValueError, match="ambiguous"):
        handler._resolve_lib_url("cells_1")


# ── P1: per-client workers ──────────────────────────────────────────────────

def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_server_accepts_clients_while_one_is_busy(plugin_module):
    port = _free_port()
    server = plugin_module.SDMCPServer(ports=[port])
    entered = []
    entered_lock = threading.Lock()
    release = threading.Event()
    both = threading.Event()

    def fake_handle(client, addr, worker_port):
        with entered_lock:
            entered.append(threading.current_thread())
            if len(entered) >= 2:
                both.set()
        release.wait(3)
        client.close()

    server._handle_client = fake_handle
    server.start()
    clients = []
    try:
        clients.append(socket.create_connection(("127.0.0.1", port), timeout=2))
        clients.append(socket.create_connection(("127.0.0.1", port), timeout=2))
        assert both.wait(3), "second client was not accepted while the first was busy"
        assert len({id(thread) for thread in entered}) == 2
    finally:
        release.set()
        for client in clients:
            try:
                client.close()
            except OSError:
                pass
        server.stop()


# ── capture_node_preview ───────────────────────────────────────────────────

class _PreviewNode:
    def __init__(self, identifier):
        self.identifier = identifier

    def getIdentifier(self):
        return self.identifier


class _PreviewGraph:
    def __init__(self, nodes):
        self.nodes = {node.getIdentifier(): node for node in nodes}

    def getIdentifier(self):
        return "G"

    def getNodeFromId(self, node_id):
        return self.nodes.get(node_id)

    def getNodes(self):
        return list(self.nodes.values())


class _FakeImage:
    def __init__(self, width=64, height=64):
        self._width = width
        self._height = height

    def width(self):
        return self._width

    def height(self):
        return self._height

    def save(self, path):
        Path(path).write_bytes(b"png")
        return True


def test_capture_node_preview_is_registered(plugin_module):
    handler = plugin_module.CommandHandler()
    assert handler.HANDLERS["capture_node_preview"] == handler.capture_node_preview
    assert (plugin_module.CommandHandler._JOB_FACTORIES["capture_node_preview"]
            == "_gen_capture_node_preview")


def test_capture_node_preview_requires_a_target(handler):
    graph = _PreviewGraph([_PreviewNode("n1")])
    with pytest.raises(ValueError, match="node_id or node_ids"):
        handler._capture_node_preview_nodes(graph)


def test_capture_node_preview_dedupes_and_caps_targets(handler, monkeypatch):
    monkeypatch.setattr(handler, "_find_node",
                        lambda graph, node_id: _PreviewNode(node_id))
    monkeypatch.setattr(handler, "_map_label",
                        lambda node: node.getIdentifier().upper())
    graph = _PreviewGraph([])

    targets = handler._capture_node_preview_nodes(
        graph, "n1", ["n2", "n1", " "])
    assert [(node.getIdentifier(), label) for node, label in targets] == [
        ("n1", "N1"), ("n2", "N2")]

    with pytest.raises(ValueError, match="At most 32"):
        handler._capture_node_preview_nodes(
            graph, None, [str(index) for index in range(33)])


def test_capture_node_preview_single_returns_preview(handler, monkeypatch, tmp_path):
    node = _PreviewNode("n1")
    graph = _PreviewGraph([node])
    monkeypatch.setattr(handler, "_resolve_graph", lambda gid: graph)
    monkeypatch.setattr(handler, "_capture_node_preview_nodes",
                        lambda graph, node_id, node_ids: [(node, "Noise")])
    monkeypatch.setattr(handler, "_open_graph_view_for_preview",
                        lambda graph, wait_ms: (object(), 7))
    monkeypatch.setattr(handler, "_grab_node_preview",
                        lambda graph_view, view_id, node, wait_ms:
                        (_FakeImage(96, 80), "preview_widget"))
    monkeypatch.setattr(handler, "_downscale_preview",
                        lambda image, max_size: image)
    output = tmp_path / "preview.png"

    result = handler.capture_node_preview(
        node_id="n1", output_path=str(output))

    assert result["method"] == "node_preview"
    assert result["capture"] == "preview_widget"
    assert result["node"] == "n1"
    assert result["label"] == "Noise"
    assert result["source_width"] == 96
    assert result["source_height"] == 80
    assert Path(result["path"]).is_file()


def test_capture_node_preview_sheet_skips_failures(handler, monkeypatch, tmp_path):
    first, second = _PreviewNode("n1"), _PreviewNode("n2")
    graph = _PreviewGraph([first, second])
    monkeypatch.setattr(handler, "_resolve_graph", lambda gid: graph)
    monkeypatch.setattr(handler, "_capture_node_preview_nodes",
                        lambda graph, node_id, node_ids:
                        [(first, "A"), (second, "B")])
    monkeypatch.setattr(handler, "_open_graph_view_for_preview",
                        lambda graph, wait_ms: (object(), 7))

    def grab(graph_view, view_id, node, wait_ms):
        if node.getIdentifier() == "n2":
            raise RuntimeError("no preview")
        return _FakeImage(), "graph_view_crop"

    monkeypatch.setattr(handler, "_grab_node_preview", grab)
    monkeypatch.setattr(handler, "_downscale_preview",
                        lambda image, max_size: image)
    seen = {}

    def render_sheet(tiles, output_path, prefix):
        seen["tiles"] = tiles
        seen["prefix"] = prefix
        dest = tmp_path / "sheet.png"
        dest.write_bytes(b"png")
        return {
            "path": str(dest), "width": 100, "height": 100,
            "columns": 1, "rows": 1,
            "items": [{"usage": tile["usage"], "label": tile["label"],
                       "node": tile["node"], "port_id": tile["port_id"],
                       "source_width": tile["source_width"],
                       "source_height": tile["source_height"]}
                      for tile in tiles],
        }

    monkeypatch.setattr(handler, "_render_contact_sheet", render_sheet)

    result = handler.capture_node_preview(
        node_ids=["n1", "n2"], output_path=str(tmp_path / "sheet.png"))

    assert result["method"] == "node_preview_sheet"
    assert result["items"][0]["capture"] == "graph_view_crop"
    assert result["skipped"] == [
        {"node": "n2", "label": "B", "reason": "no preview"}]
    assert seen["prefix"] == "node_preview_sheet"
    assert len(seen["tiles"]) == 1
