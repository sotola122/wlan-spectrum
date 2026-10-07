"""Harness regressions: synthetic frames must stay coherent with their
ack, and capability expectations must derive from the RECEIVED caps
(real FW advertises fft sizes through 1024) - never fixture constants.
SYNTHETIC fixtures only; no hardware attached."""

from __future__ import annotations

import unittest

from PySide6.QtWidgets import QApplication

from scripts.rf_gui_harness import (
    SYNTH_RATES,
    SYNTHETIC_CHANNELS,
    HarnessError,
    SyntheticDevice,
    check_util_column,
    step_rf_ready,
)
from scripts.rf_spectrum_smoke import _caps_valid
from tests.test_monitor_widget import (
    FixtureReader,
    channel_event,
    config_from_frame,
    rf_config_event,
)
from wifi_spectrum import tlv
from wifi_spectrum.main_window import MainWindow
from wifi_spectrum.monitor_data import UTIL_CONFIDENCE, UTIL_SOURCE, parse_channel_util

APP = QApplication.instance() or QApplication([])


def device_cfg(mode: int = 0, band: int = 0, sweep_ms: int = 1000,
               fft_size: int = 128, sample_rate_khz: int = 40000) -> dict:
    return {"mode": mode, "band": band, "sweep_ms": sweep_ms,
            "fft_size": fft_size, "sample_rate_khz": sample_rate_khz}


class SyntheticFrameCoherenceTests(unittest.TestCase):
    def _device(self, cfg: dict) -> SyntheticDevice:
        dev = SyntheticDevice(-1)          # never started; pure logic
        dev.cfg = cfg
        dev.epoch = 3
        dev.cycle = 7
        return dev

    @staticmethod
    def _status(dev: SyntheticDevice, cfg: dict) -> dict:
        for msg in tlv.TlvParser().feed(dev._ack(cfg)):
            if isinstance(msg, tlv.Status):
                return msg.data
        raise AssertionError("synthetic ack did not decode")

    @staticmethod
    def _frame(dev: SyntheticDevice, ch: int) -> tlv.SpectrumRf:
        for msg in tlv.TlvParser().feed(dev._frame(ch)):
            if isinstance(msg, tlv.SpectrumRf):
                return msg
        raise AssertionError("synthetic frame did not decode")

    def test_frame_rate_code_matches_span_after_40ms_selection(self) -> None:
        """Regression: _frame used to hardcode rate_code 0 even when the
        configured span was 40000 kHz (code 1)."""
        cfg = device_cfg(sample_rate_khz=40000)
        frame = self._frame(self._device(cfg), ch=6)
        entry = next(r for r in SYNTH_RATES if r["span_khz"] == 40000)
        self.assertEqual(frame.rate_code, entry["code"])
        self.assertEqual(frame.span_khz, 40000)
        self.assertNotEqual(frame.rate_code, 0)

    def test_frame_rate_code_matches_span_for_20ms(self) -> None:
        cfg = device_cfg(sample_rate_khz=20000)
        frame = self._frame(self._device(cfg), ch=6)
        entry = next(r for r in SYNTH_RATES if r["span_khz"] == 20000)
        self.assertEqual(frame.rate_code, entry["code"])
        self.assertEqual(frame.span_khz, 20000)

    def test_frame_matches_ack_effective_for_1024_request(self) -> None:
        """Frame fft/span/rate_code must equal the effective values the
        synthetic ack advertises (request 1024 adapts to cap 256)."""
        cfg = device_cfg(fft_size=1024, sample_rate_khz=40000)
        dev = self._device(cfg)
        ack = self._status(dev, cfg)
        eff = ack["spectrum_effective"]
        frame = self._frame(dev, ch=6)
        self.assertEqual(frame.fft_size, eff["fft_size"])
        self.assertEqual(frame.span_khz, eff["span_khz"])
        self.assertEqual(frame.rate_code, eff["rate_code"])
        # and the ack itself must satisfy the smoke checker's caps rules
        self.assertIsNone(_caps_valid(ack))
        self.assertIn(eff["fft_size"], ack["spectrum_caps"]["fft_sizes"])

    def test_frame_mode_and_channel_come_from_the_config(self) -> None:
        cfg = device_cfg(mode=1, band=1, sample_rate_khz=40000)
        frame = self._frame(self._device(cfg), ch=36)
        self.assertEqual(frame.mode, 1)
        self.assertEqual(frame.band, 1)
        self.assertEqual(frame.channel, 36)
        self.assertEqual(frame.center_khz, 5180000)


