"""Honest data-usage metrics: measure the real cost of running dashtop.

These are NOT pass/fail thresholds you tune once and forget.  They are
*measurements* — run them, read the numbers, and decide whether the cost
is acceptable for your use case (tablet on Wi‑Fi, tethered phone, etc.).

Every test prints its actual measurement so you can track regressions
across commits.  The ASSERTIONS are deliberately loose "smoke detector"
bounds; they should only fire on catastrophic regressions (e.g. a 10×
payload bloat), not on normal variance.
"""

import json
import os
import sys
import threading
import time

import pytest

import server as dashtop

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAST_INTERVAL = 0.2


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _size_kb(obj):
    """Encoded size of an object as JSON, in kibibytes."""
    return len(json.dumps(obj, separators=(",", ":")).encode("utf-8")) / 1024


def _size_kb_raw(data: bytes):
    return len(data) / 1024


# ---------------------------------------------------------------------------
# 1.  Per-event payload sizes — the recurring cost
# ---------------------------------------------------------------------------

class TestEventPayloadSizes:
    """Every SSE event costs bandwidth.  These are the numbers that matter
    most for a tablet on a metered or slow connection."""

    def test_sse_event_payload_size(self, live):
        """Measure one SSE data frame (the per-sample recurring cost)."""
        import http.client
        conn = http.client.HTTPConnection(live.host, live.port, timeout=5)
        sizes = []
        try:
            conn.request("GET", "/api/stream")
            resp = conn.getresponse()
            deadline = time.perf_counter() + FAST_INTERVAL * 10
            while time.perf_counter() < deadline and len(sizes) < 10:
                line = resp.readline()
                if line.startswith(b"data: "):
                    sizes.append(len(line))
            assert len(sizes) >= 1, "no SSE frames received"
        finally:
            conn.close()

        avg = sum(sizes) / len(sizes)
        hi = max(sizes)
        lo = min(sizes)
        print(f"\n    SSE event:  avg={avg:.0f} B  min={lo:.0f} B  max={hi:.0f} B  "
              f"(n={len(sizes)})")

        # Smoke detector: a single SSE frame should never exceed 64 KiB.
        # (A typical snapshot is 2–6 KiB depending on disk count.)
        assert hi < 64 * 1024, f"SSE event ballooned to {hi / 1024:.1f} KiB"

    def test_sse_bytes_per_minute(self, live):
        """Projected bandwidth: SSE bytes per minute at the default interval."""
        import http.client
        conn = http.client.HTTPConnection(live.host, live.port, timeout=5)
        sizes = []
        try:
            conn.request("GET", "/api/stream")
            resp = conn.getresponse()
            deadline = time.perf_counter() + FAST_INTERVAL * 10
            while time.perf_counter() < deadline and len(sizes) < 8:
                line = resp.readline()
                if line.startswith(b"data: "):
                    sizes.append(len(line))
        finally:
            conn.close()

        if len(sizes) < 2:
            pytest.skip("not enough SSE frames for rate projection")

        avg_bytes = sum(sizes) / len(sizes)
        # At default 2 s interval: 30 events/min.  At FAST_INTERVAL: 300/min.
        events_per_min = 60 / FAST_INTERVAL
        bytes_per_min = avg_bytes * events_per_min
        kbps = (bytes_per_min * 8) / 60

        print(f"\n    SSE bandwidth:  {bytes_per_min / 1024:.1f} KiB/min  "
              f"({kbps / 1000:.1f} kbps)  at {events_per_min:.0f} events/min")

        # Smoke detector: should not exceed 1 MiB/min per client.
        assert bytes_per_min < 1 * 1024 * 1024, (
            f"SSE bandwidth {bytes_per_min / 1024:.1f} KiB/min is excessive"
        )


# ---------------------------------------------------------------------------
# 2.  Full-summary payload — the initial load cost
# ---------------------------------------------------------------------------

class TestSummaryPayloadSize:
    """The /api/summary endpoint returns the full snapshot + history.
    This is the one-time cost on page load or SSE reconnect."""

    def test_summary_payload_size(self, live):
        _, _, body = live.request("GET", "/api/summary")
        kb = _size_kb_raw(body)
        print(f"\n    /api/summary:  {kb:.1f} KiB")

        # Smoke detector: 256 KiB is huge for a monitoring dashboard.
        assert kb < 256, f"/api/summary is {kb:.0f} KiB"

    def test_history_fraction_of_summary(self, live):
        """How much of the summary payload is history vs the latest snapshot?"""
        _, _, body = live.request("GET", "/api/summary")
        data = json.loads(body)

        latest_kb = _size_kb(data.get("latest", {}))
        history_kb = _size_kb(data.get("history", []))
        info_kb = _size_kb(data.get("info", {}))
        total_kb = _size_kb_raw(body)

        print(f"\n    Payload breakdown:  info={info_kb:.1f} KiB  "
              f"latest={latest_kb:.1f} KiB  history={history_kb:.1f} KiB  "
              f"total={total_kb:.1f} KiB")

        # History should dominate once the buffer is full, but it's a
        # timeseries of 7 numbers per point — it should stay reasonable.
        if data["history"]:
            points = len(data["history"])
            per_point = history_kb / points * 1024
            print(f"    History: {points} points × {per_point:.0f} B/point")

        # The latest snapshot is the expensive part (process list, disks, etc.)
        # Verify it's not accidentally embedding full history.
        if "history" in data.get("latest", {}):
            raise AssertionError("latest snapshot contains embedded history!")

    def test_summary_size_vs_client_count(self, live):
        """Project: N concurrent tablets all refreshing at once."""
        _, _, body = live.request("GET", "/api/summary")
        kb = _size_kb_raw(body)

        for n in (1, 2, 5, 10):
            print(f"    {n} client(s):  {n * kb:.0f} KiB burst  "
                  f"({n * kb / 1024:.2f} MiB)")


