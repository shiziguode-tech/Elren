from __future__ import annotations

import base64
import ctypes
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import tempfile
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes

_VAULT_MAGIC = b"ELRENVLT\x01"
_HEADER = struct.Struct(">HI")
_AAD_PREFIX = b"Elren local secret vault\x00"
_KEYCHAIN_SERVICE = b"com.elren.desktop.local-secret-vault"
_KEYCHAIN_ACCOUNT = b"current-user-master-key-v1"
_SECRET_TOOL_ATTRIBUTES = (
    "application",
    "elren",
    "purpose",
    "local-secret-vault-master-key-v1",
)
_ENV_ASSIGNMENT = re.compile(
    r"^(?P<prefix>\s*(?:export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*)"
    r"(?P<value>.*?)(?P<newline>\r?\n)?$"
)


class SecretStorageError(RuntimeError):
    """Base error for the local encrypted secret store."""


class SecretStorageUnavailable(SecretStorageError):
    """The operating system cannot provide a secure current-user key store."""


class SecretStorageIntegrityError(SecretStorageError):
    """An encrypted vault failed authentication or verified round-trip."""


class SecretStorageFormatError(SecretStorageError):
    """A vault or legacy credential file has an invalid format."""


class SecretProtector(Protocol):
    algorithm: str

    def protect(self, plaintext: bytes) -> bytes: ...

    def unprotect(self, protected: bytes) -> bytes: ...


@dataclass(frozen=True, slots=True, repr=False)
class VaultPayload:
    values: dict[str, Any]

    def __repr__(self) -> str:
        return "VaultPayload(values=<redacted>)"


class WindowsDPAPIProtector:
    """Protect bytes with Windows DPAPI for the interactive user only."""

    algorithm = "windows-dpapi-current-user"
    _CRYPTPROTECT_UI_FORBIDDEN = 0x01

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    def __init__(self) -> None:
        if os.name != "nt":
            raise SecretStorageUnavailable("Windows DPAPI is only available on Windows")
        from ctypes import wintypes

        self._wintypes = wintypes
        self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        blob_pointer = ctypes.POINTER(self._DATA_BLOB)
        self._crypt32.CryptProtectData.argtypes = [
            blob_pointer,
            wintypes.LPCWSTR,
            blob_pointer,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            blob_pointer,
        ]
        self._crypt32.CryptProtectData.restype = wintypes.BOOL
        self._crypt32.CryptUnprotectData.argtypes = [
            blob_pointer,
            ctypes.c_void_p,
            blob_pointer,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            blob_pointer,
        ]
        self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
        # ctypes defaults return values and parameters to 32-bit integers.  On
        # 64-bit Windows that truncates the DPAPI allocation pointer and can
        # crash during LocalFree, so the native pointer-sized ABI is explicit.
        self._kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        self._kernel32.LocalFree.restype = wintypes.HLOCAL

    @staticmethod
    def _input_blob(data: bytes) -> tuple[ctypes.Array[ctypes.c_char], _DATA_BLOB]:
        buffer = ctypes.create_string_buffer(data, max(1, len(data)))
        blob = WindowsDPAPIProtector._DATA_BLOB(
            len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        )
        return buffer, blob

    def _crypt(self, data: bytes, *, decrypt: bool) -> bytes:
        _buffer, input_blob = self._input_blob(data)
        output_blob = self._DATA_BLOB()
        operation = (
            self._crypt32.CryptUnprotectData
            if decrypt
            else self._crypt32.CryptProtectData
        )
        if decrypt:
            succeeded = operation(
                ctypes.byref(input_blob),
                None,
                None,
                None,
                None,
                self._CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output_blob),
            )
        else:
            succeeded = operation(
                ctypes.byref(input_blob),
                "Elren local secret vault",
                None,
                None,
                None,
                self._CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output_blob),
            )
        if not succeeded:
            error = ctypes.get_last_error()
            if decrypt:
                raise SecretStorageIntegrityError(
                    f"Windows DPAPI could not authenticate this vault (error {error})"
                )
            raise SecretStorageUnavailable(
                f"Windows DPAPI could not protect the vault (error {error})"
            )
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            if output_blob.pbData:
                self._kernel32.LocalFree(
                    ctypes.cast(output_blob.pbData, self._wintypes.HLOCAL)
                )

    def protect(self, plaintext: bytes) -> bytes:
        return self._crypt(plaintext, decrypt=False)

    def unprotect(self, protected: bytes) -> bytes:
        return self._crypt(protected, decrypt=True)


