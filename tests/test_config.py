"""Naming and env-merge rules — Hermes-free, so they hold on any machine."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_wslc_sandbox.config import MAX_NAME_LENGTH, as_bool, container_name, resolve_env  # noqa: E402


def test_persistent_name_is_reproducible():
    """Resumability depends on the name surviving a process restart."""
    first = container_name("task-1", persistent=True)
    assert first == container_name("task-1", persistent=True)


def test_non_persistent_name_is_unique_per_call():
    names = {container_name("task-1", persistent=False) for _ in range(5)}
    assert len(names) == 5


def test_names_are_sanitized_and_bounded():
    name = container_name("a b/c:d*e?f|g<h>i" * 8, persistent=True)
    assert len(name) <= MAX_NAME_LENGTH
    assert all(char.isalnum() or char in "-_." for char in name)


def test_empty_task_id_still_yields_a_valid_name():
    assert container_name("", persistent=True) == "hermes-wslc-default"
    assert container_name("///", persistent=True).endswith("default")


def test_as_bool_reads_config_spellings():
    assert as_bool(True, default=False) is True
    assert as_bool("true", default=False) is True
    assert as_bool("OFF", default=True) is False
    assert as_bool(None, default=True) is True


def test_resolve_env_skips_unset_forwarded_names(monkeypatch):
    monkeypatch.setenv("WSL_SANDBOX_TEST_SET", "value")
    monkeypatch.delenv("WSL_SANDBOX_TEST_UNSET", raising=False)
    resolved = resolve_env(["WSL_SANDBOX_TEST_SET", "WSL_SANDBOX_TEST_UNSET"], None)
    assert resolved == {"WSL_SANDBOX_TEST_SET": "value"}


def test_resolve_env_explicit_values_win(monkeypatch):
    monkeypatch.setenv("WSL_SANDBOX_TEST_SET", "from-host")
    resolved = resolve_env(["WSL_SANDBOX_TEST_SET"], {"WSL_SANDBOX_TEST_SET": "explicit"})
    assert resolved == {"WSL_SANDBOX_TEST_SET": "explicit"}


def test_resolve_env_renders_none_as_empty_string():
    assert resolve_env(None, {"A": None}) == {"A": ""}
