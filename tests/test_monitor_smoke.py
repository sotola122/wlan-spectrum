"""monitor_smoke CLI contract and checker fixtures (no hardware needed).

The self-test replays SYNTHETIC protocol streams (constructed bytes, not
device captures) through the exact same checker and real decoder that the
hardware run uses, so a broken contract rule fails here without a board.
"""

from __future__ import annotations

import contextlib
import io
import os
import time
import unittest
from unittest import mock

from scripts.monitor_smoke import (
    CASES,
    Device,
    _case_done,
    _run_bytes,
    build_parser,
    corrupt_config_checks,
    main,
    run_self_test,
    self_test_fixtures,
)
from wifi_spectrum import tlv


class SelfTestFixtures(unittest.TestCase):
    def test_run_self_test_passes(self) -> None:
        ok, lines = run_self_test()
        self.assertTrue(ok, "\n".join(lines))

    def test_fixture_matrix_covers_required_failures(self) -> None:
        names = [name for name, *_ in self_test_fixtures()]
        for required in ("missing-config", "missing-cycle", "wrong-epoch",
                         "malformed-json", "negative-observation",
                         "absent-channel", "unexpected-spectrum",
                         "empty-observations",
                         "identical-config-mid-cycle-join",
                         "late-incomplete-cycle-fails",
                         "no-cycle-timeout",
                         "changed-epoch-partial-first-cycle",
                         "changed-epoch-early-data-before-ack",
                         "unexpected-ch-util"):
            with self.subTest(required):
                self.assertIn(required, names)

    def test_bad_fixtures_fail_for_the_documented_reason(self) -> None:
        for name, blob, request, same_config, want_ok, want_in \
                in self_test_fixtures():
            if want_ok or want_in is None:
                continue
            report = _run_bytes(name, request, blob,
                                same_config=same_config)
            with self.subTest(name):
                self.assertFalse(report.ok, report.summary())
                self.assertTrue(
                    any(want_in in reason for reason in report.reasons),
                    f"{want_in!r} not in {report.reasons}")

    def test_good_fixture_passes_and_empty_reports_no_ap(self) -> None:
        by_name = {name: row for row in self_test_fixtures()
                   for name in [row[0]]}
        name, blob, request, same_config, *_ = by_name["good-two-cycles"]
        report = _run_bytes(name, request, blob, same_config=same_config)
        self.assertTrue(report.ok, report.reasons)
        self.assertEqual(report.cycles, 2)
        self.assertEqual(report.ap_count, 1)
        name, blob, request, same_config, *_ = by_name["empty-observations"]
        report = _run_bytes(name, request, blob, same_config=same_config)
        self.assertTrue(report.ok, report.reasons)
        self.assertIn("NO_AP_OBSERVED", report.notes)

    def test_identical_config_midcycle_join_leniency_is_capped(self) -> None:
        """The exact UART smoke regression: an identical CONFIG re-acked
        mid-cycle must not fail the joined cycle - but the leniency must
        not extend to changed-config streams (strict half stays red)."""
        fixture = {row[0]: row for row in self_test_fixtures()}[
            "identical-config-mid-cycle-join"]
        _, blob, request, same_config, *_ = fixture
        self.assertTrue(same_config)
        joined = _run_bytes("join", request, blob, same_config=True)
        self.assertTrue(joined.ok, joined.reasons)
        self.assertTrue(any("joined mid-flight after identical CONFIG" in note
                            for note in joined.notes), joined.notes)
        strict = _run_bytes("join", request, blob, same_config=False)
        self.assertFalse(strict.ok)
        self.assertTrue(any("missing channels" in reason
                            for reason in strict.reasons), strict.reasons)

    def test_corrupt_config_wire_vectors(self) -> None:
        self.assertEqual(corrupt_config_checks(), [])


def _decode_all(blob: bytes):
    from wifi_spectrum.tlv import TlvParser

    parser = TlvParser()
    events = []
    stamp = 0.0
    for offset in range(0, len(blob), 7):
        stamp += 0.001
        for msg in parser.feed(blob[offset:offset + 7]):
            events.append((stamp, msg))
    return events


class StopPredicateTests(unittest.TestCase):
    def _fixture(self, name: str):
        return {row[0]: row for row in self_test_fixtures()}[name]

    def test_pre_ack_cycles_do_not_satisfy_the_stop_predicate(self) -> None:
        # The early-data defect fixture: two cycles arrive BEFORE their
        # config ack. Stopping on them would end collection before the
        # device proved the new epoch.
        _, blob, request, *_ = self._fixture(
            "changed-epoch-early-data-before-ack")
        events = _decode_all(blob)
        self.assertFalse(_case_done(events, request, 2),
                         "pre-ack cycles must not end collection")

    def test_post_ack_epoch_band_cycles_satisfy_the_stop_predicate(self) -> None:
        _, blob, request, *_ = self._fixture("good-two-cycles")
        events = _decode_all(blob)
        self.assertTrue(_case_done(events, request, 2))


