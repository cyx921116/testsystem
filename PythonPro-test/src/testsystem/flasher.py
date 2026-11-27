"""固件烧录框架。

框架阶段：定义统一的后端接口、固件仓管理和可追溯烧录记录，内置仿真烧录器，
无硬件也能跑通烧录流程。执行器、编译器和前端的接入路线见 docs/FIRMWARE_FLASH.md。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .utils import json_dumps, new_id, render_template, utc_now

ProgressCallback = Callable[[int, str], None]

INDEX_FILE = "firmware.json"


class FlashError(RuntimeError):
    """固件烧录相关错误的统一异常。"""


def firmware_dir() -> Path:
    """固件仓目录：TESTSYSTEM_DATA_DIR/firmware，默认当前目录 data/firmware。"""
    base = Path(os.getenv("TESTSYSTEM_DATA_DIR", str(Path.cwd() / "data")))
    return base / "firmware"


def sha256_file(path: str | Path) -> str:
    """分块计算文件 SHA-256（1MB 一块，固件文件可能很大）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_firmware_index() -> list[dict[str, Any]]:
    """读取固件仓清单（firmware.json）；文件不存在视为空仓，格式错误抛 FlashError。"""
    index_path = firmware_dir() / INDEX_FILE
    if not index_path.exists():
        return []
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FlashError(f"固件清单读取失败：{exc}") from exc
    if not isinstance(data, list):
        raise FlashError("固件清单格式错误：firmware.json 应为数组")
    return data


def save_firmware_index(index: list[dict[str, Any]]) -> None:
    """把固件仓清单写回 firmware.json（目录不存在时自动创建）。"""
    directory = firmware_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / INDEX_FILE).write_text(json_dumps(index), encoding="utf-8")


def register_firmware(
    source: str | Path,
    version: str,
    product_model: str = "",
    description: str = "",
) -> dict[str, Any]:
    """把固件文件复制进固件仓并登记到清单，返回清单条目。同名固件重新登记会覆盖旧条目。"""
    source_path = Path(source)
    if not source_path.is_file():
        raise FlashError(f"固件文件不存在：{source_path}")
    if not str(version).strip():
        raise FlashError("固件版本不能为空")
    directory = firmware_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target_path = directory / source_path.name
    if not (target_path.exists() and sha256_file(target_path) == sha256_file(source_path)):
        shutil.copy2(source_path, target_path)
    entry = {
        "id": new_id("fw"),
        "name": source_path.name,
        "version": str(version),
        "product_model": product_model,
        "description": description,
        "size": target_path.stat().st_size,
        "sha256": sha256_file(target_path),
        "registered_at": utc_now(),
    }
    index = [item for item in load_firmware_index() if item.get("name") != entry["name"]]
    index.append(entry)
    save_firmware_index(index)
    return entry


def validate_firmware(entry: dict[str, Any]) -> dict[str, Any]:
    """确认固件文件存在且 SHA-256 与登记一致，返回附带实际路径的清单条目。"""
    path = firmware_dir() / str(entry.get("name", ""))
    if not path.is_file():
        raise FlashError(f"固件文件丢失：{path.name}")
    if sha256_file(path) != entry.get("sha256"):
        raise FlashError(f"固件校验和不匹配：{path.name}")
    return {**entry, "path": str(path)}


def find_firmware(name_or_id: str) -> dict[str, Any]:
    """按名称或 ID 查找固件并校验文件完整性。"""
    for item in load_firmware_index():
        if item.get("name") == name_or_id or item.get("id") == name_or_id:
            return validate_firmware(item)
    raise FlashError(f"固件未登记：{name_or_id}")


def default_flash_options() -> dict[str, Any]:
    """烧录默认选项：擦除、校验、复位开启，超时 60 秒。"""
    return {"erase": True, "verify": True, "reset": True, "timeout_ms": 60000}


def validate_flash_request(request: dict[str, Any]) -> dict[str, Any]:
    """规范化烧录请求：固件必须已登记，选项与默认值合并。"""
    if not isinstance(request, dict):
        raise FlashError("烧录请求必须是对象")
    firmware = request.get("firmware")
    if not isinstance(firmware, str) or not firmware.strip():
        raise FlashError("烧录请求缺少固件名称或 ID")
    raw_options = request.get("options")
    if raw_options is None:
        raw_options = {}
    if not isinstance(raw_options, dict):
        raise FlashError("烧录选项必须是对象")
    options = {**default_flash_options(), **raw_options}
    try:
        options["timeout_ms"] = int(options.get("timeout_ms", 60000))
    except (TypeError, ValueError) as exc:
        raise FlashError("烧录超时必须是数字") from exc
    if options["timeout_ms"] <= 0:
        raise FlashError("烧录超时必须大于 0")
    for key in ("erase", "verify", "reset"):
        options[key] = bool(options.get(key))
    variables = request.get("variables")
    return {
        "firmware": firmware.strip(),
        "sn": str(request.get("sn") or ""),
        "target": str(request.get("target") or ""),
        "options": options,
        "variables": variables if isinstance(variables, dict) else {},
    }


