"""Small authenticated OKX REST v5 client used by the desktop trader."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx


class OKXError(RuntimeError):
    def __init__(self, message: str, *, code: str = "", uncertain: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.uncertain = uncertain


@dataclass(frozen=True)
class OKXCredentials:
    api_key: str
    secret_key: str
    passphrase: str

    def complete(self) -> bool:
        return all((self.api_key.strip(), self.secret_key.strip(), self.passphrase.strip()))


OKX_API_BASE_URLS = {
    "global": "https://www.okx.com",
    "eea": "https://eea.okx.com",
    "us": "https://us.okx.com",
}


class OKXClient:
    BASE_URL = "https://www.okx.com"

    def __init__(
        self,
        credentials: OKXCredentials,
        *,
        profile: str = "demo",
        base_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        if profile not in {"demo", "live"}:
            raise ValueError("profile must be demo or live")
        self.credentials = credentials
        self.profile = profile
        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._http.close()

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _headers(self, method: str, path: str, body: str) -> dict[str, str]:
        timestamp = self._timestamp()
        payload = f"{timestamp}{method.upper()}{path}{body}"
        signature = base64.b64encode(
            hmac.new(
                self.credentials.secret_key.encode(), payload.encode(), hashlib.sha256
            ).digest()
        ).decode()
        headers = {
            "Content-Type": "application/json",
            "OK-ACCESS-KEY": self.credentials.api_key,
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.credentials.passphrase,
        }
        if self.profile == "demo":
            headers["x-simulated-trading"] = "1"
        return headers

    def _api_error_message(self, code: str, message: str, status_code: int) -> str:
        """Add actionable authentication context without exposing credentials."""
        context = f"[profile: {self.profile}; host: {self.base_url}]"
        hints = {
            "50110": "请检查 API Key 的 IP 白名单是否包含当前公网 IP。",
            "50113": "签名无效；请检查 Secret Key，并确认系统时间准确。",
            "50114": "Passphrase 不正确；它是创建 API Key 时自行设置的密码。",
            "50119": "API Key 不存在；请检查模拟盘／实盘是否匹配，并选择帐户所属的 Global、EEA 或 US 网域。",
        }
        hint = hints.get(code)
        if not hint and status_code == 401:
            hint = "请检查 API Key、Secret Key、Passphrase、模拟盘／实盘、区域网域及 IP 白名单。"
        detail = f"OKX API 错误 {code}: {message}" if code else f"OKX 身份验证失败 (HTTP {status_code})"
        return " ".join(part for part in (detail, hint, context) if part)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> list[dict[str, Any]]:
        query = urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})
        request_path = path + (f"?{query}" if query else "")
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) if payload else ""
        headers = self._headers(method, request_path, body) if auth else {"Content-Type": "application/json"}
        try:
            response = self._http.request(method, request_path, content=body or None, headers=headers)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise OKXError(f"OKX 网络请求状态不确定：{exc}", uncertain=method.upper() != "GET") from exc
        except httpx.HTTPError as exc:
            raise OKXError(f"OKX HTTP 请求失败：{exc}") from exc

        try:
            result = response.json()
        except ValueError:
            result = None
        if isinstance(result, dict):
            code = str(result.get("code", ""))
            if code and code != "0":
                message = str(result.get("msg") or "OKX API error")
                raise OKXError(
                    self._api_error_message(code, message, response.status_code), code=code
                )
        if response.status_code >= 400:
            if response.status_code == 401:
                raise OKXError(self._api_error_message("", "", response.status_code))
            raise OKXError(
                f"OKX HTTP 响应失败：HTTP {response.status_code} "
                f"[profile: {self.profile}; host: {self.base_url}]"
            )
        if not isinstance(result, dict):
            raise OKXError(
                f"OKX 返回了无法解析的响应 [profile: {self.profile}; host: {self.base_url}]"
            )
        code = str(result.get("code", ""))
        if code != "0":
            raise OKXError(str(result.get("msg") or "OKX API error"), code=code)
        data = result.get("data")
        return data if isinstance(data, list) else []

    def public_instrument(self, inst_id: str) -> dict[str, Any]:
        data = self.request(
            "GET", "/api/v5/public/instruments",
            params={"instType": "SWAP", "instId": inst_id}, auth=False,
        )
        if not data:
            raise OKXError(f"OKX 找不到合约 {inst_id}")
        return data[0]

    def account_config(self) -> dict[str, Any]:
        data = self.request("GET", "/api/v5/account/config")
        return data[0] if data else {}

    def usdt_equity(self) -> str:
        data = self.request("GET", "/api/v5/account/balance", params={"ccy": "USDT"})
        details = (data[0].get("details") if data else []) or []
        row = next((item for item in details if item.get("ccy") == "USDT"), {})
        return str(row.get("eq") or row.get("cashBal") or "0")

    def positions(self, inst_id: str) -> list[dict[str, Any]]:
        return self.request("GET", "/api/v5/account/positions", params={"instId": inst_id})

    def set_leverage(self, inst_id: str, leverage: int, margin_mode: str) -> dict[str, Any]:
        data = self.request("POST", "/api/v5/account/set-leverage", payload={
            "instId": inst_id, "lever": str(leverage), "mgnMode": margin_mode,
        })
        return data[0] if data else {}

    def place_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = self.request("POST", "/api/v5/trade/order", payload=payload)
        row = data[0] if data else {}
        if str(row.get("sCode", "0")) != "0":
            raise OKXError(str(row.get("sMsg") or "委托被拒绝"), code=str(row.get("sCode")))
        return row

    def get_order(self, inst_id: str, *, ord_id: str = "", cl_ord_id: str = "") -> dict[str, Any]:
        data = self.request("GET", "/api/v5/trade/order", params={
            "instId": inst_id, "ordId": ord_id, "clOrdId": cl_ord_id,
        })
        return data[0] if data else {}

    def amend_order(self, inst_id: str, ord_id: str, new_px: str) -> dict[str, Any]:
        data = self.request("POST", "/api/v5/trade/amend-order", payload={
            "instId": inst_id, "ordId": ord_id, "newPx": new_px,
        })
        return data[0] if data else {}

    def cancel_order(self, inst_id: str, ord_id: str) -> dict[str, Any]:
        data = self.request("POST", "/api/v5/trade/cancel-order", payload={
            "instId": inst_id, "ordId": ord_id,
        })
        return data[0] if data else {}

    def place_algo_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = self.request("POST", "/api/v5/trade/order-algo", payload=payload)
        row = data[0] if data else {}
        if str(row.get("sCode", "0")) != "0":
            raise OKXError(str(row.get("sMsg") or "保护委托被拒绝"), code=str(row.get("sCode")))
        return row

    def cancel_algos(self, items: list[dict[str, str]]) -> list[dict[str, Any]]:
        return self.request("POST", "/api/v5/trade/cancel-algos", payload=items)

    def get_algo_order(self, algo_id: str) -> dict[str, Any]:
        data = self.request("GET", "/api/v5/trade/order-algo", params={"algoId": algo_id})
        return data[0] if data else {}

    def find_pending_algo(self, algo_cl_ord_id: str) -> dict[str, Any]:
        data = self.request(
            "GET", "/api/v5/trade/orders-algo-pending",
            params={"ordType": "trigger", "algoClOrdId": algo_cl_ord_id},
        )
        return next((row for row in data if row.get("algoClOrdId") == algo_cl_ord_id), {})

    def fills(self, inst_id: str, ord_id: str = "") -> list[dict[str, Any]]:
        return self.request(
            "GET", "/api/v5/trade/fills", params={"instType": "SWAP", "instId": inst_id, "ordId": ord_id}
        )
