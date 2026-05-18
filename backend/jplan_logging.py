"""Small logging helpers for consistent JPlan terminal output."""

from __future__ import annotations

import json
import os
from typing import Any, Optional


def jplan_verbose_enabled() -> bool:
    return os.getenv("JPLAN_VERBOSE_LOGS", "").strip().lower() in {"1", "true", "yes", "on"}


def jlog(module: str, message: str, stage: Optional[str] = None) -> None:
    """Print a compact JPlan log line with a stable module/stage prefix."""
    prefix = f"[JPLAN][{module}]"
    if stage:
        prefix += f"[{stage}]"
    print(f"{prefix} {message}")


def jlog_verbose(module: str, message: str, stage: Optional[str] = None) -> None:
    if jplan_verbose_enabled():
        jlog(module, message, stage)


def jjson(module: str, label: str, payload: Any, stage: Optional[str] = None) -> None:
    """Print JSON payloads under the same module/stage convention."""
    try:
        serialized = json.dumps(payload, indent=2, ensure_ascii=True)
    except Exception:
        serialized = repr(payload)
    jlog(module, f"{label}:\n{serialized}", stage)


def jjson_verbose(module: str, label: str, payload: Any, stage: Optional[str] = None) -> None:
    if jplan_verbose_enabled():
        jjson(module, label, payload, stage)


def jsection(module: str, title: str, stage: Optional[str] = None) -> None:
    """Replace noisy ASCII banners with a single scannable section line."""
    jlog(module, f"START {title}", stage)
