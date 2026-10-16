"""WSL Containers (wslc) CLI adapter.

Pure-stdlib layer that every other module in this package builds on, so it can be
unit-tested without a Hermes checkout on the path.

Two wslc behaviours shape the whole module:

* **No Docker Go templates.** ``docker ... --format '{{.ID}}'`` is rejected; the CLI
  implements ``--format json`` only. Every machine-readable read here goes through
  JSON (``ps`` emits NDJSON, one object per line; ``inspect`` emits an array).
* **Human-readable output is localized.** ``wslc`` follows the Windows display
  language, so nothing may parse human text — not even error strings, which is why
  failures are classified by return code, never by message matching.

Flag support is probed rather than assumed: ``run --help`` is parsed once per
binary and cached, so a future wslc that grows ``--cap-drop`` gets it automatically
instead of being permanently pinned to today's limitation.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from typing import Any, Iterable, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

#: Environment override, checked before PATH.
BINARY_ENV = "HERMES_WSLC_BINARY"

#: Windows install locations, in probe order. ``wslc.exe`` is the CLI; ``container.exe``
#: ships beside it as an alias and is accepted as a fallback.
WINDOWS_SEARCH_PATHS = (
    r"C:\Program Files\WSL\wslc.exe",
    r"C:\Program Files\WSL\container.exe",
)

#: Docker-backend flags with no wslc equivalent. Harmless to omit on Docker, load-bearing
#: on wslc: ``run`` exits 1 on an unknown option, so a backend that passes them blindly
#: cannot start a container at all. Listed here so the capability report can name what a
#: wslc sandbox does *not* get, instead of silently pretending parity.
HARDENING_FLAGS = ("--security-opt", "--cap-drop", "--pids-limit", "--init", "--storage-opt")

#: Default command that keeps a detached container alive between ``exec`` calls.
KEEPALIVE_ARGV = ("sleep", "infinity")


class WslcError(RuntimeError):
    """The wslc CLI could not be resolved, or a call that had to succeed failed."""


def _decode(data: bytes) -> str:
    """Localized CLI text arrives in the console codepage; flags and JSON are ASCII."""
    for encoding in ("utf-8", "mbcs", "cp1252"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def run_capture(argv: Sequence[str], *, timeout: float, env: Optional[Mapping[str, str]] = None,
                ) -> subprocess.CompletedProcess:
    """Capture a CLI call. Bytes are decoded here rather than by ``text=True`` so a
    non-UTF-8 console codepage cannot raise and turn a healthy call into a crash."""
    proc = subprocess.run(list(argv), capture_output=True, timeout=timeout, check=False,
                          stdin=subprocess.DEVNULL, env=None if env is None else dict(env))
    return subprocess.CompletedProcess(
        list(argv), proc.returncode, _decode(proc.stdout or b""), _decode(proc.stderr or b""))


def _is_executable_file(path: str) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def resolve_wslc(explicit: Optional[str] = None) -> str:
    """Resolve the wslc CLI: explicit argument, ``HERMES_WSLC_BINARY``, PATH, then the
    default Windows install locations. Raises :class:`WslcError` when nothing is found.
    """
    if explicit:
        if _is_executable_file(explicit) or shutil.which(explicit):
            return explicit
        raise WslcError(f"wslc binary not usable: {explicit!r}")

    override = (os.environ.get(BINARY_ENV) or "").strip()
    if override:
        if _is_executable_file(override) or shutil.which(override):
            return override
        raise WslcError(f"{BINARY_ENV} points at {override!r}, which is not executable")

    for name in ("wslc", "wslc.exe", "container.exe"):
        if found := shutil.which(name):
            return found

    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    for candidate in WINDOWS_SEARCH_PATHS:
        candidate = candidate.replace(r"C:\Program Files", program_files, 1)
        if _is_executable_file(candidate):
            return candidate

    raise WslcError(
        "WSL Containers CLI (wslc) was not found. It ships with WSL 3.0.3+ "
        "(`wsl --update`), and `wslc --version` should work in a terminal. "
        f"Set {BINARY_ENV} to point at the binary explicitly.")


def version(exe: str, *, timeout: float = 10) -> str:
    """``wslc --version`` output, first line. Empty string when the call fails."""
    result = run_capture([exe, "--version"], timeout=timeout)
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip().splitlines()[0].strip() if result.stdout.strip() else ""


_supported_flags_cache: dict[str, frozenset[str]] = {}


def supported_run_flags(exe: str, *, timeout: float = 10) -> frozenset[str]:
    """Options ``wslc run`` accepts, parsed from its help text and cached per binary.

    Help text is localized, but the option names themselves are ASCII, so scanning the
    raw text is language-independent. A failed probe yields an empty set, which callers
    read as "assume nothing beyond the documented core flags".
    """
    if exe in _supported_flags_cache:
        return _supported_flags_cache[exe]
    flags: set[str] = set()
    try:
        result = run_capture([exe, "run", "--help"], timeout=timeout)
        for token in (result.stdout or "").replace(",", " ").split():
            token = token.strip()
            if token.startswith("--") and len(token) > 2 and token[2].isalpha():
                flags.add(token)
    except Exception:
        logger.debug("wslc flag probe failed for %s", exe, exc_info=True)
    resolved = frozenset(flags)
    _supported_flags_cache[exe] = resolved
    return resolved


def missing_hardening_flags(exe: str, *, timeout: float = 10) -> tuple[str, ...]:
    """Docker-hardening flags this wslc build does not implement."""
    supported = supported_run_flags(exe, timeout=timeout)
    if not supported:
        return HARDENING_FLAGS
    return tuple(flag for flag in HARDENING_FLAGS if flag not in supported)


def containers(exe: str, *, timeout: float = 30, all_containers: bool = True) -> list[dict[str, Any]]:
    """``wslc ps`` as a list of dicts. Emits NDJSON: one object per line, no wrapper array."""
    argv = [exe, "ps"] + (["-a"] if all_containers else []) + ["--format", "json"]
    result = run_capture(argv, timeout=timeout)
    if result.returncode != 0:
        raise WslcError(f"`wslc ps` failed with exit code {result.returncode}")
    rows: list[dict[str, Any]] = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue  # a localized banner or warning, not a container row
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("skipping unparsable `wslc ps` line: %.120s", line)
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def inspect(exe: str, ref: str, *, timeout: float = 30) -> Optional[dict[str, Any]]:
    """``wslc inspect <ref>`` as a dict, or None when the object does not exist.

    Existence is decided by the exit code: a missing container and an unreachable
    engine both fail, and the localized message cannot tell them apart.
    """
    result = run_capture([exe, "inspect", ref, "--format", "json"], timeout=timeout)
    if result.returncode != 0:
        return None
    text = (result.stdout or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.debug("`wslc inspect` returned non-JSON for %s: %.120s", ref, text)
        return None
    if isinstance(parsed, list):
        return parsed[0] if parsed and isinstance(parsed[0], dict) else None
    return parsed if isinstance(parsed, dict) else None


def exists(exe: str, ref: str, *, timeout: float = 30) -> bool:
    return inspect(exe, ref, timeout=timeout) is not None


def container_name_from_inspect(payload: Optional[Mapping[str, Any]]) -> str:
    """``inspect`` returns names with a leading slash (``/hermes-abc``), as Docker does."""
    name = str((payload or {}).get("Name") or "")
    return name.lstrip("/")


def container_running(payload: Optional[Mapping[str, Any]]) -> bool:
    state = (payload or {}).get("State")
    if isinstance(state, Mapping):
        return bool(state.get("Running"))
    return False


def remove(exe: str, ref: str, *, force: bool = True, timeout: float = 60) -> bool:
    argv = [exe, "rm"] + (["-f"] if force else []) + [ref]
    result = run_capture(argv, timeout=timeout)
    if result.returncode != 0:
        logger.debug("`wslc rm` on %s exited %s", ref, result.returncode)
        return False
    return True


def start(exe: str, ref: str, *, timeout: float = 120) -> None:
    result = run_capture([exe, "start", ref], timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise WslcError(f"`wslc start {ref}` failed: {detail[-1] if detail else 'no output'}")


def build_run_argv(exe: str, *, name: str, image: str, workdir: Optional[str] = None,
                   env: Optional[Mapping[str, str]] = None, volumes: Iterable[str] = (),
                   labels: Optional[Mapping[str, str]] = None, cpu: float = 0,
                   memory_mb: int = 0, network: Optional[bool] = None,
                   command: Sequence[str] = KEEPALIVE_ARGV,
                   supported_flags: Optional[frozenset[str]] = None) -> list[str]:
    """Assemble ``wslc run -d`` for a long-lived Hermes sandbox container.

    ``supported_flags`` gates optional options; omit it to pass only the core set that
    wslc 3.0 has always accepted.
    """
    argv = [exe, "run", "-d", "--name", name]
    for key, value in (labels or {}).items():
        argv += ["--label", f"{key}={value}"]
    if workdir:
        argv += ["--workdir", workdir]
    if network is False:
        argv += ["--network", "none"]
    for key, value in (env or {}).items():
        argv += ["--env", f"{key}={value}"]
    for spec in volumes:
        argv += ["--volume", spec]
    if cpu and cpu > 0:
        argv += ["--cpus", str(cpu)]
    if memory_mb and memory_mb > 0:
        argv += ["--memory", f"{int(memory_mb)}M"]
    argv.append(image)
    argv.extend(command)
    return argv


def container_labels(*, task_id: str, persistent: bool, profile: str = "") -> dict[str, str]:
    """Labels used to recognise containers this plugin owns after a crash."""
    labels = {"hermes.backend": "wslc", "hermes.task": task_id}
    if profile:
        labels["hermes.profile"] = profile
    labels["hermes.persistent"] = "true" if persistent else "false"
    return labels


def own_containers(exe: str, *, timeout: float = 30) -> list[dict[str, Any]]:
    """Containers started by this plugin (by label), for the doctor / orphan report."""
    found = []
    for row in containers(exe, timeout=timeout):
        labels = row.get("Labels") or ""
        if isinstance(labels, str) and "hermes.backend=wslc" in labels:
            found.append(row)
    return found
