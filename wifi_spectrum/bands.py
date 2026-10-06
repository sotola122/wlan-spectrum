"""Band / channel tables for 2.4 GHz and 5 GHz Wi-Fi."""

from __future__ import annotations

from dataclasses import dataclass

BAND_24 = 0
BAND_5 = 1


@dataclass(frozen=True)
class BandInfo:
    band_id: int
    name: str
    f_start: float      # display range start (MHz)
    f_stop: float       # display range end (MHz)
    step: float         # display grid resolution (MHz)
    channels: tuple[int, ...]
    ch_width: float     # nominal channel bandwidth (MHz) for bars / masks

    @property
    def n_points(self) -> int:
        return int(round((self.f_stop - self.f_start) / self.step)) + 1


def channel_freq(band_id: int, ch: int) -> float:
    """Center frequency of a 20 MHz channel in MHz."""
    if band_id == BAND_24:
        return 2484.0 if ch == 14 else 2407.0 + 5.0 * ch
    return 5000.0 + 5.0 * ch


_CH_5G = tuple(range(36, 65, 4)) + tuple(range(100, 145, 4)) + tuple(range(149, 178, 4))

BANDS: dict[int, BandInfo] = {
    BAND_24: BandInfo(BAND_24, "2.4 GHz", 2400.0, 2500.0, 0.5, tuple(range(1, 15)), 20.0),
    BAND_5: BandInfo(BAND_5, "5 GHz", 5150.0, 5895.0, 1.0, _CH_5G, 20.0),
}
