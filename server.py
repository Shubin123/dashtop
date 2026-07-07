#!/usr/bin/env python3
"""dashtop — broadcast this PC's live system state to browsers on the LAN.

Runs on Windows 10 and Linux (macOS works too). Only dependency: psutil.

    pip install -r requirements.txt
    python server.py

Then open  http://<this-pc's-ip>:8010  on the tablet.
"""

import argparse
import json
import mimetypes
import os
import platform
import socket
import sys
import threading
import time
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import psutil
except ImportError:
    sys.exit("psutil is missing. Install it with:  pip install -r requirements.txt")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

SKIP_FSTYPES = {"squashfs", "tmpfs", "devtmpfs", "overlay", "proc", "sysfs", "iso9660"}
IDLE_PROC_NAMES = {"System Idle Process", "Idle"}


def lan_ip():
    """Best-guess LAN address: route a UDP socket outward and read our end."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def static_info():
    uname = platform.uname()
    return {
        "hostname": socket.gethostname(),
        "os": f"{uname.system} {uname.release}",
        "machine": uname.machine,
        "cpu_count": psutil.cpu_count(logical=True) or 1,
        "cpu_count_physical": psutil.cpu_count(logical=False),
        "mem_total": psutil.virtual_memory().total,
        "boot_time": psutil.boot_time(),
        "lan_ip": lan_ip(),
    }


class Sampler(threading.Thread):
    """Collects one snapshot every `interval` seconds and keeps a rolling history."""

    def __init__(self, interval, history_seconds):
        super().__init__(daemon=True)
        self.interval = interval
        self.latest = None
        self.history = deque(maxlen=max(2, int(history_seconds / interval)))
        self.cond = threading.Condition()
        self.ncpu = psutil.cpu_count(logical=True) or 1
        self._prev_disk = self._safe(psutil.disk_io_counters)
        self._prev_net = self._safe(psutil.net_io_counters)
        self._prev_t = time.time()
        # Prime psutil's since-last-call counters so the first real sample is meaningful.
        psutil.cpu_percent()
        psutil.cpu_percent(percpu=True)
        for _ in psutil.process_iter(["cpu_percent"]):
            pass

    @staticmethod
    def _safe(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            return None

    def run(self):
        time.sleep(self.interval)
        while True:
            try:
                snap = self.sample()
            except Exception as exc:  # keep sampling even if one pass fails
                print(f"[dashtop] sample failed: {exc}", file=sys.stderr)
                time.sleep(self.interval)
                continue
            point = {
                "t": snap["t"],
                "cpu": snap["cpu"]["total"],
                "mem": snap["mem"]["percent"],
                "dn": snap["net"]["down_bps"],
                "up": snap["net"]["up_bps"],
                "rd": snap["io"]["read_bps"],
                "wr": snap["io"]["write_bps"],
            }
            with self.cond:
                self.latest = snap
                self.history.append(point)
                self.cond.notify_all()
            time.sleep(self.interval)

    def sample(self):
        now = time.time()
        dt = max(now - self._prev_t, 1e-6)

        cpu_total = psutil.cpu_percent()
        per_core = psutil.cpu_percent(percpu=True)
        freq = None
        f = self._safe(psutil.cpu_freq)
        if f and f.current:
            freq = round(f.current)
        load = None
        try:
            load = [round(x, 2) for x in psutil.getloadavg()]
        except (AttributeError, OSError):
            pass

        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()

        rd = wr = dn = up = 0.0
        disk_io = self._safe(psutil.disk_io_counters)
        if disk_io and self._prev_disk:
            rd = max(0.0, disk_io.read_bytes - self._prev_disk.read_bytes) / dt
            wr = max(0.0, disk_io.write_bytes - self._prev_disk.write_bytes) / dt
        net_io = self._safe(psutil.net_io_counters)
        if net_io and self._prev_net:
            dn = max(0.0, net_io.bytes_recv - self._prev_net.bytes_recv) / dt
            up = max(0.0, net_io.bytes_sent - self._prev_net.bytes_sent) / dt
        self._prev_disk, self._prev_net, self._prev_t = disk_io, net_io, now

        disks, seen_mounts = [], set()
        for part in self._safe(psutil.disk_partitions, all=False) or []:
            if part.fstype.lower() in SKIP_FSTYPES or "cdrom" in part.opts:
                continue
            if part.mountpoint in seen_mounts:
                continue
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except OSError:
                continue  # e.g. empty card reader / detached drive letter
            seen_mounts.add(part.mountpoint)
            disks.append({
                "mount": part.mountpoint,
                "fstype": part.fstype,
                "total": usage.total,
                "used": usage.used,
                "percent": usage.percent,
            })
        disks.sort(key=lambda d: d["mount"])

        battery = None
        b = self._safe(psutil.sensors_battery)
        if b:
            battery = {
                "percent": round(b.percent),
                "plugged": bool(b.power_plugged),
                "secsleft": b.secsleft if isinstance(b.secsleft, int) and b.secsleft > 0 else None,
            }

        temps = []
        if hasattr(psutil, "sensors_temperatures"):
            for name, entries in (self._safe(psutil.sensors_temperatures) or {}).items():
                for e in entries:
                    if e.current:
                        temps.append({"label": e.label or name, "c": round(e.current, 1)})
        temps = temps[:8]

        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            info = p.info
            name = info.get("name")
            if not name or name in IDLE_PROC_NAMES or info["pid"] == 0:
                continue
            procs.append({
                "pid": info["pid"],
                "name": name[:48],
                # normalize so 100% == the whole machine, like Task Manager
                "cpu": round((info["cpu_percent"] or 0.0) / self.ncpu, 1),
                "mem": round(info["memory_percent"] or 0.0, 1),
            })
        top_cpu = sorted(procs, key=lambda x: -x["cpu"])[:10]
        top_mem = sorted(procs, key=lambda x: -x["mem"])[:10]
        merged, seen_pids = [], set()
        for x in top_cpu + top_mem:
            if x["pid"] not in seen_pids:
                seen_pids.add(x["pid"])
                merged.append(x)

        return {
            "t": round(now, 3),
            "uptime": round(now - psutil.boot_time()),
            "cpu": {"total": cpu_total, "percore": per_core, "freq": freq, "load": load},
            "mem": {
                "total": vm.total, "used": vm.used, "percent": vm.percent,
                "swap_total": sw.total, "swap_used": sw.used, "swap_percent": sw.percent,
            },
            "disks": disks,
            "io": {"read_bps": round(rd), "write_bps": round(wr)},
            "net": {
                "down_bps": round(dn), "up_bps": round(up),
                "recv_total": net_io.bytes_recv if net_io else 0,
                "sent_total": net_io.bytes_sent if net_io else 0,
            },
            "battery": battery,
            "temps": temps,
            "procs": merged[:12],
        }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    sampler = None  # set in main()
    info = None

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self.serve_static("index.html")
        elif path == "/api/summary":
            with self.sampler.cond:
                body = {
                    "info": self.info,
                    "interval": self.sampler.interval,
                    "latest": self.sampler.latest,
                    "history": list(self.sampler.history),
                }
            self.send_json(body)
        elif path == "/api/stream":
            self.stream()
        elif path.startswith("/static/"):
            self.serve_static(path[len("/static/"):])
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def send_json(self, obj):
        body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def serve_static(self, rel):
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR + os.sep) and full != os.path.join(STATIC_DIR, "index.html"):
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not os.path.isfile(full):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def stream(self):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        sampler = self.sampler
        last_t = 0
        try:
            while True:
                with sampler.cond:
                    sampler.cond.wait(timeout=sampler.interval * 2)
                    snap = sampler.latest
                if snap is None:
                    continue
                if snap["t"] == last_t:
                    self.wfile.write(b": ping\n\n")  # keepalive; detects dead clients
                    self.wfile.flush()
                    continue
                last_t = snap["t"]
                payload = json.dumps(snap, separators=(",", ":")).encode("utf-8")
                self.wfile.write(b"data: " + payload + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def log_message(self, fmt, *args):
        pass  # keep the console quiet; the startup banner is the useful output


def main():
    ap = argparse.ArgumentParser(description="Serve this PC's live system state over the LAN.")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    ap.add_argument("--port", type=int, default=8010, help="port (default: 8010)")
    ap.add_argument("--interval", type=float, default=2.0, help="sample interval in seconds (default: 2)")
    ap.add_argument("--history", type=float, default=15.0, help="minutes of history to keep (default: 15)")
    args = ap.parse_args()

    sampler = Sampler(interval=args.interval, history_seconds=args.history * 60)
    sampler.start()
    Handler.sampler = sampler
    Handler.info = static_info()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True

    ip = Handler.info["lan_ip"]
    print(f"dashtop is serving {Handler.info['hostname']} ({Handler.info['os']})")
    print(f"  On the tablet, open:  http://{ip}:{args.port}")
    print(f"  On this PC:           http://localhost:{args.port}")
    if platform.system() == "Windows":
        print("  If Windows Firewall asks, allow access on Private networks.")
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
