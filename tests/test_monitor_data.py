"""MonitorState / decode_channel behavior: epoch, ack, rates, bounds."""

from __future__ import annotations

import unittest

import numpy as np

from wifi_spectrum import tlv
from wifi_spectrum.monitor_data import (
    MAX_TRACKED_APS,
    UTIL_CONFIDENCE,
    UTIL_SOURCE,
    ChannelObservation,
    MonitorState,
    decode_channel,
    parse_channel_util,
)

CFG = {"mode": 0, "band": 0, "sweep_ms": 1000, "fft_size": 64,
       "sample_rate_khz": 20000}


def config_event(epoch: int = 1, **over) -> dict:
    d = {"schema": "wifi-monitor/1", "event": "config", "epoch": epoch,
         "dwell_ms": 120, "channels": list(range(1, 14)), "tx_dropped": 0}
    d.update(CFG)
    d.update(over)
    return d


def channel_event(epoch: int = 1, cycle: int = 1, ch: int = 6,
                  packets: int = 24, peak: int | None = -48,
                  observed_ms: int = 120,
                  aps: list | None = None, band: int = 0, **over) -> dict:
    d = {"schema": "wifi-monitor/1", "event": "channel", "epoch": epoch,
         "cycle": cycle, "band": band, "ch": ch, "observed_ms": observed_ms,
         "packets": packets, "peak_rssi_dbm": peak,
         "aps": aps if aps is not None else [], "ap_dropped": 0}
    d.update(over)
    return d


def cycle_event(epoch: int = 1, cycle: int = 1, band: int = 0, **over) -> dict:
    d = {"schema": "wifi-monitor/1", "event": "cycle", "epoch": epoch,
         "cycle": cycle, "band": band, "elapsed_ms": 1608, "uptime_ms": 1608}
    d.update(over)
    return d


AP1 = {"bssid": "001122334455", "ssid_hex": "74657374",
       "primary_ch": 6, "rssi_dbm": -48}


class DecodeChannelTests(unittest.TestCase):
    def test_rate_normalization(self) -> None:
        obs = decode_channel(channel_event())
        self.assertEqual(obs.packets, 24)
        self.assertEqual(obs.packets_per_second, 200.0)  # 24 / 0.120 s

    def test_zero_packets_null_peak(self) -> None:
        obs = decode_channel(channel_event(packets=0, peak=None))
        self.assertEqual(obs.packets_per_second, 0.0)
        self.assertIsNone(obs.peak_rssi_dbm)

    def test_peak_presence_must_match_count(self) -> None:
        with self.assertRaises(ValueError):
            decode_channel(channel_event(packets=0, peak=-60))
        with self.assertRaises(ValueError):
            decode_channel(channel_event(packets=5, peak=None))

    def test_invalid_ssid_hex_rejected(self) -> None:
        bad = dict(AP1, ssid_hex="zz")
        with self.assertRaises(ValueError):
            decode_channel(channel_event(aps=[bad]))

    def test_ssid_decoded_with_replacement(self) -> None:
        ap = dict(AP1, ssid_hex="fffe")
        obs = decode_channel(channel_event(aps=[ap]))
        self.assertEqual(obs.aps[0]["ssid"], "��")
        self.assertEqual(obs.aps[0]["ssid_hex"], "fffe")

    def test_ap_list_bounded(self) -> None:
        with self.assertRaises(ValueError):
            decode_channel(channel_event(aps=[AP1] * 9))

    def test_bad_bounds_rejected(self) -> None:
        base = channel_event()
        cases: list[dict] = [{"ch": 0}, {"ch": 999}, {"observed_ms": 0},
                             {"packets": -1}, {"packets": 1.5}]
        for over in cases:
            with self.subTest(over), self.assertRaises(ValueError):
                decode_channel({**base, **over})


class MonitorStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = MonitorState()
        self.state.request(**CFG)

    def ack(self, epoch: int = 1, **over) -> None:
        self.assertTrue(self.state.accept(config_event(epoch=epoch, **over)))

    def test_config_ack_requires_matching_echo(self) -> None:
        self.assertFalse(self.state.ready)
        other = config_event(mode=1)
        self.assertFalse(self.state.accept(other))     # stale device config
        self.assertFalse(self.state.ready)
        self.ack()
        self.assertTrue(self.state.ready)
        self.assertEqual(self.state.channels, tuple(range(1, 14)))
        self.assertEqual(self.state.dwell_ms, 120)

    def test_heartbeat_config_does_not_reset_epoch(self) -> None:
        self.ack(epoch=1)
        self.state.accept(channel_event())
        self.state.accept(cycle_event(cycle=1))
        cycles_before = self.state.cycles
        self.ack(epoch=1)                              # duplicate heartbeat
        self.assertEqual(self.state.cycles, cycles_before)
        self.assertTrue(self.state.displayed)          # visible data kept

    def test_stale_epoch_measurements_discarded(self) -> None:
        self.ack(epoch=2)
        self.assertFalse(self.state.accept(channel_event(epoch=1)))
        self.assertFalse(self.state.accept(cycle_event(epoch=1)))
        self.assertEqual(self.state.displayed, {})
        self.assertTrue(self.state.accept(channel_event(epoch=2)))

    def test_wrong_band_discarded(self) -> None:
        self.ack()
        self.assertFalse(self.state.accept(channel_event(band=1)))

    def test_epoch_change_clears_measurements(self) -> None:
        self.ack(epoch=1)
        self.state.accept(channel_event())
        self.state.accept(cycle_event(cycle=1))
        self.assertTrue(self.state.displayed)
        self.ack(epoch=2)                              # device restarted
        self.assertEqual(self.state.displayed, {})
        self.assertEqual(self.state.cycles, 0)
        self.assertEqual(self.state.aps, {})

    def test_live_updates_per_channel_sweep_per_cycle(self) -> None:
        self.ack()
        self.assertTrue(self.state.accept(channel_event(ch=1)))
        self.assertIn(1, self.state.displayed)

        self.state.request(**{**CFG, "mode": 1})
        self.ack(epoch=2, mode=1)
        self.assertFalse(self.state.accept(
            channel_event(ch=1, epoch=2, cycle=1)))    # staged, not visible
        self.assertEqual(self.state.displayed, {})
        self.assertIn(1, self.state.staging)
        self.assertTrue(self.state.accept(cycle_event(epoch=2, cycle=1)))
        self.assertIn(1, self.state.displayed)

    def test_duplicate_cycle_counted_once(self) -> None:
        self.ack()
        self.state.accept(channel_event(cycle=1))
        self.assertTrue(self.state.accept(cycle_event(cycle=1)))
        self.assertFalse(self.state.accept(cycle_event(cycle=1)))
        self.assertEqual(self.state.cycles, 1)

    def test_config_change_starts_new_epoch(self) -> None:
        self.ack(epoch=1)
        self.state.accept(channel_event())
        self.state.accept(cycle_event(cycle=1))
        # GUI now requests sweep/5 GHz; firmware echoes a new epoch
        self.state.request(**{**CFG, "mode": 1, "band": 1})
        self.assertTrue(self.state.accept(config_event(
            epoch=2, mode=1, band=1, channels=[36, 40])))
        self.assertEqual(self.state.epoch, 2)
        self.assertEqual(self.state.displayed, {})
        self.assertEqual(self.state.cycles, 0)
        # sweep mode: first channel is staged until the cycle completes
        self.assertFalse(self.state.accept(
            channel_event(epoch=2, cycle=1, band=1, ch=36)))
        self.assertIn(36, self.state.staging)
        self.assertTrue(self.state.accept(
            cycle_event(epoch=2, cycle=1, band=1)))
        self.assertIn(36, self.state.displayed)

    def test_channel_error_marks_unavailable(self) -> None:
        self.ack()
        err = {"schema": "wifi-monitor/1", "event": "channel_error",
               "epoch": 1, "cycle": 1, "band": 0, "ch": 144,
               "code": "ESP_ERR_INVALID_ARG"}
        self.assertTrue(self.state.accept(err))
        self.assertIn(144, self.state.unavailable)
        self.assertIn("ESP_ERR_INVALID_ARG", self.state.last_error or "")

    def test_ap_sightings_bounded_and_keyed_by_channel(self) -> None:
        self.ack()
        for i in range(300):
            bssid = f"020000{i:06x}"     # always 6 bytes of hex
            ap = dict(AP1, bssid=bssid)
            ch = 1 + (i % 13)
            self.state.accept(channel_event(ch=ch, aps=[ap]))
        self.assertLessEqual(len(self.state.aps), MAX_TRACKED_APS)
        # same BSSID on two receive channels stays two entries
        self.state.reset()
        self.state.request(**CFG)
        self.ack()
        self.state.accept(channel_event(ch=1, aps=[AP1]))
        self.state.accept(channel_event(ch=6, aps=[AP1]))
        self.assertEqual(len(self.state.aps), 2)

    def test_error_event_visible(self) -> None:
        self.ack()
        err = {"schema": "wifi-monitor/1", "event": "error",
               "code": "invalid_config"}
        self.assertTrue(self.state.accept(err))
        self.assertEqual(self.state.last_error, "invalid_config")
        self.assertFalse(self.state.accept(err))       # unchanged text

    def test_wrong_schema_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.state.accept({"schema": "other", "event": "config"})

    def test_request_before_any_config_stays_not_ready(self) -> None:
        self.state.reset()
        self.assertIsNone(self.state.epoch)
        self.assertFalse(self.state.ready)
        self.assertFalse(self.state.accept(channel_event()))

    def test_peak_used_as_provided(self) -> None:
        self.ack()
        self.state.accept(channel_event(packets=3, peak=-48))
        obs = self.state.displayed[6]
        self.assertIsInstance(obs, ChannelObservation)
        self.assertEqual(obs.peak_rssi_dbm, -48)

    def test_partial_cycle_staging_discarded_when_cycle_advances(self) -> None:
        self.state.request(**{**CFG, "mode": 1})
        self.ack(epoch=2, mode=1)
        self.state.accept(channel_event(epoch=2, cycle=1, ch=1))
        self.assertIn(1, self.state.staging)
        # pause/abort dropped cycle 1's cycle event; the next cycle must not
        # inherit its staged leftovers
        self.state.accept(channel_event(epoch=2, cycle=2, ch=2))
        self.assertNotIn(1, self.state.staging)
        self.assertIn(2, self.state.staging)

    def test_epoch_change_clears_staging(self) -> None:
        self.state.request(**{**CFG, "mode": 1})
        self.ack(epoch=1, mode=1)
        self.state.accept(channel_event(epoch=1, cycle=1, ch=1))
        self.assertIn(1, self.state.staging)
        self.ack(epoch=2, mode=1)                  # device applied new config
        self.assertEqual(self.state.staging, {})


