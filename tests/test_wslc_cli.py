"""Tests for the CLI adapter — no Hermes, no wslc and no container required.

The fixtures are verbatim output captured from wslc 3.0.1.0 on Windows 11, including the
localized help text and the metadata blob wslc puts in its own label. They are the reason
this file can pin the parsing rules without a runtime.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_wslc_sandbox import wslc  # noqa: E402

# --- captured CLI output -----------------------------------------------------

#: `wslc run --help`, abridged to the option table (Chinese, because the CLI follows the
#: Windows display language). Chinese text must not be needed to find ASCII option names.
RUN_HELP = """
使用情况: wslc run [选项] <image> [<command>] [<arguments>...]

选项:
      --cpus                 CPU 数(例如 0.5、1、2.5)
  -d  --detach               在分离模式下运行容器
  -e  --env                  环境变量的 Key=Value 对
  -l  --label                在对象上设置元数据
  -m  --memory               内存限制(例如 512M、1G)
      --name                 容器的名称
      --network              将容器连接到网络
  -v  --volume               将装载卷绑定到容器
  -w  --workdir              容器内的工作目录
  -?  --help                 显示选定命令的帮助信息
"""

#: One `wslc ps -a --format json` line for a container this plugin created. Note `Labels`
#: is a *string* here, and the metadata JSON inside it contains commas.
PS_LINE_LABELED = (
    '{"Command":"\\"sleep infinity\\"","CreatedAt":"2026-10-16 22:51:44 +0800 GMT+8",'
    '"HealthStatus":"","ID":"39b39dc6d978","Image":"hermes-e2e:latest",'
    '"Labels":"com.microsoft.wsl.container.metadata={\\"V1\\":{\\"Flags\\":0,\\"InitProcessFlags\\":0,'
    '\\"Ports\\":[],\\"Volumes\\":[]}},hermes.backend=wslc,hermes.task=t9",'
    '"LocalVolumes":"0","Mounts":"","Names":"e2e-lbl","Networks":"bridge",'
    '"Platform":{"architecture":"amd64","os":"linux"},"Ports":"","RunningFor":"About a minute ago",'
    '"Size":"0B","State":"running","Status":"Up About a minute"}'
)

PS_LINE_UNLABELED = (
    '{"Command":"\\"sleep infinity\\"","ID":"abcdef123456","Image":"hermes-e2e:latest",'
    '"Labels":"com.microsoft.wsl.container.metadata={\\"V1\\":{\\"Flags\\":0}}",'
    '"Names":"stranger","State":"running"}'
)

#: `wslc inspect <name> --format json` — an array, and `Labels` is a *dict* here.
INSPECT_ARRAY = json.dumps([{
    "Id": "3b89d5b2c784fe8ea17c672f06316ca5fa5746182d85dae7191165c513aba1e7",
    "Name": "/e2e-full",
    "State": {"ExitCode": 0, "FinishedAt": "2026-10-16T14:56:32Z", "Health": None,
              "Running": True, "StartedAt": "2026-10-16T14:56:32Z", "Status": "running"},
    "Labels": {"hermes.backend": "wslc", "hermes.task": "t1"},
    "Config": {"Image": "hermes-e2e:latest", "WorkingDir": "/mnt/host"},
    "HostConfig": {"Memory": 536870912, "NanoCpus": 1000000000, "NetworkMode": "none"},
}])


def _fake_cli(tmp_path: Path, stdout: str = "", stderr: str = "", code: int = 0) -> str:
    """An executable that prints canned output, so argv handling is testable everywhere.

    The canned text is written to the raw byte streams as UTF-8, which is what a real CLI
    does. Writing through ``sys.stdout`` would encode with the *console* codepage instead, so
    a localized fixture would crash on an English Windows runner (cp1252) while passing on a
    Chinese one (cp936) — and the point of these fixtures is to be the same everywhere.
    """
    script = tmp_path / "fake_wslc.py"
    script.write_text(
        "import sys\n"
        f"sys.stdout.buffer.write({stdout.encode('utf-8')!r})\n"
        f"sys.stderr.buffer.write({stderr.encode('utf-8')!r})\n"
        "sys.stdout.flush()\n"
        f"sys.exit({code})\n",
        encoding="utf-8")
    if sys.platform == "win32":
        launcher = tmp_path / "fake_wslc.cmd"
        launcher.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return str(launcher)
    launcher = tmp_path / "fake_wslc"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
    launcher.chmod(0o755)
    return str(launcher)


# --- output decoding ---------------------------------------------------------

def test_decode_reads_utf8_cli_output():
    assert wslc._decode("正在检查更新".encode("utf-8")) == "正在检查更新"


def test_decode_never_raises_on_arbitrary_bytes():
    """Prose from a CLI is not always decodable — a truncated multi-byte character, a
    console-codepage byte. Reading it must still return a string rather than crash the read."""
    for payload in (b"\xff\xfe\x00\x81", b"\x8d\x90\x9d", "正在检查更新".encode("cp936")[:4]):
        assert isinstance(wslc._decode(payload), str)


# --- binary resolution -------------------------------------------------------

def test_resolve_uses_explicit_path(tmp_path):
    exe = _fake_cli(tmp_path)
    assert wslc.resolve_wslc(exe) == exe


def test_resolve_rejects_unusable_explicit_path(tmp_path):
    with pytest.raises(wslc.WslcError):
        wslc.resolve_wslc(str(tmp_path / "nope.exe"))


def test_resolve_honours_env_override(tmp_path, monkeypatch):
    exe = _fake_cli(tmp_path)
    monkeypatch.setenv(wslc.BINARY_ENV, exe)
    assert wslc.resolve_wslc() == exe


def test_resolve_reports_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setenv(wslc.BINARY_ENV, "")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    with pytest.raises(wslc.WslcError) as excinfo:
        wslc.resolve_wslc()
    message = str(excinfo.value)
    assert "wsl --update" in message and wslc.BINARY_ENV in message


# --- capability probe --------------------------------------------------------

def test_supported_flags_parsed_from_localized_help(tmp_path):
    exe = _fake_cli(tmp_path, stdout=RUN_HELP)
    wslc._supported_flags_cache.pop(exe, None)
    flags = wslc.supported_run_flags(exe)
    assert {"--cpus", "--memory", "--volume", "--workdir", "--network", "--label"} <= flags
    # The Docker hardening options wslc 3.0 rejects are absent, and that is the finding.
    assert not {"--pids-limit", "--cap-drop", "--security-opt", "--init"} & flags


def test_missing_hardening_flags_names_every_unsupported_option(tmp_path):
    exe = _fake_cli(tmp_path, stdout=RUN_HELP)
    wslc._supported_flags_cache.pop(exe, None)
    assert set(wslc.missing_hardening_flags(exe)) == set(wslc.HARDENING_FLAGS)


def test_flag_probe_failure_assumes_nothing_supported(tmp_path):
    exe = _fake_cli(tmp_path, code=1)
    wslc._supported_flags_cache.pop(exe, None)
    assert wslc.supported_run_flags(exe) == frozenset()
    assert set(wslc.missing_hardening_flags(exe)) == set(wslc.HARDENING_FLAGS)


# --- reads -------------------------------------------------------------------

def test_containers_parses_ndjson(tmp_path):
    exe = _fake_cli(tmp_path, stdout=PS_LINE_LABELED + "\n" + PS_LINE_UNLABELED + "\n")
    rows = wslc.containers(exe)
    assert [row["Names"] for row in rows] == ["e2e-lbl", "stranger"]


def test_containers_skips_localized_banner_lines(tmp_path):
    exe = _fake_cli(tmp_path, stdout="正在检查更新...\n" + PS_LINE_LABELED + "\n\n")
    rows = wslc.containers(exe)
    assert len(rows) == 1 and rows[0]["Names"] == "e2e-lbl"


def test_containers_raises_on_cli_failure(tmp_path):
    exe = _fake_cli(tmp_path, code=1)
    with pytest.raises(wslc.WslcError):
        wslc.containers(exe)


def test_inspect_accepts_array_and_object(tmp_path):
    array_exe = _fake_cli(tmp_path, stdout=INSPECT_ARRAY)
    payload = wslc.inspect(array_exe, "e2e-full")
    assert payload["Name"] == "/e2e-full"
    assert wslc.container_running(payload) is True
    assert wslc.container_name_from_inspect(payload) == "e2e-full"

    nested = tmp_path / "obj"
    nested.mkdir()
    obj_exe = _fake_cli(nested, stdout=json.dumps({"Name": "/x", "State": {"Running": False}}))
    obj = wslc.inspect(obj_exe, "x")
    assert obj["Name"] == "/x"
    assert wslc.container_running(obj) is False


def test_inspect_returns_none_when_object_is_missing(tmp_path):
    exe = _fake_cli(tmp_path, stderr="错误代码： WSLC_E_CONTAINER_NOT_FOUND\n", code=1)
    assert wslc.inspect(exe, "gone") is None
    assert wslc.exists(exe, "gone") is False


def test_inspect_returns_none_on_non_json_output(tmp_path):
    exe = _fake_cli(tmp_path, stdout="not json\n")
    assert wslc.inspect(exe, "x") is None


def test_own_containers_matches_label_inside_metadata_blob(tmp_path):
    """The metadata JSON contains commas, so labels may only be substring-matched."""
    exe = _fake_cli(tmp_path, stdout=PS_LINE_LABELED + "\n" + PS_LINE_UNLABELED + "\n")
    owned = wslc.own_containers(exe)
    assert [row["Names"] for row in owned] == ["e2e-lbl"]


# --- argv assembly -----------------------------------------------------------

def test_build_run_argv_full_option_set():
    argv = wslc.build_run_argv(
        "wslc", name="hermes-wslc-t1", image="img:latest", workdir="/mnt/host",
        env={"A": "1"}, volumes=[r"D:\data:/mnt/host"], labels={"hermes.backend": "wslc"},
        cpu=1.5, memory_mb=512, network=False)
    assert argv[:3] == ["wslc", "run", "-d"]
    assert argv[argv.index("--name") + 1] == "hermes-wslc-t1"
    assert argv[argv.index("--workdir") + 1] == "/mnt/host"
    assert argv[argv.index("--network") + 1] == "none"
    assert f"{argv[argv.index('--label') + 1]}" == "hermes.backend=wslc"
    assert argv[argv.index("--env") + 1] == "A=1"
    assert argv[argv.index("--volume") + 1] == r"D:\data:/mnt/host"
    assert argv[argv.index("--cpus") + 1] == "1.5"
    assert argv[argv.index("--memory") + 1] == "512M"
    assert argv[-3:] == ["img:latest", "sleep", "infinity"]


def test_build_run_argv_omits_optional_options():
    argv = wslc.build_run_argv("wslc", name="n", image="img")
    for flag in ("--workdir", "--cpus", "--memory", "--network", "--env", "--volume", "--label"):
        assert flag not in argv


def test_build_run_argv_never_passes_docker_only_hardening():
    """wslc exits 1 on an unknown option, so passing these would break every container."""
    argv = wslc.build_run_argv("wslc", name="n", image="img", cpu=1, memory_mb=256)
    for flag in ("--cap-drop", "--security-opt", "--pids-limit", "--init", "--storage-opt"):
        assert flag not in argv


def test_container_labels_are_greppable():
    labels = wslc.container_labels(task_id="t1", persistent=True, profile="work")
    assert labels["hermes.backend"] == "wslc"
    assert labels["hermes.task"] == "t1"
    assert labels["hermes.persistent"] == "true"
    assert labels["hermes.profile"] == "work"
