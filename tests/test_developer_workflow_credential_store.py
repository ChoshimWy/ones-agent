from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import os
import secrets

import pytest

from src.developer_workflow.credential_store import (
    CRED_TYPE_GENERIC,
    CredentialStore,
    CredentialStoreError,
    WindowsCredentialStore,
    _CredentialRecord,
    credential_target,
)
from src.developer_workflow.setup_models import RuntimeSecrets, SecretKind


@dataclass
class FakeWinCred:
    records: dict[str, _CredentialRecord]

    def __init__(self) -> None:
        self.records = {}
        self.fail_on_write: int | None = None
        self.write_count = 0
        self.last_write_buffer: bytearray | None = None
        self.deleted: list[str] = []

    def write_generic(
        self, target: str, value: bytearray, *, persist: str
    ) -> None:
        assert persist == "local_machine"
        self.write_count += 1
        self.last_write_buffer = value
        if self.fail_on_write == self.write_count:
            raise RuntimeError("TOKEN-SECRET backend detail")
        self.records[target] = _CredentialRecord(
            target=target,
            credential_type=CRED_TYPE_GENERIC,
            blob=bytearray(value),
        )

    def read_generic(self, target: str) -> _CredentialRecord | None:
        record = self.records.get(target)
        if record is None:
            return None
        return _CredentialRecord(
            target=record.target,
            credential_type=record.credential_type,
            blob=bytearray(record.blob),
        )

    def delete_generic(self, target: str) -> bool:
        self.deleted.append(target)
        return self.records.pop(target, None) is not None

    def list_generic_targets(self, prefix: str) -> tuple[str, ...]:
        return tuple(target for target in self.records if target.startswith(prefix))


@pytest.fixture
def fake_wincred() -> FakeWinCred:
    return FakeWinCred()


def test_protocol_and_fake_backend_roundtrip(fake_wincred: FakeWinCred) -> None:
    store: CredentialStore = WindowsCredentialStore(fake_wincred)
    generation = "a" * 32

    store.write("profile-1", generation, SecretKind.ONES_PASSWORD, "TOKEN-SECRET")

    assert store.read("profile-1", generation, SecretKind.ONES_PASSWORD) == (
        "TOKEN-SECRET"
    )
    store.delete("profile-1", generation, SecretKind.ONES_PASSWORD)
    with pytest.raises(CredentialStoreError, match="^credential operation failed$"):
        store.read("profile-1", generation, SecretKind.ONES_PASSWORD)


def test_credential_target_is_derived_only_from_validated_ids() -> None:
    assert credential_target("profile-1", "a" * 32, SecretKind.ONES_PASSWORD) == (
        "ones-dev/profile-1/" + "a" * 32 + "/ones_password"
    )
    for profile in ("../escape", "path\\escape", "profile:kind", "bad\nvalue", ""):
        with pytest.raises(CredentialStoreError, match="^credential operation failed$"):
            credential_target(profile, "a" * 32, SecretKind.ONES_PASSWORD)
    for generation in ("A" * 32, "f" * 31, "../" + "a" * 29):
        with pytest.raises(CredentialStoreError, match="^credential operation failed$"):
            credential_target("profile-1", generation, SecretKind.ONES_PASSWORD)
    with pytest.raises(CredentialStoreError, match="^credential operation failed$"):
        credential_target("profile-1", "a" * 32, "ones_password")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    ["", "nul\x00value", "line\nbreak", "format\u200d", "line\u2028break", "\ud800"],
)
def test_secret_values_are_strict_utf8_and_safe(
    fake_wincred: FakeWinCred, value: str
) -> None:
    with pytest.raises(CredentialStoreError, match="^credential operation failed$"):
        WindowsCredentialStore(fake_wincred).write(
            "profile-1", "a" * 32, SecretKind.ONES_PASSWORD, value
        )


def test_secret_blob_size_is_bounded(fake_wincred: FakeWinCred) -> None:
    store = WindowsCredentialStore(fake_wincred)
    store.write("profile-1", "a" * 32, SecretKind.ONES_PASSWORD, "x" * 2560)
    with pytest.raises(CredentialStoreError, match="^credential operation failed$"):
        store.write("profile-1", "a" * 32, SecretKind.ONES_PASSWORD, "x" * 2561)