def ack_epoch(blob: bytes) -> int:
    for msg in tlv.TlvParser().feed(blob):
        if isinstance(msg, tlv.Status):
            return int(msg.data["epoch"])
    raise AssertionError("ack did not decode")


class IdenticalConfigSemanticsTests(unittest.TestCase):
    """docs/wifi-monitor.md:42 - an identical CONFIG acknowledges
    WITHOUT an epoch change or cycle restart; only a real change moves
    the epoch (regression for the physical reconnect failure)."""

    def test_identical_config_keeps_epoch_cycle_and_pass(self) -> None:
        dev = SyntheticDevice(-1)
        cfg = device_cfg()
        self.assertEqual(ack_epoch(dev._on_config(cfg)), 1)
        # device is mid-pass when the GUI re-sends the same settings
        dev.cycle = 5
        dev._step_idx = 3
        dev._next_step = 123.0
        blob = dev._on_config(dict(cfg))       # identical request
        self.assertEqual(ack_epoch(blob), 1)   # same epoch in the ack
        self.assertEqual(dev.epoch, 1)
        self.assertEqual(dev.cycle, 5)         # no cycle restart
        self.assertEqual(dev._step_idx, 3)     # pass position untouched
        self.assertEqual(dev._next_step, 123.0)
        self.assertEqual(dev.cfg, cfg)

    def test_changed_config_advances_epoch_and_abandons_pass(self) -> None:
        dev = SyntheticDevice(-1)
        cfg = device_cfg()
        self.assertEqual(ack_epoch(dev._on_config(cfg)), 1)
        dev.cycle = 5
        dev._step_idx = 3
        blob = dev._on_config(device_cfg(fft_size=256))   # actual change
        self.assertEqual(ack_epoch(blob), 2)
        self.assertEqual(dev.epoch, 2)
        self.assertEqual(dev.cycle, 6)         # incomplete pass abandoned
        self.assertEqual(dev._step_idx, 0)     # fresh traversal
        cfg_after = dev.cfg
        if cfg_after is None:
            self.fail("cfg must be retained after a changed CONFIG")
        self.assertEqual(cfg_after["fft_size"], 256)


class ReceivedCapsExpectationTests(unittest.TestCase):
    """step_rf_ready must accept real-FW caps (through 1024) instead of
    comparing against the SYNTHETIC fixture list."""

    CAPS_1024 = {"source": "c5_snapshot_iq_fft",
                 "fft_sizes": [64, 128, 256, 512, 1024],
                 "rate_codes": [{"code": 0, "span_khz": 20000},
                                {"code": 1, "span_khz": 40000}],
                 "bin_unit": "centi_dbfs"}
    EFF = {"fft_size": 1024, "rate_code": 0, "span_khz": 20000}

    def setUp(self) -> None:
        self.win = MainWindow()
        self.win.show()
        APP.processEvents()
        self.fx = FixtureReader()
        self.win._attach(self.fx)
        self.win.play_btn.setChecked(True)
        self.fx.opened.emit()

    def tearDown(self) -> None:
        self.win.close()
        APP.processEvents()

    def _ack(self) -> None:
        from tests.test_monitor_widget import config_from_frame

        fields = config_from_frame(self.fx.writes[-1])
        event = rf_config_event(
            **fields, spectrum_caps=dict(self.CAPS_1024),
            spectrum_effective=dict(self.EFF))
        self.fx.deliver(tlv.encode_status_json(event))
        self.assertTrue(self.win.monitor_state.rf_ready)

    def test_step_rf_ready_derives_from_received_caps(self) -> None:
        self._ack()
        # would raise HarnessError if expectations came from constants
        step_rf_ready(self.win, None, timeout=3.0)
        items = [self.win.fft_combo.itemText(i)
                 for i in range(self.win.fft_combo.count())]
        self.assertEqual(items, ["64", "128", "256", "512", "1024"])
        self.assertEqual(self.win.fft_combo.currentText(), "1024")
        self.assertEqual(self.win.rbw_lbl.text(), "19.5 kHz")  # 20000/1024

    def test_step_rf_ready_rejects_effective_outside_caps(self) -> None:
        from tests.test_monitor_widget import config_from_frame

        fields = config_from_frame(self.fx.writes[-1])
        event = rf_config_event(
            **fields, spectrum_caps=dict(self.CAPS_1024),
            spectrum_effective={"fft_size": 4096, "rate_code": 0,
                                "span_khz": 20000})
        self.fx.deliver(tlv.encode_status_json(event))
        # fail-closed ack: caps parsing rejects 4096 -> rf never becomes ready
        self.assertFalse(self.win.monitor_state.rf_ready)
        from scripts.rf_gui_harness import Blocked

        with self.assertRaises(Blocked):   # reported, never silently passed
            step_rf_ready(self.win, None, timeout=0.3)


