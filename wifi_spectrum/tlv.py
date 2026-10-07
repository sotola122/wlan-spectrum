"""TLV binary frame codec.

Frame layout (little endian)::

    type      u8
    length    u16   (payload length in bytes; excludes header and checksum)
    payload   [length]
    crc32     u32   CRC-32/ISO-HDLC over the exact header+payload bytes
                    (zlib.crc32-compatible; poly 0xEDB88320, init/xorout
                    0xFFFFFFFF). Every frame in both directions carries it;
                    legacy CRC-less frames are rejected, never accepted.

Types:
    0x01 SPECTRUM : f_start_mhz f32, f_step_mhz f32, n u16, n x int16 (dBm * 100)
    0x02 CH_UTIL  : band u8, n u8, n x (ch u8, util_pct u8)
    0x03 STATUS   : UTF-8 JSON object  -or-  binary STATUS_BIN (see below)
    0x04 SPECTRUM_RF : see RF_HDR below (real RF snapshot FFT, centi-dBFS)
    0x10 CONFIG   : PC -> device (proposal): mode u8, band u8, sweep_ms u16,
                    fft_size u16, sample_rate_khz u32

Binary status (payload not starting with '{'):
    band u8, mode u8, sweep_count u32, uptime_ms u32, temp_c int8
"""

from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass, field

import numpy as np

T_SPECTRUM = 0x01
T_CH_UTIL = 0x02
T_STATUS = 0x03
T_SPECTRUM_RF = 0x04
T_CONFIG = 0x10

KNOWN_TYPES = {T_SPECTRUM, T_CH_UTIL, T_STATUS, T_SPECTRUM_RF, T_CONFIG}
MAX_PAYLOAD = 8192          # sanity limit used for resync on garbage
HDR = struct.Struct("<BH")
CRC = struct.Struct("<I")
SPEC_HDR = struct.Struct("<ffH")
STATUS_BIN = struct.Struct("<BBIIb")
CONFIG = struct.Struct("<BBHHI")
# epoch u32, cycle u32, band u8, channel u8, mode u8, rate_code u8,
# fft_size u16, source u16, center_khz u32, span_khz u32  (24 bytes,
# handoff v2 section 3; payload = 24 + 2 * fft_size)
RF_HDR = struct.Struct("<IIBBBBHHII")
RF_SOURCE_C5 = 0            # c5_snapshot_iq_fft; anything else is dropped
RF_MAX_BINS = 2036          # 24 + 2*N <= 4096 (FW payload cap)



def crc32(data: bytes) -> int:
    """CRC-32/ISO-HDLC over `data`. Check value: crc32(b'123456789') ==
    0xCBF43926."""
    return zlib.crc32(data) & 0xFFFFFFFF


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


@dataclass
class SpectrumRf:
    """One real RF snapshot-FFT frame (0x04). ``power_dbfs`` is per-bin
    power in dBFS (wire unit centi-dBFS int16 / 100) - a different newtype
    from dBm/RSSI: no conversion between them exists and none is offered.
    """

    epoch: int
    cycle: int
    band: int
    channel: int
    mode: int
    rate_code: int
    fft_size: int
    source: int
    center_khz: int
    span_khz: int
    power_dbfs: np.ndarray                 # float32, dBFS

    @property
    def freqs(self) -> np.ndarray:
        """Bin center frequencies in MHz, fftshift order (handoff v2.1
        section 3.1): f_k = center_khz - span_khz/2 + k*span_khz/N."""
        n = len(self.power_dbfs)
        f0 = self.center_khz - self.span_khz / 2.0
        return (f0 + np.arange(n, dtype=np.float64)
                * (self.span_khz / n)) / 1000.0


# ---------------------------------------------------------------- encode
def frame(t: int, payload: bytes) -> bytes:
    """One wire frame: header + payload + CRC over those exact bytes."""
    body = HDR.pack(t, len(payload)) + payload
    return body + CRC.pack(crc32(body))


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