def test_store_rejects_non_exact_public_argument_types(fake_wincred: FakeWinCred) -> None:
    store = WindowsCredentialStore(fake_wincred)
    with pytest.raises(CredentialStoreError, match="^credential operation failed$"):
        store.write(  # type: ignore[arg-type]
            1, "a" * 32, SecretKind.ONES_PASSWORD, "safe"
        )
    with pytest.raises(CredentialStoreError, match="^credential operation failed$"):
        store.write(  # type: ignore[arg-type]
            "profile", "a" * 32, SecretKind.ONES_PASSWORD, b"safe"
        )
    with pytest.raises(CredentialStoreError, match="^credential operation failed$"):
        store.read_generation(  # type: ignore[arg-type]
            "profile", "a" * 32, [SecretKind.ONES_PASSWORD]
        )
    with pytest.raises(CredentialStoreError, match="^credential operation failed$"):
        store.write_generation("profile", "a" * 32, {})  # type: ignore[arg-type]


def test_store_zeroizes_mutable_write_buffer(fake_wincred: FakeWinCred) -> None:
    WindowsCredentialStore(fake_wincred).write(
        "profile-1", "a" * 32, SecretKind.ONES_PASSWORD, "TOKEN-SECRET"
    )
    assert fake_wincred.last_write_buffer is not None
    assert fake_wincred.last_write_buffer == bytearray(len("TOKEN-SECRET"))


def test_read_validates_record_shape_and_zeroizes_blob(fake_wincred: FakeWinCred) -> None:
    target = credential_target("profile-1", "a" * 32, SecretKind.ONES_PASSWORD)
    blob = bytearray(b"TOKEN-SECRET")
    fake_wincred.records[target] = _CredentialRecord(
        target=target, credential_type=CRED_TYPE_GENERIC, blob=blob
    )
    returned_blob: bytearray | None = None
    original_read = fake_wincred.read_generic

    def capturing_read(name: str) -> _CredentialRecord | None:
        nonlocal returned_blob
        record = original_read(name)
        assert record is not None
        returned_blob = record.blob
        return record

    fake_wincred.read_generic = capturing_read  # type: ignore[method-assign]
    assert WindowsCredentialStore(fake_wincred).read(
        "profile-1", "a" * 32, SecretKind.ONES_PASSWORD
    ) == "TOKEN-SECRET"
    assert returned_blob == bytearray(len("TOKEN-SECRET"))


@pytest.mark.parametrize(
    "record",
    [
        _CredentialRecord("other", CRED_TYPE_GENERIC, bytearray(b"safe")),
        _CredentialRecord(
            "ones-dev/profile-1/" + "a" * 32 + "/ones_password",
            2,
            bytearray(b"safe"),
        ),
        _CredentialRecord(
            "ones-dev/profile-1/" + "a" * 32 + "/ones_password",
            CRED_TYPE_GENERIC,
            bytearray(b"bad\x00value"),
        ),
        _CredentialRecord(
            "ones-dev/profile-1/" + "a" * 32 + "/ones_password",
            CRED_TYPE_GENERIC,
            bytearray(b"\xff"),
        ),
    ],
)
def test_read_rejects_untrusted_backend_records(
    fake_wincred: FakeWinCred, record: _CredentialRecord
) -> None:
    target = credential_target("profile-1", "a" * 32, SecretKind.ONES_PASSWORD)
    fake_wincred.records[target] = record
    with pytest.raises(CredentialStoreError, match="^credential operation failed$"):
        WindowsCredentialStore(fake_wincred).read(
            "profile-1", "a" * 32, SecretKind.ONES_PASSWORD
        )


def test_store_never_exposes_backend_error_target_or_secret(
    fake_wincred: FakeWinCred,
) -> None:
    fake_wincred.fail_on_write = 1
    store = WindowsCredentialStore(fake_wincred)
    with pytest.raises(CredentialStoreError, match="^credential operation failed$") as error:
        store.write(
            "profile-1", "a" * 32, SecretKind.ONES_PASSWORD, "TOKEN-SECRET"
        )
    rendered = repr(error.value)
    assert error.value.__cause__ is None
    assert "TOKEN-SECRET" not in rendered
    assert "ones-dev" not in rendered
    assert "profile-1" not in rendered
    assert "TOKEN-SECRET" not in repr(store)