class AesGcmProtector:
    """Authenticated vault protection using an OS-kept 256-bit master key."""

    def __init__(self, algorithm: str, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("AES-GCM vault keys must contain exactly 32 bytes")
        self.algorithm = algorithm
        self._key = bytes(key)

    def protect(self, plaintext: bytes) -> bytes:
        nonce = get_random_bytes(12)
        cipher = AES.new(self._key, AES.MODE_GCM, nonce=nonce, mac_len=16)
        cipher.update(_AAD_PREFIX + self.algorithm.encode("utf-8"))
        ciphertext, tag = cipher.encrypt_and_digest(plaintext)
        return nonce + tag + ciphertext

    def unprotect(self, protected: bytes) -> bytes:
        if len(protected) < 28:
            raise SecretStorageFormatError("The encrypted vault payload is truncated")
        nonce, tag, ciphertext = protected[:12], protected[12:28], protected[28:]
        cipher = AES.new(self._key, AES.MODE_GCM, nonce=nonce, mac_len=16)
        cipher.update(_AAD_PREFIX + self.algorithm.encode("utf-8"))
        try:
            return cipher.decrypt_and_verify(ciphertext, tag)
        except ValueError as exc:
            raise SecretStorageIntegrityError(
                "The encrypted vault failed authentication"
            ) from exc


def _macos_keychain_key() -> bytes:
    """Read/create the master key through Security.framework, never process argv."""

    if platform.system() != "Darwin":
        raise SecretStorageUnavailable("macOS Keychain is only available on macOS")
    security = ctypes.CDLL(
        "/System/Library/Frameworks/Security.framework/Security"
    )
    find_password = security.SecKeychainFindGenericPassword
    find_password.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    find_password.restype = ctypes.c_int32
    add_password = security.SecKeychainAddGenericPassword
    add_password.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    add_password.restype = ctypes.c_int32
    free_content = security.SecKeychainItemFreeContent
    free_content.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    free_content.restype = ctypes.c_int32

    service = ctypes.create_string_buffer(_KEYCHAIN_SERVICE)
    account = ctypes.create_string_buffer(_KEYCHAIN_ACCOUNT)
    password_length = ctypes.c_uint32()
    password_data = ctypes.c_void_p()
    status = find_password(
        None,
        len(_KEYCHAIN_SERVICE),
        ctypes.cast(service, ctypes.c_void_p),
        len(_KEYCHAIN_ACCOUNT),
        ctypes.cast(account, ctypes.c_void_p),
        ctypes.byref(password_length),
        ctypes.byref(password_data),
        None,
    )
    if status == 0:
        try:
            key = ctypes.string_at(password_data, password_length.value)
        finally:
            free_content(None, password_data)
        if len(key) != 32:
            raise SecretStorageIntegrityError(
                "The Elren Keychain master key has an invalid length"
            )
        return key
    if status != -25300:  # errSecItemNotFound
        raise SecretStorageUnavailable(
            f"macOS Keychain could not read the Elren master key (status {status})"
        )

    key = get_random_bytes(32)
    key_buffer = ctypes.create_string_buffer(key, len(key))
    status = add_password(
        None,
        len(_KEYCHAIN_SERVICE),
        ctypes.cast(service, ctypes.c_void_p),
        len(_KEYCHAIN_ACCOUNT),
        ctypes.cast(account, ctypes.c_void_p),
        len(key),
        ctypes.cast(key_buffer, ctypes.c_void_p),
        None,
    )
    if status != 0:
        raise SecretStorageUnavailable(
            f"macOS Keychain could not store the Elren master key (status {status})"
        )
    return key


def _linux_secret_service_key() -> bytes:
    """Read/create a master key in Secret Service; there is no file-key fallback."""

    executable = shutil.which("secret-tool")
    if not executable:
        raise SecretStorageUnavailable(
            "Linux Secret Service is required for encrypted credential persistence"
        )
    lookup = [executable, "lookup", *_SECRET_TOOL_ATTRIBUTES]
    try:
        result = subprocess.run(
            lookup,
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SecretStorageUnavailable(
            "Linux Secret Service could not be contacted"
        ) from exc
    encoded = result.stdout.strip() if result.returncode == 0 else b""
    if encoded:
        try:
            key = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise SecretStorageIntegrityError(
                "The Secret Service master key is malformed"
            ) from exc
        if len(key) != 32:
            raise SecretStorageIntegrityError(
                "The Secret Service master key has an invalid length"
            )
        return key

    key = get_random_bytes(32)
    encoded = base64.b64encode(key)
    try:
        stored = subprocess.run(
            [
                executable,
                "store",
                "--label=Elren local secret vault",
                *_SECRET_TOOL_ATTRIBUTES,
            ],
            input=encoded + b"\n",
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SecretStorageUnavailable(
            "Linux Secret Service could not store the Elren master key"
        ) from exc
    if stored.returncode != 0:
        raise SecretStorageUnavailable(
            "Linux Secret Service refused the Elren master key"
        )
    # Verify the persistent copy before encrypting any only copy of a secret.
    try:
        verified = subprocess.run(
            lookup,
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SecretStorageUnavailable(
            "Linux Secret Service could not verify the Elren master key"
        ) from exc
    if verified.returncode != 0 or verified.stdout.strip() != encoded:
        raise SecretStorageIntegrityError(
            "Linux Secret Service did not verify the stored master key"
        )
    return key


def select_secret_protector() -> SecretProtector:
    system = platform.system()
    if system == "Windows":
        return WindowsDPAPIProtector()
    if system == "Darwin":
        return AesGcmProtector("macos-keychain-aesgcm", _macos_keychain_key())
    if system == "Linux":
        return AesGcmProtector(
            "linux-secret-service-aesgcm", _linux_secret_service_key()
        )
    raise SecretStorageUnavailable(
        f"No secure local credential store is implemented for {system or 'this platform'}"
    )


def _restrict_file_to_current_user(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)
        return
    try:
        import win32api
        import win32con
        import win32security
    except ImportError as exc:
        raise SecretStorageUnavailable(
            "pywin32 is required to apply the vault owner-only Windows DACL"
        ) from exc

    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
    )
    user_sid = win32security.GetTokenInformation(
        token, win32security.TokenUser
    )[0]
    dacl = win32security.ACL()
    dacl.AddAccessAllowedAce(
        win32security.ACL_REVISION,
        win32con.GENERIC_ALL,
        user_sid,
    )
    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION
        | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        dacl,
        None,
    )


def atomic_write_secure(path: Path, payload: bytes) -> None:
    """Crash-safe replace with owner-only permissions and no durable temp copy."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _restrict_file_to_current_user(temporary)
        # Windows may briefly deny replacement while Defender, indexing, or a
        # just-finished verifier is closing the destination.  Keep the old
        # authenticated vault intact and retry only that transient condition.
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.02 * (attempt + 1))
        _restrict_file_to_current_user(path)
        if os.name != "nt":
            try:
                directory_descriptor = os.open(path.parent, os.O_RDONLY)
            except OSError:
                directory_descriptor = -1
            if directory_descriptor >= 0:
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


class LocalSecretVault:
    """Authenticated, current-user-bound storage for a mapping of secrets."""

    def __init__(
        self,
        path: Path,
        protector: SecretProtector | None = None,
    ) -> None:
        self.path = Path(path)
        self.protector = protector or select_secret_protector()

    @staticmethod
    def is_encrypted_bytes(payload: bytes) -> bool:
        return payload.startswith(_VAULT_MAGIC)

    def _encode(self, values: dict[str, Any]) -> bytes:
        try:
            plaintext = json.dumps(
                {"version": 1, "values": values},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise SecretStorageFormatError(
                "Vault values must be JSON-serializable"
            ) from exc
        protected = self.protector.protect(plaintext)
        algorithm = self.protector.algorithm.encode("utf-8")
        if not algorithm or len(algorithm) > 1024:
            raise SecretStorageFormatError("The vault protection identifier is invalid")
        return _VAULT_MAGIC + _HEADER.pack(len(algorithm), len(protected)) + algorithm + protected

    def _decode_with_format(self, payload: bytes) -> tuple[VaultPayload, bool]:
        if not self.is_encrypted_bytes(payload):
            raise SecretStorageFormatError("The credential file is not an Elren vault")
        header_start = len(_VAULT_MAGIC)
        legacy_envelope = False
        if len(payload) >= header_start + _HEADER.size:
            algorithm_length, protected_length = _HEADER.unpack_from(payload, header_start)
            algorithm_start = header_start + _HEADER.size
            protected_start = algorithm_start + algorithm_length
            current_lengths_valid = bool(
                algorithm_length
                and protected_length > 0
                and protected_start + protected_length == len(payload)
            )
        else:
            current_lengths_valid = False
        if not current_lengths_valid:
            # Development builds briefly used a current-user-encrypted envelope
            # with a one-byte algorithm length and no protected-length field.
            # Accept it only when its identifier is exact, authenticate it with
            # the selected OS protector, then atomically rewrite the canonical
            # format in ``read``.  Plaintext is never accepted here.
            if len(payload) <= header_start + 1:
                raise SecretStorageFormatError("The encrypted vault header is truncated")
            algorithm_length = payload[header_start]
            algorithm_start = header_start + 1
            protected_start = algorithm_start + algorithm_length
            if not algorithm_length or protected_start >= len(payload):
                raise SecretStorageFormatError("The encrypted vault lengths are invalid")
            protected_length = len(payload) - protected_start
            legacy_envelope = True
        try:
            algorithm = payload[algorithm_start:protected_start].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecretStorageFormatError(
                "The vault protection identifier is malformed"
            ) from exc
        legacy_dpapi_name = bool(
            algorithm == "windows-dpapi"
            and self.protector.algorithm == "windows-dpapi-current-user"
        )
        if algorithm != self.protector.algorithm and not legacy_dpapi_name:
            raise SecretStorageUnavailable(
                f"This vault requires {algorithm}; the current protector is "
                f"{self.protector.algorithm}"
            )
        plaintext = self.protector.unprotect(payload[protected_start:])
        try:
            decoded = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SecretStorageIntegrityError(
                "The decrypted vault content is malformed"
            ) from exc
        if (
            not isinstance(decoded, dict)
            or decoded.get("version") != 1
            or not isinstance(decoded.get("values"), dict)
        ):
            raise SecretStorageFormatError("The decrypted vault schema is invalid")
        return VaultPayload(values=decoded["values"]), (
            legacy_envelope or legacy_dpapi_name
        )

    def _decode(self, payload: bytes) -> VaultPayload:
        return self._decode_with_format(payload)[0]

    def read(self) -> VaultPayload:
        decoded, legacy_envelope = self._decode_with_format(self.path.read_bytes())
        if legacy_envelope:
            self.write_verified(decoded.values)
        return decoded

    def write(self, values: dict[str, Any]) -> None:
        atomic_write_secure(self.path, self._encode(values))

    def write_verified(self, values: dict[str, Any]) -> None:
        self.write(values)
        verified = self.read().values
        if verified != values:
            raise SecretStorageIntegrityError(
                "The vault did not pass its post-write verification"
            )


def scrub_env_file(path: Path, names: set[str] | frozenset[str]) -> bool:
    """Atomically blank mapped secret assignments while preserving other text."""

    if not path.is_file() or not names:
        return False
    try:
        original = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SecretStorageFormatError("The legacy .env file is not UTF-8") from exc
    normalized_names = {name.casefold() for name in names}
    changed = False
    rewritten: list[str] = []
    for line in original.splitlines(keepends=True):
        match = _ENV_ASSIGNMENT.match(line)
        if (
            match
            and match.group("name").casefold() in normalized_names
            and match.group("value")
        ):
            rewritten.append(match.group("prefix") + (match.group("newline") or ""))
            changed = True
        else:
            rewritten.append(line)
    if not changed:
        return False
    atomic_write_secure(path, "".join(rewritten).encode("utf-8"))
    return True