# ---------------------------------------------------------------------------
# 3.  Static assets — the one-time transfer
# ---------------------------------------------------------------------------

class TestStaticAssetSizes:
    """The dashboard page itself — HTML, CSS, JS — is transferred once
    and cached (via no-cache + conditional revalidation in practice)."""

    def test_static_asset_sizes(self, live):
        total = 0
        for path, label in [("/", "index.html"),
                            ("/static/app.js", "app.js"),
                            ("/static/app.css", "app.css")]:
            _, _, body = live.request("GET", path)
            kb = _size_kb_raw(body)
            total += kb
            print(f"\n    {label:20s} {kb:7.1f} KiB")
        print(f"    {'total (one-time)':20s} {total:7.1f} KiB  "
              f"({total / 1024:.2f} MiB)")

        # Smoke detector: total static assets under 512 KiB.
        assert total < 512, f"static assets total {total:.0f} KiB"


# ---------------------------------------------------------------------------
# 4.  In-process memory — the server's own footprint
# ---------------------------------------------------------------------------

class TestProcessMemory:
    """How much RAM does the dashtop python process actually use?"""

    def test_sampler_memory_footprint(self):
        """Measure memory before and after creating the sampler + history."""
        import psutil
        proc = psutil.Process(os.getpid())

        # Baseline (this test process before creating a sampler).
        baseline = proc.memory_info().rss

        sampler = dashtop.Sampler(interval=2.0, history_seconds=15 * 60)
        # Fill the history so we measure worst-case steady state.
        for _ in range(sampler.history.maxlen):
            sampler.history.append({
                "t": 0, "cpu": 0, "mem": 0,
                "dn": 0, "up": 0, "rd": 0, "wr": 0,
            })

        after = proc.memory_info().rss
        delta_mib = (after - baseline) / (1024 * 1024)

        print(f"\n    sampler memory delta: {delta_mib:+.1f} MiB  "
              f"(baseline={baseline / 1024 / 1024:.1f} MiB, "
              f"after={after / 1024 / 1024:.1f} MiB)")

        # Smoke detector: the sampler's steady-state memory should stay
        # under 50 MiB.  (The deque alone for 15 min @ 2 s = 450 entries
        # of ~7 floats → a few KiB.  The real cost is psutil caches.)
        assert delta_mib < 50, f"sampler memory delta {delta_mib:.1f} MiB"

    def test_single_snapshot_size_in_memory(self, live):
        """How large is a single full snapshot dict in memory?"""
        snap = live.sampler.latest
        assert snap is not None, "no snapshot available"
        kb = _size_kb(snap)
        print(f"\n    one snapshot (JSON-encoded): {kb:.1f} KiB")

        # A snapshot is dominated by the process list (up to 12 entries ×
        # 4 fields) and disk list.  Should stay under 64 KiB.
        assert kb < 64, f"snapshot is {kb:.0f} KiB"


# ---------------------------------------------------------------------------
# 5.  Sampling CPU cost — how expensive is each sample()
# ---------------------------------------------------------------------------

