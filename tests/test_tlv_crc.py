"""CRC-32 framing contract for the Python codec.

Expected values are literal published/data constants, never manufactured by
the implementation under test: the CRC-32/ISO-HDLC check value 123456789 =
0xCBF43926 and the golden CONFIG frame bytes from the wire contract.
"""

from __future__ import annotations

import unittest
import zlib

from wifi_spectrum import tlv
from wifi_spectrum.tlv import Status, TlvParser, crc32, encode_config

# Golden CONFIG: Band Sweep, 5 GHz, 1000 ms, FFT 64, 20 MS/s + CRC 95 56 4e 1d
CONFIG_GOLDEN = bytes.fromhex("100a000101e8034000204e000095564e1d")

# Golden SPECTRUM_RF 0x04 (handoff v2 section 7, parent-verified):
# epoch=1 cycle=0 band=0 ch=1 mode=0 rate_code=0 fft_size=8 source=0
# center_khz=2412000 span_khz=20000 power_dbfs=[0,-100,...,-700] centi-dBFS
# crc32 value 0x23877a9f (LE bytes 9f7a8723)
SPECTRUM_RF_GOLDEN = bytes.fromhex(
    "04280001000000000000000001000008000000e0cd2400204e0000"
    "00009cff38ffd4fe70fe0cfea8fd44fd9f7a8723")
RF_GOLDEN_FREQS_MHZ = [2402.0, 2404.5, 2407.0, 2409.5, 2412.0,
                       2414.5, 2417.0, 2419.5]


def parse(*chunks: bytes) -> tuple[list, int]:
    parser = TlvParser()
    messages: list = []
    for chunk in chunks:
        messages.extend(parser.feed(chunk))
    return messages, parser.errors


class CrcKnownAnswerTests(unittest.TestCase):
    def test_check_value_123456789(self) -> None:
        self.assertEqual(crc32(b"123456789"), 0xCBF43926)

    def test_check_value_against_zlib_itself(self) -> None:
        # zlib is the independent reference implementation.
        self.assertEqual(crc32(b"123456789"), zlib.crc32(b"123456789"))

    def test_config_frame_matches_published_golden_bytes(self) -> None:
        frame = encode_config(1, 1, 1000, 64, 20000)
        self.assertEqual(frame, CONFIG_GOLDEN)
        # and the embedded checksum verifies against the first 13 bytes
        body, wire = frame[:13], frame[13:]
        self.assertEqual(wire, (0x1d4e5695).to_bytes(4, "little"))
        self.assertEqual(crc32(body), 0x1D4E5695)


class FrameRoundTripTests(unittest.TestCase):
    def test_every_encoder_appends_verifiable_crc(self) -> None:
        frames = [
            encode_config(0, 0, 1000, 64, 20000),
            tlv.encode_status_json({"schema": "wifi-monitor/1"}),
            tlv.encode_ch_util(0, {6: 40}),
            tlv.encode_spectrum(2412.0, 0.5, [-54.25, -70.0]),
            tlv.encode_spectrum_rf(1, 0, 0, 1, 0, 0, 0, 2412000, 20000,
                                   [0.0, -1.0, -2.0, -3.0,
                                    -4.0, -5.0, -6.0, -7.0]),
        ]
        for frame in frames:
            with self.subTest(type=frame[0]):
                ln = int.from_bytes(frame[1:3], "little")
                self.assertEqual(len(frame), 3 + ln + 4)
                self.assertEqual(crc32(frame[:3 + ln]),
                                 int.from_bytes(frame[3 + ln:], "little"))

    def test_status_roundtrip(self) -> None:
        wire = tlv.encode_status_json({"event": "channel", "packets": 0})
        messages, errors = parse(wire)
        self.assertEqual(errors, 0)
        self.assertIsInstance(messages[0], Status)
        self.assertEqual(messages[0].data["packets"], 0)

    def test_byte_at_a_time_and_concatenated(self) -> None:
        wire = (tlv.encode_status_json({"a": 1})
                + tlv.encode_status_json({"b": 2}))
        # one persistent parser across every single-byte feed
        messages, errors = parse(*(wire[i:i + 1] for i in range(len(wire))))
        self.assertEqual(errors, 0)
        self.assertEqual([m.data for m in messages], [{"a": 1}, {"b": 2}])
        # and both frames concatenated in a single chunk
        messages, errors = parse(wire)
        self.assertEqual(errors, 0)
        self.assertEqual([m.data for m in messages], [{"a": 1}, {"b": 2}])


