"""Encrypted account state; never replace corrupt or externally changed files."""

import copy
import hashlib
import json
import math
import os
import secrets
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class CredentialStoreError(RuntimeError):
    """A deliberately credential-free storage error."""


class CredentialStore:
    MAX_BYTES = 2 * 1024 * 1024

    def __init__(self, directory: Path):
        self.directory = Path(directory).absolute()
        self.key_path = self.directory / "accounts.key"
        self.state_path = self.directory / "accounts.enc"
        self._fingerprint = None
        try:
            self._check_path(self.directory)
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._check_path(self.directory)
            os.chmod(self.directory, 0o700)
            self._check_path(self.key_path)
            self._check_path(self.state_path)
            if not self.key_path.exists():
                if self.state_path.exists():
                    raise CredentialStoreError("Account encryption key is missing.")
                self._create_key()
            key = self._read_file(self.key_path, 128)
            os.chmod(self.key_path, 0o600)
            self._cipher = Fernet(key)
            self._state = self._read_state()
        except CredentialStoreError:
            raise
        except Exception:
            raise CredentialStoreError(
                "Account storage could not be opened safely."
            ) from None

    @staticmethod
    def _check_path(path: Path):
        for candidate in (path, *path.parents):
            try:
                info = os.lstat(candidate)
            except FileNotFoundError:
                continue
            attributes = getattr(info, "st_file_attributes", 0)
            if stat.S_ISLNK(info.st_mode) or attributes & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
            ):
                raise CredentialStoreError(
                    "Account storage must not use symbolic links."
                )

    @classmethod
    def _read_file(cls, path: Path, maximum: int):
        cls._check_path(path)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                raise CredentialStoreError(
                    "Account storage has an invalid file size or type."
                )
            value = stream.read(maximum + 1)
            if len(value) > maximum:
                raise CredentialStoreError("Account storage exceeds its size limit.")
            return value

    def _create_key(self):
        descriptor = os.open(
            self.key_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(Fernet.generate_key())
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(self.key_path, 0o600)

    @staticmethod
    def _validate(state):
        if not isinstance(state, dict) or state.get("version") != 1:
            raise CredentialStoreError("Account storage has an unsupported format.")
        accounts = state.get("accounts")
        notices = state.get("notices")
        if not isinstance(accounts, dict) or not isinstance(notices, dict):
            raise CredentialStoreError("Account storage is malformed.")
        for provider, account in accounts.items():
            if not isinstance(provider, str) or not isinstance(account, dict):
                raise CredentialStoreError("Account storage is malformed.")
            if (
                not isinstance(account.get("credential"), dict)
                or not account["credential"]
            ):
                raise CredentialStoreError("Account credential is malformed.")
            if account.get("state") not in {"valid", "expired", "unknown"}:
                raise CredentialStoreError("Account status is malformed.")
            if not isinstance(account.get("refresh_attempted", False), bool):
                raise CredentialStoreError("Account refresh status is malformed.")
        for key, value in notices.items():
            if (
                not isinstance(key, str)
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise CredentialStoreError("Account notification state is malformed.")

    def _read_state(self):
        if not self.state_path.exists():
            return {"version": 1, "accounts": {}, "notices": {}}
        encrypted = self._read_file(self.state_path, self.MAX_BYTES)
        try:
            state = json.loads(self._cipher.decrypt(encrypted))
        except (InvalidToken, ValueError, UnicodeError):
            raise CredentialStoreError(
                "Account storage cannot be decrypted or is corrupt."
            ) from None
        self._validate(state)
        self._fingerprint = hashlib.sha256(encrypted).digest()
        os.chmod(self.state_path, 0o600)
        return state

    def load(self):
        return copy.deepcopy(self._state)

    def save(self, state):
        self._validate(state)
        temporary = self.directory / (".accounts-" + secrets.token_hex(12) + ".tmp")
        try:
            self._check_path(self.directory)
            self._check_path(self.key_path)
            self._check_path(self.state_path)
            actual = None
            if self.state_path.exists():
                actual = hashlib.sha256(
                    self._read_file(self.state_path, self.MAX_BYTES)
                ).digest()
            if actual != self._fingerprint:
                raise CredentialStoreError(
                    "Account storage changed externally; refusing to overwrite it."
                )
            key = self._read_file(self.key_path, 128)
            if Fernet(key).decrypt(self._cipher.encrypt(b"key-check")) != b"key-check":
                raise CredentialStoreError("Account encryption key changed.")
            payload = json.dumps(state, ensure_ascii=True, allow_nan=False).encode(
                "utf-8"
            )
            encrypted = self._cipher.encrypt(payload)
            if len(encrypted) > self.MAX_BYTES:
                raise CredentialStoreError("Account storage exceeds its size limit.")
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            self._check_path(self.state_path)
            os.replace(temporary, self.state_path)
            self._fingerprint = hashlib.sha256(encrypted).digest()
            self._state = copy.deepcopy(state)
        except CredentialStoreError:
            raise
        except Exception:
            raise CredentialStoreError(
                "Account storage could not be saved safely."
            ) from None
        finally:
            try:
                if temporary.exists() and not temporary.is_symlink():
                    temporary.unlink()
            except OSError:
                pass
