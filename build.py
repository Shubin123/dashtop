#!/usr/bin/env python3
"""Build dashtop into a distributable package.

Produces  dist/dashtop/  containing:
    dashtop.exe        — the server (self-contained, no Python needed)
    static/            — HTML + CSS + JS
    platform-tools/    — adb.exe + DLLs (for Android tablet support)
    start.bat          — convenience launcher for Windows

Usage:
    python build.py              # build only
    python build.py --zip        # build + create a .zip for distribution
    python build.py --clean      # remove dist/ and build/ then rebuild
"""

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
DIST = BASE / "dist" / "dashtop"
BUILD = BASE / "build"

# Where to find platform-tools (adb).
# Priority: 1) env var  2) packaged with project  3) Downloads folder
ADB_SOURCE = os.environ.get("DASHTOP_ADB_PATH")
if not ADB_SOURCE:
    for candidate in [
        BASE / "platform-tools",
        Path.home() / "Downloads" / "platform-tools-latest-windows" / "platform-tools",
    ]:
        if candidate.is_dir():
            ADB_SOURCE = str(candidate)
            break

# Minimum ADB files needed for a working adb reverse tunnel.
ADB_FILES = ["adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll"]


def cmd(desc, *args, **kwargs):
    print(f"  {desc} …", flush=True)
    result = subprocess.run(list(args), cwd=str(BASE), **kwargs)
    if result.returncode != 0:
        print(f"  ERROR: {desc} failed (exit {result.returncode})", file=sys.stderr)
        sys.exit(result.returncode)
    return result


def clean():
    for d in (DIST.parent, BUILD):
        if d.exists():
            print(f"  removing {d}")
            shutil.rmtree(d)


def build_exe():
    print("\n[1/3] Building dashtop.exe with PyInstaller …")
    cmd("PyInstaller",
        sys.executable, "-m", "PyInstaller",
        "--distpath", str(DIST.parent),
        "--workpath", str(BUILD),
        str(BASE / "dashtop.spec"))

    exe = DIST / "dashtop.exe"
    if not exe.exists():
        # PyInstaller may have put it in dist/ directly.
        alt = DIST.parent / "dashtop.exe"
        if alt.exists():
            DIST.mkdir(parents=True, exist_ok=True)
            shutil.move(str(alt), str(exe))
        else:
            # Search for it.
            for candidate in DIST.parent.rglob("dashtop.exe"):
                print(f"  found {candidate}")
                DIST.mkdir(parents=True, exist_ok=True)
                # Move everything from that dir.
                for f in candidate.parent.iterdir():
                    dest = DIST / f.name
                    if not dest.exists():
                        shutil.move(str(f), str(dest))
                break
            else:
                print("  ERROR: dashtop.exe not found after build", file=sys.stderr)
                sys.exit(1)


def copy_static():
    print("\n[2/3] Copying static assets …")
    static_src = BASE / "static"
    static_dst = DIST / "static"
    if static_dst.exists():
        shutil.rmtree(static_dst)
    shutil.copytree(static_src, static_dst)
    for f in sorted(static_dst.iterdir()):
        print(f"    {f.name}")


def copy_adb():
    print("\n[3/3] Copying platform-tools (adb) …")
    adb_dst = DIST / "platform-tools"
    if adb_dst.exists():
        shutil.rmtree(adb_dst)
    adb_dst.mkdir(parents=True, exist_ok=True)

    if not ADB_SOURCE or not Path(ADB_SOURCE).is_dir():
        print(f"    WARNING: adb not found at {ADB_SOURCE or 'any candidate'}.")
        print(f"    The package will work for LAN-mode but not --adb mode.")
        print(f"    Set DASHTOP_ADB_PATH to the platform-tools directory.")
        return

    for fname in ADB_FILES:
        src = Path(ADB_SOURCE) / fname
        if src.is_file():
            shutil.copy2(str(src), str(adb_dst / fname))
            print(f"    {fname}")
        else:
            print(f"    WARNING: {fname} not found in {ADB_SOURCE}")

    # Also copy any .dll files we might have missed.
    for f in Path(ADB_SOURCE).glob("*.dll"):
        dst = adb_dst / f.name
        if not dst.exists():
            shutil.copy2(str(f), str(dst))
            print(f"    {f.name} (extra DLL)")


def write_launcher():
    print("\nWriting launcher scripts …")
    # Windows launcher.
    bat = DIST / "start.bat"
    bat.write_text(
        "@echo off\r\n"
        "cd /d %~dp0\r\n"
        'echo dashtop — system dashboard\r\n'
        "echo.\r\n"
        "echo Starting in LAN mode (all interfaces):\r\n"
        "dashtop.exe\r\n"
        "echo.\r\n"
        "echo To use with a USB-connected Android tablet:\r\n"
        'echo   dashtop.exe --adb\r\n'
        'echo   platform-tools\\adb reverse tcp:8010 tcp:8010\r\n'
        "echo.\r\n"
        "pause\r\n",
        encoding="utf-8",
    )
    print("    start.bat")

    # Linux/macOS launcher (the .exe won't run natively, but the spec
    # can be used with PyInstaller on Linux too).
    sh = DIST / "start.sh"
    sh.write_text(
        "#!/bin/sh\n"
        'DIR="$(cd "$(dirname "$0")" && pwd)"\n'
        'echo "dashtop — system dashboard"\n'
        'echo ""\n'
        'exec "$DIR/dashtop" "$@"\n',
        encoding="utf-8",
    )
    if sys.platform != "win32":
        sh.chmod(0o755)
    print("    start.sh")


def create_zip():
    zip_path = BASE / "dist" / "dashtop.zip"
    print(f"\nCreating {zip_path} …")
    with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(DIST):
            for f in files:
                full = Path(root) / f
                arcname = full.relative_to(DIST.parent)
                zf.write(str(full), str(arcname))
    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"    {zip_path.name}  ({size_mb:.1f} MiB)")


def main():
    ap = argparse.ArgumentParser(description="Build dashtop distributable")
    ap.add_argument("--clean", action="store_true", help="clean before build")
    ap.add_argument("--zip", action="store_true", help="also create .zip")
    args = ap.parse_args()

    if args.clean:
        clean()

    DIST.mkdir(parents=True, exist_ok=True)

    build_exe()
    copy_static()
    copy_adb()
    write_launcher()

    if args.zip:
        create_zip()

    print(f"\nDone — {DIST}")
    print(f"  Run:  {DIST / 'start.bat'}")
    print(f"  Or:   {DIST / 'dashtop.exe'} --adb")


if __name__ == "__main__":
    main()
