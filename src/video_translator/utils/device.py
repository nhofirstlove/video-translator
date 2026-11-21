"""Compute device selection with CPU fallback."""

from __future__ import annotations


def resolve_device(pref: str = "auto") -> str:
    """Map a preference (``auto``, ``cuda`` or ``cpu``) to ``"cuda"`` or ``"cpu"``.

    Falls back to CPU when CUDA is requested but torch or a GPU is unavailable.
    """
    if pref == "cpu":
        return "cpu"
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:  # torch missing or broken: run on CPU
        return "cpu"
