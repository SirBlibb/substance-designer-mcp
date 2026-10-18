"""Unit tests for plugin/port_suggest.py (pure Python, loaded without the sd package)."""
import importlib.util
from pathlib import Path

_path = Path(__file__).resolve().parents[1] / "plugin" / "port_suggest.py"
_spec = importlib.util.spec_from_file_location("port_suggest", _path)
port_suggest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(port_suggest)


def test_rank_ports_closest_first():
    assert port_suggest.rank_ports("sourc", ["opacity", "destination", "source"])[0] == "source"
    assert port_suggest.rank_ports("opactiy", ["source", "destination", "opacity"])[0] == "opacity"
    assert port_suggest.rank_ports("input", ["inputgradient", "input1"])[0] == "input1"


def test_rank_ports_substring_beats_fuzzy():
    ranked = port_suggest.rank_ports("output", ["outpt", "Noise_Output"])
    assert ranked[0] == "Noise_Output"


def test_port_error_lists_valid_ids():
    msg = port_suggest.port_error(
        "Input", "input1", "'42' (sbs::compositing::output)", ["inputNodeOutput"],
        "Output", ["output"])
    assert "Input port 'input1' not found on node '42'" in msg
    assert "Did you mean 'inputNodeOutput'?" in msg
    assert "Output port IDs: ['output']" in msg


def test_port_error_without_ports():
    msg = port_suggest.port_error("Output", "x", "'1'", [])
    assert "has no output ports" in msg
