"""MES 报告上传：按配置的报文模板把测试报告推送到 MES HTTP 接口。

要点：
- 报文模式：full 直接发送标准报告 JSON；mapped 按 payload_mapping 模板渲染
  （模板中可用 ${report_no}、${sn}、${report} 等占位符，见 utils.render_template）；
- 幂等：报告编号写入幂等请求头（默认 Idempotency-Key），MES 端可据此去重；
- 每次上传（无论成败）都写入 mes_upload_logs，报告行记录最新状态与响应摘要，
  失败可在报告中心手动重试；上传全程用 urllib 实现，无第三方依赖。
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from typing import Any

from .database import Database
from .utils import json_dumps, render_template, utc_now


class MESUploadError(RuntimeError):
    """上传失败：response 保留 MES 返回原文（截断后入库），便于排查对端问题。"""

    def __init__(self, message: str, response: str = ""):
        super().__init__(message)
        self.response = response


def build_payload(report: dict[str, Any], config: dict[str, Any]) -> Any:
    """构造待发送报文：full 模式原样返回报告；mapped 模式渲染字段映射模板。

    模板上下文中报告字段平铺可用，同时提供 ${report} 引用完整报告对象。
    """
    context = {**report, "report": report}
    if config.get("payload_mode", "mapped") == "full":
        return report
    mapping = config.get("payload_mapping")
    if not isinstance(mapping, (dict, list)):
        raise MESUploadError("MES payload_mapping 必须是 JSON 对象或数组")
    return render_template(mapping, context)


class MESUploader:
    """上传器：状态机 PENDING → UPLOADING → SUCCESS/FAILED，每次尝试都留痕。"""

    def __init__(self, database: Database):
        self.db = database

    def upload(self, report_id: str) -> dict[str, Any]:
        """上传一份报告；已成功过的报告直接返回，不重复发送（本地幂等）。"""
        # 先在事务内占位为 UPLOADING 并递增尝试次数，防止并发重复上传
        with self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            if row is None:
                raise MESUploadError("报告不存在")
            if row["mes_status"] == "SUCCESS":
                return {"status": "SUCCESS", "message": "报告已上传，无需重复上传"}
            attempt = int(row["mes_attempts"]) + 1
            connection.execute(
                "UPDATE reports SET mes_status = 'UPLOADING', mes_attempts = ? WHERE id = ?",
                (attempt, report_id),
            )
            report = json.loads(row["report_json"])

        config = self.db.setting("mes", {})
        payload: Any = None
        try:
            payload = build_payload(report, config)
            response = self._send(payload, report, config)
        except Exception as exc:
            # 失败路径：状态置 FAILED、记录响应与上传日志后，把错误抛给调用方提示
            error = exc if isinstance(exc, MESUploadError) else MESUploadError(f"MES 上传异常：{exc}")
            response_text = error.response or str(error)
            with self.db.transaction() as connection:
                connection.execute(
                    "UPDATE reports SET mes_status = 'FAILED', mes_response = ? WHERE id = ?",
                    (response_text[:8000], report_id),
                )
                connection.execute(
                    "INSERT INTO mes_upload_logs(report_id, attempt, request_json, success, response, created_at) "
                    "VALUES (?, ?, ?, 0, ?, ?)",
                    (report_id, attempt, json_dumps(payload), response_text[:8000], utc_now()),
                )
            raise error

        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE reports SET mes_status = 'SUCCESS', mes_response = ?, uploaded_at = ? WHERE id = ?",
                (response[:8000], utc_now(), report_id),
            )
            connection.execute(
                "INSERT INTO mes_upload_logs(report_id, attempt, request_json, success, response, created_at) "
                "VALUES (?, ?, ?, 1, ?, ?)",
                (report_id, attempt, json_dumps(payload), response[:8000], utc_now()),
            )
        return {"status": "SUCCESS", "response": response, "attempt": attempt}

    def _send(self, payload: Any, report: dict[str, Any], config: dict[str, Any]) -> str:
        """发送 HTTP 请求并返回响应文本；非 2xx、网络错误都包装为 MESUploadError。"""
        endpoint = str(config.get("endpoint", "")).strip()
        if not endpoint.startswith(("http://", "https://")):
            raise MESUploadError("MES 地址必须是有效的 HTTP/HTTPS URL")
        method = str(config.get("method", "POST")).upper()
        if method not in {"POST", "PUT", "PATCH"}:
            raise MESUploadError("MES 请求方法仅支持 POST、PUT、PATCH")
        headers = {str(key): str(value) for key, value in config.get("headers", {}).items()}
        headers.setdefault("Content-Type", "application/json; charset=utf-8")
        # 幂等请求头：报告编号作为幂等键，MES 端重复接收时可安全去重
        idempotency_header = str(config.get("idempotency_header", "")).strip()
        if idempotency_header:
            headers[idempotency_header] = str(report["report_no"])
        request = urllib.request.Request(
            endpoint,
            data=json_dumps(payload).encode("utf-8"),
            headers=headers,
            method=method,
        )
        # 产线自签名证书场景可配置 verify_tls=false 跳过校验（默认校验）
        ssl_context = None
        if endpoint.startswith("https://") and config.get("verify_tls", True) is False:
            ssl_context = ssl._create_unverified_context()
        try:
            with urllib.request.urlopen(
                request,
                timeout=float(config.get("timeout_seconds", 10)),
                context=ssl_context,
            ) as response:
                body = response.read(65536).decode("utf-8", errors="replace")
                if not 200 <= response.status < 300:
                    raise MESUploadError(f"MES 返回 HTTP {response.status}", body)
                return body or f"HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            body = exc.read(65536).decode("utf-8", errors="replace")
            raise MESUploadError(f"MES 返回 HTTP {exc.code}：{body[:500]}", body) from exc
        except urllib.error.URLError as exc:
            raise MESUploadError(f"MES 网络连接失败：{exc.reason}") from exc
