"""Stores the Gemini API key for this Windows user only (DPAPI-encrypted); never logged or mirrored."""
from __future__ import annotations

import base64
import ctypes
import os
import sys
from pathlib import Path
from typing import Optional

from jarvis_books.paths import data_dir

ENV_VARS = ("JARVIS_GEMINI_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("Windows could not protect the key")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def key_path(root: Optional[Path] = None) -> Path:
    return (root or data_dir()) / "gemini.key"


def save_key(key: str, root: Optional[Path] = None) -> Path:
    key = key.strip()
    if not key:
        raise ValueError("empty key")
    raw = key.encode()
    data = b"dpapi:" + base64.b64encode(_dpapi(raw, True)) if sys.platform == "win32" else b"plain:" + base64.b64encode(raw)
    path = key_path(root)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    if sys.platform != "win32":
        os.chmod(tmp, 0o600)
    tmp.replace(path)
    return path


def load_key(root: Optional[Path] = None) -> Optional[str]:
    for var in ENV_VARS:
        if os.environ.get(var):
            return os.environ[var].strip()
    path = key_path(root)
    if not path.exists():
        return None
    data = path.read_bytes()
    scheme, _, payload = data.partition(b":")
    raw = base64.b64decode(payload)
    if scheme == b"dpapi":
        raw = _dpapi(raw, False)
    return raw.decode().strip() or None
