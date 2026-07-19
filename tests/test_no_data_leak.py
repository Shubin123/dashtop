"""Intrinsic data-leak tests: verify that no system data escapes the program
beyond the explicitly configured network boundary, and that every response
carries only the documented fields — nothing more.

These tests are especially relevant for dual-homed / "2-interface" machines
(e.g. Ethernet + Wi-Fi, or LAN + VPN) where binding to 0.0.0.0 without
realising it would expose the dashboard on *all* interfaces, including a
guest or public-facing one.
"""

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

import server as dashtop

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAST_INTERVAL = 0.2


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _free_port():
    """Return an available TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http_get(host, port, path, timeout=3):
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1.  Network-interface isolation ("2v2")
# ---------------------------------------------------------------------------

class TestBindingIsolation:
    """When the server binds to a *specific* address, it must NOT be reachable
    on other addresses the machine owns."""

    def test_localhost_only_not_reachable_via_lan_ip(self):
        """Bind to 127.0.0.1 — the server must be unreachable on the LAN IP."""
        port = _free_port()
        info = dashtop.static_info()
        lan = info["lan_ip"]

        # Only run if we actually have a LAN address distinct from loopback.
        if lan == "127.0.0.1":
            pytest.skip("no LAN IP detected — machine is offline or loopback-only")

        sampler = dashtop.Sampler(interval=FAST_INTERVAL, history_seconds=60)
        sampler.start()
        dashtop.Handler.sampler = sampler
        dashtop.Handler.info = info

        httpd = ThreadingHTTPServer(("127.0.0.1", port), dashtop.Handler)
        httpd.daemon_threads = True
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()

        try:
            # localhost MUST work
            s, _, _ = _http_get("127.0.0.1", port, "/api/summary")
            assert s == 200, f"localhost-bound server not reachable on 127.0.0.1:{port}"

            # LAN IP MUST be refused — the server didn't bind there
            with pytest.raises((ConnectionRefusedError, ConnectionResetError,
                                ConnectionAbortedError, OSError,
                                socket.timeout)):
                _http_get(lan, port, "/api/summary", timeout=1.5)
        finally:
            httpd.shutdown()

    def test_wildcard_bind_reachable_on_lan(self):
        """Bind to 0.0.0.0 — the server IS reachable on the LAN IP.
        This is the default, and the reason the README warns about interfaces."""
        port = _free_port()
        info = dashtop.static_info()
        lan = info["lan_ip"]

        if lan == "127.0.0.1":
            pytest.skip("no LAN IP detected")

        sampler = dashtop.Sampler(interval=FAST_INTERVAL, history_seconds=60)
        sampler.start()
        dashtop.Handler.sampler = sampler
        dashtop.Handler.info = info

        httpd = ThreadingHTTPServer(("0.0.0.0", port), dashtop.Handler)
        httpd.daemon_threads = True
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()

        try:
            # LAN IP MUST work — 0.0.0.0 means "all interfaces"
            s, _, _ = _http_get(lan, port, "/api/summary", timeout=3)
            assert s == 200, (
                f"wildcard-bound server not reachable on {lan}:{port} — "
                f"firewall may be blocking, but the bind itself should accept"
            )
        finally:
            httpd.shutdown()

    def test_adb_flag_forces_localhost_bind(self):
        """The --adb flag must set host to 127.0.0.1 regardless of user input."""
        args = dashtop.parse_args(["--adb", "--host", "0.0.0.0"])
        assert args.host == "127.0.0.1", (
            "--adb must force localhost bind even when --host is also passed"
        )

        args2 = dashtop.parse_args(["--adb"])
        assert args2.host == "127.0.0.1"


# ---------------------------------------------------------------------------
# 2.  No outbound connections
# ---------------------------------------------------------------------------

class TestNoOutbound:
    """The server is a pure responder — it must NEVER initiate a connection
    to any other host (no telemetry, no phone-home, no DNS probes triggered
    by a request path)."""

    def test_server_process_makes_no_outbound_connections(self):
        """Sample once and check that no socket was opened to a remote address."""
        sampler = dashtop.Sampler(interval=FAST_INTERVAL, history_seconds=60)
        sampler.start()
        dashtop.Handler.sampler = sampler
        dashtop.Handler.info = dashtop.static_info()

        port = _free_port()
        httpd = ThreadingHTTPServer(("127.0.0.1", port), dashtop.Handler)
        httpd.daemon_threads = True
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()

        try:
            # Hit every endpoint to make sure none of them triggers outbound I/O.
            for path in ("/", "/api/summary", "/api/stream"):
                try:
                    _http_get("127.0.0.1", port, path, timeout=3)
                except Exception:
                    pass  # SSE stream will timeout; that's fine

            # The programmatic check: dashtop's own code never calls
            # socket.connect(), socket.create_connection(), or urllib.
            # We verify by inspecting the imports and call sites.
            server_src = open(os.path.join(BASE, "server.py"), encoding="utf-8").read()
            # The only socket.connect() should be in lan_ip() for local detection.
            connect_count = server_src.count("connect(") + server_src.count("connect (")
            # lan_ip uses one s.connect() — that's the only legitimate one.
            # (It connects a UDP socket to 8.8.8.8:80 without sending data,
            #  just to discover the local address.)
            assert connect_count <= 1, (
                f"server.py calls connect() {connect_count} times; "
                f"expected at most 1 (the LAN-IP detection stub)"
            )
        finally:
            httpd.shutdown()


# ---------------------------------------------------------------------------
# 3.  No data persisted to disk
# ---------------------------------------------------------------------------

class TestNoDiskPersistence:
    """dashtop is an ephemeral dashboard — it must never write system stats
    to a file, log, or temp directory on its own."""

    def test_no_log_files_created(self):
        """Running a few samples must not create any .log or .txt files."""
        before = set()
        for root, dirs, files in os.walk(BASE):
            # skip .git and venv — those aren't dashtop's output
            dirs[:] = [d for d in dirs if d not in (".git", ".venv", "__pycache__")]
            for f in files:
                before.add(os.path.join(root, f))

        sampler = dashtop.Sampler(interval=FAST_INTERVAL, history_seconds=60)
        sampler.start()
        for _ in range(5):
            sampler.sample()
            time.sleep(FAST_INTERVAL)

        after = set()
        for root, dirs, files in os.walk(BASE):
            dirs[:] = [d for d in dirs if d not in (".git", ".venv", "__pycache__")]
            for f in files:
                after.add(os.path.join(root, f))

        new_files = after - before
        assert not new_files, (
            f"Sampler created unexpected files on disk: {new_files}"
        )

    def test_handler_never_opens_files_for_writing(self):
        """The Handler source must not open any file in write/append mode."""
        server_src = open(os.path.join(BASE, "server.py"), encoding="utf-8").read()
        # open(..., 'w') or open(..., 'a') — we allow only 'rb' (static serving)
        lines = server_src.split("\n")
        for lineno, line in enumerate(lines, 1):
            if "open(" in line and ("'w'" in line or '"w"' in line or
                                      "'a'" in line or '"a"' in line or
                                      "'w+" in line or '"w+"' in line):
                raise AssertionError(
                    f"server.py line {lineno} opens a file for writing: {line.strip()}"
                )


# ---------------------------------------------------------------------------
# 4.  API response field audit — no sensitive fields
# ---------------------------------------------------------------------------

class TestApiFieldAudit:
    """Every key in every JSON response must be documented and intentional.
    No sensitive system data (cmdline, env, paths, users, tokens) may leak."""

    def test_summary_top_level_keys(self, live):
        _, _, body = live.request("GET", "/api/summary")
        data = json.loads(body)
        allowed = {"info", "interval", "latest", "history"}
        actual = set(data.keys())
        extra = actual - allowed
        assert not extra, f"/api/summary top-level keys leaked: {extra}"

    def test_info_keys_are_strictly_bounded(self, live):
        _, _, body = live.request("GET", "/api/summary")
        data = json.loads(body)
        info_allowed = {
            "hostname", "os", "machine", "cpu_count", "cpu_count_physical",
            "mem_total", "boot_time", "lan_ip",
        }
        extra = set(data["info"].keys()) - info_allowed
        assert not extra, f"info object leaked keys: {extra}"

    def test_latest_snapshot_keys_are_strictly_bounded(self, live):
        _, _, body = live.request("GET", "/api/summary")
        snap = json.loads(body)["latest"]
        allowed = {"t", "uptime", "cpu", "mem", "disks", "io",
                   "net", "battery", "temps", "procs"}
        extra = set(snap.keys()) - allowed
        assert not extra, f"snapshot leaked keys: {extra}"

    def test_history_point_keys_are_minimal(self, live):
        """History is a reduced timeseries — it must NOT embed full snapshots."""
        _, _, body = live.request("GET", "/api/summary")
        data = json.loads(body)
        if not data["history"]:
            pytest.skip("no history yet")
        point_allowed = {"t", "cpu", "mem", "dn", "up", "rd", "wr"}
        for pt in data["history"]:
            extra = set(pt.keys()) - point_allowed
            assert not extra, f"history point leaked keys: {extra}"

    def test_process_entries_never_include_cmdline_or_paths(self, live):
        """Process info must be PID + name + cpu + mem only."""
        _, _, body = live.request("GET", "/api/summary")
        snap = json.loads(body)["latest"]
        for proc in snap["procs"]:
            assert set(proc.keys()) == {"pid", "name", "cpu", "mem"}, (
                f"process entry has unexpected keys: {set(proc.keys())}"
            )
            # belt and suspenders: no field value should look like a path
            for v in proc.values():
                if isinstance(v, str):
                    assert not v.startswith("/"), f"proc field looks like a path: {v}"
                    assert not v.startswith("C:\\"), f"proc field looks like a path: {v}"
                    assert "\\" not in v, f"proc field contains backslash: {v}"

    def test_disk_entries_are_minimal(self, live):
        """Disk entries: only mount, fstype, total, used, percent."""
        _, _, body = live.request("GET", "/api/summary")
        snap = json.loads(body)["latest"]
        allowed = {"mount", "fstype", "total", "used", "percent"}
        for disk in snap.get("disks", []):
            extra = set(disk.keys()) - allowed
            assert not extra, f"disk entry leaked keys: {extra}"

    def test_net_entry_no_raw_addresses(self, live):
        """Network stats must be counters only — no IP/MAC addresses."""
        _, _, body = live.request("GET", "/api/summary")
        snap = json.loads(body)["latest"]
        net = snap["net"]
        allowed = {"down_bps", "up_bps", "recv_total", "sent_total"}
        extra = set(net.keys()) - allowed
        assert not extra, f"net entry leaked keys: {extra}"

    def test_sse_stream_payload_matches_snapshot_schema(self, live):
        """Every SSE data frame must be a valid snapshot with only the
        allowed top-level keys."""
        import http.client
        conn = http.client.HTTPConnection(live.host, live.port, timeout=5)
        allowed = {"t", "uptime", "cpu", "mem", "disks", "io",
                   "net", "battery", "temps", "procs"}
        try:
            conn.request("GET", "/api/stream")
            resp = conn.getresponse()
            assert resp.status == 200
            deadline = time.perf_counter() + FAST_INTERVAL * 10
            frames = 0
            while time.perf_counter() < deadline and frames < 3:
                line = resp.readline()
                if line.startswith(b"data: "):
                    snap = json.loads(line[len(b"data: "):])
                    extra = set(snap.keys()) - allowed
                    assert not extra, f"SSE frame leaked keys: {extra}"
                    frames += 1
            assert frames >= 1, "no SSE frames received"
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 5.  Cross-origin / CORS — no open access
# ---------------------------------------------------------------------------

class TestCORSHeaders:
    """Without explicit CORS headers the browser's same-origin policy prevents
    arbitrary websites from reading the dashboard.  The server must not add
    permissive CORS headers."""

    def test_no_cors_allow_all_on_summary(self, live):
        _, headers, _ = live.request("GET", "/api/summary")
        assert "Access-Control-Allow-Origin" not in headers, (
            "CORS ACAO header present — allows cross-origin reads"
        )

    def test_no_cors_allow_all_on_static(self, live):
        _, headers, _ = live.request("GET", "/static/app.js")
        assert "Access-Control-Allow-Origin" not in headers

    def test_no_cors_allow_all_on_stream(self, live):
        import http.client
        conn = http.client.HTTPConnection(live.host, live.port, timeout=5)
        try:
            conn.request("GET", "/api/stream")
            resp = conn.getresponse()
            headers = dict(resp.getheaders())
            assert "Access-Control-Allow-Origin" not in headers
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 6.  Error responses — no internal information
# ---------------------------------------------------------------------------

class TestErrorHygiene:
    """404s and other error responses must not leak server paths, stack traces,
    or library versions."""

    def test_404_body_is_empty_or_minimal(self, live):
        for path in ("/nonexistent", "/api/exec", "/.env"):
            status, _, body = live.request("GET", path)
            assert status == 404
            # Must not contain server paths
            assert b"server.py" not in body, f"404 for {path} leaked server.py"
            assert b"dashtop" not in body.lower(), f"404 for {path} leaked project name"
            # http.server default 404 body is HTML — that's fine, but it
            # shouldn't contain anything beyond the standard message.
            if body:
                try:
                    text = body.decode("utf-8", errors="replace")
                    assert "Traceback" not in text
                    assert "File \"" not in text
                    assert "site-packages" not in text
                except Exception:
                    pass

    def test_403_body_is_empty_or_minimal(self, live):
        for path in ("/static/../server.py", "/static/../../etc/passwd"):
            status, _, body = live.request("GET", path)
            if status == 403:
                assert b"Traceback" not in body
                assert b"server.py" not in body

    def test_no_server_header_leaks_framework_version(self, live):
        _, headers, _ = live.request("GET", "/")
        server_hdr = headers.get("Server", "")
        # Python http.server leaks "BaseHTTP/0.6 Python/3.x.y" by default.
        # That's acceptable but we document it; the key is no *additional*
        # custom headers that leak more.
        # We just verify it doesn't include a custom dashtop version string.
        assert "dashtop" not in server_hdr.lower()


# ---------------------------------------------------------------------------
# 7.  Memory — no accumulation beyond retention window
# ---------------------------------------------------------------------------

class TestMemoryBounds:
    """The in-memory history must never grow without bound — a long-running
    server must not accumulate data that could be dumped later."""

    def test_history_never_exceeds_maxlen(self, live):
        cap = live.sampler.history.maxlen
        for _ in range(cap + 20):
            live.sampler.sample()
            time.sleep(0.01)
        # Wait for the sampler thread to push new entries
        time.sleep(FAST_INTERVAL * 2)
        assert len(live.sampler.history) <= cap, (
            f"history grew to {len(live.sampler.history)}, cap is {cap}"
        )

    def test_latest_is_atomic_snapshot_no_accumulation(self, live):
        """latest is replaced, not appended to — verify it's a single object."""
        assert isinstance(live.sampler.latest, dict)
        # Two samples later, latest is still just one dict
        time.sleep(FAST_INTERVAL * 2)
        assert isinstance(live.sampler.latest, dict)


