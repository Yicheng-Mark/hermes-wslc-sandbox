# hermes-wslc-sandbox

Run Hermes' terminal, file and `execute_code` tools inside a **WSL Containers** (`wslc`)
sandbox — a Linux container managed by Microsoft's container runtime for WSL, with no Docker
Desktop involved.

```yaml
# config.yaml
terminal:
  backend: wslc
```

**Requires Windows.** WSL Containers is a Windows feature; on any other platform the backend
reports itself unavailable rather than failing at command time.

## Why this exists

`terminal.backend: docker` on Windows needs a Docker-compatible engine, which in practice
means Docker Desktop. Windows users who already run WSL Containers had no supported way to
use it as Hermes' sandbox — the only workaround was an unofficial `docker.exe` shim, which
breaks whenever Hermes changes how it calls the CLI. This plugin is the third-party
integration path the [terminal environment provider
guide](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/terminal-environment-plugin.md)
describes: it registers `wslc` as a backend, so the config value above works like any
built-in one.

## Requirements

- Windows 10/11 with **WSL 3.0.3+** (`wsl --update`, then `wslc --version` should work).
- A **Linux** container image. WSL Containers does not run Windows images, and it pulls from
  your own registry configuration — it does not read Docker Desktop's settings. Pre-pull with
  `wslc pull <image>` if your registry needs credentials.

## Install

Hermes resolves `wslc.exe` through `HERMES_WSLC_BINARY`, then `PATH`, then
`C:\Program Files\WSL\wslc.exe`.

```bash
git clone https://github.com/Yicheng-Mark/hermes-wslc-sandbox \
  ~/.hermes/plugins/hermes-wslc-sandbox
hermes plugins enable hermes-wslc-sandbox
hermes config set terminal.backend wslc
hermes doctor          # shows the wslc rows, including what the sandbox cannot harden
```

## Configuration

The backend reads the same container-config keys every Hermes container backend gets, so it
needs no new settings:

| Key | Effect |
|---|---|
| `terminal.container_image` | The Linux image to run. |
| `container_cpu`, `container_memory` | `--cpus`, `--memory`. |
| `container_persistent` | Keep the container between sessions (see below). |
| `docker_volumes` | Bind mounts, e.g. `- D:\work\repo:/workspace`. Read-write unless you append `:ro`. |
| `docker_env`, `docker_forward_env` | Variables for the container. Only names you list are copied from the host environment. |
| `docker_network` | `false` runs the container with `--network none`. |
| `container_disk` | **Reported, not enforced** — wslc has no `--storage-opt`. |

## What the sandbox does and does not do

**Does:** one container per environment, kept alive with `sleep infinity` and driven through
`wslc exec`. Commands, file tools and `execute_code` run inside it; the working directory
persists across calls; stdin, exit codes and merged output behave the way the core expects.

**Persistence** uses the container's own writable layer. With `container_persistent: true` the
sandbox is **stopped**, not deleted, at teardown, and the next session starts the same
container and finds its files. Non-persistent environments are removed. List leftovers with
`wslc ps -a`; remove one with `wslc rm -f <name>`. Containers this plugin owns carry
`hermes.backend=wslc` labels.

**Not hardened the way Docker is.** `wslc run` rejects `--cap-drop`, `--security-opt`,
`--pids-limit`, `--init` and `--storage-opt` (all verified against 3.0.1; the backend probes
`run --help` rather than assuming). A wslc sandbox therefore does **not** drop capabilities or
set `no-new-privileges`, and dangerous-command approval deliberately stays **enabled** for
this backend — `skip_container_guards` is `False`, unlike the container backends that can
harden themselves. If you bind-mount host paths, the container can write to them.

## Disclosure

- The plugin spawns the `wslc` CLI: one short-lived process per command, plus one long-lived
  container per environment. `--rm` is not used, so stopped persistent sandboxes stay on disk
  until you remove them.
- Containers have outbound network access unless `docker_network: false`.
- Host environment variables enter the container **only** when named in `docker_forward_env`;
  bind mounts expose **only** the paths you list in `docker_volumes`.
- The plugin reads no credentials, stores none, and makes no network calls of its own;
  image pulls are performed by `wslc` against your registry configuration.
- Nothing is sent to third parties, and there is no telemetry.
- It imports Hermes' own spawn helper (`tools.environments.base_output._popen_bash`) and
  `bash_argv`, the same seam the in-tree backends use. It does not patch, wrap or rebind any
  core function or module attribute.

## Development

```bash
pip install -e ".[test]"
python -m pytest tests/ -q                     # unit tests: no Hermes, no wslc needed

# Contract check against a real Hermes checkout:
python scripts/check_hermes_contract.py --hermes-source D:\path\to\hermes-agent

# End-to-end against a real runtime and a locally present image:
set HERMES_WSLC_LIVE=1
set HERMES_WSLC_TEST_IMAGE=<image already pulled with `wslc pull`>
set HERMES_SOURCE=D:\path\to\hermes-agent
python -m pytest tests/test_live_wslc.py -v
```

`tests/test_wslc_cli.py` pins the CLI contract with output captured from a real `wslc` —
including the localized help text and the metadata blob it stores in its own label. See
[VALIDATION.md](VALIDATION.md) for exactly what was verified, on which versions.

## Layout

```
__init__.py                  plugin entry: register(ctx)
plugin.yaml                  manifest (kind: backend)
hermes_wslc_sandbox/
  wslc.py                    CLI adapter — stdlib only, unit-tested without Hermes
  config.py                  container naming and env merging — stdlib only
  environment.py             WslcEnvironment(BaseEnvironment): lifecycle + _run_bash
  provider.py                WslcProvider: classification, probe, factory
tests/                       unit tests (always) + live tests (opt-in)
scripts/check_hermes_contract.py
```

Licence: MIT.
