"""Native core contract: compile monitor_core against the real sources, run it.

The fixture is built from the same monitor_core.c that firmware links, with
``cc -std=c11 -Wall -Wextra -Werror`` (plus an ASan/UBSan self-test run).
Wire scenarios emit real TLV bytes decoded by wifi_spectrum.tlv.TlvParser.
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

from wifi_spectrum.tlv import Status, TlvParser

ROOT = Path(__file__).resolve().parents[1]
CORE_SRC = ROOT / "firmware" / "main" / "monitor_core.c"
LINK_SRC = ROOT / "firmware" / "main" / "monitor_link.c"
RADIO_SRC = ROOT / "firmware" / "main" / "monitor_radio.c"
FAKE_SDK_DIR = ROOT / "tests" / "native" / "fake_sdk"
ADAPTER_FIXTURE_SRC = ROOT / "tests" / "native" / "monitor_adapter_fixture.c"
FIXTURE_SRC = ROOT / "tests" / "native" / "monitor_fixture.c"
CJSON_DIR = ROOT / "firmware" / "managed_components" / "espressif__cjson" / "cJSON"
CJSON_SRC = CJSON_DIR / "cJSON.c"
OUT_DIR = ROOT / "build" / "host-tests"
EXE = OUT_DIR / "monitor_fixture"

CC = shutil.which("cc") or shutil.which("gcc")
_func_stack_usage: dict[str, int] = {}
CFLAGS = ["-std=c11", "-Wall", "-Wextra", "-Werror", "-O1",
          "-I", str(ROOT / "firmware" / "main"), "-I", str(CJSON_DIR)]

# Literal documented CONFIG: Band Sweep, 5 GHz, 1000 ms, FFT 64, 20 MS/s,
# with the spec-published CRC32 bytes 95 56 4e 1d (docs/tlv-protocol.md).
CONFIG_HEX = "100a000101e8034000204e000095564e1d"


def _require_cc() -> str:
    if CC is None:
        raise AssertionError("no host C compiler")
    return CC


def _compile(sanitized: bool = False) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if not CJSON_SRC.exists():
        raise AssertionError(
            f"{CJSON_SRC} missing: run 'make build' (or idf.py reconfigure) "
            "so the component manager fetches espressif/cjson")
    tag = "_san" if sanitized else ""
    # The real espressif/cjson implementation, compiled separately without
    # -Werror (vendor code keeps its own warning profile).
    obj = OUT_DIR / f"cJSON{tag}.o"
    cc = _require_cc()
    cjson_cmd = [cc, "-std=c11", "-O1"]
    if sanitized:
        cjson_cmd += ["-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
    cjson_cmd += ["-I", str(CJSON_DIR), "-c", str(CJSON_SRC), "-o", str(obj)]
    proc = subprocess.run(cjson_cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(f"cJSON compile failed:\n{proc.stdout}\n{proc.stderr}")
    exe = OUT_DIR / f"monitor_fixture{tag}"
    cc = _require_cc()
    cmd = [cc, *CFLAGS]
    if sanitized:
        cmd += ["-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
    cmd += [str(CORE_SRC), str(FIXTURE_SRC), str(obj), "-o", str(exe)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(f"compile failed:\n{proc.stdout}\n{proc.stderr}")
    return exe


def _compile_adapters() -> Path:
    """Compile the REAL adapter sources against the fake SDK seam."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if not CJSON_SRC.exists():
        raise AssertionError(f"{CJSON_SRC} missing: run 'make build'")
    cc = _require_cc()
    cjson_obj = OUT_DIR / "cJSON_adapters.o"
    proc = subprocess.run(
        [cc, "-std=c11", "-O1", "-I", str(CJSON_DIR), "-c", str(CJSON_SRC),
         "-o", str(cjson_obj)],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(f"cJSON compile failed:\n{proc.stdout}\n{proc.stderr}")
    exe = OUT_DIR / "monitor_adapter_fixture"
    cmd = [cc, "-std=c11", "-Wall", "-Wextra", "-Werror", "-O1",
           "-I", str(FAKE_SDK_DIR), "-I", str(ROOT / "firmware" / "main"),
           "-I", str(CJSON_DIR),
           str(CORE_SRC), str(LINK_SRC), str(RADIO_SRC),
           str(FAKE_SDK_DIR / "fake_sdk.c"), str(ADAPTER_FIXTURE_SRC),
           str(cjson_obj), "-pthread", "-o", str(exe)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(
            f"adapter seam compile failed:\n{proc.stdout}\n{proc.stderr}")
    return exe


def _tx_task_stack_bytes() -> int:
    """Compile monitor_link.c with -fstack-usage and read a function's own
    frame size — the regression for 'frame lives on the task stack'."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    obj = OUT_DIR / "monitor_link_su.o"
    cmd = [_require_cc(), "-std=c11", "-Wall", "-Wextra", "-Werror", "-O1",
           "-fstack-usage", "-I", str(FAKE_SDK_DIR),
           "-I", str(ROOT / "firmware" / "main"), "-I", str(CJSON_DIR),
           "-c", str(LINK_SRC), "-o", str(obj)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(f"su compile failed:\n{proc.stdout}\n{proc.stderr}")
    su_path = obj.with_suffix(".su")
    # .su lines: <file>:<line>:<col>\t<function>\t<bytes>\t<qualifier>
    for line in su_path.read_text().splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            name = parts[0].rsplit(":", 1)[-1]
            if name in {"tx_task", "monitor_link_submit_json"}:
                _func_stack_usage[name] = int(parts[1])
    missing = {"tx_task", "monitor_link_submit_json"} - set(_func_stack_usage)
    if missing:
        raise AssertionError(
            f"missing from {su_path}: {missing}:\n{su_path.read_text()}")
    return 0


def _run(exe: Path, *args: str) -> bytes:
    proc = subprocess.run([str(exe), *args], capture_output=True, timeout=60)
    return proc.stdout if proc.returncode == 0 else (_ for _ in ()).throw(
        AssertionError(f"{exe.name} {' '.join(args)} failed rc={proc.returncode}\n"
                       f"{proc.stderr.decode(errors='replace')}"))


def _parse_wire(data: bytes) -> tuple[list, int]:
    parser = TlvParser()
    messages: list = []
    for i in range(len(data)):
        messages.extend(parser.feed(data[i:i + 1]))
    return messages, parser.errors


class FirmwareCoreTests(unittest.TestCase):
    """Compiled native fixture runs; real TLV codec decodes its output."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.exe = _compile()

    # ------------------------------------------------------------ config
    def test_literal_config_fields_parse(self) -> None:
        out = _run(self.exe, "config-parse", CONFIG_HEX).decode()
        self.assertEqual(
            out.strip(),
            "mode=1 band=1 sweep_ms=1000 fft_size=64 sample_rate_khz=20000")

    def test_config_every_split_byte_at_a_time_and_noise(self) -> None:
        # The fixture itself walks every split point, a byte-at-a-time feed,
        # two concatenated frames, invalid length, garbage-before-CONFIG, and
        # illegal field values keeping the previous config.
        _run(self.exe, "selftest", "config")

    def test_dwell_boundaries(self) -> None:
        out = _run(self.exe, "dwell").decode().split()
        self.assertEqual(out, ["120", "500", "0"])

    # ------------------------------------------------------- ap parsing
    def test_ap_parsing_cases(self) -> None:
        _run(self.exe, "selftest", "ap")

    def test_length_sweep_fuzz(self) -> None:
        _run(self.exe, "selftest", "fuzz")

    # ---------------------------------------------------- observation
    def test_observation_aggregation(self) -> None:
        _run(self.exe, "selftest", "observation")

    def test_run_epoch_cycle_semantics(self) -> None:
        # config A, one partial cycle, config B, then a full cycle:
        # B starts a new epoch, no completion for the partial A cycle,
        # identical B does not increment epoch (all inside the fixture).
        _run(self.exe, "selftest", "run")

    # ---------------------------------------------------------- wire
    def test_crc_known_answers(self) -> None:
        _run(self.exe, "selftest", "crc")

    def test_cjson_real_implementation_and_memory_bound(self) -> None:
        out = _run(self.exe, "selftest", "cjson-memory").decode()
        peaks = dict(line.split("=") for line in out.split())
        for key in ("cjson_peak_bytes", "cjson_cfg_peak_bytes"):
            peak = int(peaks[key])
            self.assertGreater(peak, 0)
            self.assertLessEqual(peak, 16384,
                                 f"{key}={peak} exceeds documented bound")

    def test_empty_observation_is_not_fake_power(self) -> None:
        wire = _run(self.exe, "empty-channel")
        messages, errors = _parse_wire(wire)
        self.assertEqual(errors, 0)
        self.assertEqual(len(messages), 1)
        self.assertIsInstance(messages[0], Status)
        data = messages[0].data
        self.assertEqual(data["schema"], "wifi-monitor/1")
        self.assertEqual(data["event"], "channel")
        self.assertEqual(data["packets"], 0)
        self.assertIsNone(data["peak_rssi_dbm"])
        self.assertEqual(data["aps"], [])

    def test_full_channel_wire_roundtrip(self) -> None:
        wire = _run(self.exe, "full-channel")
        messages, errors = _parse_wire(wire)
        self.assertEqual(errors, 0)
        self.assertEqual(len(messages), 1)
        data = messages[0].data
        self.assertEqual(data["schema"], "wifi-monitor/1")
        self.assertEqual(data["event"], "channel")
        self.assertEqual(data["packets"], 24)
        self.assertEqual(data["peak_rssi_dbm"], -48)
        self.assertEqual(len(data["aps"]), 8)
        first = data["aps"][0]
        self.assertEqual(first["bssid"], "001122334455")
        self.assertEqual(first["ssid_hex"], "74657374")
        self.assertEqual(first["primary_ch"], 6)
        # 32-byte non-UTF8 SSID survives as hex
        long_ssid = max(data["aps"], key=lambda ap: len(ap["ssid_hex"]))
        self.assertEqual(len(long_ssid["ssid_hex"]), 64)
        bytes.fromhex(long_ssid["ssid_hex"])     # must be valid hex
        self.assertGreaterEqual(data["observed_ms"], 120)

    def test_config_event_wire(self) -> None:
        wire = _run(self.exe, "config-event")
        messages, errors = _parse_wire(wire)
        self.assertEqual(errors, 0)
        data = messages[0].data
        self.assertEqual(data["event"], "config")
        self.assertEqual(data["country"], "JP")
        self.assertEqual(data["chip"], "ESP32-C5")
        self.assertEqual(data["idf"], "v6.0.3")
        self.assertEqual(data["dwell_ms"], 120)
        self.assertEqual(data["channels"], list(range(1, 14)))
        self.assertIs(data["spectrum"], False)
        self.assertIs(data["cca"], False)
        self.assertIs(data["fft_supported"], False)
        self.assertEqual(data["mode"], 0)
        self.assertEqual(data["band"], 0)
        self.assertEqual(data["sweep_ms"], 1000)
        self.assertEqual(data["fft_size"], 64)
        self.assertEqual(data["sample_rate_khz"], 20000)

    def test_cycle_and_error_events_wire(self) -> None:
        for mode, expect in (
            ("cycle-event", {"event": "cycle", "epoch": 1, "cycle": 1}),
            ("error-event", {"event": "error", "code": "invalid_config"}),
            ("channel-error-event", {"event": "channel_error", "ch": 144,
                                     "code": "ESP_ERR_INVALID_ARG"}),
        ):
            with self.subTest(mode):
                wire = _run(self.exe, mode)
                messages, errors = _parse_wire(wire)
                self.assertEqual(errors, 0)
                self.assertEqual(len(messages), 1)
                for key, value in expect.items():
                    self.assertEqual(messages[0].data[key], value)

    def test_serialization_never_emits_truncated_body(self) -> None:
        # Fixture exits non-zero (and writes nothing) if any formatter
        # overflows its bounded buffer.
        _run(self.exe, "selftest", "serialize")

    def test_sanitized_selftest(self) -> None:
        if CC is None:
            self.skipTest("no host C compiler")
        exe = _compile(sanitized=True)
        for case in ("config", "ap", "observation", "fuzz", "serialize",
                     "crc", "run"):
            with self.subTest(case):
                _run(exe, "selftest", case)


class AppMainApplyAckTests(unittest.TestCase):
    """The REAL app_main() loop, both MONITOR_BOUNDARY_APPLY sites:
    the new-epoch config ack must precede every new-epoch measurement
    (smoke's missing-first-channels regression)."""

    @staticmethod
    def _build(target: Path, app_main_source: Path) -> None:
        cc = _require_cc()
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        if not CJSON_SRC.exists():
            raise AssertionError(f"{CJSON_SRC} missing: run 'make build'")
        cjson_obj = OUT_DIR / "cJSON_appmain.o"
        proc = subprocess.run(
            [cc, "-std=c11", "-O1", "-I", str(CJSON_DIR), "-c", str(CJSON_SRC),
             "-o", str(cjson_obj)], capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise AssertionError(f"cJSON compile failed:\n{proc.stderr}")
        cmd = [cc, "-std=c11", "-Wall", "-Wextra", "-Werror", "-O1",
               "-I", str(FAKE_SDK_DIR), "-I", str(ROOT / "firmware" / "main"),
               "-I", str(CJSON_DIR),
               f'-DAPP_MAIN_PATH="{app_main_source}"',
               str(ROOT / "tests" / "native" / "app_main_fixture.c"),
               str(CORE_SRC), str(cjson_obj), "-o", str(target)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise AssertionError(
                f"app_main fixture compile failed:\n{proc.stdout}\n{proc.stderr}")

    def test_apply_sites_ack_before_new_epoch_measurements(self) -> None:
        exe = OUT_DIR / "app_main_fixture"
        self._build(exe, ROOT / "firmware" / "main" / "app_main.c")
        for site in ("1", "2"):
            with self.subTest(site=site):
                proc = subprocess.run([str(exe), site], capture_output=True,
                                      text=True, timeout=120)
                self.assertEqual(
                    proc.returncode, 0,
                    f"apply site {site}: rc={proc.returncode}\n"
                    f"{proc.stdout}\n{proc.stderr}")

    def test_negative_control_detects_missing_apply_ack(self) -> None:
        # Strip BOTH apply-site acks from a scratch copy; the same fixture
        # must then FAIL — proving the test exercises the real defect.
        src = (ROOT / "firmware" / "main" / "app_main.c").read_text()
        patched = src.replace(
            "send_config_event();\n                cycle_discarded = true;",
            "cycle_discarded = true;").replace(
            "send_config_event();    /* ack new epoch first */\n"
            "                        cycle_discarded = true;",
            "cycle_discarded = true;")
        self.assertNotEqual(src, patched, "apply-site acks not found in source")
        self.assertNotIn("ack new epoch first", patched)
        scratch = OUT_DIR / "app_main_no_ack.c"
        scratch.write_text(patched)
        exe = OUT_DIR / "app_main_fixture_no_ack"
        self._build(exe, scratch)
        for site in ("1", "2"):
            with self.subTest(site=site):
                proc = subprocess.run([str(exe), site], capture_output=True,
                                      text=True, timeout=120)
                self.assertNotEqual(
                    proc.returncode, 0,
                    f"site {site}: defect NOT detected (rc=0)")
                self.assertIn("epoch-2 measurement before ack",
                              proc.stderr + proc.stdout)


class AdapterSeamTests(unittest.TestCase):
    """The REAL monitor_link.c / monitor_radio.c against the fake SDK.

    Covers the concrete review findings at the adapter/config seam, not the
    portable core: TX frame ownership/partial writes, bounded poll budget,
    whole-frame queue drops, beacon copy lengths 255/256/oversized, packet
    gating, and queue-drop/packet-count independence.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.exe = _compile_adapters()

    def test_tx_partial_write_resumes_whole_frame(self) -> None:
        _run(self.exe, "tx-partial")

    def test_tx_queue_full_drops_whole_frame_only(self) -> None:
        _run(self.exe, "tx-queue-full")

    def test_poll_budget_is_bounded_per_call(self) -> None:
        _run(self.exe, "poll-budget")

    def test_uart_transport_config(self) -> None:
        # UART0 @921600 on SoC-default pins; fake constants must mirror the
        # real IDF C5 header when it exists (no guessed pins).
        real = Path("<LOCAL_HOME>/.espressif/v6.0.3/esp-idf/components/soc/"
                    "esp32c5/include/soc/uart_pins.h")
        if real.exists():
            text = real.read_text()
            self.assertIn("#define U0TXD_GPIO_NUM 11", text)
            self.assertIn("#define U0RXD_GPIO_NUM 12", text)
        _run(self.exe, "uart-config")

    def test_invalid_config_error_rate_limited(self) -> None:
        _run(self.exe, "invalid-rate")

    def test_tx_write_starvation_deadline(self) -> None:
        # firmware backpressure: never-accepting FIFO -> 30 s deadline fires,
        # whole frame counted dropped, wire clean, TX task still alive.
        _run(self.exe, "tx-starvation")

    def test_radio_sighting_lengths_255_256_oversized(self) -> None:
        _run(self.exe, "radio-sightings")

    def test_radio_gating_and_queue_drop_independence(self) -> None:
        _run(self.exe, "radio-gating")

    def test_tx_task_frame_not_on_task_stack(self) -> None:
        # A 4103-byte MonitorTlvFrame local would exceed the 4096-byte TX
        # task stack (and nested wasting the app task's stack) before any
        # call; the frames must be persistent storage.
        _tx_task_stack_bytes()
        for func in ("tx_task", "monitor_link_submit_json"):
            usage = _func_stack_usage[func]
            self.assertLess(usage, 1024,
                            f"{func} own frame {usage} B — frame on stack?")


if __name__ == "__main__":
    unittest.main()
