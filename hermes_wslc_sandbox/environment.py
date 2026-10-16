"""A Hermes terminal environment backed by a WSL Containers (``wslc``) sandbox.

One long-lived container per environment, kept alive with ``sleep infinity`` and driven
with ``wslc exec``. Hermes' own plumbing does the rest: command wrapping, cwd tracking,
stdin, timeouts and background processes all come from :class:`BaseEnvironment`, which is
why this module is mostly container lifecycle and argv assembly.

Persistence uses the container's own writable layer, not bind mounts. ``container_persistent:
true`` leaves the container in place and merely stops it at teardown; the next session starts
the same container and finds its files. Non-persistent environments are removed outright.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Iterable, Mapping, Optional

from tools.environments.base import BaseEnvironment, EnvironmentConnectionError
# In-tree backends spawn through the same helper (see tools/environments/singularity.py):
# it owns the Windows no-console-window creation flags, the utf-8 pipe setup and the
# asynchronous stdin writer, none of which a plugin should reimplement.
from tools.environments.base_output import _popen_bash
from tools.environments.remote_common import bash_argv

from . import wslc
from .config import container_name, resolve_env

logger = logging.getLogger(__name__)

#: How often a running command may cost one extra ``wslc inspect``. The check exists to
#: catch a container that died out from under a live session; it is not worth a CLI call
#: per command, and staleness of a few seconds cannot change the outcome.
_LIVENESS_INTERVAL_S = 30.0

#: Creating a container from a locally present image is fast, but a large image being
#: unpacked for the first time is not; this is the ceiling for ``wslc run``.
_CREATE_TIMEOUT_S = 300.0


class WslcEnvironment(BaseEnvironment):
    """Run commands inside a WSL Containers sandbox on this Windows host."""

    _snapshot_timeout = 120
    _stdin_mode = "pipe"
    # The core can pre-empt its password-prompt path for backends that are root inside the
    # sandbox (``BaseEnvironment._sudo_nopasswd_works``). This backend deliberately leaves that
    # switch alone: the admission scanner reads the marker as a passwordless-sudoers entry and
    # fails the plugin outright, so setting it would trade a small prompt-saving for a blocked
    # listing. The cost of leaving it unset is bounded — the core falls back to "a password may
    # be required", which for a root container means at worst one spurious prompt.

    def __init__(self, *, image: str, cwd: str = "/root", timeout: int = 60, cpu: float = 0,
                 memory: int = 0, disk: int = 0, network: Optional[bool] = None,
                 volumes: Optional[Iterable[str]] = None,
                 env: Optional[Mapping[str, Any]] = None,
                 forward_env: Optional[Iterable[str]] = None,
                 persistent_filesystem: bool = False, task_id: str = "default",
                 runtime: Optional[str] = None, container: Optional[str] = None,
                 profile: str = ""):
        if cwd == "~":
            cwd = "/root"
        super().__init__(cwd=cwd, timeout=timeout)
        # Set before anything can fail so __del__ -> cleanup() is always safe.
        self._closed = True
        self._lock = threading.RLock()
        if not image:
            raise EnvironmentConnectionError("WSL Containers backend needs a container image")
        self._exe = runtime or wslc.resolve_wslc()
        self._image = image
        self._cpu = float(cpu or 0)
        self._memory = int(memory or 0)
        # Recorded, never applied: wslc has no --storage-opt, and silently ignoring a
        # requested disk limit would be worse than reporting it (see doctor_checks()).
        self._disk = int(disk or 0)
        self._network = network
        self._volumes = [str(spec) for spec in (volumes or [])]
        self._env = resolve_env(forward_env, env)
        self._persistent = bool(persistent_filesystem)
        self._task_id = str(task_id or "default")
        self._profile = profile
        self._name = container or container_name(self._task_id, persistent=self._persistent)
        self._created = False
        self._liveness_checked_at = 0.0
        self._start_container()
        self._closed = False
        try:
            self.init_session()
        except Exception:
            self.cleanup()
            raise

    # --- container lifecycle -------------------------------------------------

    @property
    def container(self) -> str:
        return self._name

    @property
    def executable(self) -> str:
        return self._exe

    def _labels(self) -> dict[str, str]:
        return wslc.container_labels(task_id=self._task_id, persistent=self._persistent,
                                     profile=self._profile)

    def _start_container(self) -> None:
        """Adopt a running persistent container, or create one."""
        existing = wslc.inspect(self._exe, self._name)
        if existing is not None:
            if self._persistent:
                if not wslc.container_running(existing):
                    logger.info("WSL Containers: restarting stopped sandbox %s", self._name)
                    wslc.start(self._exe, self._name)
                else:
                    logger.info("WSL Containers: reusing sandbox %s", self._name)
                return
            # A non-persistent name is unique per run, so anything already under it is the
            # leftover of a process that died before its teardown.
            logger.warning("WSL Containers: removing leftover sandbox %s", self._name)
            wslc.remove(self._exe, self._name, force=True)

        argv = wslc.build_run_argv(
            self._exe, name=self._name, image=self._image, workdir=self.cwd,
            env=self._env, volumes=self._volumes, labels=self._labels(),
            cpu=self._cpu, memory_mb=self._memory, network=self._network)
        result = wslc.run_capture(argv, timeout=_CREATE_TIMEOUT_S)
        if result.returncode != 0:
            raise EnvironmentConnectionError(
                f"WSL Containers could not start a sandbox from image {self._image!r} "
                f"(wslc exited {result.returncode}). Run `wslc run {self._image}` by hand to see "
                "the engine error; the image has to be a Linux image (WSL Containers does not "
                "run Windows images).",
                retry_hint="The image may need to be pulled on the host first (`wslc pull <image>`).")
        self._created = True
        logger.info("WSL Containers: started %s from %s", self._name, self._image)

    def _ensure_alive(self) -> None:
        state = wslc.inspect(self._exe, self._name)
        if state is None:
            if not self._persistent:
                logger.warning("WSL Containers: sandbox %s vanished; recreating it", self._name)
                self._start_container()
                # Tell the model the workspace is not what it was: background processes are
                # gone and anything outside the bind mounts did not survive.
                self._mark_recreated()
                return
            raise EnvironmentConnectionError(
                f"WSL Containers sandbox {self._name} no longer exists",
                retry_hint="Delete the environment's task state or start a new session to "
                           "recreate the sandbox.")
        if not wslc.container_running(state):
            logger.info("WSL Containers: sandbox %s is stopped; starting it", self._name)
            wslc.start(self._exe, self._name)

    def _before_execute(self) -> None:
        super()._before_execute()
        now = time.monotonic()
        if now - self._liveness_checked_at < _LIVENESS_INTERVAL_S:
            return
        self._liveness_checked_at = now
        self._ensure_alive()

    # --- execution -----------------------------------------------------------

    def _run_bash(self, cmd_string: str, *, login: bool = False, timeout: int = 120,
                  stdin_data: Optional[str] = None):
        if self._closed:
            raise EnvironmentConnectionError("WSL Containers environment is closed")
        argv = [self._exe, "exec"]
        if stdin_data is not None:
            # Without --interactive the CLI closes the container process's stdin.
            argv.append("-i")
        argv.append(self._name)
        argv.extend(bash_argv(cmd_string, login))
        return _popen_bash(argv, stdin_data)

    def execute(self, *args, **kwargs) -> dict:
        with self._lock:
            result = super().execute(*args, **kwargs)
        # The provider guide documents exit_code; the core reads returncode. Publish both.
        if "returncode" in result:
            result.setdefault("exit_code", result["returncode"])
        return result

    # --- teardown ------------------------------------------------------------

    def cleanup(self, *, force_remove: bool = False) -> None:
        if not hasattr(self, "_lock"):
            return
        with self._lock:
            if self._closed:
                return
            self._closed = True
            keep = self._persistent and not force_remove and self._created
            if keep:
                # Stopping frees the CPU/RAM while the container's writable layer — the whole
                # persistent workspace — stays on disk for the next session to start again.
                result = wslc.run_capture([self._exe, "stop", self._name], timeout=120)
                if result.returncode != 0:
                    logger.warning("WSL Containers: could not stop %s cleanly", self._name)
                return
            if self._created or force_remove:
                wslc.remove(self._exe, self._name, force=True)

    def capability_report(self) -> list[str]:
        """Human-readable notes for ``hermes doctor``: what this backend cannot honour."""
        notes = []
        try:
            missing = wslc.missing_hardening_flags(self._exe)
        except Exception:
            missing = wslc.HARDENING_FLAGS
        if missing:
            notes.append(
                "wslc does not accept " + ", ".join(missing) +
                " — a wslc sandbox is not capability-dropped the way the Docker backend's is, "
                "so dangerous-command approval stays enabled.")
        if self._disk:
            notes.append(
                f"container_disk={self._disk}MB is reported but not enforced: wslc has no "
                "--storage-opt, so the container's disk use is bounded by the WSL disk image.")
        return notes
