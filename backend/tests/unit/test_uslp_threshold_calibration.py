"""Unit tests for the USLP threshold calibration memory model.

Verifies the GPU VRAM calculations in
``igea/management/commands/calibrate_uslp_thresholds.py``:

  1. ``compute_max_safe_pool_size`` — solves for the max pool size N
     that keeps ``head_batch_size`` feasible given GPU VRAM.
  2. ``compute_adaptive_batch`` — replicates
     ``TorchUSLP._adaptive_head_batch_size`` for a given pool size.
  3. Cross-check: the max safe pool size should produce an adaptive
     batch >= the requested head batch size.
  4. Known-GPU sanity checks: RTX 4070 Ti SUPER (16 GB) and
     RTX 2070 (8 GB) should produce expected ballparks.

These tests do NOT require a database or GPU — they test the pure
math of the memory model.

Memory model (from TorchUSLP source):
  Persistent per pool entity:  844 bytes
  Peak per (head, entity):     24 bytes  (6 float32 tensors)
  FastText overhead:           200 MB
  Safety factor:               0.70
"""

import os

import pytest

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
django.setup()

from igea.management.commands.calibrate_uslp_thresholds import (
    compute_max_safe_pool_size,
    compute_adaptive_batch,
    FASTTEXT_OVERHEAD_BYTES,
    PERSISTENT_BYTES_PER_ENTITY,
    PEAK_BYTES_PER_HEAD_PER_ENTITY,
    VRAM_SAFETY_FACTOR,
    DEFAULT_HEAD_BATCH_SIZE,
)


# ── Constants ────────────────────────────────────────────────────────────

GB = 1024 ** 3
RTX_4070_TI_SUPER = int(15.6 * GB)   # 16,743,280,640 bytes
RTX_2070 = int(7.6 * GB)             # 8,159,746,048 bytes


# ── compute_max_safe_pool_size ───────────────────────────────────────────


class TestComputeMaxSafePoolSize:
    """Tests for the max safe pool size solver."""

    def test_rtx_4070_ti_super_16gb_head_batch_256(self):
        """RTX 4070 Ti SUPER (15.6 GB) with head_batch=256 should allow
        a pool of ~1.5–2.5M entities (well above the current 200K limit)."""
        result = compute_max_safe_pool_size(
            gpu_total_bytes=RTX_4070_TI_SUPER,
            head_batch_size=256,
        )
        assert result["max_pool"] > 1_000_000, (
            f"Expected >1M max pool on 16GB GPU, got {result['max_pool']:,}"
        )
        assert result["max_pool"] < 3_000_000, (
            f"Expected <3M max pool on 16GB GPU, got {result['max_pool']:,}"
        )
        # Adaptive batch at max pool should be >= 256
        assert result["adaptive_batch"] >= 256, (
            f"Adaptive batch {result['adaptive_batch']} < 256 at max pool"
        )

    def test_rtx_2070_8gb_head_batch_256(self):
        """RTX 2070 (7.6 GB) with head_batch=256 should allow
        a pool of ~700K–1M entities."""
        result = compute_max_safe_pool_size(
            gpu_total_bytes=RTX_2070,
            head_batch_size=256,
        )
        assert result["max_pool"] > 500_000, (
            f"Expected >500K max pool on 8GB GPU, got {result['max_pool']:,}"
        )
        assert result["max_pool"] < 1_200_000, (
            f"Expected <1.2M max pool on 8GB GPU, got {result['max_pool']:,}"
        )

    def test_larger_head_batch_reduces_max_pool(self):
        """A larger head batch requires more VRAM per batch, so the max
        safe pool size should decrease."""
        r256 = compute_max_safe_pool_size(RTX_4070_TI_SUPER, head_batch_size=256)
        r512 = compute_max_safe_pool_size(RTX_4070_TI_SUPER, head_batch_size=512)
        r1024 = compute_max_safe_pool_size(RTX_4070_TI_SUPER, head_batch_size=1024)
        assert r256["max_pool"] > r512["max_pool"] > r1024["max_pool"], (
            f"Max pool should decrease with larger head batch: "
            f"{r256['max_pool']:,} > {r512['max_pool']:,} > {r1024['max_pool']:,}"
        )

    def test_zero_gpu_returns_zero(self):
        """A GPU with 0 bytes (CPU fallback) should return max_pool=0."""
        result = compute_max_safe_pool_size(0, head_batch_size=256)
        assert result["max_pool"] == 0
        assert result["adaptive_batch"] == 0

    def test_insufficient_vram_returns_zero(self):
        """A GPU with less VRAM than the FastText overhead should return 0."""
        result = compute_max_safe_pool_size(
            FASTTEXT_OVERHEAD_BYTES - 1,
            head_batch_size=256,
        )
        assert result["max_pool"] == 0

    def test_persistent_vram_scales_linearly(self):
        """Persistent VRAM should be ~844 bytes per pool entity."""
        result = compute_max_safe_pool_size(RTX_4070_TI_SUPER, head_batch_size=256)
        expected_persistent = result["max_pool"] * PERSISTENT_BYTES_PER_ENTITY
        assert abs(result["persistent_vram_gb"] * GB - expected_persistent) < GB * 0.01, (
            f"Persistent VRAM mismatch: {result['persistent_vram_gb']:.2f} GB "
            f"vs expected {expected_persistent / GB:.2f} GB"
        )

    def test_free_vram_plus_persistent_equals_available(self):
        """free_vram + persistent_vram should equal (total - fasttext)."""
        result = compute_max_safe_pool_size(RTX_4070_TI_SUPER, head_batch_size=256)
        available = RTX_4070_TI_SUPER - FASTTEXT_OVERHEAD_BYTES
        total = result["free_vram_gb"] * GB + result["persistent_vram_gb"] * GB
        assert abs(total - available) < GB * 0.01, (
            f"free + persistent ({total / GB:.2f} GB) != available ({available / GB:.2f} GB)"
        )


