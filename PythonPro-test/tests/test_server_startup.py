from __future__ import annotations

import socket
import subprocess
import sys
import time

import pytest

import testsystem

NETSTAT_SAMPLE = """
活动连接

  协议    本地地址          外部地址        状态           PID
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1234
  TCP    127.0.0.1:8000         0.0.0.0:0              LISTENING       5678
  TCP    127.0.0.1:8000         127.0.0.1:51000        TIME_WAIT       0
  TCP    127.0.0.1:8001         0.0.0.0:0              LISTENING       9999
  TCP    [::]:8000              [::]:0                 LISTENING       5678
  UDP    0.0.0.0:500            *:*                                    4321
"""


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_port_available_reflects_bound_socket():
    port = free_port()
    assert testsystem._port_available("127.0.0.1", port)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))
        sock.listen()
        assert not testsystem._port_available("127.0.0.1", port)
    assert testsystem._port_available("127.0.0.1", port)


def test_pid_file_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTSYSTEM_DATA_DIR", str(tmp_path))
    assert testsystem._read_pid_file() is None
    testsystem._write_pid_file()
    assert testsystem._read_pid_file() == testsystem.os.getpid()
    testsystem._remove_pid_file()
    assert testsystem._read_pid_file() is None


def test_read_pid_file_ignores_garbage(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTSYSTEM_DATA_DIR", str(tmp_path))
    (tmp_path / "testsystem.pid").write_text("not-a-pid", encoding="utf-8")
    assert testsystem._read_pid_file() is None


def test_parse_netstat_pids_matches_only_listeners_on_port():
    assert testsystem._parse_netstat_pids(NETSTAT_SAMPLE, 8000) == [5678]
    assert testsystem._parse_netstat_pids(NETSTAT_SAMPLE, 8001) == [9999]
    assert testsystem._parse_netstat_pids(NETSTAT_SAMPLE, 9999) == []


def test_release_stale_server_noop_when_port_free():
    testsystem._release_stale_server("127.0.0.1", free_port())


def test_main_writes_then_removes_pid_file(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTSYSTEM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTSYSTEM_PORT", str(free_port()))
    seen: dict = {}

    def fake_run(*args, **kwargs):
        seen["pid"] = testsystem._read_pid_file()
        seen["kwargs"] = kwargs

    monkeypatch.setattr("uvicorn.run", fake_run)
    testsystem.main()
    assert seen["pid"] == testsystem.os.getpid()
    assert seen["kwargs"]["timeout_graceful_shutdown"] == 3
    assert testsystem._read_pid_file() is None


def test_main_removes_pid_file_on_keyboard_interrupt(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTSYSTEM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTSYSTEM_PORT", str(free_port()))

    def fake_run(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("uvicorn.run", fake_run)
    with pytest.raises(KeyboardInterrupt):
        testsystem.main()
    assert testsystem._read_pid_file() is None


def test_release_stale_server_raises_for_unknown_owner(monkeypatch):
    monkeypatch.setattr(testsystem, "_port_available", lambda host, port: False)
    monkeypatch.setattr(testsystem, "_read_pid_file", lambda: None)
    monkeypatch.setattr(testsystem, "_listening_pids", lambda port: [])
    with pytest.raises(SystemExit):
        testsystem._release_stale_server("127.0.0.1", 8000)


@pytest.mark.skipif(sys.platform != "win32", reason="依赖 Windows 进程镜像校验")
def test_release_stale_server_kills_leftover_python(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTSYSTEM_DATA_DIR", str(tmp_path))
    port = free_port()
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import socket, time; s = socket.socket(); s.bind(('127.0.0.1', %d)); s.listen(); time.sleep(60)" % port,
        ]
    )
    try:
        deadline = time.monotonic() + 10
        while testsystem._port_available("127.0.0.1", port):
            assert time.monotonic() < deadline, "子进程未能及时占用端口"
            time.sleep(0.05)
        (tmp_path / "testsystem.pid").write_text(str(child.pid), encoding="utf-8")

        testsystem._release_stale_server("127.0.0.1", port)

        assert testsystem._port_available("127.0.0.1", port)
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
