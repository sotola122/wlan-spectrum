"""Unit tests for display-only frequency smoothing."""

import unittest

import numpy as np

from wifi_spectrum.spectrum_smooth import smooth_frequency_bins


class SpectrumSmoothTests(unittest.TestCase):
    def test_nan_center_stays_nan(self) -> None:
        values = np.array([10.0, np.nan, 10.0], dtype=np.float32)
        out = smooth_frequency_bins(values, 3)
        self.assertTrue(np.isnan(out[1]))
        self.assertEqual(out[0], 10.0)
        self.assertEqual(out[2], 10.0)

    def test_disjoint_runs_do_not_mix_across_gap(self) -> None:
        """Width spans the gap but each run smooths only inside itself."""
        values = np.array([-20.0, np.nan, -80.0], dtype=np.float32)
        out = smooth_frequency_bins(values, 5)
        self.assertEqual(out[0], -20.0)
        self.assertTrue(np.isnan(out[1]))
        self.assertEqual(out[2], -80.0)

    def test_interior_spike_arithmetic_mean_in_run(self) -> None:
        values = np.zeros(9, dtype=np.float32)
        values[4] = 10.0
        out = smooth_frequency_bins(values, 5)
        self.assertEqual(out[0], 0.0)
        self.assertEqual(out[8], 0.0)
        self.assertAlmostEqual(out[4], 2.0)

    def test_run_endpoint_uses_partial_window_inside_run(self) -> None:
        values = np.array([10.0, 10.0, 10.0], dtype=np.float32)
        out = smooth_frequency_bins(values, 5)
        self.assertAlmostEqual(out[0], 10.0)
        self.assertAlmostEqual(out[1], 10.0)
        self.assertAlmostEqual(out[2], 10.0)


if __name__ == "__main__":
    unittest.main()