# ── compute_adaptive_batch ───────────────────────────────────────────────


class TestComputeAdaptiveBatch:
    """Tests for the adaptive batch size calculator."""

    def test_200k_pool_on_16gb(self):
        """200K pool on 16GB GPU should allow a large adaptive batch (>1000)."""
        batch = compute_adaptive_batch(200_000, RTX_4070_TI_SUPER)
        assert batch > 1000, f"Expected >1000 batch for 200K pool on 16GB, got {batch}"

    def test_1m_pool_on_16gb(self):
        """1M pool on 16GB GPU should still allow batch >= 256."""
        batch = compute_adaptive_batch(1_000_000, RTX_4070_TI_SUPER)
        assert batch >= 256, f"Expected >=256 batch for 1M pool on 16GB, got {batch}"

    def test_2m_pool_on_16gb(self):
        """2M pool on 16GB GPU should have a reduced batch (< 256)."""
        batch = compute_adaptive_batch(2_000_000, RTX_4070_TI_SUPER)
        assert batch < 256, f"Expected <256 batch for 2M pool on 16GB, got {batch}"

    def test_200k_pool_on_8gb(self):
        """200K pool on 8GB GPU should allow batch > 500."""
        batch = compute_adaptive_batch(200_000, RTX_2070)
        assert batch > 500, f"Expected >500 batch for 200K pool on 8GB, got {batch}"

    def test_1m_pool_on_8gb(self):
        """1M pool on 8GB GPU should have a reduced batch (< 256)."""
        batch = compute_adaptive_batch(1_000_000, RTX_2070)
        assert batch < 256, f"Expected <256 batch for 1M pool on 8GB, got {batch}"

    def test_zero_pool_returns_default(self):
        """A pool of 0 should return the default head batch size."""
        batch = compute_adaptive_batch(0, RTX_4070_TI_SUPER)
        assert batch == DEFAULT_HEAD_BATCH_SIZE

    def test_larger_pool_reduces_batch(self):
        """Adaptive batch should decrease as pool size increases."""
        batches = [
            compute_adaptive_batch(n, RTX_4070_TI_SUPER)
            for n in [100_000, 200_000, 500_000, 1_000_000, 2_000_000]
        ]
        for i in range(len(batches) - 1):
            assert batches[i] >= batches[i + 1], (
                f"Batch should decrease: {batches[i]} >= {batches[i + 1]}"
            )

    def test_matches_torchuslp_formula(self):
        """Verify our formula matches TorchUSLP._adaptive_head_batch_size.

        The source code computes:
            free = total - allocated
            bytes_per_head = N * 4 * 6
            safe = max(1, int(free * 0.70 / bytes_per_head))
        """
        N = 200_000
        total = RTX_4070_TI_SUPER
        # Simulate: allocated = fasttext + persistent
        allocated = FASTTEXT_OVERHEAD_BYTES + N * PERSISTENT_BYTES_PER_ENTITY
        free = total - allocated
        bytes_per_head = N * 4 * 6
        expected = max(1, int(free * 0.70 / bytes_per_head))

        actual = compute_adaptive_batch(N, total)
        assert actual == expected, (
            f"Formula mismatch: got {actual}, expected {expected}"
        )


