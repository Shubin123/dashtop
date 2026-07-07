"""Performance tests: sampling must stay cheap, responses fast and bounded,
and the server must hold up to several tablets connecting at once.

Thresholds are deliberately generous — they catch regressions (an accidental
O(n^2), an unbounded buffer), not micro-variance between machines.
"""

import http.client
import json
import threading
import time

from conftest import FAST_INTERVAL


# ---------- sampling cost ----------

def test_sample_is_cheaper_than_the_interval(live):
    """One sample must cost well under the 2s default interval, or the
    sampler thread falls behind and the dashboard lags."""
    durations = []
    for _ in range(3):
        t0 = time.perf_counter()
        live.sampler.sample()
        durations.append(time.perf_counter() - t0)
    assert min(durations) < 1.0, f"sample() took {min(durations):.2f}s"


# ---------- response latency & size ----------

def test_summary_latency(live):
    live.request("GET", "/api/summary")  # warm up
    times = []
    for _ in range(10):
        t0 = time.perf_counter()
        status, _, _ = live.request("GET", "/api/summary")
        times.append(time.perf_counter() - t0)
        assert status == 200
    avg = sum(times) / len(times)
    assert avg < 0.25, f"/api/summary averaged {avg * 1000:.0f}ms"


def test_static_latency(live):
    live.request("GET", "/static/app.js")
    times = []
    for _ in range(10):
        t0 = time.perf_counter()
        status, _, _ = live.request("GET", "/static/app.js")
        times.append(time.perf_counter() - t0)
        assert status == 200
    avg = sum(times) / len(times)
    assert avg < 0.1, f"/static/app.js averaged {avg * 1000:.0f}ms"


def test_summary_payload_is_bounded(live):
    """A tablet on wifi should get the initial payload in one quick round
    trip: full history plus snapshot must stay small."""
    status, _, body = live.request("GET", "/api/summary")
    assert status == 200
    assert len(body) < 256 * 1024, f"summary payload is {len(body) / 1024:.0f} KB"


# ---------- history memory bound ----------

def test_history_is_bounded(live):
    expected_max = int(60 / FAST_INTERVAL)  # fixture keeps 60s of history
    assert live.sampler.history.maxlen == expected_max
    assert len(live.sampler.history) <= expected_max
    # the payload can never grow past retention, no matter how long it runs
    status, _, body = live.request("GET", "/api/summary")
    assert status == 200
    assert len(json.loads(body)["history"]) <= expected_max


# ---------- streaming ----------

def test_sse_first_event_arrives_quickly(live):
    """A connecting tablet must see data within a couple of intervals."""
    conn = http.client.HTTPConnection(live.host, live.port, timeout=5)
    try:
        conn.request("GET", "/api/stream")
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.getheader("Content-Type").startswith("text/event-stream")
        t0 = time.perf_counter()
        deadline = t0 + FAST_INTERVAL * 10
        while time.perf_counter() < deadline:
            line = resp.readline()
            if line.startswith(b"data: "):
                json.loads(line[len(b"data: "):])  # a valid snapshot
                break
        else:
            raise AssertionError("no SSE data event before deadline")
        assert time.perf_counter() - t0 < FAST_INTERVAL * 5
    finally:
        conn.close()


# ---------- concurrency ----------

def test_handles_concurrent_clients(live):
    """Several tablets at once: 16 parallel summary requests all succeed,
    and the whole burst clears fast (the server is threaded, not serial)."""
    results = []
    lock = threading.Lock()

    def hit():
        status, _, body = live.request("GET", "/api/summary", timeout=10)
        with lock:
            results.append((status, len(body)))

    threads = [threading.Thread(target=hit) for _ in range(16)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - t0

    assert len(results) == 16
    assert all(status == 200 and size > 0 for status, size in results)
    assert elapsed < 5.0, f"16 concurrent requests took {elapsed:.1f}s"


def test_sse_client_disconnect_does_not_wedge_the_server(live):
    """Open streams, drop them abruptly, then verify the server still answers
    promptly — dead SSE threads must not pile up or block sampling."""
    for _ in range(5):
        conn = http.client.HTTPConnection(live.host, live.port, timeout=5)
        conn.request("GET", "/api/stream")
        resp = conn.getresponse()
        resp.readline()
        conn.close()  # abrupt disconnect mid-stream
    t0 = time.perf_counter()
    status, _, _ = live.request("GET", "/api/summary")
    assert status == 200
    assert time.perf_counter() - t0 < 1.0