class ApExpiryTests(unittest.TestCase):
    def test_ap_sightings_expire_after_30_seconds(self) -> None:
        now = [1000.0]
        state = MonitorState(clock=lambda: now[0])
        state.request(**CFG)
        self.assertTrue(state.accept(config_event()))
        state.accept(channel_event(aps=[AP1]))
        self.assertEqual(len(state.aps), 1)
        now[0] += 29.0
        state.prune_aps()
        self.assertEqual(len(state.aps), 1)        # still within 30 s
        now[0] += 2.0
        state.prune_aps()
        self.assertEqual(state.aps, {})            # aged out


class CycleCompletenessTests(unittest.TestCase):
    """A completed cycle that misses advertised channels must expose those
    channels as unavailable (lossy UART, no flow control) instead of
    silently keeping the previous cycle's observation visible. The next
    valid observation recovers the row; duplicate or stale cycle events
    never mutate state."""

    CHANNELS = (1, 2, 3)

    def setUp(self) -> None:
        self.state = MonitorState()
        self.state.request(**CFG)

    def ack(self, mode: int = 0) -> None:
        self.assertTrue(self.state.accept(
            config_event(mode=mode, channels=list(self.CHANNELS))))

    def observe(self, ch: int, cycle: int) -> None:
        self.state.accept(channel_event(ch=ch, cycle=cycle))

    def test_live_cycle_missing_channel_marked_unavailable_then_recovers(self) -> None:
        self.ack()
        for ch in self.CHANNELS:
            self.observe(ch, cycle=1)
        self.assertTrue(self.state.accept(cycle_event(cycle=1)))
        self.assertEqual(self.state.unavailable, set())
        # cycle 2 loses channel 3 in transit
        self.observe(1, cycle=2)
        self.observe(2, cycle=2)
        self.assertTrue(self.state.accept(cycle_event(cycle=2)))
        self.assertIn(3, self.state.unavailable)   # stale value must be hidden
        self.assertIn(3, self.state.displayed)      # record kept, view gated
        self.state.accept(channel_event(ch=3, cycle=3))
        self.assertNotIn(3, self.state.unavailable)  # recovered

    def test_sweep_cycle_missing_channel_marked_unavailable(self) -> None:
        self.state.request(**{**CFG, "mode": 1})
        self.ack(mode=1)
        for ch in self.CHANNELS:
            self.observe(ch, cycle=1)
        self.assertTrue(self.state.accept(cycle_event(cycle=1)))
        self.assertEqual(self.state.unavailable, set())
        # cycle 2 observes only channel 1 (with a fresh measurement)
        self.state.accept(channel_event(ch=1, cycle=2, packets=7, peak=-60))
        self.assertTrue(self.state.accept(cycle_event(cycle=2)))
        self.assertIn(2, self.state.unavailable)
        self.assertIn(3, self.state.unavailable)
        self.assertNotIn(1, self.state.unavailable)
        self.assertEqual(self.state.displayed[1].packets, 7)  # flushed c2 obs

    def test_duplicate_and_stale_cycles_do_not_mutate(self) -> None:
        self.ack()
        for ch in self.CHANNELS:
            self.observe(ch, cycle=1)
        self.assertTrue(self.state.accept(cycle_event(cycle=1)))
        before = (dict(self.state.displayed), set(self.state.unavailable),
                  self.state.cycles, dict(self.state.staging),
                  self.state.elapsed_ms, self.state.uptime_ms)
        self.assertFalse(self.state.accept(cycle_event(cycle=1)))  # duplicate
        self.assertFalse(self.state.accept(cycle_event(cycle=0)))  # stale
        after = (dict(self.state.displayed), set(self.state.unavailable),
                 self.state.cycles, dict(self.state.staging),
                 self.state.elapsed_ms, self.state.uptime_ms)
        self.assertEqual(before, after)

    def test_cold_partial_join_marks_unobserved_unavailable(self) -> None:
        # joined mid-cycle: only channel 3 seen before the cycle closes
        self.ack()
        self.observe(3, cycle=1)
        self.assertTrue(self.state.accept(cycle_event(cycle=1)))
        self.assertIn(1, self.state.unavailable)
        self.assertIn(2, self.state.unavailable)
        self.assertNotIn(3, self.state.unavailable)
        self.assertNotIn(1, self.state.displayed)  # never observed: honest "—"

    def test_lost_cycle_event_binds_coverage_to_new_cycle(self) -> None:
        # Repro: cycle 1's observation for channel 3 arrives but its
        # completion marker is lost; cycle 2 observes every OTHER channel
        # and completes. Channel 3 has no cycle-2 observation, so it must
        # not stay current from cycle-1 coverage.
        self.ack()
        self.observe(3, cycle=1)            # marker for cycle 1 lost
        self.observe(1, cycle=2)
        self.observe(2, cycle=2)
        self.assertTrue(self.state.accept(cycle_event(cycle=2)))
        self.assertIn(3, self.state.unavailable)
        self.assertNotIn(1, self.state.unavailable)
        self.assertNotIn(2, self.state.unavailable)

    def test_marker_without_observations_closes_empty_cycle(self) -> None:
        # All of cycle 2's observations were lost; its marker must close
        # an EMPTY coverage set, never treat cycle 1's channels as current.
        self.ack()
        self.observe(3, cycle=1)            # marker for cycle 1 lost too
        self.assertTrue(self.state.accept(cycle_event(cycle=2)))
        self.assertIn(3, self.state.unavailable)
        self.assertIn(1, self.state.unavailable)
        self.assertIn(2, self.state.unavailable)

    def test_lost_marker_does_not_flush_staging_as_current(self) -> None:
        self.state.request(**{**CFG, "mode": 1})
        self.ack(mode=1)
        self.observe(3, cycle=1)            # staged; marker for cycle 1 lost
        self.assertIn(3, self.state.staging)
        self.assertTrue(self.state.accept(cycle_event(cycle=2)))
        self.assertEqual(self.state.displayed, {})  # not flushed as current
        self.assertIn(3, self.state.unavailable)

    def test_stale_observation_after_completed_cycle_rejected(self) -> None:
        self.ack()
        self.observe(3, cycle=1)            # packets=24, peak=-48
        self.assertTrue(self.state.accept(cycle_event(cycle=1)))
        before = self.state.displayed[3]
        before_unavailable = set(self.state.unavailable)
        late = self.state.accept(channel_event(ch=3, cycle=1,
                                               packets=99, peak=-90))
        self.assertFalse(late)              # must not resurrect old values
        self.assertEqual(self.state.displayed[3], before)
        self.assertEqual(self.state.unavailable, before_unavailable)