class CorruptionAndResyncTests(unittest.TestCase):
    def _corrupted(self) -> bytes:
        frame = bytearray(encode_config(0, 0, 1000, 64, 20000))
        frame[5] ^= 0x01                 # flip one payload bit
        return bytes(frame)

    def test_corrupted_header_rejected(self) -> None:
        frame = bytearray(CONFIG_GOLDEN)
        frame[0] ^= 0x01                 # type becomes unknown
        messages, errors = parse(bytes(frame))
        self.assertEqual(messages, [])
        self.assertGreater(errors, 0)

    def test_corrupted_payload_rejected(self) -> None:
        messages, errors = parse(self._corrupted())
        self.assertEqual(messages, [])
        self.assertGreater(errors, 0)

    def test_corrupted_length_rejected(self) -> None:
        frame = bytearray(CONFIG_GOLDEN)
        frame[1] ^= 0x08                 # length 10 -> 2
        messages, errors = parse(bytes(frame))
        self.assertEqual(messages, [])
        self.assertGreater(errors, 0)

    def test_corrupted_checksum_rejected(self) -> None:
        frame = bytearray(CONFIG_GOLDEN)
        frame[-1] ^= 0xFF
        messages, errors = parse(bytes(frame))
        self.assertEqual(messages, [])
        self.assertGreater(errors, 0)

    def _wake_frames(self) -> bytes:
        """Enough valid traffic to break the parser's worst-case bounded
        stall (a false header may wait for up to MAX_PAYLOAD bytes)."""
        good = tlv.encode_status_json({"ok": True})
        need = 3 + tlv.MAX_PAYLOAD + 4
        reps = need // len(good) + 2
        return good * reps

    def test_legacy_crc_less_frame_rejected_not_fallback(self) -> None:
        legacy = CONFIG_GOLDEN[:13]      # no checksum bytes at all
        good = tlv.encode_status_json({"ok": True})
        # alone it must never decode (bounded wait, no false accept)
        messages, _ = parse(legacy)
        self.assertEqual(messages, [])
        # followed by real traffic the legacy bytes fail CRC; after the
        # bounded stall is filled the stream resynchronizes
        messages, errors = parse(legacy + good + self._wake_frames())
        self.assertGreaterEqual(
            len([m for m in messages if m.data == {"ok": True}]), 1)
        self.assertGreater(errors, 0)

    def test_recovery_after_garbage_and_corruption(self) -> None:
        wire = (b"\xff\x00garbage" + self._corrupted() + b"\x13\x37"
                + tlv.encode_status_json({"ok": True}) + self._wake_frames())
        messages, errors = parse(wire)
        self.assertGreaterEqual(
            len([m for m in messages if m.data == {"ok": True}]), 1)
        self.assertGreater(errors, 0)     # garbage is counted, never hidden

    def test_recovery_from_two_valid_after_split(self) -> None:
        a = tlv.encode_status_json({"n": 1})
        b = tlv.encode_status_json({"n": 2})
        cut = len(a) - 2
        messages, errors = parse(a[:cut], a[cut:] + b)
        self.assertEqual(errors, 0)
        self.assertEqual([m.data["n"] for m in messages], [1, 2])

    def test_oversized_length_rejected_without_stalling(self) -> None:
        header = bytes([tlv.T_STATUS, 0x01, 0x20])   # 8193 > MAX_PAYLOAD
        messages, errors = parse(header + b"x" * 10)
        self.assertEqual(messages, [])
        self.assertGreater(errors, 0)

    def test_buffer_stays_bounded(self) -> None:
        parser = TlvParser()
        # literal maximum header: STATUS with payload_length = 8192
        header = bytes([tlv.T_STATUS, 0x00, 0x20])
        self.assertEqual(header,
                         bytes([0x03]) + (8192).to_bytes(2, "little"))
        parser.feed(header)
        self.assertLessEqual(len(parser._buf), 3 + 8192 + 4)
        # fill the real 8192-byte candidate in chunks; after each feed the
        # residual buffer stays within HDR + MAX_PAYLOAD + CRC
        for _ in range(4):                     # 3 + 4 * 4096 > 8199
            parser.feed(b"\x00" * 4096)
            self.assertLessEqual(len(parser._buf), 3 + 8192 + 4)
        self.assertGreater(parser.errors, 0)   # the filled candidate is CRC-failed

    def test_maximum_status_frame_fragmented_and_corruption_recovery(self) -> None:
        payload = b'{"pad":"' + b"x" * 8182 + b'"}'
        self.assertEqual(len(payload), tlv.MAX_PAYLOAD)
        wire = tlv.frame(tlv.T_STATUS, payload)
        self.assertEqual(int.from_bytes(wire[1:3], "little"), 8192)
        # valid maximum STATUS, byte-at-a-time through one persistent parser
        parser = TlvParser()
        messages = []
        for i in range(len(wire)):
            messages.extend(parser.feed(wire[i:i + 1]))
            self.assertLessEqual(len(parser._buf), 3 + 8192 + 4)
        self.assertEqual(parser.errors, 0)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].data["pad"], "x" * 8182)
        # CRC-before-accept: flip a padding byte inside other valid JSON
        # ("x" -> "y"). Decoding first would accept it; only checking the
        # checksum before decode rejects it.
        corrupt = bytearray(wire)
        offset = 3 + 8                         # first padding byte
        self.assertEqual(corrupt[offset:offset + 1], b"x")
        corrupt[offset] = ord("y")
        parser = TlvParser()
        self.assertEqual(parser.feed(bytes(corrupt)), [])
        self.assertGreater(parser.errors, 0)
        # bounded corruption recovery: a following valid frame still decodes
        messages = parser.feed(tlv.encode_status_json({"ok": True}))
        self.assertEqual([m.data for m in messages], [{"ok": True}])


