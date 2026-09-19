"""Read a manager-owned DPAPI settings snapshot without touching GUI settings."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path

from pa_agent.config.settings import Settings


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def load_managed_settings(path: str):
    data = Path(path).read_bytes()
    buf = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    output = _Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source), None, None,
                                                     None, None, 0x01, ctypes.byref(output)):
        raise RuntimeError("Manager settings cannot be decrypted by this Windows user")
    try:
        return Settings.model_validate_json(ctypes.string_at(output.pbData, output.cbData))
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)
