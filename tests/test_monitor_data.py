"""MonitorState / decode_channel behavior: epoch, ack, rates, bounds."""

from __future__ import annotations

import unittest

from wifi_spectrum.monitor_data import (
    MAX_TRACKED_APS,
    ChannelObservation,
    MonitorState,
    decode_channel,
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


if __name__ == "__main__":
    unittest.main()
