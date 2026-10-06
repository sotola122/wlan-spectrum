"""ESP32-C5 firmware build command adapter.

Three backends, one pinned SDK: EIM on the host, the official espressif/idf
container under Podman, and Microsoft's Windows Linux-container CLI (wslc).
Every backend builds the same `firmware/` tree into `build/<backend>/`.

Dry-run prints POSIX-quoted argv for humans; subprocess always gets a plain
argument vector, so Windows shells never see the quoted string.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
from pathlib import Path

VERSION = "v6.0.3"
IMAGE = f"docker.io/espressif/idf:{VERSION}"


def build_shell(backend: str) -> str:
    """Version-gated build, run from the repository root.

    idf.py does not chdir into -C, so -B is resolved against the current
    working directory (repo root for all three backends).
    """
    return (
        'test "$(idf.py --version)" = "ESP-IDF v6.0.3" && '
        f'idf.py -C firmware -B build/{backend} '
        '-D IDF_TARGET=esp32c5 build'
    )


def command(backend: str, root: Path) -> list[str]:
    if backend == "eim":
        return ["eim", "run", build_shell(backend), VERSION]
    if backend not in {"podman", "wslc"}:
        raise ValueError(backend)
    engine = "podman" if backend == "podman" else "wslc.exe"
    args = [engine, "run", "--rm"]
    if backend == "podman":
        args += ["--userns=keep-id"]
    args += ["-v", f"{root}:/work", "-w", "/work", IMAGE,
             "bash", "-lc", build_shell(backend)]
    return args


def main() -> None:
    parser = argparse.ArgumentParser(description="Build firmware for one backend")
    parser.add_argument("backend", choices=("eim", "podman", "wslc"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    cmd = command(args.backend, root)
    if args.dry_run:
        print(shlex.join(cmd))
    else:
        raise SystemExit(subprocess.run(cmd, cwd=root, check=False).returncode)


if __name__ == "__main__":
    main()
