"""Tiny name -> class registry so providers can be chosen from config.

Usage::

    @register("asr", "gemini")
    class GeminiASR(ASRProvider): ...

    provider_cls = get("asr", "gemini")
"""

from __future__ import annotations

from collections.abc import Callable

_REGISTRY: dict[str, dict[str, type]] = {}


def register(kind: str, name: str) -> Callable[[type], type]:
    """Class decorator registering ``cls`` under ``(kind, name)``."""

    def decorator(cls: type) -> type:
        _REGISTRY.setdefault(kind, {})[name] = cls
        return cls

    return decorator


def get(kind: str, name: str) -> type:
    """Return the class registered under ``(kind, name)`` or raise KeyError."""
    try:
        return _REGISTRY[kind][name]
    except KeyError:
        known = ", ".join(available(kind)) or "none"
        raise KeyError(f"No {kind!r} provider named {name!r} (available: {known})") from None


def available(kind: str) -> list[str]:
    """Names of all providers registered for ``kind``, sorted."""
    return sorted(_REGISTRY.get(kind, {}))