def encode_spectrum_rf(epoch: int, cycle: int, band: int, channel: int,
                       mode: int, rate_code: int, source: int,
                       center_khz: int, span_khz: int, power_dbfs) -> bytes:
    """One 0x04 SPECTRUM_RF frame. Bin count is derived from the array
    (wire N always equals the bin count)."""
    raw = np.clip(np.round(np.asarray(power_dbfs, dtype=np.float64) * 100),
                  -32768, 32767).astype("<i2")
    payload = RF_HDR.pack(epoch, cycle, band, channel, mode, rate_code,
                          len(raw), source, center_khz, span_khz)
    return frame(T_SPECTRUM_RF, payload + raw.tobytes())


def encode_status_bin(band: int, mode: int, sweep_count: int, uptime_ms: int, temp_c: int) -> bytes:
    return frame(T_STATUS, STATUS_BIN.pack(band, mode, sweep_count, uptime_ms, temp_c))


def encode_config(mode: int, band: int, sweep_ms: int, fft_size: int, sample_rate_khz: int) -> bytes:
    return frame(T_CONFIG, CONFIG.pack(mode, band, sweep_ms, fft_size, sample_rate_khz))


# ---------------------------------------------------------------- decode
def _length_plausible(t: int, ln: int) -> bool:
    """Length constraints provable from the 3-byte header alone.

    CONFIG is exactly CONFIG.size bytes; SPECTRUM is SPEC_HDR.size plus two
    bytes per int16 sample; SPECTRUM_RF is RF_HDR.size plus two bytes per
    bin with an even bin count 2..RF_MAX_BINS, so its payload lives in
    28..4096 bytes mod 4 - the whole admissible range is decidable from
    the header alone; CH_UTIL is 2 + 2 bytes per entry. STATUS JSON has
    no such bound - its length is only decidable from payload bytes and
    the CRC, so no header-only claim can be judged for STATUS.
    """
    if t == T_CONFIG:
        return ln == CONFIG.size
    if t == T_SPECTRUM:
        return ln >= SPEC_HDR.size and (ln - SPEC_HDR.size) % 2 == 0
    if t == T_SPECTRUM_RF:
        # payload = 24 + 2*N, even N in [2, RF_MAX_BINS]: reject anything
        # outside 28..4096 from the 3-byte header instead of buffering up
        # to MAX_PAYLOAD for a false claim.
        return (RF_HDR.size + 4 <= ln <= RF_HDR.size + 2 * RF_MAX_BINS
                and (ln - RF_HDR.size) % 4 == 0)
    if t == T_CH_UTIL:
        return ln >= 2 and ln % 2 == 0
    return True


