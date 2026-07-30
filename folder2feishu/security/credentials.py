from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import tempfile
import threading
from contextlib import suppress
from pathlib import Path
from typing import Protocol

_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_DPAPI_DESCRIPTION = "Folder2FeishuWiki credentials"
_FALLBACK_WARNING = (
    "TEST-ONLY FALLBACK: values are plaintext and protected only by file permissions"
)


class CredentialStoreError(RuntimeError):
    """Credential storage failed without exposing the credential value."""


class DPAPIUnavailableError(CredentialStoreError):
    pass


class CredentialStore(Protocol):
    backend_name: str
    persistent: bool

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str) -> None: ...

    def delete(self, key: str) -> None: ...


def _validate_key(key: str) -> str:
    if not _KEY_PATTERN.fullmatch(key):
        raise ValueError("credential key must match [A-Za-z0-9_.:-]{1,128}")
    return key


def _atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        with suppress(OSError):
            os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
        with suppress(OSError):
            os.chmod(path, mode)
    finally:
        with suppress(OSError):
            temporary_path.unlink(missing_ok=True)


class MemoryCredentialStore:
    """Non-persistent store intended for tests and short-lived tooling."""

    backend_name = "memory-test-fallback"
    persistent = False

    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._lock = threading.RLock()

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._values.get(_validate_key(key))

    def set(self, key: str, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError("credential value must be text")
        with self._lock:
            self._values[_validate_key(key)] = value

    def delete(self, key: str) -> None:
        with self._lock:
            self._values.pop(_validate_key(key), None)


class RestrictedFileCredentialStore:
    """Explicit non-Windows test fallback.

    Values are *not encrypted*. The JSON file is marked as a test-only fallback
    and is written with mode 0600 where the platform supports POSIX modes.
    Production callers must use :func:`create_credential_store`, which refuses
    this backend unless ``allow_test_fallback`` is explicitly enabled.
    """

    backend_name = "restricted-file-test-fallback-plaintext"
    persistent = True

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CredentialStoreError("credential fallback file is unreadable") from exc
        if payload.get("backend") != self.backend_name:
            raise CredentialStoreError("unexpected credential fallback file format")
        values = payload.get("values")
        if not isinstance(values, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in values.items()
        ):
            raise CredentialStoreError("credential fallback file is malformed")
        return values

    def _write(self, values: dict[str, str]) -> None:
        payload = {
            "version": 1,
            "backend": self.backend_name,
            "warning": _FALLBACK_WARNING,
            "values": values,
        }
        _atomic_write(
            self.path,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._read().get(_validate_key(key))

    def set(self, key: str, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError("credential value must be text")
        with self._lock:
            values = self._read()
            values[_validate_key(key)] = value
            self._write(values)

    def delete(self, key: str) -> None:
        with self._lock:
            values = self._read()
            values.pop(_validate_key(key), None)
            self._write(values)


if os.name == "nt":

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", ctypes.c_uint32),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    def _blob_from_bytes(value: bytes) -> tuple[_DATA_BLOB, object]:
        buffer = ctypes.create_string_buffer(value)
        blob = _DATA_BLOB(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        return blob, buffer

    def _dpapi_protect(value: bytes) -> bytes:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        input_blob, input_buffer = _blob_from_bytes(value)
        output_blob = _DATA_BLOB()
        # Keep the input buffer alive through CryptProtectData.
        _ = input_buffer
        if not crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            _DPAPI_DESCRIPTION,
            None,
            None,
            None,
            0x1,  # CRYPTPROTECT_UI_FORBIDDEN
            ctypes.byref(output_blob),
        ):
            raise CredentialStoreError("Windows DPAPI encryption failed")
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            kernel32.LocalFree(output_blob.pbData)

    def _dpapi_unprotect(value: bytes) -> bytes:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        input_blob, input_buffer = _blob_from_bytes(value)
        output_blob = _DATA_BLOB()
        _ = input_buffer
        if not crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            None,
            None,
            None,
            None,
            0x1,
            ctypes.byref(output_blob),
        ):
            raise CredentialStoreError("Windows DPAPI decryption failed for the current user")
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            kernel32.LocalFree(output_blob.pbData)

else:

    def _dpapi_protect(value: bytes) -> bytes:
        del value
        raise DPAPIUnavailableError("Windows DPAPI is unavailable on this platform")

    def _dpapi_unprotect(value: bytes) -> bytes:
        del value
        raise DPAPIUnavailableError("Windows DPAPI is unavailable on this platform")


class DPAPICredentialStore:
    """Per-user Windows DPAPI encrypted credential store."""

    backend_name = "windows-dpapi-current-user"
    persistent = True

    def __init__(self, path: str | Path) -> None:
        if os.name != "nt":
            raise DPAPIUnavailableError("Windows DPAPI is unavailable")
        self.path = Path(path)
        self._lock = threading.RLock()

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            envelope = json.loads(self.path.read_text(encoding="utf-8"))
            if envelope.get("backend") != self.backend_name:
                raise CredentialStoreError("unexpected DPAPI credential file format")
            ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
            payload = json.loads(_dpapi_unprotect(ciphertext).decode("utf-8"))
        except CredentialStoreError:
            raise
        except (OSError, KeyError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise CredentialStoreError("DPAPI credential file is unreadable") from exc
        values = payload.get("values")
        if not isinstance(values, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in values.items()
        ):
            raise CredentialStoreError("DPAPI credential payload is malformed")
        return values

    def _write(self, values: dict[str, str]) -> None:
        plaintext = json.dumps(
            {"version": 1, "values": values},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        ciphertext = _dpapi_protect(plaintext)
        envelope = json.dumps(
            {
                "version": 1,
                "backend": self.backend_name,
                "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        _atomic_write(self.path, envelope)

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._read().get(_validate_key(key))

    def set(self, key: str, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError("credential value must be text")
        with self._lock:
            values = self._read()
            values[_validate_key(key)] = value
            self._write(values)

    def delete(self, key: str) -> None:
        with self._lock:
            values = self._read()
            values.pop(_validate_key(key), None)
            self._write(values)


def create_credential_store(
    path: str | Path, *, allow_test_fallback: bool = False
) -> CredentialStore:
    """Return the supported production store for the current platform.

    Folder2Feishu is a Windows application, so production credentials must use
    DPAPI. A visibly marked plaintext file fallback is available only when a
    non-Windows test explicitly opts in.
    """

    if os.name == "nt":
        return DPAPICredentialStore(path)
    if allow_test_fallback:
        return RestrictedFileCredentialStore(path)
    raise DPAPIUnavailableError(
        "production credential persistence requires Windows DPAPI; "
        "use MemoryCredentialStore for tests"
    )