def build_flash_record(
    request: dict[str, Any],
    result: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成可追溯的烧录记录（哪个 SN 烧了什么固件、结果如何），供后续持久化。"""
    context = context or {}
    return {
        "run_id": context.get("run_id", ""),
        "item": context.get("item", ""),
        "sn": request.get("sn", ""),
        "station": context.get("station", ""),
        "operator": context.get("operator", ""),
        "firmware": result.get("firmware", {}),
        "backend": result.get("backend", ""),
        "ok": bool(result.get("ok")),
        "message": result.get("message", ""),
        "duration_ms": result.get("duration_ms", 0),
        "details": result.get("details", {}),
        "finished_at": result.get("finished_at", utc_now()),
    }


class BaseFlasher:
    """烧录后端统一接口。config 为该后端的连接参数，结构与协议配置一致为普通 dict。"""

    name = "base"

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        self.connected = False

    def probe(self) -> dict[str, Any]:
        """检查驱动与烧录器是否可用，返回 {"ok": bool, "message": str}。"""
        raise NotImplementedError

    def flash(self, request: dict[str, Any], on_progress: ProgressCallback | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def close(self) -> None:
        self.connected = False

    @staticmethod
    def _tick(on_progress: ProgressCallback | None, percent: int, message: str) -> None:
        if on_progress:
            on_progress(percent, message)


class SimulatorFlasher(BaseFlasher):
    """仿真烧录器：不访问硬件，分阶段模拟烧录过程，可用 fail_at_stage 注入失败。"""

    name = "simulator"

    def probe(self) -> dict[str, Any]:
        return {"ok": True, "message": "仿真烧录器就绪"}

    def flash(self, request: dict[str, Any], on_progress: ProgressCallback | None = None) -> dict[str, Any]:
        """按选项组合出阶段序列逐段模拟；fail_at_stage 命中的阶段抛错，用于演示失败路径。"""
        request = validate_flash_request(request)
        firmware = find_firmware(request["firmware"])
        options = request["options"]
        stages = ["连接烧录器"]
        if options["erase"]:
            stages.append("擦除 Flash")
        stages.append("写入固件")
        if options["verify"]:
            stages.append("校验")
        if options["reset"]:
            stages.append("复位运行")
        fail_at = str(self.config.get("fail_at_stage", ""))
        delay = float(self.config.get("step_delay_ms", 50)) / 1000
        started = time.monotonic()
        done: list[str] = []
        for index, stage in enumerate(stages):
            self._tick(on_progress, int((index + 1) / len(stages) * 100), stage)
            if delay > 0:
                time.sleep(delay)
            if fail_at == stage:
                raise FlashError(f"仿真烧录在阶段“{stage}”失败")
            done.append(stage)
        return {
            "ok": True,
            "backend": self.name,
            "firmware": firmware,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "message": f"仿真烧录完成：{firmware['name']} v{firmware['version']}",
            "details": {"stages": done},
            "finished_at": utc_now(),
        }


class JLinkFlasher(BaseFlasher):
    """J-Link 烧录：连接、擦除、下载、复位的骨架。

    Flash 下载使用 SEGGER J-Link 自带设备算法；量产前需按芯片确认
    安全解锁、量产校验和复位时序，详见 docs/FIRMWARE_FLASH.md。
    """

    name = "jlink"

    def probe(self) -> dict[str, Any]:
        module = DRIVER_MODULES[self.name]
        if module and importlib.util.find_spec(module) is None:
            return {"ok": False, "message": "缺少 pylink-square，请执行 uv sync --extra hardware"}
        return {"ok": True, "message": "pylink-square 可用"}

    def flash(self, request: dict[str, Any], on_progress: ProgressCallback | None = None) -> dict[str, Any]:
        """J-Link 烧录主流程：连接 → 擦除 → 下载 → 校验 → 复位；设备句柄在 finally 中关闭。"""
        request = validate_flash_request(request)
        firmware = find_firmware(request["firmware"])
        options = request["options"]
        started = time.monotonic()
        try:
            import pylink
        except ImportError as exc:
            raise FlashError("缺少 pylink-square，请执行 uv sync --extra hardware") from exc
        device = None
        details: dict[str, Any] = {"chip": self.config.get("chip_name", "Cortex-M4"), "verified": False}
        try:
            device = pylink.JLink()
            device.open(self.config.get("dll_path") or None)
            interface = self.config.get("interface", "SWD")
            device.set_tif(pylink.enums.JLinkInterfaces[interface])
            device.connect(details["chip"], speed=int(self.config.get("speed_khz", 4000)))
            self.connected = True
            self._tick(on_progress, 5, "J-Link 已连接")
            # TODO: 安全解锁（RDP/TrustZone 回退等）按芯片封装。
            if options["erase"]:
                self._tick(on_progress, 15, "擦除 Flash")
                device.erase_chip()
            base_address = int(str(self.config.get("base_address", "0x0")), 0)
            self._tick(on_progress, 30, f"下载固件 {firmware['name']}")
            device.flash_file(firmware["path"], base_address)
            if options["verify"]:
                self._tick(on_progress, 85, "校验 Flash")
                # TODO: 量产校验按芯片封装（CRC 或回读比对），当前明确标记为未校验。
                details["verify_note"] = "校验尚未按芯片封装，已跳过"
            if options["reset"]:
                self._tick(on_progress, 95, "复位运行")
                device.reset()
            return {
                "ok": True,
                "backend": self.name,
                "firmware": firmware,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "message": f"J-Link 烧录完成：{firmware['name']} v{firmware['version']}",
                "details": details,
                "finished_at": utc_now(),
            }
        except FlashError:
            raise
        except Exception as exc:
            raise FlashError(f"J-Link 烧录失败：{exc}") from exc
        finally:
            if device is not None:
                device.close()
            self.connected = False


class ExternalToolFlasher(BaseFlasher):
    """外部命令行工具烧录：通过命令模板调用 STM32_Programmer_CLI、openocd 等。

    config 字段：command（命令模板，支持 ${firmware}、${sn}、${target} 及变量）、
    timeout_ms（默认 120000）、success_text（返回码为 0 且输出包含该文本才成功，空则只看返回码）。
    """

    name = "external_tool"

    def probe(self) -> dict[str, Any]:
        if not str(self.config.get("command", "")).strip():
            return {"ok": False, "message": "未配置烧录命令模板"}
        return {"ok": True, "message": "已配置外部烧录命令"}

    def flash(self, request: dict[str, Any], on_progress: ProgressCallback | None = None) -> dict[str, Any]:
        """渲染命令模板并执行外部工具：返回码为 0 且输出含 success_text（若配置）才算成功。"""
        request = validate_flash_request(request)
        firmware = find_firmware(request["firmware"])
        command_template = str(self.config.get("command", ""))
        if not command_template.strip():
            raise FlashError("未配置烧录命令模板")
        context = {
            "firmware": firmware["path"],
            "sn": request["sn"],
            "target": request["target"],
            **request["variables"],
        }
        command = render_template(command_template, context)
        timeout_ms = int(self.config.get("timeout_ms", 120000))
        self._tick(on_progress, 10, "启动外部烧录工具")
        started = time.monotonic()
        try:
            completed = subprocess.run(
                _split_command(command),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_ms / 1000,
            )
        except subprocess.TimeoutExpired as exc:
            raise FlashError(f"外部烧录工具超时（{timeout_ms} ms）") from exc
        except OSError as exc:
            raise FlashError(f"外部烧录工具启动失败：{exc}") from exc
        output = (completed.stdout or "") + (completed.stderr or "")
        success_text = str(self.config.get("success_text", ""))
        if completed.returncode != 0 or (success_text and success_text not in output):
            raise FlashError(f"外部烧录工具失败（返回码 {completed.returncode}）：{output.strip()[-500:]}")
        return {
            "ok": True,
            "backend": self.name,
            "firmware": firmware,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "message": "外部烧录工具执行成功",
            "details": {"returncode": completed.returncode, "output_tail": output.strip()[-500:]},
            "finished_at": utc_now(),
        }


def _split_command(command: str) -> list[str]:
    # Windows 路径含反斜杠，POSIX 拆分规则会把反斜杠当转义符，因此按非 POSIX 规则拆分后去掉引号。
    if os.name == "nt":
        return [part.strip('"') for part in shlex.split(command, posix=False)]
    return shlex.split(command)


# 烧录后端注册表：名称 → 实现类
FLASHER_CLASSES = {
    "simulator": SimulatorFlasher,
    "jlink": JLinkFlasher,
    "external_tool": ExternalToolFlasher,
}

# 后端 → 依赖的驱动模块（None 表示无外部依赖），用于目录中的“驱动可用”标记
DRIVER_MODULES = {
    "simulator": None,
    "jlink": "pylink",
    "external_tool": None,
}


def create_flasher(name: str, config: dict[str, Any] | None = None) -> BaseFlasher:
    """按名称实例化烧录后端；未知名称抛 FlashError。"""
    try:
        return FLASHER_CLASSES[name](config)
    except KeyError as exc:
        raise FlashError(f"不支持的烧录后端：{name}") from exc


def flasher_catalog() -> list[dict[str, Any]]:
    """烧录后端目录（固件烧录页下拉用）：描述取类 docstring 首行，并标记驱动可用性。"""
    catalog = []
    for name, cls in FLASHER_CLASSES.items():
        module = DRIVER_MODULES[name]
        description = (cls.__doc__ or "").strip()
        catalog.append({
            "name": name,
            "description": description.splitlines()[0] if description else "",
            "driver_available": module is None or importlib.util.find_spec(module) is not None,
        })
    return catalog
