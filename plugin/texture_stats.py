"""
texture_stats.py - pure-Python per-channel statistics for SDTexture buffers.

Kept free of sd.api/PySide imports so it can be unit-tested outside Substance
Designer and hot-reloaded by the plugin's reload_plugin command.

Speed matters here: get_map_stats and inspect_outputs run over multi-megapixel
textures, where a naive per-pixel Python loop is ~100x too slow. The three
format families take different routes:

* 8-bit integers: work on whole byte slices per channel. min/max/sum run as
  C built-ins over bytes, and the histogram is bytes.translate into bucket
  bytes followed by bytes.count per bucket (bins <= 64; Counter otherwise).
  Histograms are exact.
* 16-bit integers: one struct.unpack per row; per-channel tuple slices give
  exact min/max/sum, while evenly strided samples are collected into compact
  array('H') per channel and bucketed once lo/hi are known.
* float formats: one row pass with the same exact min/max/sum plus sample
  collection into array('e'/'f'); the histogram buckets samples through
  bisect_right over interior bin edges (matches int() binning at exact
  edges).

Histograms for 16-bit/float formats use at most MAX_HISTOGRAM_SAMPLES samples
per channel (stride 1, i.e. exact counts, for maps up to that many pixels);
histogram.sample_stride reports the stride. Non-finite float samples are
excluded from min/max/mean/histograms and counted exactly in 'non_finite'.
"""
import math
import struct
from array import array
from bisect import bisect_right
from collections import Counter
from itertools import repeat

# pixel_format name -> (struct code, bytes per component, integer max or None
# for float formats, channel names, channel order in the buffer). Integer
# samples are reported in their normalized 0..1 domain; float formats retain
# their stored values. BGR storage is reordered to RGB.
FORMATS = {
    "LUM8":        ("B", 1, 255.0, ("gray",),                (0,)),
    "LUMALPHA8":   ("B", 1, 255.0, ("gray", "alpha"),       (0, 1)),
    "RGB8":        ("B", 1, 255.0, ("r", "g", "b"),         (0, 1, 2)),
    "BGR8":        ("B", 1, 255.0, ("r", "g", "b"),         (2, 1, 0)),
    "RGBA8":       ("B", 1, 255.0, ("r", "g", "b", "alpha"), (0, 1, 2, 3)),
    "BGRA8":       ("B", 1, 255.0, ("r", "g", "b", "alpha"), (2, 1, 0, 3)),
    "LUM16":       ("H", 2, 65535.0, ("gray",),              (0,)),
    "LUMALPHA16":  ("H", 2, 65535.0, ("gray", "alpha"),     (0, 1)),
    "RGB16":       ("H", 2, 65535.0, ("r", "g", "b"),       (0, 1, 2)),
    "RGBA16":      ("H", 2, 65535.0, ("r", "g", "b", "alpha"), (0, 1, 2, 3)),
    "LUM16F":      ("e", 2, None, ("gray",),                (0,)),
    "LUMALPHA16F": ("e", 2, None, ("gray", "alpha"),        (0, 1)),
    "RGB16F":      ("e", 2, None, ("r", "g", "b"),          (0, 1, 2)),
    "RGBA16F":     ("e", 2, None, ("r", "g", "b", "alpha"), (0, 1, 2, 3)),
    "LUM32F":      ("f", 4, None, ("gray",),                (0,)),
    "LUMALPHA32F": ("f", 4, None, ("gray", "alpha"),        (0, 1)),
    "RGB32F":      ("f", 4, None, ("r", "g", "b"),          (0, 1, 2)),
    "RGBA32F":     ("f", 4, None, ("r", "g", "b", "alpha"), (0, 1, 2, 3)),
}

# bins <= this use bytes.translate + bytes.count for 8-bit histograms;
# larger bin counts fall back to Counter(map(lut.__getitem__, col)).
_TRANSLATE_MAX_BINS = 64

# Histograms for 16-bit and float formats are built from at most this many
# evenly strided samples per channel (stride 1, i.e. exact, for maps up to
# this many pixels). Sorting every raw value into bucket dicts costs ~80x
# more than an evenly spaced sample pass and changes nothing for the
# distribution shape. min/max/mean/count/non_finite stay exact regardless.
MAX_HISTOGRAM_SAMPLES = 1000000


def _int_bucket_lut(raw_low, raw_high, span, bins):
    """raw value -> histogram bucket, matching int((v-low)*bins/(high-low))."""
    if raw_low is None or raw_high <= raw_low:
        return [0] * span
    scale = bins / float(raw_high - raw_low)
    low = raw_low
    last = bins - 1
    lut = [0] * span
    for raw in range(span):
        bucket = int((raw - low) * scale)
        lut[raw] = 0 if bucket < 0 else (last if bucket > last else bucket)
    return lut