class SpectrumRfStateTests(unittest.TestCase):
    """0x04 gating and RF capability ack (handoff v2 sections 2/4/5):
    fail-closed capability parsing, stale-epoch/band/cycle drops, live
    per-frame vs sweep staged-until-cycle."""

    CAPS = {"source": "c5_snapshot_iq_fft", "fft_sizes": [64, 128, 256],
            "rate_codes": [{"code": 0, "span_khz": 20000}],
            "bin_unit": "centi_dbfs"}
    EFF = {"fft_size": 128, "rate_code": 0, "span_khz": 20000}

    def setUp(self) -> None:
        self.state = MonitorState()
        self.state.request(**CFG)

    def ack(self, epoch: int = 1, **over) -> bool:
        return self.state.accept(config_event(epoch=epoch, **over))

    def rf_ack(self, epoch: int = 1, **over) -> bool:
        return self.ack(epoch=epoch, spectrum=True,
                        spectrum_caps=dict(self.CAPS),
                        spectrum_effective=dict(self.EFF),
                        utilization={"available": False,
                                     "blocker": "cca_semantics_unproven"},
                        **over)

    @staticmethod
    def frame(epoch: int = 1, cycle: int = 1, band: int = 0,
              channel: int = 6, mode: int = 0) -> tlv.SpectrumRf:
        # coherent with the class ack: effective fft 128, span 20000,
        # rate_code 0, advertised channel, active mode
        return tlv.SpectrumRf(epoch, cycle, band, channel, mode, 0, 128, 0,
                              2412000, 20000,
                              np.array([-30.0] * 128, dtype=np.float32))

    def test_rf_ack_parses_caps_effective_and_utilization(self) -> None:
        self.assertTrue(self.rf_ack())
        self.assertTrue(self.state.rf_ready)
        self.assertEqual(
            self.state.spectrum_caps,
            {"fft_sizes": [64, 128, 256],
             "rate_codes": [{"code": 0, "span_khz": 20000}],
             "bin_unit": "centi_dbfs"})
        self.assertEqual(self.state.spectrum_effective, self.EFF)
        self.assertFalse(self.state.utilization_available)

    def test_spectrum_false_fails_closed_even_with_caps_present(self) -> None:
        self.assertTrue(self.ack(spectrum=False,
                                 spectrum_caps=dict(self.CAPS),
                                 spectrum_effective=dict(self.EFF)))
        self.assertTrue(self.state.ready)
        self.assertFalse(self.state.rf_ready)
        self.assertIsNone(self.state.spectrum_caps)
        self.assertIsNone(self.state.spectrum_effective)

    def test_missing_caps_or_effective_fails_closed(self) -> None:
        self.assertTrue(self.ack(spectrum=True,
                                 spectrum_effective=dict(self.EFF)))
        self.assertFalse(self.state.rf_ready)      # no caps dict
        self.assertTrue(self.ack(spectrum=True, spectrum_caps=dict(self.CAPS)))
        self.assertFalse(self.state.rf_ready)      # no effective dict

    def test_malformed_caps_fail_closed_but_config_still_acks(self) -> None:
        broken = {
            "empty-fft-sizes": dict(self.CAPS, fft_sizes=[]),
            "zero-span": dict(self.CAPS,
                              rate_codes=[{"code": 0, "span_khz": 0}]),
            "missing-code": dict(self.CAPS,
                                 rate_codes=[{"span_khz": 20000}]),
            "non-int-size": dict(self.CAPS, fft_sizes=["256"]),
        }
        for name, caps in broken.items():
            with self.subTest(name):
                state = MonitorState()
                state.request(**CFG)
                self.assertTrue(state.accept(config_event(
                    spectrum=True, spectrum_caps=caps,
                    spectrum_effective=dict(self.EFF))))
                self.assertTrue(state.ready)       # ack itself is valid
                self.assertFalse(state.rf_ready)   # RF path stays off
                self.assertIsNone(state.spectrum_caps)

    def test_effective_must_match_capability_entries(self) -> None:
        cases = {
            "unknown-rate-code": dict(self.EFF, rate_code=7),
            "span-disagrees-with-code": dict(self.EFF, span_khz=40000),
            "unsupported-fft-size": dict(self.EFF, fft_size=1024),
        }
        for name, eff in cases.items():
            with self.subTest(name):
                state = MonitorState()
                state.request(**CFG)
                state.accept(config_event(spectrum=True,
                                          spectrum_caps=dict(self.CAPS),
                                          spectrum_effective=eff))
                self.assertFalse(state.rf_ready)
                self.assertIsNone(state.spectrum_effective)

    def test_rf_frames_dropped_without_valid_capability_ack(self) -> None:
        self.assertFalse(self.state.accept_rf(self.frame()))   # no ack
        self.ack()                                             # baseline
        self.assertFalse(self.state.accept_rf(self.frame()))   # spectrum:false
        self.rf_ack()
        self.assertTrue(self.state.accept_rf(self.frame()))

    def test_rf_frames_from_stale_epoch_or_wrong_band_dropped(self) -> None:
        self.rf_ack(epoch=1)
        self.assertFalse(self.state.accept_rf(self.frame(epoch=2)))
        self.assertTrue(self.state.accept_rf(self.frame(band=0)))
        self.assertFalse(self.state.accept_rf(self.frame(band=1)))

    def test_live_frames_accepted_immediately_per_frame(self) -> None:
        self.rf_ack()                                # mode 0 (CFG default)
        self.assertTrue(self.state.accept_rf(self.frame(cycle=1, channel=6)))
        self.assertTrue(self.state.accept_rf(self.frame(cycle=1, channel=7)))
        # after the cycle marker the cycle is closed: late frames dropped
        self.assertTrue(self.state.accept(cycle_event(cycle=1)))
        self.assertFalse(self.state.accept_rf(self.frame(cycle=1, channel=8)))
        self.assertTrue(self.state.accept_rf(self.frame(cycle=2, channel=6)))

    def test_sweep_frames_stage_until_cycle_marker(self) -> None:
        self.state.request(**{**CFG, "mode": 1})
        self.assertTrue(self.rf_ack(mode=1))
        a = self.frame(cycle=1, channel=6, mode=1)
        b = self.frame(cycle=1, channel=7, mode=1)
        self.assertFalse(self.state.accept_rf(a))    # staged, not visible
        self.assertFalse(self.state.accept_rf(b))
        self.assertEqual(self.state.rf_flushed, [])  # nothing yet
        self.assertTrue(self.state.accept(cycle_event(cycle=1)))
        self.assertEqual(self.state.rf_flushed, [a, b])   # arrival order
        self.assertEqual(self.state.rf_stage, {})
        # next cycle stages fresh; marker flushes only its own frames
        c = self.frame(cycle=2, channel=6, mode=1)
        self.assertFalse(self.state.accept_rf(c))
        self.assertTrue(self.state.accept(cycle_event(cycle=2)))
        self.assertEqual(self.state.rf_flushed, [c])

    def test_sweep_frames_from_closed_cycles_dropped(self) -> None:
        self.state.request(**{**CFG, "mode": 1})
        self.assertTrue(self.rf_ack(mode=1))
        self.state.accept_rf(self.frame(cycle=1, mode=1))
        self.assertTrue(self.state.accept(cycle_event(cycle=1)))
        self.assertFalse(self.state.accept_rf(self.frame(cycle=1, mode=1)))  # replay
        self.assertFalse(self.state.accept_rf(self.frame(cycle=0)))  # stale

    def test_cycle_marker_without_frames_yields_empty_flush(self) -> None:
        self.state.request(**{**CFG, "mode": 1})
        self.assertTrue(self.rf_ack(mode=1))
        self.assertTrue(self.state.accept(cycle_event(cycle=1)))
        self.assertEqual(self.state.rf_flushed, [])  # reset, not leftover

    def test_epoch_change_clears_rf_staging(self) -> None:
        self.state.request(**{**CFG, "mode": 1})
        self.assertTrue(self.rf_ack(mode=1))
        self.state.accept_rf(self.frame(cycle=1, mode=1))
        self.assertEqual(len(self.state.rf_stage), 1)
        # applied CONFIG change (same requested fields, new epoch) resets
        # staging AND the RF capability line until a fresh ack arrives.
        self.assertTrue(self.ack(epoch=2, mode=1))
        self.assertEqual(self.state.rf_stage, {})
        self.assertEqual(self.state.rf_flushed, [])
        self.assertFalse(self.state.rf_ready)
        self.assertFalse(self.state.accept_rf(self.frame(epoch=2)))
        self.assertEqual(self.state.rf_stage, {})   # dropped, not staged
        self.assertTrue(self.rf_ack(epoch=2, mode=1))
        # sweep semantics resumed with the new ack: the frame stages
        # (False) instead of being dropped for lack of capabilities
        self.assertFalse(self.state.accept_rf(self.frame(epoch=2, mode=1)))
        self.assertEqual(len(self.state.rf_stage), 1)

    def test_rf_frame_must_match_effective_fft_span_and_mode(self) -> None:
        self.rf_ack()          # effective fft 128, span 20000, mode 0
        self.assertTrue(self.state.accept_rf(self.frame()))
        cases = {
            "wrong-fft": tlv.SpectrumRf(
                1, 1, 0, 6, 0, 0, 64, 0, 2412000, 20000,
                np.full(64, -30.0, dtype=np.float32)),
            "wrong-span": tlv.SpectrumRf(
                1, 1, 0, 6, 0, 0, 128, 0, 2412000, 40000,
                np.full(128, -30.0, dtype=np.float32)),
            "wrong-mode": tlv.SpectrumRf(
                1, 1, 0, 6, 1, 0, 128, 0, 2412000, 20000,
                np.full(128, -30.0, dtype=np.float32)),
            "unadvertised-channel": tlv.SpectrumRf(
                1, 1, 0, 14, 0, 0, 128, 0, 2412000, 20000,
                np.full(128, -30.0, dtype=np.float32)),
        }
        for name, bad in cases.items():
            with self.subTest(name):
                self.assertFalse(self.state.accept_rf(bad))

    def test_rf_rate_code_must_match_caps_entry(self) -> None:
        self.rf_ack()          # caps: [{code: 0, span_khz: 20000}]
        unknown_code = tlv.SpectrumRf(
            1, 1, 0, 6, 0, 5, 128, 0, 2412000, 20000,
            np.full(128, -30.0, dtype=np.float32))
        self.assertFalse(self.state.accept_rf(unknown_code))
        mismatched = tlv.SpectrumRf(
            1, 1, 0, 6, 0, 1, 128, 0, 2412000, 20000,
            np.full(128, -30.0, dtype=np.float32))
        # code 1 means span 40000 in the caps table: never matches 20000
        self.assertFalse(self.state.accept_rf(mismatched))
        self.assertTrue(self.state.accept_rf(self.frame()))

    def test_wrong_bin_unit_fails_rf_closed(self) -> None:
        self.assertTrue(self.ack(
            spectrum=True,
            spectrum_caps=dict(self.CAPS, bin_unit="dbm"),
            spectrum_effective=dict(self.EFF)))
        self.assertTrue(self.state.ready)       # ack itself stays valid
        self.assertFalse(self.state.rf_ready)   # wrong unit: RF stays off
        self.assertIsNone(self.state.spectrum_caps)
        self.assertFalse(self.state.accept_rf(self.frame()))

    def test_sweep_stage_bounded_one_frame_per_channel(self) -> None:
        self.state.request(**{**CFG, "mode": 1})
        self.assertTrue(self.rf_ack(mode=1))
        first = tlv.SpectrumRf(
            1, 1, 0, 6, 1, 0, 128, 0, 2412000, 20000,
            np.full(128, -30.0, dtype=np.float32))
        duplicate = tlv.SpectrumRf(
            1, 1, 0, 6, 1, 0, 128, 0, 2412000, 20000,
            np.full(128, -40.0, dtype=np.float32))
        other = self.frame(cycle=1, channel=11, mode=1)
        self.assertFalse(self.state.accept_rf(first))
        self.assertFalse(self.state.accept_rf(duplicate))   # replaces
        self.assertFalse(self.state.accept_rf(other))
        self.assertEqual(len(self.state.rf_stage), 2)   # bounded per channel
        self.assertIs(self.state.rf_stage[6], duplicate)  # latest wins
        self.assertLessEqual(len(self.state.rf_stage),
                             len(self.state.channels))
        self.assertTrue(self.state.accept(cycle_event(cycle=1)))
        self.assertEqual(self.state.rf_flushed, [duplicate, other])


