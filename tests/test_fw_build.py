"""Build-adapter contract tests: exact backend commands, no side effects."""

from __future__ import annotations

import importlib.util
import subprocess
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("fw_build_adapter", _ROOT / "scripts" / "fw.py")
assert _spec is not None and _spec.loader is not None
_fw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fw)
IMAGE, VERSION, build_shell, command = _fw.IMAGE, _fw.VERSION, _fw.build_shell, _fw.command

ROOT = _ROOT


class BuildCommandTests(unittest.TestCase):
    def test_eim_pins_version_target_and_build_dir(self) -> None:
        cmd = command("eim", ROOT)
        self.assertEqual(cmd[:2], ["eim", "run"])
        self.assertIn(VERSION, cmd[2])
        self.assertIn("IDF_TARGET=esp32c5", cmd[2])
        self.assertIn("build/eim", cmd[2])
        self.assertEqual(cmd[3], VERSION)
        self.assertIn('idf.py --version', cmd[2])

    def test_backends_use_separate_build_dirs(self) -> None:
        dirs = {b: command(b, ROOT) for b in ("eim", "podman", "wslc")}
        joined = {b: " ".join(c) for b, c in dirs.items()}
        self.assertIn("build/eim", joined["eim"])
        self.assertIn("build/podman", joined["podman"])
        self.assertIn("build/wslc", joined["wslc"])
        self.assertEqual(len({joined[b].split("build/")[1].split()[0]
                              for b in joined}), 3)

    def test_podman_uses_fully_qualified_v603_image(self) -> None:
        cmd = command("podman", ROOT)
        self.assertEqual(cmd[0], "podman")
        self.assertEqual(IMAGE, "docker.io/espressif/idf:v6.0.3")
        self.assertIn(IMAGE, cmd)
        self.assertIn("build/podman", cmd[-1])
        self.assertNotIn("wslc.exe", cmd)

    def test_wslc_invokes_windows_cli_with_space_safe_mount(self) -> None:
        win_root = Path("C:/Program Files/wlan spectrum")
        cmd = command("wslc", win_root)
        self.assertEqual(cmd[0], "wslc.exe")
        self.assertIn(IMAGE, cmd)
        mount = cmd[cmd.index("-v") + 1]
        self.assertEqual(mount, f"{win_root}:/work")   # one argv element
        self.assertIn("-w", cmd)
        self.assertEqual(cmd[cmd.index("-w") + 1], "/work")
        self.assertIn("build/wslc", cmd[-1])

    def test_unknown_backend_rejected(self) -> None:
        with self.assertRaises(ValueError):
            command("docker", ROOT)

    def test_shell_snippet_asserts_version_before_build(self) -> None:
        shell = build_shell("eim")
        self.assertTrue(shell.startswith('test "$(idf.py --version)"'))
        self.assertIn('"ESP-IDF v6.0.3"', shell)
        self.assertIn("-C firmware", shell)
        self.assertIn("-B build/eim", shell)
        self.assertIn("build", shell.split("&&")[1])

    def test_dry_run_prints_without_executing(self) -> None:
        proc = subprocess.run(
            ["python", "scripts/fw.py", "eim", "--dry-run"],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("eim run", proc.stdout)
        self.assertIn(VERSION, proc.stdout)


if __name__ == "__main__":
    unittest.main()
