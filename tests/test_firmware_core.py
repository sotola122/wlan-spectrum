"""Native core contract: compile monitor_core against the real sources, run it.

The fixture is built from the same monitor_core.c that firmware links, with
``cc -std=c11 -Wall -Wextra -Werror`` (plus an ASan/UBSan self-test run).
Wire scenarios emit real TLV bytes decoded by wifi_spectrum.tlv.TlvParser.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

import numpy as np

from wifi_spectrum.tlv import Status, TlvParser

ROOT = Path(__file__).resolve().parents[1]
CORE_SRC = ROOT / "firmware" / "main" / "monitor_core.c"
SPECTRUM_SRC = ROOT / "firmware" / "main" / "monitor_spectrum.c"
CAPTURE_SRC = ROOT / "firmware" / "main" / "monitor_capture.c"
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
CONFIG_HEX = "100d000101e8034000204e0000f00010ff596e4a"


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
           "-DMONITOR_CCA_CTRL_TEST",
           "-I", str(FAKE_SDK_DIR), "-I", str(ROOT / "firmware" / "main"),
           "-I", str(CJSON_DIR),
           str(CORE_SRC), str(SPECTRUM_SRC), str(LINK_SRC), str(RADIO_SRC),
           str(FAKE_SDK_DIR / "fake_sdk.c"), str(ADAPTER_FIXTURE_SRC),
           str(cjson_obj), "-pthread", "-lm", "-o", str(exe)]
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
            "mode=1 band=1 sweep_ms=1000 fft_size=64 sample_rate_khz=20000 "
            "channel_dwell_ms=240 cca_attempts=16")

    def test_config_every_split_byte_at_a_time_and_noise(self) -> None:
        # The fixture itself walks every split point, a byte-at-a-time feed,
        # two concatenated frames, invalid length, garbage-before-CONFIG, and
        # illegal field values keeping the previous config.
        _run(self.exe, "selftest", "config")

    def test_dwell_boundaries(self) -> None:
        # auto floor / auto sweep / no channels / cca floor (32 -> 160 ms) /
        # explicit override / explicit+cca competing (max wins)
        out = _run(self.exe, "dwell").decode().split()
        self.assertEqual(out, ["120", "500", "0", "160", "1500", "160"])

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
        # ONE v1 CONFIG: the ack echoes BOTH requested new fields and the
        # effective dwell (defaults: AUTO dwell, 16 CCA attempts).
        self.assertEqual(data["channel_dwell_ms"], 0)
        self.assertEqual(data["cca_attempts"], 16)
        self.assertEqual(data["dwell_ms"], 120)

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
               str(CORE_SRC), str(SPECTRUM_SRC), str(cjson_obj), "-lm",
               "-o", str(target)]
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
                if site == "1":
                    self.assertIn("site1 dwell-tick-schedule ok",
                                  proc.stdout)
                else:
                    self.assertIn("site2 backoff-no-ticks ok", proc.stdout)

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

    def test_capture_failure_propagates_channel_error(self) -> None:
        """Snapshot failure => channel_error spectrum_capture, NO 0x04 frame."""
        exe = OUT_DIR / "app_main_fixture"
        self._build(exe, ROOT / "firmware" / "main" / "app_main.c")
        proc = subprocess.run([str(exe), "3"], capture_output=True,
                              text=True, timeout=120)
        self.assertEqual(
            proc.returncode, 0,
            f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("capture-failure propagation ok", proc.stdout)


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
        sdk = os.environ.get("IDF_PATH")
        real = (Path(sdk) / "components/soc/esp32c5/include/soc/uart_pins.h"
                if sdk else None)
        if real is not None and real.exists():
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

    def test_radio_util_window(self) -> None:
        # per-valid-dwell armed one-shot: call-site value, exactly one arm,
        # strict validity (busy<=total, completion, endpoint), util object
        # keys, truthful gap when invalid, no arm on failed begin
        _run(self.exe, "radio-util")

    def test_radio_util_pooled_8slot(self) -> None:
        # CURRENT contract: 8 distributed windows pooled into samples /
        # attempted / busy / total / window_us_upper; strict count bounds;
        # NO version key anywhere (wifi-monitor/1 outer schema only).
        # The fixture prints TWO pooled JSONs: the constant 8-window dwell,
        # then a MIXED dwell (valid/invalid/valid -> samples2 attempted3).
        out = _run(self.exe, "radio-util-pooled").decode()
        self.assertIn("radio-util-pooled ok", out)
        events = [json.loads(ln) for ln in out.splitlines()
                  if ln.startswith("{")]
        self.assertEqual(len(events), 2, "expected 8-window + mixed JSON")

        event = events[0]
        self.assertEqual(event["event"], "channel")
        util = event["util"]
        self.assertNotIn("version", util)
        self.assertNotIn("version", event)
        self.assertEqual(util["source"], "c5_v6.0.3_phy_cca_cnt")
        self.assertEqual(util["confidence"], "experimental_sampled")
        self.assertEqual(util["samples"], 8)
        self.assertEqual(util["attempted"], 8)
        self.assertEqual(util["total"], 8 * 0x400)
        # constant fake trace: A saturates 0->0x400 in 13 reads, B +40 per
        # read (520/window), identical windows -> pooled busy 8 x 520
        self.assertEqual(util["busy"], 8 * 520)
        # pooled count bounds (host-frozen contract)
        self.assertGreaterEqual(util["samples"], 1)
        self.assertGreaterEqual(util["attempted"], util["samples"])
        self.assertLessEqual(util["attempted"], 8)
        self.assertGreaterEqual(util["total"], util["samples"])
        self.assertLessEqual(util["total"], util["samples"] * 0x07FFFFFF)
        self.assertLessEqual(util["busy"], util["total"])
        self.assertGreaterEqual(util["window_us_upper"], util["samples"])
        self.assertLessEqual(util["window_us_upper"],
                             5000 * util["samples"])

        # mixed dwell: valid/invalid/valid -> only valid windows pooled
        mixed = events[1]["util"]
        self.assertNotIn("version", mixed)
        self.assertEqual(mixed["samples"], 2)
        self.assertEqual(mixed["attempted"], 3)
        self.assertEqual(mixed["total"], 2 * 0x400)
        self.assertEqual(mixed["busy"], 520 + 520)
        self.assertGreaterEqual(mixed["samples"], 1)
        self.assertGreaterEqual(mixed["attempted"], mixed["samples"])
        self.assertLessEqual(mixed["attempted"], 8)
        self.assertLessEqual(mixed["busy"], mixed["total"])
        self.assertGreaterEqual(mixed["window_us_upper"], mixed["samples"])
        self.assertLessEqual(mixed["window_us_upper"],
                             5000 * mixed["samples"])

    def test_util_pooled_count_bound_32(self) -> None:
        # config range cca_attempts 1..32: the formatter must publish
        # samples/attempted up to 32 and omit the whole util object above
        # (truthful gap, never clamped).
        out = _run(self.exe, "util-bounds").decode()
        self.assertIn("util-bounds ok", out)

    def test_tx_task_frame_not_on_task_stack(self) -> None:
        # A 4103-byte MonitorTlvFrame local would exceed the 4096-byte TX
        # task stack (and nested wasting the app task's stack) before any
        # call; the frames must be persistent storage.
        _tx_task_stack_bytes()
        for func in ("tx_task", "monitor_link_submit_json"):
            usage = _func_stack_usage[func]
            self.assertLess(usage, 1024,
                            f"{func} own frame {usage} B — frame on stack?")


class SpectrumWireTests(unittest.TestCase):
    """monitor_spectrum.c/monitor_capture.c against the frozen handoff v2.1:
    golden frame, numpy tone oracle (independent FFT), honest rate/fft
    mapping, sentinel validation for partial/overrun captures."""

    exe: Path

    @classmethod
    def setUpClass(cls) -> None:
        cc = _require_cc()
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        cjson_obj = OUT_DIR / "cJSON.o"
        subprocess.run(
            [cc, "-std=c11", "-O1", "-I", str(CJSON_DIR), "-c",
             str(CJSON_SRC), "-o", str(cjson_obj)],
            check=True, capture_output=True, text=True, timeout=120)
        cls.exe = OUT_DIR / "spectrum_fixture"
        cmd = [cc] + CFLAGS + ["-I", str(FAKE_SDK_DIR),
                        str(ROOT / "tests" / "native" /
                            "monitor_spectrum_fixture.c"),
                        str(SPECTRUM_SRC), str(CAPTURE_SRC), str(CORE_SRC),
                        str(ROOT / "tests" / "native" / "fake_sdk" /
                            "fake_sdk.c"),
                        str(cjson_obj), "-lm", "-o", str(cls.exe)]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=120)
        if proc.returncode != 0:
            raise AssertionError(
                f"spectrum fixture compile failed:\n{proc.stdout}\n{proc.stderr}")

    @classmethod
    def _run_mode(cls, *args: str, stdin: bytes = b"") -> str:
        proc = subprocess.run([str(cls.exe), *args], input=stdin,
                              capture_output=True, timeout=120)
        if proc.returncode != 0:
            raise AssertionError(
                f"fixture {args} rc={proc.returncode}: "
                f"{proc.stdout!r} {proc.stderr!r}")
        return proc.stdout.decode().strip()

    def test_golden_frame_matches_parent_hex(self) -> None:
        golden = (
            "04280001000000000000000001000008000000e0cd2400204e00000000"
            "9cff38ffd4fe70fe0cfea8fd44fd9f7a8723")
        self.assertEqual(self._run_mode("golden"), golden)

    def test_effective_mapping_reports_actuals(self) -> None:
        for fft, rate, expected in (
            (64, 20000, "64 2 20000"),
            (128, 40000, "128 1 40000"),
            (100, 40000, "64 1 40000"),      # request adapts DOWN honestly
            (2048, 20000, "1024 2 20000"),
            (64, 12345, "64 255 0"),          # unknown stays unknown
        ):
            with self.subTest(fft=fft, rate=rate):
                self.assertEqual(
                    self._run_mode("map", str(fft), str(rate)), expected)

    def test_rejection_paths(self) -> None:
        self.assertEqual(self._run_mode("invalid"), "ok")

    def test_capture_sentinel_validation(self) -> None:
        # OK, partial(range), first-word-only, overrun, NULL — see fixture
        self.assertEqual(self._run_mode("validate"), "0 2 2 3 1")

    def test_bench_runs_and_reports(self) -> None:
        # DSP benchmark (timing only, no thresholds): production path for
        # both FFT sizes + the Hann cosf-vs-twiddle A/B, all parseable.
        out = self._run_mode("bench")
        self.assertIn("bench power_dbfs fft=64 frames=", out)
        self.assertIn("bench power_dbfs fft=1024 frames=", out)
        self.assertIn("bench hann cosf_ns=", out)
        self.assertIn("twiddle_ns=", out)

    @staticmethod
    def _tone_words(n: int, k0: int, amp: int) -> np.ndarray:
        nn = np.arange(n)
        i = np.rint(np.cos(2 * np.pi * k0 * nn / n) * amp)
        q = np.rint(np.sin(2 * np.pi * k0 * nn / n) * amp)
        i10 = np.clip(i, -512, 511).astype(np.int64) & 0x3FF
        q10 = np.clip(q, -512, 511).astype(np.int64) & 0x3FF
        return (i10 | (q10 << 10)).astype("<u4")

    @staticmethod
    def _reference_bins_noremove(words: np.ndarray) -> np.ndarray:
        """Pre-correction convention (kept ONLY to prove non-DC tones are
        unchanged by the mean removal)."""
        n = len(words)
        w = words.astype(np.uint32)
        i = (w & 0x3FF).astype(np.int64)
        i = np.where(i & 0x200, i - 0x400, i)
        q = ((w >> 10) & 0x3FF).astype(np.int64)
        q = np.where(q & 0x200, q - 0x400, q)
        x = (i + 1j * q) / 512.0
        nn = np.arange(n)
        hann = 0.5 * (1.0 - np.cos(2 * np.pi * nn / n))
        X = np.fft.fft(x * hann)
        W = hann.sum()
        P = np.abs(X) ** 2 / (W * W)
        dbfs = 10.0 * np.log10(np.maximum(P, 1.0e-30))
        idx = (np.arange(n) + n // 2) % n          # frozen fftshift order
        shifted = dbfs[idx]
        return np.clip(np.round(shifted * 100.0), -32768, 32767)

    @staticmethod
    def _reference_bins(words: np.ndarray) -> np.ndarray:
        n = len(words)
        w = words.astype(np.uint32)
        i = (w & 0x3FF).astype(np.int64)
        i = np.where(i & 0x200, i - 0x400, i)
        q = ((w >> 10) & 0x3FF).astype(np.int64)
        q = np.where(q & 0x200, q - 0x400, q)
        # Production contract: per-capture mean I/Q removed BEFORE Hann.
        # (f32 is exact here: integer sums / power-of-two sizes.)
        x = ((i - i.mean()) + 1j * (q - q.mean())) / 512.0
        nn = np.arange(n)
        hann = 0.5 * (1.0 - np.cos(2 * np.pi * nn / n))
        X = np.fft.fft(x * hann)
        W = hann.sum()
        P = np.abs(X) ** 2 / (W * W)
        dbfs = 10.0 * np.log10(np.maximum(P, 1.0e-30))
        idx = (np.arange(n) + n // 2) % n          # frozen fftshift order
        shifted = dbfs[idx]
        return np.clip(np.round(shifted * 100.0), -32768, 32767)

    def _fixture_bins(self, words: np.ndarray) -> np.ndarray:
        out = self._run_mode("bins", str(len(words)),
                             stdin=words.tobytes())
        return np.array([int(v) for v in out.split()], dtype=np.int64)

    def test_tones_match_numpy_reference(self) -> None:
        """Independent oracle: positive/negative/DC/half/noise vectors. Any
        twiddle/stride defect (e.g. one stride for all FFT stages) fails
        this before hardware.

        Comparison ranges: the tracked lobe (within 60 dB of the reference
        peak) must match the float64 reference within 30 centi-dB; the deep
        noise floor is float32-vs-float64 limited, so it is only bounded
        (never above the peak, never fabricated) there.
        """
        cases = [
            ("pos", 64, 5, 511),
            ("neg", 64, -7, 511),
            ("half", 64, 5, 255),
            ("dc", 64, 0, 200),
        ]
        peak_dbfs: dict[str, int] = {}
        for name, n, k0, amp in cases:
            with self.subTest(case=name):
                words = self._tone_words(n, k0, amp)
                got = self._fixture_bins(words)
                want = self._reference_bins(words)
                self.assertEqual(len(got), n)
                peak_want = int(np.max(want))
                mask = want >= peak_want - 6000
                diff = np.abs(got - want)[mask]
                self.assertLessEqual(
                    int(np.max(diff)), 30,
                    f"{name}: tracked lobe diverges: "
                    f"max={int(np.max(diff))} centi-dB")
                # No fabricated energy above the reference peak anywhere.
                self.assertLessEqual(int(np.max(got)), peak_want + 50,
                                     f"{name}: bin above reference peak")
                if name != "half" and name != "dc":
                    expect_k = (k0 - n // 2) % n
                    self.assertEqual(int(np.argmax(got)), expect_k,
                                     f"{name}: fftshift peak misplaced")
                peak_dbfs[name] = int(np.max(got))
                if name == "dc":
                    # Mean removal collapses a pure DC capture to the
                    # POWER_FLOOR: every bin at -300 dB, never 0 dBFS.
                    self.assertLessEqual(int(np.max(got)), -29900,
                                         f"dc: not at floor: {int(np.max(got))}")
                    self.assertGreater(int(np.min(got)), -32768,
                                       "dc: floor underflow")
                    continue
                # Peak magnitude itself must hit the float64 reference.
                self.assertLessEqual(
                    abs(int(np.max(got)) - int(np.max(want))), 3,
                    f"{name}: peak power off")
                # Non-DC tones are UNCHANGED vs the pre-correction
                # convention: the removal is a no-op away from centre.
                noremove = self._reference_bins_noremove(words)
                unchanged = np.abs(got - noremove)[mask]
                self.assertLessEqual(
                    int(np.max(unchanged)), 30,
                    f"{name}: non-DC tone changed by DC correction: "
                    f"{int(np.max(unchanged))} centi-dB")
        # complex-tone amplitude law: -6.02 dB at half amplitude
        delta = peak_dbfs["half"] - peak_dbfs["pos"]
        self.assertLessEqual(abs(delta - (-602)), 8,
                             f"half-vs-full peak delta {delta} centi-dB")

    def test_tone_with_dc_offset_collapses_center_only(self) -> None:
        """bin-centre tone + DC offset: mean removal collapses the offset
        out of the centre bin while the tone lobe stays put."""
        n, k0, amp, dc = 64, 5, 511, 150
        nn = np.arange(n)
        i = np.rint(np.cos(2 * np.pi * k0 * nn / n) * amp) + dc
        q = np.rint(np.sin(2 * np.pi * k0 * nn / n) * amp)
        i = np.clip(i, -512, 511).astype(np.int64) & 0x3FF
        q = np.clip(q, -512, 511).astype(np.int64) & 0x3FF
        words = (i | (q << 10)).astype("<u4")

        got = self._fixture_bins(words)
        want = self._reference_bins(words)
        no = self._reference_bins_noremove(words)
        peak_want = int(np.max(want))
        mask = want >= peak_want - 6000
        diff = np.abs(got - want)[mask]
        self.assertLessEqual(int(np.max(diff)), 30,
                             f"tone+DC lobe diverges: {int(np.max(diff))}")
        # centre bin: the offset collapses by >= 30 dB vs the old curve
        centre = n // 2
        self.assertLessEqual(int(got[centre]) - int(no[centre]), -3000,
                             "tone+DC: centre not collapsed")
        # off-centre lobe is untouched by the correction
        lobe = int(np.argmax(want))
        self.assertLessEqual(abs(int(got[lobe]) - int(no[lobe])), 30,
                             "tone+DC: lobe changed")

    def test_noise_is_deterministic_and_finite(self) -> None:
        rng = np.random.default_rng(7)
        words = rng.integers(0, 1 << 20, 128, dtype="<u4")
        first = self._fixture_bins(words)
        second = self._fixture_bins(words)
        np.testing.assert_array_equal(first, second)
        self.assertTrue(np.all(first >= -32768) and np.all(first <= 32767))
        want = self._reference_bins(words)
        peak_want = int(np.max(want))
        mask = want >= peak_want - 6000
        diff = np.abs(first - want)[mask]
        self.assertLessEqual(int(np.max(diff)), 30,
                             f"noise tracked range diverges: "
                             f"max={int(np.max(diff))}")
        self.assertLessEqual(int(np.max(first)), peak_want + 50)


if __name__ == "__main__":
    unittest.main()
