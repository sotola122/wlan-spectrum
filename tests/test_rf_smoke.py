"""RF smoke CLI contract (no hardware): SYNTHETIC fixtures through the
real TlvParser and the same evaluator the hardware run uses."""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

import scripts.rf_spectrum_smoke as rf_smoke
from scripts.rf_spectrum_smoke import (
    ConfigRequest,
    _caps_valid,
    _full_cycle,
    _parse,
    _rf_ack,
    build_request_args,
    evaluate,
    finalize,
    main,
    run_port,
    run_self_test,
    self_test_cases,
)


class SelfTestTests(unittest.TestCase):
    def test_run_self_test_passes_and_prints(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok = run_self_test()
        out = buf.getvalue()
        self.assertTrue(ok, out)
        self.assertIn("[ok] rf smoke self-test", out)
        self.assertIn("[PASS] good-live", out)
        self.assertNotIn("[FAIL]", out)

    def test_main_self_test_exits_zero(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--self-test"]), 0)

    def test_cli_requires_port_without_self_test(self) -> None:
        with (contextlib.redirect_stderr(io.StringIO()),
              self.assertRaises(SystemExit) as ctx):
            main([])
        self.assertEqual(ctx.exception.code, 2)

    def test_cli_defaults(self) -> None:
        args = build_request_args(["--port", "/dev/null"])
        self.assertEqual(args, ConfigRequest(0, 0, 1000, 64, 20000))
        args = build_request_args(
            ["--port", "/dev/null", "--mode", "sweep", "--band", "1"])
        self.assertEqual(args, ConfigRequest(1, 1, 1000, 64, 20000))


class FixtureMatrixTests(unittest.TestCase):
    """Every fixture must verdict exactly as documented."""

    def test_cases_verdict_as_documented(self) -> None:
        for name, stream, request, expect_ok, substring in self_test_cases():
            with self.subTest(case=name):
                messages, errors = _parse(*stream)
                events = [(float(i), m) for i, m in enumerate(messages)]
                rep = evaluate(request, events)
                rep = finalize(rep, tlv_errors=errors, ack_errors=0,
                               min_cycles=1 if expect_ok else 0)
                if expect_ok:
                    self.assertTrue(rep.ok, f"{name}: {rep.failures}")
                else:
                    self.assertFalse(rep.ok, f"{name} unexpectedly passed")
                    if substring:
                        self.assertTrue(
                            any(substring in f for f in rep.failures),
                            f"{name}: {substring!r} not in {rep.failures!r}")

    def test_good_live_counts_frames_and_cycles(self) -> None:
        request = ConfigRequest(0, 0, 1000, 64, 20000)
        stream = [_rf_ack(request)] + _full_cycle(request, cycle=1)
        messages, errors = _parse(*stream)
        rep = evaluate(request, [(0.0, m) for m in messages])
        rep = finalize(rep, tlv_errors=errors, ack_errors=0, min_cycles=1)
        self.assertTrue(rep.ok, rep.failures)
        self.assertEqual(rep.frames, 3)          # one frame per channel
        self.assertEqual(rep.cycles, 1)
        self.assertTrue(rep.spectrum)
        self.assertFalse(rep.utilization)
        self.assertEqual(rep.channels, [1, 6, 11])

    def test_zero_rf_and_false_capability_never_pass(self) -> None:
        """Even with enough cycle markers, spectrum:false must read
        BLOCKED and a capability ack without frames must fail - RF
        acceptance can never silently pass on zero RF."""
        by_name = {n: (s, r, ok, sub)
                   for n, s, r, ok, sub in self_test_cases()}
        for name, substring in (("zero-rf-frames-never-pass", "zero RF"),
                                ("no-rf-capability-blocked", "BLOCKED")):
            with self.subTest(case=name):
                stream, request, expect_ok, _ = by_name[name]
                self.assertFalse(expect_ok)
                messages, errors = _parse(*stream)
                rep = evaluate(request,
                               [(0.0, m) for m in messages])
                rep = finalize(rep, tlv_errors=errors, ack_errors=0,
                               min_cycles=0)
                self.assertFalse(rep.ok)
                self.assertTrue(any(substring in f for f in rep.failures),
                                rep.failures)

    def test_channel_error_recorded_as_legal_gap(self) -> None:
        request = ConfigRequest(0, 0, 1000, 64, 20000)
        stream = next(s for n, s, _r, ok, _sub in self_test_cases()
                      if n == "channel-error-legal-gap-not-missing" and ok)
        messages, errors = _parse(*stream)
        rep = evaluate(request, [(0.0, m) for m in messages])
        rep = finalize(rep, tlv_errors=errors, ack_errors=0, min_cycles=1)
        self.assertTrue(rep.ok, rep.failures)
        self.assertEqual(rep.gaps, ["ch 11: spectrum_capture"])
        self.assertTrue(any(n == "gap ch 11: spectrum_capture"
                            for n in rep.notes))

    def test_util_events_counted_and_zero_busy_accepted(self) -> None:
        """busy=0 is a MEASURED zero: counted as a real util event, never
        fake coverage; the event counter reports actual samples only."""
        by_name = {n: (s, r, ok, sub)
                   for n, s, r, ok, sub in self_test_cases()}
        stream, request, expect_ok, _ = by_name[
            "util-samples-counted-zero-busy-accepted"]
        self.assertTrue(expect_ok)
        messages, errors = _parse(*stream)
        rep = finalize(evaluate(request, [(0.0, m) for m in messages]),
                       tlv_errors=errors, ack_errors=0, min_cycles=1)
        self.assertTrue(rep.ok, rep.failures)
        self.assertEqual(rep.util_events, 3)   # one per advertised channel
        self.assertTrue(any("util_samples=3" in n for n in rep.notes))

    def test_util_capability_and_sample_faults_rejected(self) -> None:
        by_name = {n: (s, r, ok, sub)
                   for n, s, r, ok, sub in self_test_cases()}
        for name in ("util-capability-wrong-source-fails",
                     "util-sample-wrong-source-fails",
                     "util-sample-bool-busy-fails",
                     "util-sample-busy-over-total-fails",
                     "util-capability-zero-samples-fails"):
            with self.subTest(case=name):
                stream, request, expect_ok, substring = by_name[name]
                self.assertFalse(expect_ok)
                messages, errors = _parse(*stream)
                rep = finalize(evaluate(request,
                                        [(0.0, m) for m in messages]),
                               tlv_errors=errors, ack_errors=0, min_cycles=0)
                self.assertFalse(rep.ok)
                self.assertTrue(substring and any(substring in f
                                                  for f in rep.failures),
                                rep.failures)

    def test_missing_coverage_and_coldjoin_notes(self) -> None:
        by_name = {n: (s, r, ok, sub)
                   for n, s, r, ok, sub in self_test_cases()}
        # second-cycle missing coverage must name the missing channels
        stream, request, _, _ = by_name[
            "missing-channel-second-cycle-fails"]
        messages, errors = _parse(*stream)
        rep = finalize(evaluate(request, [(0.0, m) for m in messages]),
                       tlv_errors=errors, ack_errors=0, min_cycles=0)
        self.assertFalse(rep.ok)
        self.assertTrue(any("missing RF for channels [6, 11]" in f
                            for f in rep.failures))
        # a genuine mid-pass join is only noted, never failed
        stream, request, _, _ = by_name["coldjoin-partial-first-cycle-ok"]
        messages, errors = _parse(*stream)
        rep = finalize(evaluate(request, [(0.0, m) for m in messages]),
                       tlv_errors=errors, ack_errors=0, min_cycles=1)
        self.assertTrue(rep.ok, rep.failures)
        self.assertTrue(any("joined mid-flight" in n for n in rep.notes))

    def test_midcycle_heartbeat_preserves_rf_coverage(self) -> None:
        """An identical CONFIG heartbeat mid-cycle must not reset cycle
        coverage, first-channel, or marker state (600s physical root
        cause: heartbeat cleared coverage -> false missing-channel)."""
        by_name = {n: (s, r, ok, sub)
                   for n, s, r, ok, sub in self_test_cases()}
        stream, request, expect_ok, _ = by_name[
            "midcycle-heartbeat-keeps-rf-coverage"]
        self.assertTrue(expect_ok)
        messages, errors = _parse(*stream)
        rep = finalize(evaluate(request,
                                [(0.0, m) for m in messages]),
                       tlv_errors=errors, ack_errors=0, min_cycles=1)
        self.assertTrue(rep.ok, rep.failures)
        self.assertEqual(rep.cycles, 2)

    def test_channel_status_alone_never_covers_rf(self) -> None:
        """Channel STATUS presence must not mask missing RF frames."""
        by_name = {n: (s, r, ok, sub)
                   for n, s, r, ok, sub in self_test_cases()}
        for name in ("missing-channel-second-cycle-fails",
                     "channel-status-without-rf-fails",
                     "lost-marker-new-cycle-rf-coverage-not-mixed"):
            with self.subTest(case=name):
                stream, request, expect_ok, substring = by_name[name]
                self.assertFalse(expect_ok)
                messages, errors = _parse(*stream)
                rep = finalize(evaluate(request,
                                        [(0.0, m) for m in messages]),
                               tlv_errors=errors, ack_errors=0, min_cycles=0)
                self.assertFalse(rep.ok)
                self.assertTrue(
                    substring and any(substring in f
                                      for f in rep.failures),
                    rep.failures)

    def test_preack_frames_ignored_but_postack_violations_fail(self) -> None:
        by_name = {n: (s, r, ok, sub)
                   for n, s, r, ok, sub in self_test_cases()}
        # cold-join frame before the matching ack: ignored, not failed
        stream, request, expect_ok, _ = by_name[
            "preack-rf-ignored-until-matching-ack"]
        self.assertTrue(expect_ok)
        messages, errors = _parse(*stream)
        rep = finalize(evaluate(request, [(0.0, m) for m in messages]),
                       tlv_errors=errors, ack_errors=0, min_cycles=1)
        self.assertTrue(rep.ok, rep.failures)
        # a frame after a spectrum:false ack is a contract violation
        stream, request, expect_ok, substring = by_name[
            "rf-while-spectrum-false-fails"]
        self.assertFalse(expect_ok)
        messages, errors = _parse(*stream)
        rep = finalize(evaluate(request, [(0.0, m) for m in messages]),
                       tlv_errors=errors, ack_errors=0, min_cycles=0)
        self.assertTrue(
            substring and any(substring in f for f in rep.failures),
            rep.failures)


class BootRaceRunPortTests(unittest.TestCase):
    """run_port resends the idempotent CONFIG while no matching ack has
    arrived (post-ctrl gate: run1 exited 1, identical run2 passed); an
    absent device stays bounded and the failure message stays visible."""

    class _FakeSer:
        def __init__(self, *args, **kwargs):
            self.writes: list[bytes] = []
            self._queue = bytearray()
            self.ack_blob = b""

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        @property
        def in_waiting(self) -> int:
            return len(self._queue)

        def write(self, data: bytes) -> int:
            self.writes.append(data)
            if len(self.writes) >= 2 and not self._queue:
                # boot finished: only later CONFIGs get the matching ack
                self._queue += self.ack_blob
            return len(data)

        def read(self, size: int) -> bytes:
            chunk = bytes(self._queue[:size])
            del self._queue[:len(chunk)]
            return chunk

    def test_resends_until_fresh_matching_ack(self) -> None:
        request = ConfigRequest(0, 0, 1000, 64, 20000)
        ack = _rf_ack(request)
        made: list[BootRaceRunPortTests._FakeSer] = []

        def factory(*args, **kwargs):
            ser = self._FakeSer(*args, **kwargs)
            ser.ack_blob = ack
            made.append(ser)
            return ser

        with mock.patch.object(rf_smoke, "RESEND_INTERVAL_S", 0.05), \
                mock.patch("serial.Serial", factory):
            rep = run_port("/dev/pty-fake", 921600, request, 0.6, 0)
        self.assertTrue(made, "serial port was never opened")
        self.assertGreaterEqual(len(made[0].writes), 2,
                                "CONFIG must be resent until the ack")
        self.assertTrue(rep.acked, rep.failures)
        # the gate failure itself is gone; zero-RF stays an intact separate
        # rule (fixtures) - this fake device only acks, no 0x04 stream
        self.assertFalse(any("no config acknowledgement" in f
                             for f in rep.failures), rep.failures)

    def test_absent_device_stays_bounded_and_visible(self) -> None:
        request = ConfigRequest(0, 0, 1000, 64, 20000)
        made: list[BootRaceRunPortTests._FakeSer] = []

        def factory(*args, **kwargs):
            ser = self._FakeSer(*args, **kwargs)
            made.append(ser)
            return ser

        with mock.patch.object(rf_smoke, "RESEND_INTERVAL_S", 0.05), \
                mock.patch.object(rf_smoke, "MAX_RESENDS", 3), \
                mock.patch("serial.Serial", factory):
            rep = run_port("/dev/pty-fake", 921600, request, 0.4, 0)
        self.assertTrue(made, "serial port was never opened")
        self.assertEqual(len(made[0].writes), 1 + 3,
                         "resends must stop at MAX_RESENDS")
        self.assertFalse(rep.acked)
        self.assertFalse(rep.ok)
        self.assertTrue(any("no config acknowledgement" in f
                            for f in rep.failures), rep.failures)


class CapsValidationTests(unittest.TestCase):
    CAPS = {"source": "c5_snapshot_iq_fft", "fft_sizes": [64, 128],
            "rate_codes": [{"code": 0, "span_khz": 20000}],
            "bin_unit": "centi_dbfs"}

    def _ack(self, **over) -> dict:
        base = {"spectrum_caps": dict(self.CAPS),
                "spectrum_effective": {"fft_size": 128, "rate_code": 0,
                                       "span_khz": 20000}}
        base.update(over)
        return base

    def test_coherent_caps_pass(self) -> None:
        self.assertIsNone(_caps_valid(self._ack()))

    def test_every_broken_variant_is_rejected(self) -> None:
        broken = {
            "missing-effective": {"spectrum_effective": None},
            "unsupported-fft": self._ack(
                spectrum_effective={"fft_size": 1024, "rate_code": 0,
                                    "span_khz": 20000}),
            "unknown-rate-code": self._ack(
                spectrum_effective={"fft_size": 128, "rate_code": 9,
                                    "span_khz": 20000}),
            "span-disagrees": self._ack(
                spectrum_effective={"fft_size": 128, "rate_code": 0,
                                    "span_khz": 40000}),
            "wrong-bin-unit": self._ack(
                spectrum_caps=dict(self.CAPS, bin_unit="dbm")),
            "empty-fft-sizes": self._ack(
                spectrum_caps=dict(self.CAPS, fft_sizes=[])),
            "zero-span": self._ack(
                spectrum_caps=dict(
                    self.CAPS,
                    rate_codes=[{"code": 0, "span_khz": 0}])),
        }
        for name, data in broken.items():
            with self.subTest(name):
                reason = _caps_valid(data)
                self.assertIsNotNone(reason)

    def test_script_never_imports_the_mock_generator(self) -> None:
        import scripts.rf_spectrum_smoke as mod

        self.assertFalse(hasattr(mod, "MockSource"))
        with open(mod.__file__, encoding="utf-8") as fh:  # noqa: PTH123
            source = fh.read()
        self.assertNotIn("wifi_spectrum.mock", source)
        self.assertNotIn("MockDevice", source)


if __name__ == "__main__":
    unittest.main()
