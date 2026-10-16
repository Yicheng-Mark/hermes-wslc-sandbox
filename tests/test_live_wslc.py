"""End-to-end tests against a real wslc runtime and a real Linux image.

Opt in, because they need a Windows host with WSL Containers installed and an image that is
already present locally:

    set HERMES_WSLC_LIVE=1
    set HERMES_WSLC_TEST_IMAGE=alpine:latest
    set HERMES_SOURCE=D:\\path\\to\\hermes-agent        # or have Hermes importable
    python -m pytest tests/test_live_wslc.py -v

They exercise the path Hermes actually uses — ``BaseEnvironment.execute`` through this
backend's ``_run_bash`` — so cwd markers, stdin, exit codes and stream separation are tested
as the agent experiences them, not as raw CLI calls.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if os.environ.get("HERMES_SOURCE"):
    sys.path.insert(0, os.environ["HERMES_SOURCE"])

pytest.importorskip("tools.environments.base", reason="needs a Hermes checkout on PYTHONPATH")

from hermes_wslc_sandbox import wslc  # noqa: E402
from hermes_wslc_sandbox.environment import WslcEnvironment  # noqa: E402

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("HERMES_WSLC_LIVE") != "1",
                       reason="set HERMES_WSLC_LIVE=1 to run against a real wslc runtime"),
]


def _image() -> str:
    return os.environ.get("HERMES_WSLC_TEST_IMAGE", "")


def _runtime() -> str:
    return os.environ.get("HERMES_WSLC_BINARY") or wslc.resolve_wslc()


@pytest.fixture(autouse=True)
def _require_image():
    if not _image():
        pytest.skip("set HERMES_WSLC_TEST_IMAGE to a locally available Linux image")


@pytest.fixture
def env():
    """A throwaway non-persistent sandbox, removed even when the test fails."""
    created: list[WslcEnvironment] = []

    def factory(**kwargs):
        kwargs.setdefault("task_id", f"pytest-{uuid.uuid4().hex[:8]}")
        instance = WslcEnvironment(image=_image(), **kwargs)
        created.append(instance)
        return instance

    yield factory
    for instance in created:
        try:
            instance.cleanup(force_remove=True)
        except Exception:
            pass


def test_execute_round_trip(env):
    sandbox = env()
    result = sandbox.execute("echo hermes-wslc-ok")
    assert result["returncode"] == 0
    assert "hermes-wslc-ok" in result["output"]
    # The core reads returncode, the provider guide documents exit_code; both are published.
    assert result["exit_code"] == 0


def test_exit_code_propagates(env):
    sandbox = env()
    assert sandbox.execute("exit 7")["returncode"] == 7


def test_working_directory_is_tracked(env):
    """cwd survives across calls via the in-band markers BaseEnvironment parses."""
    sandbox = env()
    sandbox.execute("mkdir -p /tmp/hermes-wslc-cwd && cd /tmp/hermes-wslc-cwd")
    assert sandbox.execute("pwd")["output"].strip().endswith("/tmp/hermes-wslc-cwd")


def test_stdin_is_piped(env):
    sandbox = env()
    result = sandbox.execute("cat", stdin_data="piped-through-wslc\n")
    assert "piped-through-wslc" in result["output"]


def test_stderr_is_captured_with_stdout(env):
    sandbox = env()
    result = sandbox.execute("echo to-stderr >&2; echo to-stdout")
    assert "to-stderr" in result["output"] and "to-stdout" in result["output"]


def test_forwarded_env_reaches_the_container(env, monkeypatch):
    monkeypatch.setenv("HERMES_WSLC_PROBE", "forwarded-value")
    sandbox = env(forward_env=["HERMES_WSLC_PROBE"])
    assert "forwarded-value" in sandbox.execute("echo $HERMES_WSLC_PROBE")["output"]


def test_consecutive_commands_reuse_one_container(env):
    sandbox = env()
    sandbox.execute("echo marker > /tmp/hermes-wslc-reuse")
    assert "marker" in sandbox.execute("cat /tmp/hermes-wslc-reuse")["output"]


def test_non_persistent_cleanup_removes_the_container(env):
    sandbox = env()
    name = sandbox.container
    sandbox.cleanup()
    assert wslc.inspect(_runtime(), name) is None


def test_persistent_sandbox_survives_teardown(env):
    """A persistent environment is stopped, not deleted, and the next one resumes it."""
    task_id = f"pytest-persist-{uuid.uuid4().hex[:8]}"
    first = env(task_id=task_id, persistent_filesystem=True)
    name = first.container
    first.execute("echo persisted > /root/hermes-wslc-persist")
    first.cleanup()

    state = wslc.inspect(_runtime(), name)
    assert state is not None, "persistent sandbox must not be removed"
    assert not wslc.container_running(state), "persistent sandbox should be stopped at teardown"

    second = env(task_id=task_id, persistent_filesystem=True)
    assert second.container == name
    assert "persisted" in second.execute("cat /root/hermes-wslc-persist")["output"]


def test_recreate_after_external_removal_is_reported(env):
    """A sandbox deleted behind Hermes' back is recreated, and the model is told."""
    sandbox = env()
    sandbox.execute("echo alive")
    wslc.remove(_runtime(), sandbox.container, force=True)
    sandbox._liveness_checked_at = 0.0  # force the next execute to re-check
    result = sandbox.execute("echo after-recreate")
    assert "after-recreate" in result["output"]
    assert result.get("environment_recreated") is True
