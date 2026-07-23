import os
import time
import torch

from igea.services.torch_uslp_service import TorchUSLP


def main():
    # Optional: configure PyTorch CUDA allocator for fragmentation resilience
    # This respects the PYTORCH_CUDA_ALLOC_CONF env var if set by the caller.

    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    print(f"Using device: {device}")

    # Synthetic parameters: ~1.3M candidates, ~40k heads
    pool_size = 1_300_000
    head_count = 40_000

    # Minimal dummy candidate pool compatible with GPUAcceleratedUSLP
    pool = [
        {
            "osm_id": i,
            "lat": -35.5 + (i % 1000) * 1e-4,
            "lon": 149.0 + (i % 1000) * 1e-4,
            "tags": {"name": f"candidate_{i}"},
            "wkg_class": "wkgs:Place",
        }
        for i in range(pool_size)
    ]

    # Minimal heads: one literal tag that maps to a known USLP relation
    heads = [
        {
            "osm_id": 10_000_000 + i,
            "lat": -35.5,
            "lon": 149.0,
            "tags": {"addr:city": f"head_{i}"},
        }
        for i in range(head_count)
    ]

    print(f"Building TorchUSLP service and loading pool of {pool_size:,} candidates...")
    service = TorchUSLP(device=device, head_batch_size=256)

    t0 = time.time()
    n_loaded = service.load_candidate_pool(pool)
    t1 = time.time()
    print(f"Loaded {n_loaded:,} candidates in {t1 - t0:.1f}s")

    if device.startswith("cuda"):
        print(
            f"GPU memory after pool load: "
            f"allocated={torch.cuda.memory_allocated() / 1024**3:.2f} GB, "
            f"reserved={torch.cuda.memory_reserved() / 1024**3:.2f} GB"
        )

    print(f"Scoring {head_count:,} synthetic heads in batches...")
    t2 = time.time()
    links = service.predict_links_batch(heads, threshold=0.7, top_k=5)
    t3 = time.time()

    print(f"Prediction complete: {len(links):,} links in {t3 - t2:.1f}s")
    if links:
        print("Sample link:", links[0])


if __name__ == "__main__":
    main()