def _prefix_consistent(t: int, ln: int, buf: bytearray) -> bool:
    """Cross-check the header's length against the fixed payload prefix
    once those bytes are buffered. Called only while a candidate frame is
    still incomplete; reads format-declared offsets (CH_UTIL's entry count,
    SPECTRUM's sample count) and never scans into the payload body. Returns
    True while the prefix itself is incomplete so a valid fragmented frame
    keeps waiting."""
    if t == T_CH_UTIL:
        if len(buf) < HDR.size + 2:
            return True
        return ln == 2 + 2 * buf[HDR.size + 1]
    if t == T_SPECTRUM:
        if len(buf) < HDR.size + SPEC_HDR.size:
            return True
        samples = int.from_bytes(buf[HDR.size + 8:HDR.size + 10], "little")
        return ln == SPEC_HDR.size + 2 * samples
    if t == T_SPECTRUM_RF:
        if len(buf) < HDR.size + 14:       # fft_size sits at payload off 12
            return True
        bins = int.from_bytes(buf[HDR.size + 12:HDR.size + 14], "little")
        return ln == RF_HDR.size + 2 * bins
    return True


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
            try:
                data = json.loads(payload.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError("malformed status json") from exc
            if not isinstance(data, dict):
                raise ValueError("status json must be an object")
            return Status(data)
        if len(payload) != STATUS_BIN.size:
            raise ValueError("bad binary status")
        band, mode, cnt, up, temp = STATUS_BIN.unpack(payload)
        return Status({"band": band, "mode": mode, "sweep_count": cnt, "uptime_ms": up, "temp_c": temp})
    if t == T_SPECTRUM_RF:
        if len(payload) < RF_HDR.size:
            raise ValueError("short spectrum_rf header")
        (epoch, cycle, band, channel, mode, rate_code,
         n, source, center_khz, span_khz) = RF_HDR.unpack_from(payload)
        if n == 0 or n % 2:
            raise ValueError("fft_size must be a positive even number")
        if n > RF_MAX_BINS:
            raise ValueError("fft_size exceeds payload cap")
        if len(payload) != RF_HDR.size + 2 * n:
            raise ValueError("spectrum_rf length mismatch")
        if band not in (0, 1) or mode not in (0, 1) or not 1 <= channel <= 177:
            raise ValueError("invalid spectrum_rf enum")
        if source != RF_SOURCE_C5:
            raise ValueError("unknown spectrum_rf source")
        if span_khz <= 0:
            raise ValueError("non-positive span_khz")
        bins = np.frombuffer(payload, dtype="<i2", count=n,
                             offset=RF_HDR.size)
        return SpectrumRf(epoch, cycle, band, channel, mode, rate_code, n,
                          source, center_khz, span_khz,
                          bins.astype(np.float32) / 100.0)
    if t == T_CONFIG:
        mode, band, sweep_ms, fft, sr = CONFIG.unpack(payload)
        return {"mode": mode, "band": band, "sweep_ms": sweep_ms, "fft_size": fft, "sample_rate_khz": sr}
    raise ValueError(f"unknown type 0x{t:02x}")


class TlvParser:
    """Incremental stream parser. Feed arbitrary byte chunks, get messages back.

    The frame format has no sync word: a candidate frame must pass its CRC
    before any decoding happens. Impossible type/length combinations are
    rejected from the header - and SPECTRUM/CH_UTIL from their fixed-size
    payload prefix - *before* the advertised length is buffered; a STATUS
    JSON length is only decidable from its payload, so a plausible long
    STATUS claim keeps buffering until it is filled. On an unknown type, an
    impossible length, a CRC mismatch, or a payload that fails to decode we
    drop one byte and retry, so corrupted frames, legacy CRC-less frames,
    and garbage all resynchronize. Recovery is bounded in BYTES but not in
    TIME: after ``feed`` returns, the residual buffer never exceeds
    HDR + MAX_PAYLOAD + CRC (one ``feed`` call first appends its whole
    chunk, so inside the call the transient size is that bound plus the
    chunk), and a finite stream of corrupt + valid bytes may stay pending
    until enough bytes arrive. `errors` counts discarded bytes, not frames.
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
            if (t not in KNOWN_TYPES or ln > MAX_PAYLOAD
                    or not _length_plausible(t, ln)):
                del buf[0]
                self.errors += 1
                continue
            total = HDR.size + ln + CRC.size
            if len(buf) < total:
                if not _prefix_consistent(t, ln, buf):
                    del buf[0]      # header length contradicts the prefix
                    self.errors += 1
                    continue
                break                           # wait for more data
            body = bytes(buf[:HDR.size + ln])
            wire_crc, = CRC.unpack_from(buf, HDR.size + ln)
            if crc32(body) != wire_crc:
                del buf[0]                     # CRC failure: resync
                self.errors += 1
                continue
            payload = body[HDR.size:]
            try:
                msg = decode(t, payload)
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError, struct.error):
                del buf[0]
                self.errors += 1
                continue
            del buf[:total]
            out.append(msg)
        return out
