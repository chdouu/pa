"""Local secret storage helpers.

OKX secrets are never written to settings.json in plaintext.  On Windows they
are protected for the current Windows user with DPAPI.
"""
from __future__ import annotations

import base64
import sys


class SecretStorageError(RuntimeError):
    pass


def protect_secret(value: str) -> str:
    if not value:
        return ""
    if sys.platform != "win32":
        raise SecretStorageError("OKX 密钥加密目前仅支持 Windows DPAPI")
    try:
        import win32crypt  # type: ignore[import]

        protected = win32crypt.CryptProtectData(
            value.encode("utf-8"), "PA Agent OKX", None, None, None, 0
        )
        blob = protected[1] if isinstance(protected, tuple) else protected
        return "dpapi:" + base64.b64encode(blob).decode("ascii")
    except Exception as exc:  # noqa: BLE001
        raise SecretStorageError(f"无法使用 Windows DPAPI 加密密钥：{exc}") from exc


def unprotect_secret(value: str) -> str:
    if not value:
        return ""
    if not value.startswith("dpapi:"):
        raise SecretStorageError("拒绝读取非 DPAPI 格式的 OKX 密钥")
    if sys.platform != "win32":
        raise SecretStorageError("OKX 密钥解密目前仅支持 Windows DPAPI")
    try:
        import win32crypt  # type: ignore[import]

        raw = base64.b64decode(value[6:].encode("ascii"), validate=True)
        unprotected = win32crypt.CryptUnprotectData(raw, None, None, None, 0)
        blob = unprotected[1] if isinstance(unprotected, tuple) else unprotected
        return blob.decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        raise SecretStorageError(f"无法使用 Windows DPAPI 解密密钥：{exc}") from exc
