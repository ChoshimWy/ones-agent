"""Native Keychain adapter. Secret bytes never enter subprocess arguments."""

from __future__ import annotations

from contextlib import contextmanager
import os
import stat
import time
from typing import Iterator

from .credential_store import (
    CRED_TYPE_GENERIC, WindowsCredentialStore, _CredentialRecord,
    _fail, _safe_backend_call,
)
from .platform_support import user_data_directory
from .private_paths import prepare_private_directory


@contextmanager
def _generation_lock(_profile: str, _generation: str) -> Iterator[None]:
    import fcntl

    root = prepare_private_directory(user_data_directory())
    path = root / "keychain.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise _fail()
        deadline = time.monotonic() + 30
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise _fail() from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


class _KeychainBackend:
    def __init__(self) -> None:
        import Security

        self.api = Security

    def _query(self, target: str | None = None) -> dict:
        api = self.api
        query = {api.kSecClass: api.kSecClassGenericPassword,
                 api.kSecAttrService: "ones-dev.credentials"}
        if target is not None:
            query[api.kSecAttrAccount] = target
        return query

    def write_generic(self, target: str, value: bytearray, *, persist: str) -> None:
        api = self.api
        query = self._query(target)
        attributes = {api.kSecValueData: bytes(value)}
        status = api.SecItemUpdate(query, attributes)
        if status == api.errSecItemNotFound:
            status, _ = api.SecItemAdd({**query, **attributes}, None)
        if status != api.errSecSuccess:
            raise _fail()

    def read_generic(self, target: str) -> _CredentialRecord | None:
        api = self.api
        status, value = api.SecItemCopyMatching(
            {**self._query(target), api.kSecReturnData: True,
             api.kSecMatchLimit: api.kSecMatchLimitOne}, None,
        )
        if status == api.errSecItemNotFound:
            return None
        if status != api.errSecSuccess or value is None:
            raise _fail()
        return _CredentialRecord(target, CRED_TYPE_GENERIC, bytearray(value))

    def delete_generic(self, target: str) -> bool:
        api = self.api
        status = api.SecItemDelete(self._query(target))
        if status not in (api.errSecSuccess, api.errSecItemNotFound):
            raise _fail()
        return status == api.errSecSuccess

    def list_generic_targets(self, prefix: str) -> tuple[str, ...]:
        api = self.api
        status, values = api.SecItemCopyMatching(
            {**self._query(), api.kSecReturnAttributes: True,
             api.kSecMatchLimit: api.kSecMatchLimitAll}, None,
        )
        if status == api.errSecItemNotFound:
            return ()
        if status != api.errSecSuccess or values is None:
            raise _fail()
        result = []
        for item in values:
            target = item.get(api.kSecAttrAccount)
            if not isinstance(target, str):
                raise _fail()
            if target.startswith(prefix):
                result.append(str(target))
        return tuple(sorted(result))


class MacOSCredentialStore(WindowsCredentialStore):
    """Reuse generation validation/rollback with a native Keychain backend."""

    def __init__(self) -> None:
        super().__init__(_safe_backend_call(_KeychainBackend), lock_factory=_generation_lock)
