"""Hermes plugin entry point.

Install by cloning this repository into ``~/.hermes/plugins/hermes-wslc-sandbox`` (or with
``hermes plugins install``), then select the backend:

    hermes plugins enable hermes-wslc-sandbox
    hermes config set terminal.backend wslc
"""


def register(ctx):
    from .hermes_wslc_sandbox.provider import WslcProvider

    ctx.register_terminal_environment_provider(WslcProvider())
