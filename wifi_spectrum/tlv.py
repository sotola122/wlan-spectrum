"""TLV binary frame codec.

Frame layout (little endian)::

    type   u8
    length u16   (payload length in bytes)
    payload[length]

Types:
    0x01 SPECTRUM : f_start_mhz f32, f_step_mhz f32, n u16, n x int16 (dBm * 100)
    0x02 CH_UTIL  : band u8, n u8, n x (ch u8, util_pct u8)
    0x03 STATUS   : UTF-8 JSON object  -or-  binary STATUS_BIN (see below)
    0x10 CONFIG   : PC -> device (proposal): mode u8, band u8, sweep_ms u16,
                    fft_size u16, sample_rate_khz u32

Binary status (payload not starting with '{'):
    band u8, mode u8, sweep_count u32, uptime_ms u32, temp_c int8
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field

import numpy as np

T_SPECTRUM = 0x01
T_CH_UTIL = 0x02
T_STATUS = 0x03
T_CONFIG = 0x10

KNOWN_TYPES = {T_SPECTRUM, T_CH_UTIL, T_STATUS, T_CONFIG}
MAX_PAYLOAD = 8192          # sanity limit used for resync on garbage
HDR = struct.Struct("<BH")
SPEC_HDR = struct.Struct("<ffH")
STATUS_BIN = struct.Struct("<BBIIb")
CONFIG = struct.Struct("<BBHHI")


# ---------------------------------------------------------------- messages
@dataclass
class Spectrum:
    f_start: float
    f_step: float
    dbm: np.ndarray                     # float32 array, dBm

    @property
    def freqs(self) -> np.ndarray:
        return self.f_start + self.f_step * np.arange(len(self.dbm), dtype=np.float64)


@dataclass
class ChannelUtil:
    band: int
    util: dict[int, int] = field(default_factory=dict)   # ch -> %


@dataclass
class Status:
    data: dict


# ---------------------------------------------------------------- encode
def frame(t: int, payload: bytes) -> bytes:
    return HDR.pack(t, len(payload)) + payload


def encode_spectrum(f_start: float, f_step: float, dbm) -> bytes:
    raw = np.clip(np.round(np.asarray(dbm, dtype=np.float64) * 100), -32768, 32767)
    raw = raw.astype("<i2")
    return frame(T_SPECTRUM, SPEC_HDR.pack(f_start, f_step, len(raw)) + raw.tobytes())


def encode_ch_util(band: int, util: dict[int, int]) -> bytes:
    body = bytes([band & 0xFF, len(util)])
    for ch, u in util.items():
        body += bytes([ch & 0xFF, max(0, min(100, int(u)))])
    return frame(T_CH_UTIL, body)


def encode_status_json(d: dict) -> bytes:
    return frame(T_STATUS, json.dumps(d, separators=(",", ":")).encode())


def encode_status_bin(band: int, mode: int, sweep_count: int, uptime_ms: int, temp_c: int) -> bytes:
    return frame(T_STATUS, STATUS_BIN.pack(band, mode, sweep_count, uptime_ms, temp_c))


def encode_config(mode: int, band: int, sweep_ms: int, fft_size: int, sample_rate_khz: int) -> bytes:
    return frame(T_CONFIG, CONFIG.pack(mode, band, sweep_ms, fft_size, sample_rate_khz))


# ---------------------------------------------------------------- decode
def decode(t: int, payload: bytes):
    """Decode one payload into a message object. Raises ValueError if malformed."""
    if t == T_SPECTRUM:
        if len(payload) < SPEC_HDR.size:
            raise ValueError("short spectrum header")
        f0, df, n = SPEC_HDR.unpack_from(payload)
        if len(payload) != SPEC_HDR.size + 2 * n:
            raise ValueError("spectrum length mismatch")
        raw = np.frombuffer(payload, dtype="<i2", count=n, offset=SPEC_HDR.size)
        return Spectrum(f0, df, raw.astype(np.float32) / 100.0)
    if t == T_CH_UTIL:
        if len(payload) < 2 or len(payload) != 2 + 2 * payload[1]:
            raise ValueError("ch util length mismatch")
        util = {payload[2 + 2 * i]: payload[3 + 2 * i] for i in range(payload[1])}
        return ChannelUtil(payload[0], util)
    if t == T_STATUS:
        if payload[:1] == b"{":
            return Status(json.loads(payload.decode("utf-8")))
        if len(payload) != STATUS_BIN.size:
            raise ValueError("bad binary status")
        band, mode, cnt, up, temp = STATUS_BIN.unpack(payload)
        return Status({"band": band, "mode": mode, "sweep_count": cnt, "uptime_ms": up, "temp_c": temp})
    if t == T_CONFIG:
        mode, band, sweep_ms, fft, sr = CONFIG.unpack(payload)
        return {"mode": mode, "band": band, "sweep_ms": sweep_ms, "fft_size": fft, "sample_rate_khz": sr}
    raise ValueError(f"unknown type 0x{t:02x}")


class TlvParser:
    """Incremental stream parser. Feed arbitrary byte chunks, get messages back.

    The frame format has no sync word, so on an unknown type, an oversized
    length or a payload that fails to decode we drop one byte and retry.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self.errors = 0

    def feed(self, data: bytes) -> list:
        self._buf += data
        out = []
        buf = self._buf
        while len(buf) >= HDR.size:
            t, ln = HDR.unpack_from(buf)
            if t not in KNOWN_TYPES or ln > MAX_PAYLOAD:
                del buf[0]
                self.errors += 1
                continue
            if len(buf) < HDR.size + ln:
                break                           # wait for more data
            payload = bytes(buf[HDR.size:HDR.size + ln])
            try:
                msg = decode(t, payload)
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError, struct.error):
                del buf[0]
                self.errors += 1
                continue
            del buf[:HDR.size + ln]
            out.append(msg)
        return out