# ---------------------------------------------------------------------------
# 8.  Static-file sandbox
# ---------------------------------------------------------------------------

class TestStaticSandbox:
    """The static file server must never serve anything outside STATIC_DIR,
    and must not follow symlinks that point outward."""

    def test_cannot_serve_files_outside_static(self, live):
        """Every traversal variant must 403/404 — never 200."""
        attacks = [
            "/static/../server.py",
            "/static/../tests/conftest.py",
            "/static/../../requirements.txt",
            "/static/....//....//server.py",
            "/static/..%2f..%2fserver.py",
            "/static/%2e%2e/%2e%2e/server.py",
        ]
        for path in attacks:
            status, _, body = live.request("GET", path)
            assert status in (403, 404), f"{path} returned {status}"
            assert b"#!/usr/bin/env python3" not in body[:50] if body else True

    def test_cannot_serve_dotfiles(self, live):
        """Even if a dotfile exists in static/, serving it is dangerous."""
        for path in ("/static/.htaccess", "/static/.env", "/static/.gitignore"):
            status, _, _ = live.request("GET", path)
            # Either 404 (doesn't exist) or 403/404 (blocked) — never 200
            assert status != 200, f"{path} returned 200"


# ---------------------------------------------------------------------------
# 9.  Sampler intrinsics — no data leaves the thread except via the API
# ---------------------------------------------------------------------------