class TestSamplingCost:
    """How much CPU time does one sample() call consume?"""

    def test_sample_cpu_time(self):
        """Measure wall-clock time of repeated sample() calls."""
        sampler = dashtop.Sampler(interval=2.0, history_seconds=60)
        # Warm up — first call primes psutil caches.
        sampler.sample()

        times = []
        for _ in range(20):
            t0 = time.perf_counter()
            sampler.sample()
            times.append(time.perf_counter() - t0)

        avg = sum(times) / len(times)
        hi = max(times)
        lo = min(times)
        p50 = sorted(times)[len(times) // 2]
        p95 = sorted(times)[int(len(times) * 0.95)]

        print(f"\n    sample() cost:  avg={avg * 1000:.1f} ms  "
              f"p50={p50 * 1000:.1f} ms  p95={p95 * 1000:.1f} ms  "
              f"min={lo * 1000:.1f} ms  max={hi * 1000:.1f} ms  "
              f"(n={len(times)})")

        # Interval check: sample() must cost well under the default 2 s
        # interval or the sampler falls behind.
        cpu_pct = (avg / 2.0) * 100
        print(f"    CPU overhead:  {cpu_pct:.1f}% of one core at 2 s interval")

        # Smoke detector: a single sample must not wildly overshoot the
        # default 2 s interval.  On Windows psutil.process_iter() alone can
        # take 1–2 s; the honest numbers are printed above for review.
        assert hi < 5.0, f"sample() took {hi:.2f}s — catastrophic regression!"


# ---------------------------------------------------------------------------
# 6.  Total data transferred over a session — the cumulative cost
# ---------------------------------------------------------------------------

class TestSessionDataVolume:
    """Project how much data a tablet consumes over a typical monitoring
    session (initial load + N minutes of streaming)."""

    def test_data_volume_over_session(self, live):
        """Print a projection table for 5 / 15 / 60 minute sessions."""
        # Measure the one-time cost.
        _, _, summary_body = live.request("GET", "/api/summary")
        static_total = 0
        for path in ("/", "/static/app.js", "/static/app.css"):
            _, _, b = live.request("GET", path)
            static_total += len(b)

        import http.client
        sse_sizes = []
        conn = http.client.HTTPConnection(live.host, live.port, timeout=5)
        try:
            conn.request("GET", "/api/stream")
            resp = conn.getresponse()
            deadline = time.perf_counter() + FAST_INTERVAL * 10
            while time.perf_counter() < deadline and len(sse_sizes) < 8:
                line = resp.readline()
                if line.startswith(b"data: "):
                    sse_sizes.append(len(line))
        finally:
            conn.close()

        if len(sse_sizes) < 2:
            pytest.skip("not enough SSE frames")

        avg_sse = sum(sse_sizes) / len(sse_sizes)
        # At the *default* 2 s interval.
        events_per_min_default = 30

        onetime = static_total + len(summary_body)

        print(f"\n    One-time cost (static + summary):  {onetime / 1024:.1f} KiB")
        print(f"    Recurring cost (SSE, 2 s interval): {avg_sse * 30 / 1024:.1f} KiB/min")
        print(f"\n    Session projections (one client, default settings):")
        print(f"    {'Duration':<12} {'One-time':<12} {'SSE':<12} {'Total':<12}")
        for mins in (5, 15, 30, 60, 120):
            sse_total = avg_sse * events_per_min_default * mins
            total = onetime + sse_total
            print(f"    {f'{mins} min':<12} {onetime / 1024:>6.1f} KiB  "
                  f"{sse_total / 1024:>6.1f} KiB  {total / 1024:>7.1f} KiB")

        # Smoke detector: 1 hour should not transfer more than 50 MiB.
        hour_total = onetime + avg_sse * events_per_min_default * 60
        assert hour_total < 50 * 1024 * 1024, (
            f"1-hour projection: {hour_total / 1024 / 1024:.1f} MiB"
        )


# ---------------------------------------------------------------------------
# 7.  Impact of adding clients — multi-tablet cost
# ---------------------------------------------------------------------------

class TestMultiClientCost:
    """What happens when 2, 3, or 5 tablets connect simultaneously?
    Each SSE client gets its own copy of every event."""

    def test_multi_client_bandwidth(self, live):
        """Project total server bandwidth with N concurrent SSE clients."""
        import http.client

        sse_sizes = []
        conn = http.client.HTTPConnection(live.host, live.port, timeout=5)
        try:
            conn.request("GET", "/api/stream")
            resp = conn.getresponse()
            deadline = time.perf_counter() + FAST_INTERVAL * 10
            while time.perf_counter() < deadline and len(sse_sizes) < 5:
                line = resp.readline()
                if line.startswith(b"data: "):
                    sse_sizes.append(len(line))
        finally:
            conn.close()

        if not sse_sizes:
            pytest.skip("no SSE frames")

        avg_sse = sum(sse_sizes) / len(sse_sizes)
        # At default 2 s interval.
        events_per_min = 30

        print(f"\n    Server bandwidth by client count (default 2 s interval):")
        print(f"    {'Clients':<10} {'KiB/min':<12} {'kbps':<12} {'MiB/hr':<12}")
        for n in (1, 2, 3, 5, 10):
            kib_min = avg_sse * events_per_min * n / 1024
            kbps = avg_sse * 8 * events_per_min * n / 60
            mib_hr = kib_min * 60 / 1024
            print(f"    {n:<10} {kib_min:>7.1f}       {kbps / 1000:>6.1f}       {mib_hr:>6.1f}")

        # Smoke detector: 10 clients should not saturate a typical home
        # uplink.  50 kbps for 10 clients is ~5 KB/s — tiny.  We flag
        # only if it exceeds 10 Mbps (a real problem).
        ten_client_bps = avg_sse * 8 * events_per_min * 10 / 60
        assert ten_client_bps < 10_000_000, (
            f"10-client bandwidth {ten_client_bps / 1000:.1f} kbps exceeds 10 Mbps!"
        )