class UtilContractDeviceTests(unittest.TestCase):
    """The synthetic device must speak the approved Sampled PHY CCA
    contract: capability metadata on the ack, contract-legal raw samples on
    every channel except an intentional gap channel (SYNTHETIC fixtures)."""

    def test_ack_advertises_proven_capability(self) -> None:
        dev = SyntheticDevice(-1)
        cfg = device_cfg()
        blob = dev._on_config(dict(cfg))
        ack = next(m for m in tlv.TlvParser().feed(blob)
                   if isinstance(m, tlv.Status)).data
        util = ack["utilization"]
        self.assertTrue(util["available"])
        self.assertEqual(util["source"], UTIL_SOURCE)
        self.assertEqual(util["confidence"], UTIL_CONFIDENCE)

    def test_channel_events_carry_contract_util_with_gap_channel(self) -> None:
        dev = SyntheticDevice(-1)
        cfg = device_cfg()
        dev._on_config(dict(cfg))
        channels = SYNTHETIC_CHANNELS[cfg["band"]]
        for ch in channels:
            msg = next(m for m in tlv.TlvParser().feed(dev._channel_event(ch))
                       if isinstance(m, tlv.Status)).data
            if ch == channels[-1]:
                self.assertNotIn("util", msg, "gap channel must be absent")
                continue
            sample = parse_channel_util(msg.get("util"))
            if sample is None:
                self.fail(f"channel {ch}: non-contract util sample")
            # the synthetic device speaks the pooled contract (no versioning)
            self.assertNotIn("version", msg.get("util") or {})
            self.assertEqual(sample["samples"], 3)
            self.assertEqual(sample["attempted"], 4)
            self.assertLessEqual(sample["busy"], sample["total"])
            self.assertLessEqual(sample["window_us_upper"],
                                 5000 * sample["samples"])
            self.assertTrue(1 <= sample["samples"] <= 8)
            self.assertTrue(sample["samples"] <= sample["attempted"] <= 8)
        # first channel is a MEASURED zero (busy=0), still valid
        first = next(m for m in tlv.TlvParser().feed(
            dev._channel_event(channels[0])) if isinstance(m, tlv.Status)).data
        sample = parse_channel_util(first.get("util"))
        if sample is None:
            self.fail("zero-busy sample must stay valid")
        self.assertEqual(sample["busy"], 0)


class UtilColumnCheckerTests(unittest.TestCase):
    """The REAL util acceptance (no fixture assumptions): missing actual
    samples must fail, the mapping/metadata must be exact, and the honest
    UNAVAILABLE state passes without any percent."""

    CAP = {"available": True, "source": UTIL_SOURCE,
           "confidence": UTIL_CONFIDENCE}

    def setUp(self) -> None:
        self.win = MainWindow()
        self.win.show()
        APP.processEvents()
        self.fx = FixtureReader()
        self.win._attach(self.fx)
        self.win.play_btn.setChecked(True)
        self.fx.opened.emit()

    def tearDown(self) -> None:
        self.win.close()
        APP.processEvents()

    def _ack(self, utilization: dict) -> None:
        fields = config_from_frame(self.fx.writes[-1])
        self.fx.deliver(tlv.encode_status_json(
            rf_config_event(**fields, utilization=utilization)))

    def test_missing_actual_samples_fails_the_real_checker(self) -> None:
        self._ack(self.CAP)          # proven capability, zero samples
        with self.assertRaises(HarnessError):
            check_util_column(self.win, synthetic=False)

    def test_unavailable_state_passes_without_any_percent(self) -> None:
        self._ack({"available": False, "blocker": "unproven"})
        check_util_column(self.win, synthetic=False)   # honest unavailable

    def test_mapping_and_metadata_are_exact_for_the_real_checker(self) -> None:
        self._ack(self.CAP)
        self.fx.deliver(tlv.encode_status_json(channel_event(
            ch=6, util={"source": UTIL_SOURCE,
                        "confidence": UTIL_CONFIDENCE,
                        "busy": 1, "total": 65536,
                        "samples": 1, "attempted": 1,
                        "window_us_upper": 819})))
        self.assertIn(6, self.win._util)
        check_util_column(self.win, synthetic=False)   # exact mapping passes
        self.win._util[6] = 99.0        # corrupt the rendered fraction
        with self.assertRaises(HarnessError):
            check_util_column(self.win, synthetic=False)


if __name__ == "__main__":
    unittest.main()
