from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
from pathlib import Path

_PID_FILE_NAME = "testsystem.pid"
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _data_dir() -> Path:
    return Path(os.getenv("TESTSYSTEM_DATA_DIR", str(Path.cwd() / "data")))


def _pid_file() -> Path:
    return _data_dir() / _PID_FILE_NAME


def _read_pid_file() -> int | None:
    try:
        return int(_pid_file().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _write_pid_file() -> None:
    try:
        path = _pid_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass


def _remove_pid_file() -> None:
    try:
        _pid_file().unlink(missing_ok=True)
    except OSError:
        pass


def _port_available(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    probe_host = host
    if host in {"0.0.0.0", "::"}:
        probe_host = "::1" if family == socket.AF_INET6 else "127.0.0.1"
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((probe_host, port))
        except OSError:
            return False
    return True


def _wait_port_free(host: str, port: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_available(host, port):
            return True
        time.sleep(0.1)
    return _port_available(host, port)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong(0)
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _process_image(pid: int) -> str:
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(len(buffer))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return buffer.value
            return ""
        finally:
            kernel32.CloseHandle(handle)
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return ""


def _parse_netstat_pids(output: str, port: int) -> list[int]:
    pids: set[int] = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 5 or not parts[0].upper().startswith("TCP"):
            continue
        if parts[-2].upper() != "LISTENING" or not parts[-1].isdigit():
            continue
        if parts[1].rsplit(":", 1)[-1] == str(port):
            pids.add(int(parts[-1]))
    return sorted(pids)


def _listening_pids(port: int) -> list[int]:
    if os.name == "nt":
        command = ["netstat", "-ano"]
    else:
        command = ["lsof", "-nP", f"-tiTCP:{port}", "-sTCP:LISTEN"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, errors="ignore", timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if os.name == "nt":
        return _parse_netstat_pids(result.stdout, port)
    return sorted({int(token) for token in result.stdout.split() if token.isdigit()})


def _terminate(pid: int) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    return False


def _release_stale_server(host: str, port: int) -> None:
    if _port_available(host, port):
        return
    candidates: list[int] = []
    recorded = _read_pid_file()
    if recorded:
        candidates.append(recorded)
    candidates.extend(pid for pid in _listening_pids(port) if pid not in candidates)
    for pid in candidates:
        if pid == os.getpid() or not _pid_alive(pid):
            continue
        image = _process_image(pid)
        if "python" not in image.lower():
            continue
        print(f"检测到上次异常退出的服务进程仍占用端口 {port}（PID {pid}），正在结束该进程…", flush=True)
        if _terminate(pid) and _wait_port_free(host, port):
            print(f"端口 {port} 已释放，继续启动。", flush=True)
            return
    if os.name == "nt":
        hint = f"  netstat -ano | findstr :{port}\n  taskkill /PID <进程号> /F"
    else:
        hint = f"  lsof -nP -iTCP:{port} -sTCP:LISTEN\n  kill <进程号>"
    raise SystemExit(
        f"错误：端口 {port} 已被其他程序占用，且无法确认为本系统的残留进程，未自动结束。\n"
        f"请手动检查并释放端口后重试：\n{hint}"
    )


def main() -> None:
    import uvicorn

    from .app import app

    host = os.getenv("TESTSYSTEM_HOST", "127.0.0.1")
    port = int(os.getenv("TESTSYSTEM_PORT", "8000"))
    _release_stale_server(host, port)
    _write_pid_file()
    try:
        uvicorn.run(app, host=host, port=port, timeout_graceful_shutdown=3)
    finally:
        _remove_pid_file()


__all__ = ["main"]
