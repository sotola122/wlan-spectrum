"""Display-only frequency-axis smoothing for the spectrum current curve."""

from __future__ import annotations

import numpy as np


def smooth_frequency_bins(values: np.ndarray, width: int) -> np.ndarray:
    """Odd width in frequency bins along the display grid.

    Smoothing is applied independently within each contiguous run of
    finite bins. NaN gaps split runs; windows never cross a gap. Each
    output bin is the arithmetic mean of display values (dBm or dBFS) in
    the clipped window — visual smoothing only, not linear power.
    """
    if width < 3:
        return np.asarray(values, dtype=np.float32, order="C").copy()
    if width % 2 == 0:
        width += 1
    half = width // 2
    src = np.asarray(values, dtype=np.float32)
    out = np.full(src.shape, np.nan, dtype=np.float32)
    n = len(src)
    i = 0
    while i < n:
        if not np.isfinite(src[i]):
            i += 1
            continue
        start = i
        while i < n and np.isfinite(src[i]):
            i += 1
        end = i
        for j in range(start, end):
            lo = max(start, j - half)
            hi = min(end, j + half + 1)
            out[j] = float(np.mean(src[lo:hi]))
    return out
