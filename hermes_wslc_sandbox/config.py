"""Container naming and config shapers.

Hermes-free on purpose: these are the rules a reviewer will want to check (what a container
is named, which host variables may enter it), and keeping them out of the Hermes-importing
modules means they are provable without a Hermes install.
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Any, Iterable, Mapping, Optional

#: Containers are host-visible objects, so names get the conservative treatment.
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")

#: wslc accepts 63 characters for a container name, like Docker.
MAX_NAME_LENGTH = 63


def as_bool(value: Any, *, default: bool) -> bool:
    """Booleans from Hermes config arrive as bools, but YAML/env spellings are common too."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def container_name(task_id: str, *, persistent: bool, prefix: str = "hermes-wslc") -> str:
    """Container name for a task.

    A persistent name must be reproducible across processes — that is what makes the sandbox
    resumable — so it carries no random part. A non-persistent one gets a per-run suffix so
    two sessions that share a task id cannot adopt each other's sandbox, and so a leftover
    from a crashed run is never mistaken for the current one.
    """
    safe = _UNSAFE_NAME_CHARS.sub("-", str(task_id or "default")).strip("-.") or "default"
    if persistent:
        return f"{prefix}-{safe}"[:MAX_NAME_LENGTH]
    return f"{prefix}-{safe}-{uuid.uuid4().hex[:8]}"[:MAX_NAME_LENGTH]


def resolve_env(forward_env: Optional[Iterable[str]] = None,
                env: Optional[Mapping[str, Any]] = None) -> dict[str, str]:
    """Merge Hermes' two env channels for the container: named host variables, then explicit
    values.

    Order matters: an explicitly configured value wins, and a named variable that is unset on
    the host is skipped rather than forwarded as an empty string (forwarding an empty value
    would silently shadow a container-side default).
    """
    resolved: dict[str, str] = {}
    for name in forward_env or ():
        name = str(name).strip()
        if not name:
            continue
        value = os.environ.get(name)
        if value is not None:
            resolved[name] = value
    for key, value in (env or {}).items():
        resolved[str(key)] = "" if value is None else str(value)
    return resolved