def test_generation_roundtrip_overwrite_list_and_delete(
    fake_wincred: FakeWinCred,
) -> None:
    store = WindowsCredentialStore(fake_wincred)
    first = "a" * 32
    second = "b" * 32
    store.write_generation(
        "profile-1",
        first,
        RuntimeSecrets(
            {
                SecretKind.ONES_EMAIL: "agent@example.invalid",
                SecretKind.ONES_PASSWORD: "old",
            }
        ),
    )
    store.write("profile-1", first, SecretKind.ONES_PASSWORD, "new")
    store.write_generation(
        "profile-1",
        second,
        RuntimeSecrets({SecretKind.PROVIDER_TOKEN: "provider-token"}),
    )

    loaded = store.read_generation(
        "profile-1",
        first,
        (SecretKind.ONES_EMAIL, SecretKind.ONES_PASSWORD),
    )
    assert loaded.values == {
        SecretKind.ONES_EMAIL: "agent@example.invalid",
        SecretKind.ONES_PASSWORD: "new",
    }
    assert store.list_generations("profile-1") == (first, second)
    store.delete_generation("profile-1", first)
    assert store.list_generations("profile-1") == (second,)


def test_generation_write_rolls_back_only_targets_written_in_this_call(
    fake_wincred: FakeWinCred,
) -> None:
    store = WindowsCredentialStore(fake_wincred)
    other_generation = "b" * 32
    store.write("profile-1", other_generation, SecretKind.PROVIDER_TOKEN, "keep")
    fake_wincred.write_count = 0
    fake_wincred.fail_on_write = 2

    with pytest.raises(CredentialStoreError, match="^credential operation failed$"):
        store.write_generation(
            "profile-1",
            "a" * 32,
            RuntimeSecrets(
                {
                    SecretKind.ONES_EMAIL: "agent@example.invalid",
                    SecretKind.ONES_PASSWORD: "password",
                }
            ),
        )

    assert credential_target(
        "profile-1", "a" * 32, SecretKind.ONES_EMAIL
    ) not in fake_wincred.records
    assert store.read("profile-1", other_generation, SecretKind.PROVIDER_TOKEN) == "keep"


def test_generation_read_fails_if_any_requested_kind_is_missing(
    fake_wincred: FakeWinCred,
) -> None:
    store = WindowsCredentialStore(fake_wincred)
    store.write("profile-1", "a" * 32, SecretKind.ONES_EMAIL, "agent@example.invalid")
    with pytest.raises(CredentialStoreError, match="^credential operation failed$"):
        store.read_generation(
            "profile-1",
            "a" * 32,
            (SecretKind.ONES_EMAIL, SecretKind.ONES_PASSWORD),
        )


def test_list_ignores_malformed_and_foreign_targets(fake_wincred: FakeWinCred) -> None:
    good = credential_target("profile-1", "a" * 32, SecretKind.ONES_PASSWORD)
    fake_wincred.records[good] = _CredentialRecord(
        good, CRED_TYPE_GENERIC, bytearray(b"safe")
    )
    for target in (
        "other-app/profile-1/" + "b" * 32 + "/ones_password",
        "ones-dev/profile-1/not-a-generation/ones_password",
        "ones-dev/profile-1/" + "b" * 32 + "/unknown",
        "ones-dev/profile-10/" + "c" * 32 + "/ones_password",
    ):
        fake_wincred.records[target] = _CredentialRecord(
            target, CRED_TYPE_GENERIC, bytearray(b"safe")
        )
    assert WindowsCredentialStore(fake_wincred).list_generations("profile-1") == (
        "a" * 32,
    )


def test_concurrent_writes_to_distinct_targets_are_isolated(
    fake_wincred: FakeWinCred,
) -> None:
    store = WindowsCredentialStore(fake_wincred)
    generations = tuple(f"{index:032x}" for index in range(16))

    def write_one(generation: str) -> None:
        store.write("profile-1", generation, SecretKind.PROVIDER_TOKEN, generation)

    with ThreadPoolExecutor(max_workers=8) as executor:
        tuple(executor.map(write_one, generations))

    assert store.list_generations("profile-1") == generations
    assert all(
        store.read("profile-1", generation, SecretKind.PROVIDER_TOKEN) == generation
        for generation in generations
    )


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Credential Manager")
def test_real_windows_roundtrip_and_repeated_overwrite() -> None:
    profile = "pytest-" + secrets.token_hex(8)
    generation = secrets.token_hex(16)
    store = WindowsCredentialStore()
    try:
        store.write(profile, generation, SecretKind.PROVIDER_TOKEN, "first")
        store.write(profile, generation, SecretKind.PROVIDER_TOKEN, "second")
        assert store.read(profile, generation, SecretKind.PROVIDER_TOKEN) == "second"
        assert generation in store.list_generations(profile)
    finally:
        store.delete(profile, generation, SecretKind.PROVIDER_TOKEN)
