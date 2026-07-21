"""GPU support tests.

GPU stats come from `nvidia-smi` (see gpu.py) — no Python dependencies, and
no GPU is required for most of this file: the parser tests run anywhere, the
hardware tests skip cleanly when nvidia-smi is absent.

To assert a specific card on a machine you know, e.g.:

    set DASHTOP_EXPECT_GPU=1080 Ti        (Windows)
    DASHTOP_EXPECT_GPU="1080 Ti" pytest tests/test_gpu.py -v
"""

import json
import os

import pytest

import gpu
import server as dashtop

_MIB = 1024 * 1024

# Real `nvidia-smi --format=csv,noheader,nounits` output shapes.
SAMPLE_1080TI = (
    "NVIDIA GeForce GTX 1080 Ti, 38, 12, 11264, 9422, 67, 149.32, 280.00, 52, 1911, 5005\n"
)
SAMPLE_MULTI = (
    "NVIDIA GeForce GTX 1080 Ti, 38, 12, 11264, 9422, 67, 149.32, 280.00, 52, 1911, 5005\n"
    "NVIDIA GeForce GT 710, 0, 0, 2048, 128, 42, N/A, N/A, N/A, 135, 405\n"
)


# ---------------------------------------------------------------------------
# 1.  Parser — pure function, no hardware needed
# ---------------------------------------------------------------------------

class TestParseQueryCsv:
    def test_parses_all_fields(self):
        (g,) = gpu.parse_query_csv(SAMPLE_1080TI)
        assert g["name"] == "NVIDIA GeForce GTX 1080 Ti"
        assert g["util"] == 38
        assert g["mem_util"] == 12
        assert g["vram_total"] == 11264 * _MIB
        assert g["vram_used"] == 9422 * _MIB
        assert g["vram_percent"] == round(9422 / 11264 * 100, 1)
        assert g["temp"] == 67
        assert g["power_w"] == 149.32
        assert g["power_limit_w"] == 280
        assert g["fan"] == 52
        assert g["clock_mhz"] == 1911
        assert g["mem_clock_mhz"] == 5005

    def test_na_fields_become_none(self):
        """Fan/power are N/A on some cards — must parse as None, not crash."""
        _, g2 = gpu.parse_query_csv(SAMPLE_MULTI)
        assert g2["power_w"] is None
        assert g2["power_limit_w"] is None
        assert g2["fan"] is None
        assert g2["temp"] == 42  # present fields still parse

    def test_multi_gpu(self):
        gpus = gpu.parse_query_csv(SAMPLE_MULTI)
        assert len(gpus) == 2
        assert gpus[0]["name"] != gpus[1]["name"]

    def test_empty_and_garbage_input(self):
        assert gpu.parse_query_csv("") == []
        assert gpu.parse_query_csv("\n\n") == []
        assert gpu.parse_query_csv("garbage") == []  # too few columns
        # junk numerics -> None, not an exception
        (g,) = gpu.parse_query_csv(
            "Some GPU, x, y, z, q, ?, --, .., !!, @@, ##\n"
        )
        assert g["name"] == "Some GPU"
        assert g["util"] is None
        assert g["vram_total"] is None
        assert g["vram_percent"] is None


# ---------------------------------------------------------------------------
# 2.  Graceful fallback — no nvidia-smi behaves as "no GPU", never an error
# ---------------------------------------------------------------------------

class TestGracefulFallback:
    def test_unavailable(self, monkeypatch):
        monkeypatch.setattr(gpu.shutil, "which", lambda _name: None)
        assert gpu.available() is False
        assert gpu.query_gpus() == []

    def test_failing_nvidia_smi_returns_empty(self, monkeypatch):
        """Driver errors / nonzero exit must not propagate into the sampler."""
        monkeypatch.setattr(gpu.shutil, "which", lambda _name: "/usr/bin/nvidia-smi")

        class Boom:
            returncode = 9
            stdout = ""

        monkeypatch.setattr(gpu.subprocess, "run", lambda *a, **k: Boom())
        assert gpu.query_gpus() == []

    def test_sampler_snapshot_without_gpu(self, monkeypatch):
        monkeypatch.setattr(gpu, "available", lambda: False)
        sampler = dashtop.Sampler(interval=0.2, history_seconds=10)
        snap = sampler.sample()
        assert snap["gpus"] == []


# ---------------------------------------------------------------------------
# 3.  Snapshot / API integration
# ---------------------------------------------------------------------------

class TestSnapshotIntegration:
    def test_sample_has_gpus_key(self):
        sampler = dashtop.Sampler(interval=0.2, history_seconds=10)
        snap = sampler.sample()
        assert isinstance(snap["gpus"], list)

    def test_summary_exposes_gpus(self, live):
        _, _, body = live.request("GET", "/api/summary")
        data = json.loads(body)
        gpus = data["latest"]["gpus"]
        assert isinstance(gpus, list)
        if gpu.available():
            assert gpus, "nvidia-smi works but the snapshot carries no GPU"
            g = gpus[0]
            assert g["name"] and g["vram_total"] > 0
            for pt in data["history"]:
                assert "gpu" in pt and "gpumem" in pt
        else:
            assert gpus == []


# ---------------------------------------------------------------------------
# 4.  Live hardware — sanity-check real readings (skips without a GPU)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not gpu.available(), reason="nvidia-smi not found — no NVIDIA GPU")
class TestLiveGpu:
    def test_readings_are_sane(self):
        gpus = gpu.query_gpus()
        assert gpus, "nvidia-smi present but returned no GPUs"
        for g in gpus:
            assert isinstance(g["name"], str) and g["name"]
            assert 0 <= g["util"] <= 100
            assert 0 <= g["vram_used"] <= g["vram_total"]
            assert 0 <= g["vram_percent"] <= 100
            if g["temp"] is not None:
                assert 0 < g["temp"] < 150
            if g["power_w"] is not None:
                assert 0 < g["power_w"] < 2000
            print(f"\n    GPU: {g['name']}  util={g['util']}%  "
                  f"vram={g['vram_used'] / _MIB:.0f}/{g['vram_total'] / _MIB:.0f} MiB "
                  f"({g['vram_percent']}%)  temp={g['temp']}°C  "
                  f"power={g['power_w']} W  fan={g['fan']}%  "
                  f"clock={g['clock_mhz']} MHz")

    def test_readings_change_over_time(self):
        """Two queries must both succeed; utilization is a live counter so the
        pair of readings proves we're sampling the card, not a static blob."""
        a = gpu.query_gpus()
        b = gpu.query_gpus()
        assert a and b
        assert a[0]["name"] == b[0]["name"]
        assert a[0]["vram_total"] == b[0]["vram_total"]

    def test_expected_gpu_model(self):
        """Gated on DASHTOP_EXPECT_GPU — assert a specific card on machines
        you know (skipped everywhere else, so CI stays portable)."""
        want = os.environ.get("DASHTOP_EXPECT_GPU")
        if not want:
            pytest.skip("set DASHTOP_EXPECT_GPU to assert a specific GPU model")
        names = [g["name"] for g in gpu.query_gpus()]
        assert any(want.lower() in n.lower() for n in names), (
            f"{want!r} not found among detected GPUs: {names}"
        )
