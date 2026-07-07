"""Shared fixture: boot a real dashtop server on an ephemeral port."""

import http.client
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as dashtop  # noqa: E402

FAST_INTERVAL = 0.2  # sample fast so tests don't wait on the 2s default


class Live:
    def __init__(self, host, port, sampler, httpd):
        self.host = host
        self.port = port
        self.sampler = sampler
        self.httpd = httpd

    def request(self, method, path, timeout=5):
        """Raw request via http.client — it does NOT normalize the path,
        which is exactly what the traversal tests need."""
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            conn.request(method, path)
            resp = conn.getresponse()
            body = resp.read()
            return resp.status, dict(resp.getheaders()), body
        finally:
            conn.close()


@pytest.fixture(scope="session")
def live():
    sampler = dashtop.Sampler(interval=FAST_INTERVAL, history_seconds=60)
    sampler.start()
    dashtop.Handler.sampler = sampler
    dashtop.Handler.info = dashtop.static_info()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), dashtop.Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    deadline = time.time() + 10
    while sampler.latest is None and time.time() < deadline:
        time.sleep(0.05)
    assert sampler.latest is not None, "sampler produced no snapshot within 10s"

    yield Live("127.0.0.1", httpd.server_address[1], sampler, httpd)
    httpd.shutdown()
