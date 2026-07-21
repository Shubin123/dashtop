"""Optional NVIDIA GPU sampling via nvidia-smi — no extra dependencies.

nvidia-smi ships with the NVIDIA driver on Windows and Linux.  When it is on
PATH, dashtop reports per-GPU utilization, VRAM, temperature, power, fan and
clocks; when it is not, GPU stats are simply omitted from snapshots.

One subprocess call per query (~100–300 ms on Windows) — the Sampler rate-
limits calls, see GPU_MIN_INTERVAL in server.py.
"""

import csv
import shutil
import subprocess
import sys

# nvidia-smi query fields → snapshot keys, in query order.
FIELDS = [
    ("name", "name"),
    ("utilization.gpu", "util"),
    ("utilization.memory", "mem_util"),
    ("memory.total", "vram_total"),      # MiB in, bytes out
    ("memory.used", "vram_used"),        # MiB in, bytes out
    ("temperature.gpu", "temp"),         # °C
    ("power.draw", "power_w"),           # W
    ("power.limit", "power_limit_w"),    # W
    ("fan.speed", "fan"),                # %
    ("clocks.sm", "clock_mhz"),          # MHz
    ("clocks.mem", "mem_clock_mhz"),     # MHz
]

_MIB = 1024 * 1024


def _to_number(text):
    """'38' -> 38, '149.32' -> 149.32, 'N/A' / '' / junk -> None."""
    text = text.strip()
    if not text or text.upper() == "N/A":
        return None
    try:
        f = float(text)
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def parse_query_csv(text):
    """Parse `nvidia-smi --query-gpu=... --format=csv,noheader,nounits` output
    into a list of per-GPU dicts.  Pure function — unit-tested without hardware.

    Numeric fields that the driver reports as N/A (fan on passively-cooled
    cards, power on some laptops) come out as None; VRAM is converted to bytes
    to match the byte units used everywhere else in the snapshot.
    """
    gpus = []
    for row in csv.reader(text.strip().splitlines()):
        if len(row) < len(FIELDS):
            continue
        g = {}
        for cell, (field, key) in zip(row, FIELDS):
            g[key] = cell.strip() if field == "name" else _to_number(cell)
        if not g["name"]:
            continue
        for k in ("vram_total", "vram_used"):
            if g[k] is not None:
                g[k] *= _MIB
        used, total = g["vram_used"], g["vram_total"]
        g["vram_percent"] = (
            round(used / total * 100, 1) if used is not None and total else None
        )
        gpus.append(g)
    return gpus


def available():
    """True when nvidia-smi is on PATH (i.e. an NVIDIA driver is installed)."""
    return shutil.which("nvidia-smi") is not None


def query_gpus():
    """One nvidia-smi call → list of per-GPU dicts; [] on any failure."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    cmd = [
        exe,
        "--query-gpu=" + ",".join(field for field, _ in FIELDS),
        "--format=csv,noheader,nounits",
    ]
    kwargs = {"capture_output": True, "text": True, "timeout": 5}
    if sys.platform == "win32":
        # Never pop a console window, even from a windowed PyInstaller build.
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        kwargs["startupinfo"] = si
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        out = subprocess.run(cmd, **kwargs)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    try:
        return parse_query_csv(out.stdout)
    except Exception:
        return []