def stats_from_buffer(buffer, width, height, row_pitch, format_name, bins=16):
    """Per-channel min/max/mean + histogram for one raw texture buffer.

    buffer: the texture's pixel bytes. Row y starts at
    max(0, -(height-1)*row_pitch) + y*row_pitch, so negative pitches walk
    backwards through the buffer. width/height are in pixels; bins is the
    histogram bucket count (1..512). Histograms use each channel's observed
    finite min/max range and count every value for 8-bit and small
    (<= MAX_HISTOGRAM_SAMPLES pixel) 16-bit/float textures; larger maps are
    histogrammed from evenly strided samples (histogram.sample_stride).
    Returns:

    {width, height, pixel_format, bytes_per_pixel, row_pitch, value_domain,
     channels: {name: {min, max, mean, count, non_finite,
                       histogram: {bins, range, counts}}}}
    """
    spec = FORMATS.get(format_name)
    if spec is None:
        raise ValueError(
            "Unsupported SDTexture pixel format '{}'.".format(format_name))
    if bins < 1 or bins > 512:
        raise ValueError("histogram_bins must be between 1 and 512.")
    code, component_bytes, integer_max, channel_names, order = spec
    component_count = len(order)
    bytes_per_pixel = component_bytes * component_count
    row_size = width * bytes_per_pixel
    if width <= 0 or height <= 0:
        raise ValueError(
            "Unexpected SDTexture layout: {}x{}.".format(width, height))
    if abs(row_pitch) < row_size:
        raise ValueError(
            "SDTexture row pitch {} is smaller than its {}-byte row."
            .format(row_pitch, row_size))
    base = max(0, -(height - 1) * row_pitch)
    last_row = base + (height - 1) * row_pitch
    needed = max(base, last_row) + row_size
    if len(buffer) < needed:
        raise ValueError(
            "Texture buffer too small: {} bytes for a {}x{} {} texture."
            .format(len(buffer), width, height, format_name))

    is_int = integer_max is not None
    rows = [base + y * row_pitch for y in range(height)]
    non_finite = [0] * component_count
    histograms = [[0] * bins for _ in channel_names]
    sample_stride = 1  # overridden for 16-bit/float formats (see module doc)

    if component_bytes == 1:
        # ── 8-bit integer formats: byte-slice operations ──
        # Aggregates are order-independent, so a contiguous buffer (pitch ==
        # row size, either sign) is processed as one segment.
        if abs(row_pitch) == row_size:
            segments = [(0, height * row_size)]
        else:
            segments = [(start, row_size) for start in rows]
        counts = [width * height] * component_count
        raw_min = [None] * component_count
        raw_max = [None] * component_count
        raw_sum = [0] * component_count
        for out_index, raw_index in enumerate(order):
            cmin, cmax, csum = 256, -1, 0
            for start, length in segments:
                col = buffer[start + raw_index:start + length:component_count]
                value = min(col)
                if value < cmin:
                    cmin = value
                value = max(col)
                if value > cmax:
                    cmax = value
                csum += sum(col)
            raw_min[out_index] = cmin
            raw_max[out_index] = cmax
            raw_sum[out_index] = csum
        for out_index, raw_index in enumerate(order):
            histogram = histograms[out_index]
            lut = _int_bucket_lut(raw_min[out_index], raw_max[out_index],
                                  256, bins)
            if bins <= _TRANSLATE_MAX_BINS:
                translate = bytes(lut)
                bucket_range = range(bins)
                for start, length in segments:
                    translated = buffer[
                        start + raw_index:start + length:component_count
                    ].translate(translate)
                    for bucket in bucket_range:
                        histogram[bucket] += translated.count(bucket)
            else:
                get_bucket = lut.__getitem__
                for start, length in segments:
                    col = buffer[
                        start + raw_index:start + length:component_count]
                    for bucket, occurrences in Counter(
                            map(get_bucket, col)).items():
                        histogram[bucket] += occurrences
    elif code == "H":
        # ── 16-bit integer formats: one unpack per row ──
        # Exact min/max/sum from every value; histogram samples are collected
        # in the same pass as compact arrays and bucketed once lo/hi is known.
        counts = [width * height] * component_count
        sample_stride = max(1, (width * height) // MAX_HISTOGRAM_SAMPLES)
        raw_min = [None] * component_count
        raw_max = [None] * component_count
        raw_sum = [0] * component_count
        samples = [array("H") for _ in channel_names]
        unpack_row = struct.Struct(
            "<" + str(row_size // 2) + "H").unpack_from
        for start in rows:
            values = unpack_row(buffer, start)
            for out_index, raw_index in enumerate(order):
                col = values if component_count == 1 \
                    else values[raw_index::component_count]
                value = min(col)
                if raw_min[out_index] is None or value < raw_min[out_index]:
                    raw_min[out_index] = value
                value = max(col)
                if raw_max[out_index] is None or value > raw_max[out_index]:
                    raw_max[out_index] = value
                raw_sum[out_index] += sum(col)
                samples[out_index].extend(col[::sample_stride])
        for out_index in range(component_count):
            lut = _int_bucket_lut(raw_min[out_index], raw_max[out_index],
                                  65536, bins)
            histogram = histograms[out_index]
            for bucket, occurrences in Counter(
                    map(lut.__getitem__, samples[out_index])).items():
                histogram[bucket] += occurrences
            samples[out_index] = None  # release before returning
    else:
        # ── float formats: two row passes ──
        isfinite = math.isfinite
        counts = [0] * component_count
        values_min = [None] * component_count
        values_max = [None] * component_count
        values_sum = [0.0] * component_count
        unpack_row = struct.Struct(
            "<" + str(width * component_count) + code).unpack_from
        sample_stride = max(1, (width * height) // MAX_HISTOGRAM_SAMPLES)
        # raw samples stay exact: 'e'/'f' both round-trip through float32.
        samples = [array("f") for _ in channel_names]
        for start in rows:
            values = unpack_row(buffer, start)
            for out_index, raw_index in enumerate(order):
                col = values if component_count == 1 \
                    else values[raw_index::component_count]
                finite = sum(map(isfinite, col))
                if finite != width:
                    # Rare: fall back to per-value checks for this row.
                    non_finite[out_index] += width - finite
                    for value in col:
                        if not isfinite(value):
                            continue
                        if (values_min[out_index] is None
                                or value < values_min[out_index]):
                            values_min[out_index] = value
                        if (values_max[out_index] is None
                                or value > values_max[out_index]):
                            values_max[out_index] = value
                        values_sum[out_index] += value
                    counts[out_index] += finite
                else:
                    value = min(col)
                    if (values_min[out_index] is None
                            or value < values_min[out_index]):
                        values_min[out_index] = value
                    value = max(col)
                    if (values_max[out_index] is None
                            or value > values_max[out_index]):
                        values_max[out_index] = value
                    values_sum[out_index] += sum(col)
                    counts[out_index] += width
                samples[out_index].extend(col[::sample_stride])
        for out_index in range(component_count):
            low = values_min[out_index]
            high = values_max[out_index]
            if low is None or high < low:
                continue
            histogram = histograms[out_index]
            if high <= low:
                histogram[0] = counts[out_index]
                continue
            sample_col = samples[out_index]
            if non_finite[out_index]:
                scale = bins / (high - low)
                for value in sample_col:
                    if not isfinite(value):
                        continue
                    bucket = int((value - low) * scale)
                    histogram[0 if bucket < 0
                              else (bins - 1 if bucket >= bins
                                    else bucket)] += 1
            else:
                # bisect_right over interior bin edges matches int() binning.
                edges = [low + (high - low) * i / bins
                         for i in range(1, bins)]
                for bucket, occurrences in Counter(
                        map(bisect_right, repeat(edges), sample_col)).items():
                    histogram[bucket] += occurrences
            samples[out_index] = None  # release before returning

    # ── assemble the per-channel result ──
    channels = {}
    for index, name in enumerate(channel_names):
        count = counts[index]
        if count:
            if is_int:
                cmin = raw_min[index] / integer_max
                cmax = raw_max[index] / integer_max
                cmean = raw_sum[index] / count / integer_max
            else:
                cmin = values_min[index]
                cmax = values_max[index]
                cmean = values_sum[index] / count
        else:
            cmin = cmax = cmean = None
        channels[name] = {
            "min": cmin,
            "max": cmax,
            "mean": cmean,
            "count": count,
            "non_finite": non_finite[index],
            "histogram": {
                "bins": bins,
                "range": [cmin, cmax] if count else None,
                "counts": histograms[index],
                "sample_stride": sample_stride,
            },
        }
    return {
        "width": width,
        "height": height,
        "pixel_format": format_name,
        "bytes_per_pixel": bytes_per_pixel,
        "row_pitch": row_pitch,
        "value_domain": ("normalized_0_1" if is_int else "native_float"),
        "channels": channels,
    }
