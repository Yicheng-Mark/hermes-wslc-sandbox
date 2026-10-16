"""Registration and classification for the ``wslc`` terminal backend.

The provider is what makes ``terminal.backend: wslc`` a first-class selection: the picker,
the dashboard probe, ``hermes status``, the system-prompt environment hints, container
path handling and the approval layer all read these attributes instead of a hardcoded name
list, so declaring them here is what keeps a wslc sandbox from being treated as a local
shell.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Optional

from agent.terminal_env_provider import TerminalEnvironmentProvider

from . import wslc
from .config import as_bool

logger = logging.getLogger(__name__)

#: Shared container-config keys. They are how the terminal factory spells the container
#: surface for every container backend (see ``_CONTAINER_KEYS`` in
#: ``tools/terminal_tool_backends.py``); a plugin backend reads the same ones.
_CONFIG_KEYS = ("container_cpu", "container_memory", "container_disk", "container_persistent")
_MOUNT_KEYS = ("docker_volumes", "docker_forward_env", "docker_env", "docker_network")


class WslcProvider(TerminalEnvironmentProvider):
    """Run Hermes terminal tools in Microsoft's WSL Containers runtime on Windows."""

    name = "wslc"
    display_name = "WSL Containers (wslc)"
    # Commands run off-host, inside a Linux container's own filesystem: host OS, home and
    # cwd hints must not reach the prompt, and cwds are container paths.
    is_remote = True
    is_container = True
    # The Docker backend can drop capabilities and set no-new-privileges; wslc rejects both,
    # so a wslc sandbox is a weaker boundary and must not inherit the "skip approval" default.
    skip_container_guards = False
    # A non-persistent environment gets a freshly named container, never a shared one.
    session_isolated_when_nonpersistent = True

    @property
    def description(self) -> str:
        return ("Run commands in a Linux container on the WSL Containers runtime, without "
                "Docker Desktop.")

    @property
    def env_description(self) -> str:
        return "a Linux container running under WSL Containers (wslc) on this Windows host"

    @property
    def cache_path_base(self) -> str:
        # Synced ~/.hermes/cache files land in the container's root home, not a host path.
        return "/root/.hermes"

    @property
    def strip_env_keys(self) -> frozenset:
        # The backend owns no vendor credentials; host variables reach the container only
        # when docker_forward_env names them explicitly.
        return frozenset()

    # --- availability --------------------------------------------------------

    def _resolve(self) -> Optional[str]:
        if sys.platform != "win32":
            return None
        try:
            return wslc.resolve_wslc()
        except wslc.WslcError:
            return None

    def is_available(self) -> bool:
        """Cheap enough for UI paints: PATH and file checks only, no subprocess."""
        return self._resolve() is not None

    def check_requirements(self, config: dict[str, Any]) -> bool:
        if sys.platform != "win32":
            logger.error("The wslc backend runs WSL Containers, which is Windows-only; "
                         "this host reports platform %r.", sys.platform)
            return False
        try:
            exe = wslc.resolve_wslc()
        except wslc.WslcError as exc:
            logger.error("wslc backend selected but the CLI was not found: %s", exc)
            return False
        reported = wslc.version(exe)
        if not reported:
            logger.error("wslc backend selected, but `%s --version` failed. Run `wsl --update` "
                         "and check that `wslc --version` works in a terminal.", exe)
            return False
        logger.info("wslc backend ready: %s (%s)", exe, reported)
        return True

    def probe(self) -> tuple[str, str]:
        if sys.platform != "win32":
            return ("unavailable", "WSL Containers is a Windows-only runtime.")
        try:
            exe = wslc.resolve_wslc()
        except wslc.WslcError:
            return ("needs_setup", "wslc was not found. Run `wsl --update` to install WSL 3.x, "
                                   "which ships the WSL Containers CLI.")
        try:
            reported = wslc.version(exe, timeout=5)
        except Exception:
            return ("unavailable", f"{exe} did not respond to --version.")
        if not reported:
            return ("unavailable", f"{exe} is present but not answering.")
        return ("ready", reported)

    def setup_instructions(self) -> list[str]:
        return [
            "WSL Containers ships with WSL 3.0.3+ — run `wsl --update`, then check `wslc --version`.",
            "Container images are Linux images; pull them with `wslc pull <image>` (or let the "
            "first run pull them).",
            "Images must be reachable from your registry configuration; WSL Containers does not "
            "read Docker Desktop's settings.",
        ]

    def doctor_checks(self) -> list[tuple[bool, str, str]]:
        rows: list[tuple[bool, str, str]] = []
        exe = self._resolve()
        if exe is None:
            detail = ("only available on Windows" if sys.platform != "win32"
                      else "wslc not found — run `wsl --update`")
            return [(False, "WSL Containers backend", detail)]
        rows.append((True, "wslc binary", exe))
        try:
            missing = wslc.missing_hardening_flags(exe)
        except Exception:
            missing = wslc.HARDENING_FLAGS
        if missing:
            rows.append((True, "wslc sandbox hardening",
                         "not available: " + ", ".join(missing) +
                         " (approval prompts stay on; the Docker backend drops caps, wslc cannot)"))
        return rows

    # --- environment construction -------------------------------------------

    def create_environment(self, *, cwd: str, timeout: int, task_id: str = "default",
                           image: Optional[str] = None, container_config: Optional[dict] = None,
                           **kwargs: Any):
        from .environment import WslcEnvironment

        cc = container_config or {}
        values = {key: cc.get(key) for key in (*_CONFIG_KEYS, *_MOUNT_KEYS)}
        persistent = as_bool(values["container_persistent"], default=True)
        # A probe environment is a throwaway: it must never adopt or leave behind a real one.
        if kwargs.get("probe_only") or task_id == "prompt-backend-probe":
            persistent = False
        return WslcEnvironment(
            image=image or "",
            cwd=cwd or "/root",
            timeout=timeout,
            cpu=float(values["container_cpu"] or 0),
            memory=int(values["container_memory"] or 0),
            disk=int(values["container_disk"] or 0),
            network=as_bool(values["docker_network"], default=True),
            volumes=values["docker_volumes"] or [],
            env=values["docker_env"] or {},
            forward_env=values["docker_forward_env"] or [],
            persistent_filesystem=persistent,
            task_id=str(task_id),
            profile=str(kwargs.get("profile") or ""),
        )