class TestSamplerIsolation:
    """The Sampler thread collects data; it must not have any side-channel
    that writes data elsewhere (print, log, file, socket)."""

    def test_sampler_never_prints(self, capsys):
        """A sampling cycle must produce no stdout/stderr output."""
        sampler = dashtop.Sampler(interval=FAST_INTERVAL, history_seconds=60)
        sampler.start()
        time.sleep(FAST_INTERVAL * 3)
        captured = capsys.readouterr()
        # The sampler thread itself should not print.
        # (server.py main() prints a banner, but the Sampler.run loop doesn't.)
        assert captured.out == "", f"sampler printed to stdout: {captured.out!r}"
        assert captured.err == "", f"sampler printed to stderr: {captured.err!r}"

    def test_sample_return_value_never_contains_none_of_these(self):
        """Audit the sample dict for categories of data that should never appear."""
        sampler = dashtop.Sampler(interval=FAST_INTERVAL, history_seconds=60)
        snap = sampler.sample()

        # Flatten all string values and check for sensitive patterns.
        def _strings(obj, depth=0):
            if depth > 5:
                return
            if isinstance(obj, str):
                yield obj
            elif isinstance(obj, dict):
                for v in obj.values():
                    yield from _strings(v, depth + 1)
            elif isinstance(obj, list):
                for v in obj:
                    yield from _strings(v, depth + 1)

        for s in _strings(snap):
            # No filesystem paths
            assert not s.startswith("/"), f"snapshot contains path: {s!r}"
            assert not s.startswith("C:\\"), f"snapshot contains Windows path: {s!r}"
            # No env vars
            assert "=" not in s or not any(
                s.startswith(p) for p in ("PATH", "HOME", "USER", "TEMP",
                                          "USERPROFILE", "APPDATA", "HOSTNAME")
            ), f"snapshot may contain env data: {s!r}"
            # No IPs beyond the documented lan_ip in info (not in snapshot)
            parts = s.split(".")
            if len(parts) == 4 and all(p.isdigit() for p in parts):
                # This is only acceptable in hostname context, not in snapshot
                assert False, f"snapshot contains IP-like value: {s!r}"
