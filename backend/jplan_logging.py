"""Small logging helpers for consistent JPlan terminal output."""

from __future__ import annotations

import json
from typing import Any, Optional


def jlog(module: str, message: str, stage: Optional[str] = None) -> None:
    """Print a compact JPlan log line with a stable module/stage prefix."""
    prefix = f"[JPLAN][{module}]"
    if stage:
        prefix += f"[{stage}]"
    print(f"{prefix} {message}")


def jjson(module: str, label: str, payload: Any, stage: Optional[str] = None) -> None:
    """Print JSON payloads under the same module/stage convention."""
    try:
        serialized = json.dumps(payload, indent=2, ensure_ascii=True)
    except Exception:
        serialized = repr(payload)
    jlog(module, f"{label}:\n{serialized}", stage)


def jsection(module: str, title: str, stage: Optional[str] = None) -> None:
    """Replace noisy ASCII banners with a single scannable section line."""
    jlog(module, f"START {title}", stage)