# ── Cross-checks ─────────────────────────────────────────────────────────


class TestCrossCheck:
    """Cross-check: max safe pool should produce adaptive batch >= requested."""

    def test_max_pool_provides_requested_batch_16gb(self):
        """At the max safe pool size, adaptive batch should be >= requested."""
        for head_batch in [128, 256, 512, 1024]:
            result = compute_max_safe_pool_size(RTX_4070_TI_SUPER, head_batch)
            assert result["adaptive_batch"] >= head_batch, (
                f"head_batch={head_batch}: adaptive={result['adaptive_batch']} "
                f"< requested at max_pool={result['max_pool']:,}"
            )

    def test_max_pool_provides_requested_batch_8gb(self):
        """Same cross-check for the 8GB GPU."""
        for head_batch in [128, 256, 512]:
            result = compute_max_safe_pool_size(RTX_2070, head_batch)
            assert result["adaptive_batch"] >= head_batch, (
                f"head_batch={head_batch}: adaptive={result['adaptive_batch']} "
                f"< requested at max_pool={result['max_pool']:,}"
            )

    def test_current_200k_limit_is_safe_on_16gb(self):
        """The current limit=200,000 should be well within safe bounds on 16GB."""
        batch = compute_adaptive_batch(200_000, RTX_4070_TI_SUPER)
        assert batch >= 256, (
            f"Current limit 200K should allow batch>=256 on 16GB, got {batch}"
        )

    def test_current_200k_limit_is_safe_on_8gb(self):
        """The current limit=200,000 should be safe on 8GB too."""
        batch = compute_adaptive_batch(200_000, RTX_2070)
        assert batch >= 256, (
            f"Current limit 200K should allow batch>=256 on 8GB, got {batch}"
        )


# ── Memory model constants ───────────────────────────────────────────────


class TestMemoryModelConstants:
    """Verify the memory model constants match the TorchUSLP source code."""

    def test_persistent_bytes_per_entity(self):
        """844 bytes = 400 (name emb) + 400 (class emb) + 8 (coords)
        + 24 (3 geohash matrices × 8) + 12 (3 d_max × 4)."""
        expected = 400 + 400 + 8 + (3 * 8) + (3 * 4)
        assert PERSISTENT_BYTES_PER_ENTITY == expected, (
            f"Persistent bytes: {PERSISTENT_BYTES_PER_ENTITY} != {expected}"
        )

    def test_peak_bytes_per_head_per_entity(self):
        """24 bytes = 6 simultaneous (H,N) float32 tensors × 4 bytes."""
        expected = 6 * 4
        assert PEAK_BYTES_PER_HEAD_PER_ENTITY == expected, (
            f"Peak bytes: {PEAK_BYTES_PER_HEAD_PER_ENTITY} != {expected}"
        )

    def test_safety_factor_matches_source(self):
        """Safety factor should be 0.70 (matches _adaptive_head_batch_size)."""
        assert VRAM_SAFETY_FACTOR == 0.70