class EarlyLengthRejectionTests(unittest.TestCase):
    """Impossible type/length combinations must be rejected before the
    parser buffers the advertised length - without a sync word, without
    decoding the payload, and without extra wake frames. Each fixture is
    chosen so the bytes *after* the rejected header do not themselves form
    another plausible header (the high length bytes 0x05/0x0F are unknown
    types), which is exactly the limit of byte-wise resync."""

    @staticmethod
    def _good() -> bytes:
        return tlv.encode_status_json({"ok": True})

    def _assert_good_after(self, false_prefix: bytes) -> None:
        # three frames only: nowhere near a filled false length
        messages, errors = parse(false_prefix + self._good() * 3)
        goods = [m for m in messages
                 if isinstance(m, Status) and m.data == {"ok": True}]
        self.assertEqual(len(goods), 3, f"pending after {false_prefix.hex()}")
        self.assertGreater(errors, 0)

    def test_false_config_length_rejected_from_header(self) -> None:
        # CONFIG payload is exactly CONFIG.size (10) bytes; any other
        # advertised length is garbage from the header alone. Fixture
        # meanings are tool-computed below, not asserted from memory.
        for high in (0x05, 0x0F):
            with self.subTest(length_high=high):
                false_header = bytes([tlv.T_CONFIG, 0xE8, high])
                claimed = int.from_bytes(false_header[1:3], "little")
                self.assertEqual(claimed, {0x05: 1512, 0x0F: 4072}[high])
                self.assertNotEqual(claimed, tlv.CONFIG.size)
                self._assert_good_after(false_header)

    def test_implausible_spectrum_lengths_rejected_early(self) -> None:
        fixtures = {
            "odd-sample-count": bytes([tlv.T_SPECTRUM, 0xE9, 0x05]),
            "length-disagreeing-with-n": bytes([tlv.T_SPECTRUM, 0xE8, 0x05]),
        }
        for name, false_header in fixtures.items():
            with self.subTest(name):
                self._assert_good_after(false_header)

    def test_implausible_ch_util_lengths_rejected_early(self) -> None:
        fixtures = {
            "odd-entry-length": bytes([tlv.T_CH_UTIL, 0xE9, 0x05]),
            "length-disagreeing-with-n": bytes([tlv.T_CH_UTIL, 0xE8, 0x05]),
        }
        for name, false_header in fixtures.items():
            with self.subTest(name):
                self._assert_good_after(false_header)

    def test_rf_payload_bounds_rejected_from_header(self) -> None:
        # 0x04 payload must be 24 + 2*N with 2 <= N <= RF_MAX_BINS (2036),
        # i.e. 28..4096 bytes, mod 4. Each fixture's payload prefix agrees
        # with its claimed length (fft_size at payload offset 12), so only
        # a header-level bound can reject it: without the bound the parser
        # waits for the advertised total (up to a full MAX_PAYLOAD stall)
        # and the three good frames behind it never decode.
        def rf_prefix(ln: int) -> bytes:
            bins = (ln - 24) // 2
            return (bytes([tlv.T_SPECTRUM_RF, ln & 0xFF, ln >> 8])
                    + bytes(12) + bins.to_bytes(2, "little"))

        for ln in (24, 5120, 8192):
            with self.subTest(payload_length=ln):
                false_prefix = rf_prefix(ln)
                self.assertEqual(int.from_bytes(false_prefix[1:3], "little"),
                                 ln)
                self.assertEqual((ln - 24) % 4, 0)   # mod-4 passes: the
                # header bound (28..4096) is the only thing left to reject
                self._assert_good_after(false_prefix)

    def test_valid_fragmented_frames_survive_length_checks(self) -> None:
        frames = [
            tlv.encode_status_json({"n": 1}),
            tlv.encode_spectrum(2412.0, 0.5, [-54.25, -70.0]),
            tlv.encode_ch_util(0, {6: 40, 11: 7}),
            encode_config(1, 1, 1000, 64, 20000),
            SPECTRUM_RF_GOLDEN,
        ]
        for frame in frames:
            with self.subTest(type=frame[0]):
                messages, errors = parse(
                    *(bytes([b]) for b in frame))   # byte-at-a-time
                self.assertEqual(errors, 0)
                self.assertEqual(len(messages), 1)

    def test_status_length_is_byte_bounded_not_time_bounded(self) -> None:
        # 64-byte JSON is plausible from the header alone: indistinguishable
        # from a real, fragmented STATUS frame - finite corrupt + good input
        # stays pending rather than being misjudged.
        good = self._good()
        false_header = bytes([tlv.T_STATUS, 0x40, 0x00])
        messages, errors = parse(false_header + good)
        self.assertEqual(messages, [])
        self.assertEqual(errors, 0)          # waiting, not rejected
        # byte-bounded (not time-bounded): once the claimed 71 bytes are
        # filled, CRC fails and the stream resynchronizes.
        messages, errors = parse(false_header + good * 5)
        goods = [m for m in messages
                 if isinstance(m, Status) and m.data == {"ok": True}]
        self.assertGreaterEqual(len(goods), 1)
        self.assertGreater(errors, 0)


