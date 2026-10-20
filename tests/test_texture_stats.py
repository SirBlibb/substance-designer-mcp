"""Unit tests for plugin/texture_stats.py (pure Python, loaded without the sd package)."""
import importlib.util
import struct
from pathlib import Path

_path = Path(__file__).resolve().parents[1] / "plugin" / "texture_stats.py"
_spec = importlib.util.spec_from_file_location("texture_stats", _path)
texture_stats = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(texture_stats)


def _stats(buffer, width, height, row_pitch, fmt, bins=16):
    return texture_stats.stats_from_buffer(buffer, width, height, row_pitch, fmt, bins)


def test_lum8_basic_normalization_and_histogram():
    result = _stats(bytes([0, 64, 192, 255]), 2, 2, 2, "LUM8", bins=4)
    assert result["width"] == 2 and result["height"] == 2
    assert result["value_domain"] == "normalized_0_1"
    assert result["bytes_per_pixel"] == 1
    gray = result["channels"]["gray"]
    assert gray["min"] == 0.0
    assert gray["max"] == 1.0
    assert gray["mean"] == 511 / 4 / 255
    assert gray["count"] == 4
    assert gray["non_finite"] == 0
    assert gray["histogram"]["counts"] == [1, 1, 0, 2]
    assert gray["histogram"]["range"] == [0.0, 1.0]


def test_bgr8_is_reordered_to_rgb():
    result = _stats(bytes([30, 20, 10]), 1, 1, 3, "BGR8")
    channels = result["channels"]
    assert channels["r"]["mean"] == 10 / 255
    assert channels["g"]["mean"] == 20 / 255
    assert channels["b"]["mean"] == 30 / 255


def test_lum16_normalized():
    result = _stats(struct.pack("<2H", 0, 65535), 2, 1, 4, "LUM16")
    gray = result["channels"]["gray"]
    assert gray["min"] == 0.0
    assert gray["max"] == 1.0
    assert gray["mean"] == 0.5
    assert result["value_domain"] == "normalized_0_1"


def test_rgb16f_non_finite_excluded():
    nan, inf = float("nan"), float("inf")
    buffer = struct.pack("<6e", 0.0, 0.5, 1.0, 1.0, nan, inf)
    result = _stats(buffer, 2, 1, 12, "RGB16F", bins=4)
    assert result["value_domain"] == "native_float"
    r, g, b = (result["channels"][name] for name in ("r", "g", "b"))
    assert (r["min"], r["max"], r["mean"], r["count"], r["non_finite"]) == (
        0.0, 1.0, 0.5, 2, 0)
    assert r["histogram"]["counts"] == [1, 0, 0, 1]
    assert (g["min"], g["max"], g["mean"], g["count"], g["non_finite"]) == (
        0.5, 0.5, 0.5, 1, 1)
    assert g["histogram"]["counts"] == [1, 0, 0, 0]
    assert (b["min"], b["max"], b["mean"], b["count"], b["non_finite"]) == (
        1.0, 1.0, 1.0, 1, 1)
    assert b["histogram"]["counts"] == [1, 0, 0, 0]


def test_negative_row_pitch_walks_backwards():
    # Row 0 sits at offset 2, row 1 at offset 0 in memory.
    result = _stats(bytes([1, 2, 10, 20]), 2, 2, -2, "LUM8")
    gray = result["channels"]["gray"]
    assert gray["min"] == 1 / 255
    assert gray["max"] == 20 / 255
    assert gray["count"] == 4


def test_all_non_finite_channel_reports_no_range():
    result = _stats(struct.pack("<f", float("nan")), 1, 1, 4, "LUM32F", bins=4)
    gray = result["channels"]["gray"]
    assert gray["min"] is None and gray["max"] is None and gray["mean"] is None
    assert gray["count"] == 0
    assert gray["non_finite"] == 1
    assert gray["histogram"]["range"] is None
    assert gray["histogram"]["counts"] == [0, 0, 0, 0]


def test_large_u16_map_histogram_is_sampled():
    pixels = 2048 * 1024
    buffer = struct.pack("<%dH" % pixels,
                         *([0] * (pixels // 2) + [65535] * (pixels // 2)))
    result = _stats(buffer, 2048, 1024, 2048 * 2, "LUM16", bins=16)
    gray = result["channels"]["gray"]
    assert gray["min"] == 0.0 and gray["max"] == 1.0
    assert gray["count"] == pixels
    histogram = gray["histogram"]
    assert histogram["sample_stride"] == 2
    assert sum(histogram["counts"]) == pixels // 2
    assert histogram["counts"][0] == pixels // 4
    assert histogram["counts"][-1] == pixels // 4
    assert sum(histogram["counts"][1:-1]) == 0


def test_padded_pitch_processes_per_row():
    # LUM8 rows padded to 3 bytes: [10,20,_][30,40,_]
    result = _stats(bytes([10, 20, 0, 30, 40, 0]), 2, 2, 3, "LUM8", bins=4)
    gray = result["channels"]["gray"]
    assert gray["min"] == 10 / 255
    assert gray["max"] == 40 / 255
    assert gray["count"] == 4
    assert sum(gray["histogram"]["counts"]) == 4
    # LUM16 rows padded to 6 bytes: rows at offsets 0 and 6
    buffer = struct.pack("<2H2x2H2x", 1000, 2000, 3000, 4000)
    result = _stats(buffer, 2, 2, 6, "LUM16", bins=4)
    gray = result["channels"]["gray"]
    assert gray["min"] == 1000 / 65535
    assert gray["max"] == 4000 / 65535
    assert gray["count"] == 4
    assert sum(gray["histogram"]["counts"]) == 4


def test_many_bins_uses_counter_fallback():
    result = _stats(bytes([0, 85, 170, 255]), 2, 2, 2, "LUM8", bins=100)
    gray = result["channels"]["gray"]
    assert len(gray["histogram"]["counts"]) == 100
    assert sum(gray["histogram"]["counts"]) == 4


def test_uniform_channel_lands_in_first_bin():
    result = _stats(bytes([7, 7, 7, 7]), 2, 2, 2, "LUM8", bins=4)
    gray = result["channels"]["gray"]
    assert gray["histogram"]["counts"] == [4, 0, 0, 0]


def test_rejects_unsupported_format():
    try:
        _stats(b"", 1, 1, 1, "NOPE8")
    except ValueError as exc:
        assert "Unsupported SDTexture pixel format" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_rejects_buffer_too_small():
    try:
        _stats(bytes(3), 2, 2, 2, "LUM8")
    except ValueError as exc:
        assert "too small" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_rejects_bins_out_of_range():
    try:
        _stats(bytes(4), 2, 2, 2, "LUM8", bins=0)
    except ValueError as exc:
        assert "histogram_bins" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_rejects_pitch_smaller_than_row():
    try:
        _stats(bytes(4), 2, 2, 1, "LUM8")
    except ValueError as exc:
        assert "row pitch" in str(exc)
    else:
        raise AssertionError("expected ValueError")
