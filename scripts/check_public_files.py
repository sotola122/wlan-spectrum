"""Reject machine-specific identifiers in public source; never print values.

Run without arguments before publication, or with --staged in a commit hook.
This is a targeted privacy check, not a credential scanner or image/OCR audit.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
RULES = (
    ("personal home path", re.compile(r"/(?:home|Users)/[A-Za-z0-9_.-]+")),
    ("Windows home path", re.compile(r"[A-Za-z]:[\\/]Users[\\/][A-Za-z0-9_.-]+")),
    ("host workspace path", re.compile(r"/data_\d+\b")),
    ("USB device identifier", re.compile(r"/dev/serial/(?:by-id|by-path)/[A-Za-z0-9_:.\-]+")),
    ("Windows USB instance", re.compile(r"USB[\\#]+VID_[0-9A-Fa-f]{4}.*PID_[0-9A-Fa-f]{4}[\\#]+[A-Za-z0-9&]+", re.I)),
    ("private IPv4 address", re.compile(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))(?:\.\d{1,3}){2,3}\b")),
)
MAC = re.compile(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b")
# Public synthetic fixtures only; real identifiers must not be added here.
SYNTHETIC_MACS = {"00:11:22:33:44:55", "aa:bb:cc:dd:ee:ff"}


def findings(path: str, data: bytes) -> list[str]:
    parts = Path(path).parts
    if (any(p in {".hermes", ".local", "captures", "shots-win"} for p in parts)
            or Path(path).suffix.lower() == ".reg"
            or path.lower().endswith(".raw.bin")
            or (Path(path).name.startswith(".env")
                and Path(path).name != ".env.example")):
        return [f"{path}: private artifact must not be tracked"]
    if b"\0" in data:
        return []  # Images/binary assets require a separate manual review.
    text = data.decode("utf-8", errors="replace")
    result = []
    for number, line in enumerate(text.splitlines(), 1):
        for label, pattern in RULES:
            if pattern.search(line):
                result.append(f"{path}:{number}: {label}")
        if any(m.group().lower().replace("-", ":") not in SYNTHETIC_MACS
               for m in MAC.finditer(line)):
            result.append(f"{path}:{number}: hardware MAC/serial")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="check index, not working copies")
    args = parser.parse_args()
    cmd = ["git", "ls-files", "-z", "--cached"]
    if not args.staged:
        cmd += ["--others", "--exclude-standard"]
    paths = subprocess.check_output(cmd, cwd=ROOT).decode().split("\0")
    problems = []
    for path in sorted(set(filter(None, paths))):
        if args.staged:
            data = subprocess.check_output(["git", "show", f":{path}"], cwd=ROOT)
        else:
            source = ROOT / path
            if not source.exists():
                continue
            if source.is_symlink():
                problems.append(f"{path}: review symlink before publication")
                continue
            data = source.read_bytes()
        problems.extend(findings(path, data))
    for problem in problems:
        print(problem)
    print(f"Public-file privacy check: {len(problems)} finding(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
