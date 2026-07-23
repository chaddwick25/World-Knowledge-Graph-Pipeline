"""
Pipeline exceptions for flow control.

These allow Celery tasks to signal non-error conditions:
- Entropy gate blocked → skip remaining pipeline steps gracefully
- No pre-trained pickle → fall back to FastText-only encoding

Additional dispatch-level exceptions for concurrency control:
- PipelineDispatchError → base for dispatch failures
- PipelineAlreadyRunning → duplicate run rejection
"""

# TODO: Refactor this entire file to use a more structured approach
class SkipPipeline(Exception):
    """Raise to skip the remaining pipeline steps without marking as failure.

    Used when the entropy gate blocks processing or when a country has
    no meaningful data to process.
    """
    def __init__(self, reason: str, detail: dict = None):
        self.reason = reason
        self.detail = detail or {}
        super().__init__(f"Pipeline skipped: {reason}")


class EntropyGateBlocked(SkipPipeline):
    """Entropy below threshold — region is semantically homogeneous."""
    def __init__(self, entropy: float, threshold: float, delta: float = None):
        detail = {
            "entropy": entropy,
            "threshold": threshold,
            "delta": delta,
        }
        super().__init__(
            f"Entropy {entropy:.3f} < threshold {threshold}",
            detail=detail,
        )


class NoPretrainedModel(SkipPipeline):
    """Country has no pre-trained GeoVectors pickle — using FastText only.

    This is expected for countries not covered by the original GeoVectors paper.
    Step 5 (train_gv_nle) will generate the first authoritative embeddings.
    """
    def __init__(self, iso: str):
        super().__init__(
            f"No pre-trained NLE model for {iso} — FastText only",
            detail={"iso": iso, "has_pretrained_nle": False},
        )


class PipelineDispatchError(Exception):
    """Raised when a pipeline cannot be dispatched."""
    pass


class PipelineAlreadyRunning(PipelineDispatchError):
    """Raised when a pipeline is already running for a country."""
    pass
