"""Security tests: the server must only ever read files under static/,
never mutate anything, and never hand untrusted markup to the page."""

import json
import os

import server as dashtop

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------- path traversal ----------

TRAVERSAL_PATHS = [
    "/static/../server.py",
    "/static/../../etc/passwd",
    "/static/../requirements.txt",
    "/static/....//server.py",
    "/static/..%2f..%2fserver.py",       # encoded dots stay literal — must 404
    "/static/%2e%2e/%2e%2e/etc/passwd",
    "/static//etc/passwd",               # absolute path smuggled into join()
    "/static//C:/Windows/win.ini",
    "/static/..\\..\\server.py",
]


def test_path_traversal_is_blocked(live):
    with open(os.path.join(BASE, "server.py"), "rb") as fh:
        server_source = fh.read()
    for path in TRAVERSAL_PATHS:
        status, _, body = live.request("GET", path)
        assert status in (403, 404), f"{path} returned {status}"
        assert server_source not in body, f"{path} leaked server.py"
        assert b"root:" not in body, f"{path} leaked /etc/passwd"


def test_no_directory_listing(live):
    for path in ("/static/", "/static/.", "/static/.."):
        status, _, body = live.request("GET", path)
        assert status in (403, 404), f"{path} returned {status}"
        assert b"app.js" not in body, f"{path} listed directory contents"


def test_legitimate_static_files_still_served(live):
    for path, marker in [("/", b"dashtop"), ("/static/app.js", b"use strict"),
                         ("/static/app.css", b"--surface-1")]:
        status, _, body = live.request("GET", path)
        assert status == 200, f"{path} returned {status}"
        assert marker in body


# ---------- attack surface: read-only, GET-only ----------

def test_write_methods_are_not_implemented(live):
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        status, _, _ = live.request(method, "/api/summary")
        assert status == 501, f"{method} returned {status}, expected 501"


def test_handler_defines_no_mutating_methods():
    for name in ("do_POST", "do_PUT", "do_DELETE", "do_PATCH"):
        assert not hasattr(dashtop.Handler, name), f"Handler unexpectedly defines {name}"


def test_unknown_routes_404(live):
    for path in ("/api", "/api/", "/api/exec", "/admin", "/.git/config", "/favicon.ico"):
        status, _, _ = live.request("GET", path)
        assert status == 404, f"{path} returned {status}"


# ---------- response hygiene ----------

def test_api_content_type_and_caching(live):
    status, headers, body = live.request("GET", "/api/summary")
    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert headers["Cache-Control"] == "no-cache"
    json.loads(body)  # must be valid JSON, not HTML


def test_summary_exposes_no_secrets(live):
    _, _, body = live.request("GET", "/api/summary")
    data = json.loads(body)
    # only the documented keys — nothing extra creeping into the payload
    assert set(data.keys()) == {"info", "interval", "latest", "history"}
    assert set(data["info"].keys()) <= {
        "hostname", "os", "machine", "cpu_count", "cpu_count_physical",
        "mem_total", "boot_time", "lan_ip",
    }
    # process entries carry name/pid/cpu/mem only — never cmdline, env or paths
    for proc in data["latest"]["procs"]:
        assert set(proc.keys()) == {"pid", "name", "cpu", "mem"}


def test_process_names_are_length_capped(live):
    """The UI renders process names; the server caps them so a hostile
    process name can't balloon the payload."""
    snap = live.sampler.sample()
    for proc in snap["procs"]:
        assert len(proc["name"]) <= 48


# ---------- frontend XSS guard ----------

def test_frontend_never_uses_innerhtml():
    """Process names and mount points are untrusted; the page must build DOM
    via textContent, never innerHTML/outerHTML/document.write."""
    with open(os.path.join(BASE, "static", "app.js"), encoding="utf-8") as fh:
        js = fh.read()
    for banned in ("innerHTML", "outerHTML", "document.write", "insertAdjacentHTML"):
        assert banned not in js, f"app.js uses {banned}"
    assert "textContent" in js