class UtilContractTests(unittest.TestCase):
    """Sampled PHY CCA contract: capability metadata gate + strict raw
    sample validation (measured zero valid, B>A invalid, no clamp, bools
    rejected, unknown additive keys tolerated)."""

    def setUp(self) -> None:
        self.state = MonitorState()
        self.state.request(**CFG)

    def _ack(self, utilization: dict, epoch: int = 1, **over) -> bool:
        return self.state.accept(
            config_event(epoch=epoch, utilization=utilization, **over))

    @staticmethod
    def cap(**over) -> dict:
        d = {"available": True, "source": UTIL_SOURCE,
             "confidence": UTIL_CONFIDENCE}
        d.update(over)
        return d

    @staticmethod
    def sample(**over) -> dict:
        d = {"source": UTIL_SOURCE, "confidence": UTIL_CONFIDENCE,
             "busy": 100, "total": 65536, "window_us_upper": 819}
        d.update(over)
        return d

    def test_capability_requires_known_source_and_confidence(self) -> None:
        self.assertTrue(self._ack(self.cap()))
        self.assertTrue(self.state.utilization_available)
        # a config ack with broken provenance stays a VALID config, but the
        # capability gate must fail closed
        self.assertTrue(self._ack(self.cap(source="wrong_source")))
        self.assertFalse(self.state.utilization_available)
        self.assertTrue(self._ack(self.cap(confidence="calibrated")))
        self.assertFalse(self.state.utilization_available)
        self.assertTrue(
            self._ack({"available": True, "confidence": UTIL_CONFIDENCE}))
        self.assertFalse(self.state.utilization_available)
        # available:false stays unavailable regardless of metadata
        self.assertTrue(self._ack({"available": False,
                                   "blocker": "unproven"}))
        self.assertFalse(self.state.utilization_available)

    def test_capability_tolerates_unknown_additive_keys(self) -> None:
        self.assertTrue(self._ack(self.cap(window_us_upper=819,
                                           future_key="ignored")))
        self.assertTrue(self.state.utilization_available)

    def test_parse_channel_util_strict_contract(self) -> None:
        good = parse_channel_util(self.sample(busy=0))   # measured zero
        if good is None:
            self.fail("valid zero-busy sample rejected")
        self.assertEqual(good["busy"], 0)
        bad = [
            self.sample(busy=True),            # bools are not ints
            self.sample(total=True),
            self.sample(busy=65537),           # B = A+1 endpoint: invalid
            self.sample(busy=-1),
            self.sample(total=0),
            self.sample(total=0x08000000),     # beyond 27 bits
            self.sample(window_us_upper=0),
            self.sample(window_us_upper=5001),
            self.sample(window_us_upper=True),
            self.sample(source="wrong"),
            self.sample(confidence="calibrated"),
            self.sample(total="65536"),        # str is not int
        ]
        for i, sample in enumerate(bad):
            with self.subTest(i=i, sample=sample):
                self.assertIsNone(parse_channel_util(sample))
        self.assertIsNone(parse_channel_util(None))
        self.assertIsNone(parse_channel_util("util"))
        self.assertIsNotNone(parse_channel_util(
            self.sample(extra_key="additive keys are tolerated")))

    def test_channel_util_stored_gated_and_cleared(self) -> None:
        self.assertTrue(self._ack({"available": False,
                                   "blocker": "unproven"}))
        self.assertTrue(self.state.accept(channel_event(util=self.sample())))
        self.assertEqual(self.state.util_samples, {})
        # capability proven -> stored raw
        self.assertTrue(self._ack(self.cap()))
        util = self.sample()
        self.assertTrue(self.state.accept(
            channel_event(ch=6, cycle=2, util=util)))
        self.assertEqual(self.state.util_samples.get(6), util)
        # invalid sample on an accepted event = gap (never stale/zero)
        self.assertTrue(self.state.accept(
            channel_event(ch=6, cycle=3,
                          util=self.sample(busy=70000))))
        self.assertNotIn(6, self.state.util_samples)
        # absence = gap
        self.assertTrue(self.state.accept(channel_event(ch=6, cycle=4)))
        self.assertNotIn(6, self.state.util_samples)
        # epoch change clears (no stale reuse across epochs)
        self.assertTrue(self.state.accept(
            channel_event(ch=6, cycle=5, util=util)))
        self.assertIn(6, self.state.util_samples)
        self.assertTrue(self._ack(self.cap(), epoch=2))
        self.assertEqual(self.state.util_samples, {})

    def test_bool_envelope_rejected_and_cycle_uint32_bounds(self) -> None:
        self.assertTrue(self._ack(self.cap()))
        # True == 1 must never satisfy an epoch/band equality
        self.assertFalse(self.state.accept(
            channel_event(epoch=True, cycle=1, util=self.sample())))
        self.assertFalse(self.state.accept(
            channel_event(band=True, cycle=1, util=self.sample())))
        with self.assertRaises(ValueError):
            self.state.accept(channel_event(cycle=-1))
        with self.assertRaises(ValueError):
            self.state.accept(channel_event(cycle=0x100000000))
        with self.assertRaises(ValueError):
            self.state.accept(channel_event(ch=True))
        # cycle 0 is a legal uint32 value
        self.assertTrue(self.state.accept(
            channel_event(cycle=0, ch=6, util=self.sample())))
        self.assertEqual(self.state.util_samples.get(6), self.sample())

    def test_sample_needs_advertised_channel_and_stage_bounded(self) -> None:
        self.assertTrue(self._ack(self.cap()))
        # an unadvertised channel's observation keeps the older packet
        # semantics (accepted), but no util may enter the bounded maps
        self.assertTrue(self.state.accept(
            channel_event(ch=50, cycle=1, util=self.sample())))
        self.assertEqual(self.state.util_samples, {})
        self.assertEqual(self.state.util_stage, {})
        # duplicate-channel frames in one cycle cannot grow the stage
        self.assertTrue(self.state.accept(
            channel_event(ch=6, cycle=2, util=self.sample())))
        self.assertTrue(self.state.accept(
            channel_event(ch=6, cycle=2, util=self.sample(busy=200))))
        self.assertEqual(len(self.state.util_stage), 1)
        self.assertLessEqual(len(self.state.util_stage),
                             len(self.state.channels))
        self.assertEqual(self.state.util_stage[6]["busy"], 200)

    def test_cycle_flush_replaces_and_missing_cycles_clear(self) -> None:
        self.state.request(**{**CFG, "mode": 1})
        self.assertTrue(self._ack(self.cap(), mode=1))
        # sweep: staged, NOT published before the marker
        self.assertFalse(self.state.accept(
            channel_event(ch=6, cycle=1, util=self.sample())))
        self.assertEqual(len(self.state.util_stage), 1)
        self.assertEqual(self.state.util_samples, {})
        self.assertTrue(self.state.accept(cycle_event(cycle=1)))
        self.assertIn(6, self.state.util_samples)   # flush on marker
        self.assertEqual(self.state.util_stage, {})
        # next cycle: only ch1 staged -> ch6 becomes a gap at its marker
        self.assertFalse(self.state.accept(
            channel_event(ch=1, cycle=2, util=self.sample(busy=50))))
        self.assertTrue(self.state.accept(cycle_event(cycle=2)))
        self.assertEqual(list(self.state.util_samples), [1])
        # a marker for a cycle with NO channel signal: everything gapped
        self.assertTrue(self.state.accept(cycle_event(cycle=3)))
        self.assertEqual(self.state.util_samples, {})

    def test_lost_marker_never_bridges_stale_util(self) -> None:
        self.assertTrue(self._ack(self.cap()))
        self.assertTrue(self.state.accept(
            channel_event(ch=6, cycle=1, util=self.sample())))
        self.assertTrue(self.state.accept(cycle_event(cycle=1)))
        self.assertEqual(list(self.state.util_samples), [6])
        # marker2 lost: the first cycle3 signal starts a fresh scope, so
        # cycle1's percent can never be reused
        self.assertTrue(self.state.accept(
            channel_event(ch=1, cycle=3, util=self.sample(busy=5))))
        self.assertEqual(list(self.state.util_samples), [1])

    def test_channel_error_invalidates_staged_and_published_util(self) -> None:
        err = {"schema": "wifi-monitor/1", "event": "channel_error",
               "epoch": 1, "cycle": 1, "band": 0, "ch": 6,
               "code": "spectrum_capture"}
        # live, before any marker: the published value dies immediately
        self.assertTrue(self._ack(self.cap()))
        self.assertTrue(self.state.accept(
            channel_event(ch=6, cycle=1, util=self.sample())))
        self.assertIn(6, self.state.util_samples)
        self.state.accept(err)
        self.assertNotIn(6, self.state.util_samples)
        self.assertNotIn(6, self.state.util_stage)
        # sweep: a staged value dies too (the marker must not flush it)
        self.state.request(**{**CFG, "mode": 1})
        self.assertTrue(self._ack(self.cap(), mode=1))
        self.assertFalse(self.state.accept(
            channel_event(ch=6, cycle=2, util=self.sample())))
        self.assertEqual(len(self.state.util_stage), 1)
        self.state.accept({**err, "cycle": 2})
        self.assertEqual(self.state.util_stage, {})
        self.assertTrue(self.state.accept(cycle_event(cycle=2)))
        self.assertEqual(self.state.util_samples, {})

    def test_capability_loss_same_epoch_gaps_everything(self) -> None:
        self.assertTrue(self._ack(self.cap()))
        self.assertTrue(self.state.accept(
            channel_event(ch=6, cycle=1, util=self.sample())))
        self.assertIn(6, self.state.util_samples)
        # SAME epoch, capability revoked: metadata + samples invalidated
        self.assertTrue(self._ack({"available": False,
                                   "blocker": "unproven"}))
        self.assertFalse(self.state.utilization_available)
        self.assertEqual(self.state.util_samples, {})
        self.assertEqual(self.state.util_stage, {})

    def test_newer_empty_marker_publishes_empty_not_stale_stage(self) -> None:
        """marker1 LOST + zero channels of cycle2: the stale cycle1 stage
        must NEVER publish as cycle2 (flush only on exact equality)."""
        self.state.request(**{**CFG, "mode": 1})
        self.assertTrue(self._ack(self.cap(), mode=1))
        self.assertFalse(self.state.accept(
            channel_event(ch=6, cycle=1, util=self.sample())))
        self.assertEqual(len(self.state.util_stage), 1)
        # marker1 lost: zero channels of cycle2, marker2 arrives
        self.assertTrue(self.state.accept(cycle_event(cycle=2)))
        self.assertEqual(self.state.util_samples, {})   # not cycle1's data
        self.assertEqual(self.state.util_stage, {})
        # a later cycle's OWN marker publishes its exact staged subset
        self.assertFalse(self.state.accept(
            channel_event(ch=6, cycle=3, util=self.sample(busy=50))))
        self.assertTrue(self.state.accept(cycle_event(cycle=3)))
        self.assertEqual(list(self.state.util_samples), [6])
        self.assertEqual(self.state.util_samples[6]["busy"], 50)


if __name__ == "__main__":
    unittest.main()