class SpectrumRfCodecTests(unittest.TestCase):
    """0x04 SPECTRUM_RF per handoff v2: frozen 24-byte header, fftshift
    bin order, centi-dBFS power, no dBm conversion anywhere."""

    def test_golden_frame_parses_to_published_fields(self) -> None:
        self.assertEqual(len(SPECTRUM_RF_GOLDEN), 47)
        # CRC recompute over bytes[:-4] must equal the published value
        self.assertEqual(crc32(SPECTRUM_RF_GOLDEN[:-4]), 0x23877A9F)
        messages, errors = parse(SPECTRUM_RF_GOLDEN)
        self.assertEqual(errors, 0)
        msg = messages[0]
        self.assertIsInstance(msg, tlv.SpectrumRf)
        self.assertEqual(
            (msg.epoch, msg.cycle, msg.band, msg.channel, msg.mode,
             msg.rate_code, msg.fft_size, msg.source),
            (1, 0, 0, 1, 0, 0, 8, 0))
        self.assertEqual((msg.center_khz, msg.span_khz), (2412000, 20000))
        self.assertEqual(list(msg.power_dbfs),
                         [0.0, -1.0, -2.0, -3.0, -4.0, -5.0, -6.0, -7.0])
        # fftshift order: f_k = center - span/2 + k*span/N [MHz]
        self.assertEqual([round(f, 3) for f in msg.freqs],
                         RF_GOLDEN_FREQS_MHZ)

    def test_encoder_reproduces_golden_bytes(self) -> None:
        frame = tlv.encode_spectrum_rf(1, 0, 0, 1, 0, 0, 0, 2412000, 20000,
                                       [0.0, -1.0, -2.0, -3.0,
                                        -4.0, -5.0, -6.0, -7.0])
        self.assertEqual(frame, SPECTRUM_RF_GOLDEN)

    def test_power_is_plain_float_dbfs_field_not_dbm(self) -> None:
        messages, _ = parse(SPECTRUM_RF_GOLDEN)
        msg = messages[0]
        self.assertEqual(msg.power_dbfs.dtype.name, "float32")
        # wire int16 centi-dBFS -3450 must arrive as -34.50 dBFS
        frame = tlv.encode_spectrum_rf(1, 1, 1, 36, 1, 2, 0, 5180000,
                                       40000, [-34.5, -60.01])
        parsed, errors = parse(frame)
        self.assertEqual(errors, 0)
        self.assertEqual([round(float(v), 2) for v in parsed[0].power_dbfs],
                         [-34.5, -60.01])

    def test_payload_length_mismatch_rejected(self) -> None:
        # Decode-level: payload shorter than its own declared fft_size.
        payload = SPECTRUM_RF_GOLDEN[3:43]        # the 40-byte payload
        self.assertRaises(ValueError, tlv.decode, tlv.T_SPECTRUM_RF,
                          payload[:-2])
        # Wire-level: a length below the 24-byte header is garbage from
        # the header alone; the stream resyncs to the valid frames after.
        false_header = bytes([tlv.T_SPECTRUM_RF, 0x0A, 0x00])
        messages, errors = parse(false_header + SPECTRUM_RF_GOLDEN)
        self.assertEqual(len([m for m in messages
                              if isinstance(m, tlv.SpectrumRf)]), 1)
        self.assertGreater(errors, 0)

    def test_declared_length_disagreeing_with_fft_size_rejected_early(self) -> None:
        # prefix says fft_size=8 -> payload must be 40; claim 42 instead.
        # Rejected before buffering the advertised length: a valid golden
        # frame immediately after must still decode.
        false_header = bytes([tlv.T_SPECTRUM_RF, 0x2A, 0x00])
        messages, errors = parse(false_header + SPECTRUM_RF_GOLDEN * 2)
        goldens = [m for m in messages if isinstance(m, tlv.SpectrumRf)]
        self.assertEqual(len(goldens), 2)
        self.assertGreater(errors, 0)

    def test_odd_fft_size_rejected_from_header(self) -> None:
        # payload 24 + 2*7 = 38 (0x26): ln ≡ 2 (mod 4) means odd N -> drop
        # the header without buffering, before any golden frame after it.
        false_header = bytes([tlv.T_SPECTRUM_RF, 0x26, 0x00])
        messages, errors = parse(false_header + SPECTRUM_RF_GOLDEN * 2)
        goldens = [m for m in messages if isinstance(m, tlv.SpectrumRf)]
        self.assertEqual(len(goldens), 2)
        self.assertGreater(errors, 0)

    def test_unknown_source_is_dropped_as_parser_error(self) -> None:
        # source != 0 must be ignored AND counted (handoff section 4), and
        # the valid frame after it must survive.
        bad = bytearray(tlv.encode_spectrum_rf(
            1, 0, 0, 1, 0, 0, source=7, center_khz=2412000, span_khz=20000,
            power_dbfs=[-10.0, -20.0]))
        messages, errors = parse(bytes(bad) + SPECTRUM_RF_GOLDEN)
        self.assertEqual(len([m for m in messages
                              if isinstance(m, tlv.SpectrumRf)]), 1)
        self.assertGreater(errors, 0)

    def test_zero_span_rejected(self) -> None:
        frame = tlv.encode_spectrum_rf(1, 0, 0, 1, 0, 0, 0, 2412000, 0,
                                       [-10.0, -20.0])
        messages, errors = parse(frame + SPECTRUM_RF_GOLDEN)
        self.assertEqual(len([m for m in messages
                              if isinstance(m, tlv.SpectrumRf)]), 1)
        self.assertGreater(errors, 0)

    def test_invalid_enum_values_rejected(self) -> None:
        cases = {
            "band=2": tlv.encode_spectrum_rf(1, 0, 2, 1, 0, 0, 0,
                                             2412000, 20000,
                                             [-10.0, -20.0]),
            "mode=3": tlv.encode_spectrum_rf(1, 0, 0, 1, 3, 0, 0,
                                             2412000, 20000,
                                             [-10.0, -20.0]),
            "channel=0": tlv.encode_spectrum_rf(1, 0, 0, 0, 0, 0, 0,
                                                2412000, 20000,
                                                [-10.0, -20.0]),
        }
        for name, frame in cases.items():
            with self.subTest(name):
                messages, errors = parse(frame)
                self.assertEqual(messages, [])
                self.assertGreater(errors, 0)

    def test_golden_frame_byte_at_a_time_and_concatenated(self) -> None:
        wire = SPECTRUM_RF_GOLDEN * 2
        messages, errors = parse(*(wire[i:i + 1]
                                   for i in range(len(wire))))
        self.assertEqual(errors, 0)
        self.assertEqual(len(messages), 2)
        messages, errors = parse(wire)
        self.assertEqual(errors, 0)
        self.assertEqual(len(messages), 2)

    def test_corrupted_rf_payload_rejected(self) -> None:
        corrupt = bytearray(SPECTRUM_RF_GOLDEN)
        corrupt[30] ^= 0x01                 # one bit inside the bins
        messages, errors = parse(bytes(corrupt))
        self.assertEqual(messages, [])
        self.assertGreater(errors, 0)


if __name__ == "__main__":
    unittest.main()
