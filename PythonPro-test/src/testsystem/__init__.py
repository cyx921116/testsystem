"""应用入口与启动辅助：PID 文件管理、残留服务进程的端口释放。

直接运行 ``python src/main.py``（或 ``python -m testsystem``）时由 ``main()`` 启动 uvicorn。
考虑到生产工作站常被直接断电/关窗，端口可能被上次异常退出的进程占用，
启动前会按 PID 文件与端口监听记录找到残留的 python 进程并结束它（仅限 python 进程，
避免误杀其他程序）；无法确认归属时给出明确的手动处理提示并退出。
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
from pathlib import Path

_PID_FILE_NAME = "testsystem.pid"
_STILL_ACTIVE = 259  # Windows GetExitCodeProcess 的“仍在运行”退出码
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _data_dir() -> Path:
    """数据目录：默认 ``<启动时工作目录>/data``，可用 TESTSYSTEM_DATA_DIR 环境变量统一指定。

    注意：从项目根目录与从 src 目录启动会得到不同的数据目录（数据库也随之不同），
    部署时应固定启动目录或显式设置 TESTSYSTEM_DATA_DIR。
    """
    return Path(os.getenv("TESTSYSTEM_DATA_DIR", str(Path.cwd() / "data")))


def _pid_file() -> Path:
    """PID 文件路径，用于下次启动时识别并清理残留进程。"""
    return _data_dir() / _PID_FILE_NAME


def _read_pid_file() -> int | None:
    """读取上次记录的 PID；文件缺失或内容非法时返回 None。"""
    try:
        return int(_pid_file().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _write_pid_file() -> None:
    """写入当前进程 PID；失败（如目录无权限）不影响启动。"""
    try:
        path = _pid_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass


def _remove_pid_file() -> None:
    """服务正常退出时删除 PID 文件，避免下次启动误判残留。"""
    try:
        _pid_file().unlink(missing_ok=True)
    except OSError:
        pass


def _port_available(host: str, port: int) -> bool:
    """探测端口是否可绑定（即可用）。通配地址改用回环地址探测，兼容 IPv4/IPv6。"""
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
    """轮询等待端口释放（进程结束后端口可能短暂处于释放延迟中）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_available(host, port):
            return True
        time.sleep(0.1)
    return _port_available(host, port)


def _pid_alive(pid: int) -> bool:
    """跨平台判断进程是否存活：Windows 用 OpenProcess + 退出码，其他系统用信号 0。"""
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
    """取进程的可执行文件路径/名称，用于确认占用者是不是 python（防止误杀）。"""
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
    """解析 Windows netstat -ano 输出，提取监听指定端口的 TCP 进程 PID。"""
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
    """列出正在监听指定端口的进程 PID：Windows 用 netstat，其他系统用 lsof。"""
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
    """发送 SIGTERM 结束进程并等待其退出（最多 5 秒），返回是否已结束。"""
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
    """启动前端口被占用时，尝试识别并结束上次残留的 python 服务进程。

    只结束“镜像名含 python”的进程（PID 文件记录的优先，其次端口监听者）；
    占用者不是 python 或结束后端口仍不释放时，抛出带手动处理命令的 SystemExit。
    """
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
    """服务入口：释放残留端口占用 → 记录 PID → 启动 uvicorn → 退出时清理 PID 文件。

    监听地址由 TESTSYSTEM_HOST / TESTSYSTEM_PORT 环境变量控制，默认 127.0.0.1:8000。
    """
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