class HardwareCaseCountTests(unittest.TestCase):
    def test_full_run_produces_twelve_protocol_cases(self) -> None:
        """Orchestration count for the default hardware run, verified by
        stubbing the serial layer (no port is opened)."""
        import scripts.monitor_smoke as smoke

        calls: list[str] = []

        class FakeDev:
            def __init__(self, port: str, baud: int) -> None:
                self.parser = smoke.TlvParser()
                self.rx = 0
                self.sync_fields = None
                self.session_baseline = 0

            def sync(self) -> bool:
                self.sync_fields = (0, 0, 1000, 64, 20000)
                return True

            def close(self) -> None:
                pass

        def fake_run_case(dev, name, request, window_s, min_cycles=2,
                          chunks=None, prev_active=None):
            calls.append(name)
            return smoke.CaseReport(name=name)

        def fake_active_check(dev, name, previous, blob, window):
            calls.append(name)
            return smoke.CaseReport(name=name)

        args = smoke.build_parser().parse_args(["--port", "/dev/null"])
        out = io.StringIO()
        with mock.patch.object(smoke, "Device", FakeDev), \
                mock.patch.object(smoke, "run_case", fake_run_case), \
                mock.patch.object(smoke, "run_active_config_check",
                                  fake_active_check), \
                contextlib.redirect_stdout(out):
            code = smoke.main(["--port", "/dev/null"])
        self.assertEqual(args.cases, None)
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(calls, [
            "live-2.4", "sweep-2.4", "live-5", "sweep-5",
            "dwell-100ms", "fft-1024-rate-40000",
            "invalid-config", "corrupt-config",
            "invalid-band2", "wrong-length-config",
            "split-config-bytewise", "concat-config-newest-wins",
        ])
        self.assertIn("result: PASS (12 checks)", out.getvalue())


@unittest.skipUnless(os.name == "posix", "PTY integration requires POSIX")
class SessionBaselineTests(unittest.TestCase):
    def test_crc_damage_in_intercase_drain_fails_session_check(self) -> None:
        """Corruption landing between per-case baselines (fed by
        clear_input) must still fail the session-wide verdict."""
        import pty
        import tty

        from scripts.monitor_smoke import check_session_errors

        master, slave = pty.openpty()
        tty.setraw(slave)
        slave_path = os.ttyname(slave)
        os.close(slave)
        dev = Device(slave_path, 921600)
        try:
            dev.clear_input()
            dev.session_baseline = dev.parser.errors   # post-sync position
            # inter-case drain window: one healthy frame, then CRC damage
            os.write(master, tlv.encode_status_json(
                {"schema": "wifi-monitor/1", "event": "config",
                 "epoch": 1, "mode": 0, "band": 0, "sweep_ms": 1000,
                 "fft_size": 64, "sample_rate_khz": 20000}))
            damaged = bytearray(tlv.encode_status_json(
                {"schema": "wifi-monitor/1", "event": "error",
                 "code": "x"}))
            damaged[5] ^= 0x01                # payload flip -> CRC mismatch
            os.write(master, bytes(damaged))
            time.sleep(0.05)
            dev.clear_input()                 # feeds both, drops messages
            case_baseline = dev.parser.errors
            # a case starting NOW would see zero errors...
            self.assertEqual(dev.parser.errors - case_baseline, 0)
            # ...but the session-wide verdict still fails
            session = check_session_errors(
                dev.parser.errors - dev.session_baseline)
            self.assertFalse(session.ok, session.summary())
            self.assertGreaterEqual(session.tlv_errors, 1)
        finally:
            dev.close()
            with contextlib.suppress(OSError):
                os.close(master)


class CliContractTests(unittest.TestCase):
    def test_port_required_without_self_test(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            main([])
        self.assertEqual(ctx.exception.code, 2)

    def test_seconds_defaults_to_30_and_port_optional_in_parser(self) -> None:
        args = build_parser().parse_args(["--port", "/dev/null"])
        self.assertEqual(args.seconds, 30.0)
        self.assertEqual(args.baud, 921600)
        self.assertFalse(args.stream)

    def test_unknown_case_is_usage_error(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            main(["--port", "/dev/null", "--cases", "not-a-case"])
        self.assertEqual(ctx.exception.code, 2)

    def test_case_names_cover_both_modes_both_bands(self) -> None:
        self.assertEqual([name for name, *_ in CASES],
                         ["live-2.4", "sweep-2.4", "live-5", "sweep-5"])
        self.assertEqual(sorted({band for _, _, band in CASES}), [0, 1])
        self.assertEqual(sorted({mode for _, mode, _ in CASES}), [0, 1])

    def test_self_test_main_exits_zero_and_prints(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--self-test"])
        self.assertEqual(code, 0)
        self.assertIn("self-test: PASS", out.getvalue())

    def test_never_imports_the_mock_generator(self) -> None:
        import scripts.monitor_smoke as smoke

        self.assertFalse(hasattr(smoke, "MockDevice"))
        self.assertNotIn("mock", dir(smoke))


if __name__ == "__main__":
    unittest.main()
