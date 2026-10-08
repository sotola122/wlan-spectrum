"""Public-file privacy checks and explicit device selection, without hardware."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts.check_public_files import findings

ROOT = Path(__file__).resolve().parents[1]


class DeviceSelectionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("make"), "GNU Make required")
    def test_device_commands_require_explicit_selection(self) -> None:
        env = {k: v for k, v in os.environ.items()
               if k not in {"PORT", "JTAG_SERIAL", "MAKEFLAGS", "MFLAGS"}}
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
            # The historical Makefile invokes uv literally, not $(UV).
            # Shadow it too: a failing guard test must never open hardware.
            stub = Path(directory) / "uv"
            stub.write_text("#!/bin/sh\nexit 91\n")
            stub.chmod(0o700)
            env["PATH"] = directory + os.pathsep + env["PATH"]
            for target, setting in (("flash-uart", "PORT"), ("smoke", "PORT"),
                                    ("jtag-probe", "JTAG_SERIAL"),
                                    ("flash-jtag", "JTAG_SERIAL")):
                with self.subTest(target=target):
                    proc = subprocess.run(
                        ["make", target, "EIM=false", "UV=false"], cwd=ROOT,
                        env=env, capture_output=True, text=True, timeout=10,
                    )
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertIn(f"Set {setting} explicitly", proc.stderr)

    @unittest.skipUnless(shutil.which("make"), "GNU Make required")
    def test_explicit_selection_reaches_command(self) -> None:
        for target, setting, value in (
            ("flash-uart", "PORT", "COM_TEST"),
            ("smoke", "PORT", "COM_TEST"),
            ("jtag-probe", "JTAG_SERIAL", "TEST_ADAPTER"),
            ("flash-jtag", "JTAG_SERIAL", "TEST_ADAPTER"),
        ):
            with self.subTest(target=target):
                proc = subprocess.run(
                    ["make", "-n", target, f"{setting}={value}"], cwd=ROOT,
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn(value, proc.stdout)


class PrivacyCheckTests(unittest.TestCase):
    def test_private_identifiers_are_rejected_without_echoing_values(self) -> None:
        for value in ("/" + "home/test-user/sdk/", "/dev/serial/" + "by-id/usb-TEST_123",
                      ":".join(["12", "34", "56", "78", "9a", "bc"])):
            with self.subTest(kind=value.split("/")[0]):
                result = findings("example.txt", value.encode())
                self.assertTrue(result)
                self.assertNotIn(value, "\n".join(result))

    def test_generic_examples_and_synthetic_fixture_are_allowed(self) -> None:
        self.assertEqual(findings("example.txt", b"/dev/ttyUSB0 COM5 ${IDF_PATH} 00:11:22:33:44:55"), [])

    def test_private_artifacts_rejected_even_if_binary(self) -> None:
        self.assertTrue(findings("captures/trace.raw.bin", b"\0"))
        self.assertTrue(findings("devices.reg", b"\0"))

    def test_private_root_paths_need_no_trailing_separator(self) -> None:
        for value in ("/" + "home/test-user", "C:" + "\\Users\\test-user",
                      "/" + "data_1"):
            with self.subTest(value=value):
                self.assertTrue(findings("example.txt", value.encode()))

    def test_hyphenated_mac_is_rejected(self) -> None:
        value = "-".join(["12", "34", "56", "78", "9a", "bc"])
        self.assertTrue(findings("example.txt", value.encode()))

    def test_ignored_capture_paths_are_rejected_if_force_added(self) -> None:
        for path in ("shots-win/live.png", "trace.RAW.BIN", "devices.REG"):
            with self.subTest(path=path):
                self.assertTrue(findings(path, b"\0"))

    def test_public_files(self) -> None:
        proc = subprocess.run(
            ["python", "scripts/check_public_files.py"], cwd=ROOT,
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
