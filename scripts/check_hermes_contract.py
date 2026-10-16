#!/usr/bin/env python3
"""Check this plugin against a real Hermes install — no substitutes, no stubs.

    python scripts/check_hermes_contract.py --hermes-source D:\\path\\to\\hermes-agent

Verifies the interfaces the adapter depends on (imports, signatures, abstract methods) and
that Hermes' own backend registry accepts the provider, then resolves the backend through the
terminal factory. It does not start a container; run the live tests for that.
"""
from __future__ import annotations

import argparse
import inspect
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-source", type=Path,
                        help="Path to a hermes-agent checkout, if it is not already importable")
    args = parser.parse_args()
    if args.hermes_source:
        sys.path.insert(0, str(args.hermes_source.expanduser().resolve()))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    try:
        from agent.terminal_env_provider import TerminalEnvironmentProvider
        from agent import terminal_env_registry
        from tools.environments.base import BaseEnvironment, EnvironmentConnectionError
        from tools.environments.base_output import _popen_bash
        from tools.environments.remote_common import bash_argv
        from tools import terminal_tool_backends
        from hermes_wslc_sandbox.environment import WslcEnvironment
        from hermes_wslc_sandbox.provider import WslcProvider
    except ImportError as exc:
        print(f"FAIL: import failed; use a current Hermes environment: {exc}", file=sys.stderr)
        return 1

    errors: list[str] = []

    if inspect.isabstract(WslcProvider):
        errors.append(f"Provider has unimplemented abstract methods: {WslcProvider.__abstractmethods__}")
    if not issubclass(WslcProvider, TerminalEnvironmentProvider):
        errors.append("WslcProvider does not derive from TerminalEnvironmentProvider")
    for method in ("init_session", "execute", "_run_bash", "cleanup"):
        if not callable(getattr(BaseEnvironment, method, None)):
            errors.append(f"BaseEnvironment.{method} is unavailable")
    if not callable(_popen_bash) or not callable(bash_argv):
        errors.append("Hermes' process/argv helpers are unavailable to plugins")
    if "retry_hint" not in inspect.signature(EnvironmentConnectionError).parameters:
        errors.append("EnvironmentConnectionError lacks retry_hint")

    provider = WslcProvider()
    if provider.name != "wslc":
        errors.append(f"provider.name is {provider.name!r}, so `terminal.backend: wslc` would miss")
    if provider.name in terminal_env_registry.BUILTIN_BACKEND_NAMES:
        errors.append(f"'{provider.name}' collides with a built-in backend name")
    # A wslc sandbox cannot drop capabilities, so it must not inherit skip-approval behaviour.
    if provider.skip_container_guards:
        errors.append("skip_container_guards must stay False: wslc has no --cap-drop")
    for attribute in ("is_remote", "is_container", "session_isolated_when_nonpersistent"):
        if not isinstance(getattr(provider, attribute), bool):
            errors.append(f"{attribute} must be a bool")
    for member in ("description", "env_description", "cache_path_base", "strip_env_keys"):
        try:
            getattr(provider, member)
        except Exception as exc:  # a raising property is fail-soft in core, but still a bug
            errors.append(f"provider.{member} raised: {exc}")

    # The real registry, not a mock: registration must succeed and be visible under the name
    # the config uses.
    previous = terminal_env_registry.get_provider("wslc")
    try:
        terminal_env_registry.register_provider(provider)
        if "wslc" not in terminal_env_registry.plugin_backend_names():
            errors.append("registered provider is not listed by plugin_backend_names()")

        # And the factory must route an unknown backend name to plugin providers.
        sentinel = object()
        original = WslcProvider.create_environment
        WslcProvider.create_environment = lambda self, **kwargs: sentinel
        try:
            resolved = terminal_tool_backends._create_environment(
                "wslc", image="probe:latest", cwd="/root", timeout=30)
            if resolved is not sentinel:
                errors.append("the terminal factory did not resolve 'wslc' to this plugin")
        finally:
            WslcProvider.create_environment = original
    except Exception as exc:
        errors.append(f"registering with agent.terminal_env_registry failed: {exc!r}")
    finally:
        if previous is not None:
            terminal_env_registry.restore_registration("wslc", provider, previous)

    if not hasattr(WslcEnvironment, "_run_bash"):
        errors.append("WslcEnvironment does not implement _run_bash")

    if errors:
        print("FAIL:\n" + "\n".join(f"- {error}" for error in errors), file=sys.stderr)
        return 1
    print("PASS: installed Hermes interfaces match this plugin's contract.")
    print("Checked: imports, signatures, registry acceptance and factory dispatch.")
    print("Not checked here: live container operation (see tests/test_live_wslc.py).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
