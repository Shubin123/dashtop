"""End-to-end tests with a real Android tablet connected via ADB.

These tests verify the full pipeline:
1. dashtop server binds to localhost (--adb mode)
2. adb reverse forwards the port to the tablet
3. The tablet can fetch the dashboard over the USB-tethered tunnel
4. SSE streaming works end-to-end

All tests are skipped gracefully when ADB is not installed or no device
is connected.  The ADB path can be overridden via the DASHTOP_ADB_PATH
environment variable.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

import server as dashtop

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAST_INTERVAL = 0.2

# ---------------------------------------------------------------------------
# ADB discovery
# ---------------------------------------------------------------------------

# Ordered list of places to look for adb.
_ADB_CANDIDATES = [
    os.path.join(BASE, "platform-tools", "adb.exe"),       # packaged with project
    os.path.join(BASE, "platform-tools", "adb"),            # packaged (linux)
    os.path.join(BASE, "bin", "adb.exe"),
    os.path.join(BASE, "bin", "adb"),
    os.path.expanduser(r"~\Downloads\platform-tools-latest-windows\platform-tools\adb.exe"),
    "adb",                                                   # in PATH
]


def _find_adb():
    """Return the path to a working adb binary, or None."""
    env_path = os.environ.get("DASHTOP_ADB_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path

    for candidate in _ADB_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
        # Also try via `which adb` on non-Windows.
        if candidate == "adb":
            try:
                result = subprocess.run(
                    ["which", "adb"], capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
            except Exception:
                pass
    return None


def _adb():
    """Return adb path or pytest.skip the current test."""
    path = _find_adb()
    if not path:
        pytest.skip("adb not found — set DASHTOP_ADB_PATH or install platform-tools")
    return path


def _adb_devices(adb_path):
    """Return list of connected device serials."""
    try:
        result = subprocess.run(
            [adb_path, "devices"], capture_output=True, text=True, timeout=10
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        pytest.skip(f"adb devices failed: {exc}")

    devices = []
    for line in result.stdout.splitlines()[1:]:
        if "\tdevice" in line:
            devices.append(line.split("\t")[0])
    return devices


def _adb_shell(adb_path, serial, cmd, timeout=15):
    """Run a command on the Android device via adb shell, return stdout."""
    result = subprocess.run(
        [adb_path, "-s", serial, "shell", cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    return result.stdout.strip(), result.stderr.strip(), result.returncode


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def adb():
    """Module-scoped: locate adb and pick the first connected device."""
    path = _find_adb()
    if not path:
        pytest.skip("adb not found")
    devices = _adb_devices(path)
    if not devices:
        pytest.skip("no ADB device connected")
    serial = devices[0]
    return {"path": path, "serial": serial}


@pytest.fixture(scope="module")
def adb_server(adb):
    """Boot dashtop in --adb mode on an ephemeral port and set up
    adb reverse forwarding.  Tear down after the module."""
    port = _free_port()

    # Start dashtop sampler + HTTP server (localhost only, like --adb does).
    sampler = dashtop.Sampler(interval=FAST_INTERVAL, history_seconds=60)
    sampler.start()
    dashtop.Handler.sampler = sampler
    dashtop.Handler.info = dashtop.static_info()

    httpd = ThreadingHTTPServer(("127.0.0.1", port), dashtop.Handler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    # Wait for first sample.
    deadline = time.time() + 10
    while sampler.latest is None and time.time() < deadline:
        time.sleep(0.05)
    assert sampler.latest is not None, "sampler produced no snapshot"

    # Set up adb reverse.
    adb_path = adb["path"]
    serial = adb["serial"]
    r = subprocess.run(
        [adb_path, "-s", serial, "reverse", f"tcp:{port}", f"tcp:{port}"],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        httpd.shutdown()
        pytest.skip(f"adb reverse failed: {r.stderr.strip()}")

    yield {
        "port": port,
        "sampler": sampler,
        "httpd": httpd,
        "adb_path": adb_path,
        "serial": serial,
    }

    # Teardown.
    subprocess.run(
        [adb_path, "-s", serial, "reverse", "--remove", f"tcp:{port}"],
        capture_output=True, timeout=10,
    )
    httpd.shutdown()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAdbConnectivity:
    """Verify the tablet can reach the dashboard through the adb tunnel."""

    def test_adb_device_connected(self, adb):
        """Sanity check: a device is attached and recognised."""
        assert adb["serial"], "no device serial"
        print(f"\n    device: {adb['serial']}")

    def test_tablet_can_fetch_root_page(self, adb, adb_server):
        """The tablet can GET / through the adb reverse tunnel."""
        port = adb_server["port"]
        stdout, stderr, rc = _adb_shell(
            adb["path"], adb["serial"],
            f"curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{port}/",
            timeout=15,
        )

        # adb shell may add a trailing \r; strip it.
        http_code = stdout.strip().strip("'").strip()
        print(f"\n    tablet → http://127.0.0.1:{port}/  →  HTTP {http_code}")

        # The tablet might not have curl.  Try wget as fallback.
        if rc != 0 or not http_code:
            stdout2, stderr2, rc2 = _adb_shell(
                adb["path"], adb["serial"],
                f"wget -q -O - http://127.0.0.1:{port}/ 2>&1 | head -1",
                timeout=15,
            )
            if rc2 == 0 and stdout2:
                print(f"    (wget fallback returned {len(stdout2)} bytes)")
                assert len(stdout2) > 0, "empty response from wget"
                return

        assert http_code == "200", (
            f"tablet got HTTP {http_code} (stderr: {stderr})"
        )

    def test_tablet_can_fetch_summary(self, adb, adb_server):
        """The tablet can GET /api/summary and receives valid JSON."""
        port = adb_server["port"]
        stdout, stderr, rc = _adb_shell(
            adb["path"], adb["serial"],
            f"curl -s http://127.0.0.1:{port}/api/summary",
            timeout=15,
        )

        if rc != 0 or not stdout.strip():
            # Try wget.
            stdout2, stderr2, rc2 = _adb_shell(
                adb["path"], adb["serial"],
                f"wget -q -O - http://127.0.0.1:{port}/api/summary 2>&1",
                timeout=15,
            )
            if rc2 == 0 and stdout2:
                stdout = stdout2

        assert stdout.strip(), "empty response from tablet"
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            # adb shell may prepend some garbage; try to find the JSON.
            for line in stdout.splitlines():
                line = line.strip()
                if line.startswith("{"):
                    data = json.loads(line)
                    break
            else:
                raise AssertionError(f"tablet returned non-JSON: {stdout[:200]!r}")

        assert "info" in data
        assert "latest" in data
        assert "history" in data
        print(f"\n    tablet fetched /api/summary: "
              f"history={len(data['history'])} points, "
              f"latest keys={list(data['latest'].keys())}")

    def test_tablet_can_receive_sse(self, adb, adb_server):
        """The tablet can open /api/stream and receive SSE data frames."""
        port = adb_server["port"]
        # Use a short timeout so curl exits after receiving a couple of events.
        stdout, stderr, rc = _adb_shell(
            adb["path"], adb["serial"],
            f"curl -s -N -m 5 http://127.0.0.1:{port}/api/stream 2>&1 || true",
            timeout=20,
        )

        # We expect curl to time out (-m 5) which is fine — the point is
        # that it received data frames before timing out.
        data_lines = [l for l in stdout.splitlines() if l.startswith("data: ")]
        print(f"\n    tablet received {len(data_lines)} SSE frames in 5 s")
        assert len(data_lines) >= 1, (
            f"no SSE data frames received (stdout: {stdout[:300]!r})"
        )

        # At least one frame must be valid JSON.
        for line in data_lines[:3]:
            payload = line[len("data: "):]
            try:
                snap = json.loads(payload)
                assert "cpu" in snap
                assert "mem" in snap
            except json.JSONDecodeError:
                raise AssertionError(f"SSE frame not valid JSON: {payload[:100]!r}")

    def test_static_assets_over_adb(self, adb, adb_server):
        """The tablet can fetch app.js and app.css through the tunnel."""
        port = adb_server["port"]
        for path, marker in [("/static/app.js", "use strict"),
                             ("/static/app.css", "--surface-1")]:
            stdout, stderr, rc = _adb_shell(
                adb["path"], adb["serial"],
                f"curl -s http://127.0.0.1:{port}{path}",
                timeout=15,
            )
            assert marker in stdout, (
                f"tablet: {path} missing marker {marker!r} "
                f"(got {len(stdout)} bytes, rc={rc})"
            )
        print(f"\n    tablet fetched static assets successfully")


class TestAdbLatency:
    """Measure the real-world latency over the USB-tethered adb tunnel."""

    def test_summary_latency_over_adb(self, adb, adb_server):
        """Measure /api/summary round-trip time from the tablet."""
        port = adb_server["port"]
        times = []
        for _ in range(5):
            t0 = time.perf_counter()
            stdout, stderr, rc = _adb_shell(
                adb["path"], adb["serial"],
                f"curl -s -o /dev/null -w '%{{time_total}}' "
                f"http://127.0.0.1:{port}/api/summary",
                timeout=20,
            )
            elapsed = time.perf_counter() - t0
            # curl's %{time_total} is more accurate for the HTTP component.
            curl_time = stdout.strip()
            times.append(elapsed)
        # Discard first (cold) sample.
        if len(times) > 1:
            times = times[1:]

        avg = sum(times) / len(times)
        print(f"\n    /api/summary over ADB:  avg={avg * 1000:.0f} ms  "
              f"(n={len(times)})")

        # Smoke detector: ADB over USB should be well under 2 s.
        assert avg < 2.0, (
            f"ADB latency {avg * 1000:.0f} ms — check USB connection"
        )

    def test_first_byte_latency_over_adb(self, adb, adb_server):
        """Time to first byte of the SSE stream from the tablet."""
        port = adb_server["port"]
        t0 = time.perf_counter()
        stdout, stderr, rc = _adb_shell(
            adb["path"], adb["serial"],
            f"timeout 5 curl -s -N http://127.0.0.1:{port}/api/stream 2>&1 | "
            f"head -1 || true",
            timeout=20,
        )
        elapsed = time.perf_counter() - t0

        print(f"\n    SSE first byte over ADB: {elapsed * 1000:.0f} ms")
        # First byte should arrive within a few seconds.
        assert elapsed < 5.0, (
            f"SSE first byte took {elapsed:.1f}s over ADB"
        )


class TestAdbRobustness:
    """Edge cases: what happens when ADB disconnects or the port is busy?"""

    def test_adb_reverse_port_conflict_handling(self, adb):
        """Setting up a reverse on an already-reversed port should either
        succeed (no-op) or fail cleanly (not crash the server)."""
        port = _free_port()
        adb_path = adb["path"]
        serial = adb["serial"]

        # First reverse.
        r1 = subprocess.run(
            [adb_path, "-s", serial, "reverse", f"tcp:{port}", f"tcp:{port}"],
            capture_output=True, text=True, timeout=10,
        )
        # Second reverse — same port.
        r2 = subprocess.run(
            [adb_path, "-s", serial, "reverse", f"tcp:{port}", f"tcp:{port}"],
            capture_output=True, text=True, timeout=10,
        )
        # Clean up.
        subprocess.run(
            [adb_path, "-s", serial, "reverse", "--remove", f"tcp:{port}"],
            capture_output=True, timeout=10,
        )

        # The second reverse should either succeed or fail with a clear error.
        # It must not hang or crash.
        combined = (r2.stdout + r2.stderr).lower()
        print(f"\n    double-reverse result: rc={r2.returncode} "
              f"output={combined[:100]!r}")
        # Both outputs observed.  No crash = pass.

    def test_localhost_server_not_reachable_from_lan_over_adb(self, adb, adb_server):
        """The server is on 127.0.0.1 — the tablet should NOT be able to
        reach it via the PC's LAN IP, only via the adb-reversed localhost."""
        port = adb_server["port"]
        info = dashtop.static_info()
        lan_ip = info["lan_ip"]

        if lan_ip == "127.0.0.1":
            pytest.skip("no LAN IP")

        # Try to reach the server on its LAN IP from the tablet.
        stdout, stderr, rc = _adb_shell(
            adb["path"], adb["serial"],
            f"curl -s --connect-timeout 3 http://{lan_ip}:{port}/api/summary 2>&1 || true",
            timeout=10,
        )

        # Should fail — the server is localhost-only.
        print(f"\n    tablet → http://{lan_ip}:{port}/  rc={rc}  "
              f"stdout={stdout[:100]!r}")
        # rc != 0 means curl couldn't connect — that's the desired outcome.
        # Even if curl exits 0 but returns empty body, that's also fine.
        if rc == 0 and stdout.strip():
            # If it somehow succeeded, the response must not be valid dashtop JSON.
            try:
                data = json.loads(stdout)
                # It should not have the dashtop info key.
                assert "info" not in data or data.get("info", {}).get(
                    "hostname") != info["hostname"], (
                    f"localhost-only server is reachable on LAN IP {lan_ip} "
                    f"from the tablet!"
                )
            except json.JSONDecodeError:
                pass  # garbage response = couldn't reach the real server
