"""WSL Containers (``wslc``) terminal backend for Hermes Agent.

Deliberately import-free: :mod:`hermes_wslc_sandbox.wslc` is pure stdlib and is unit-tested
without a Hermes checkout, while :mod:`hermes_wslc_sandbox.provider` and
:mod:`hermes_wslc_sandbox.environment` import Hermes and are only loadable inside a running
Hermes. Importing them here would make the stdlib layer untestable on its own.
"""
