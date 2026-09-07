from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 1_024


class PasswordPolicyError(ValueError):
    """Raised when a new password does not meet the local password policy."""


@dataclass(frozen=True, slots=True)
class PasswordVerification:
    valid: bool
    replacement_hash: str | None = None


_PASSWORD_HASHER = PasswordHasher(type=Type.ID)
_DUMMY_HASH = _PASSWORD_HASHER.hash("dummy-password-used-only-to-equalize-login-work")


def clean_username(username: str) -> str:
    """Return the display form used by the app after NFKC and trimming."""

    if not isinstance(username, str):
        raise TypeError("username must be a string")
    return unicodedata.normalize("NFKC", username).strip()


def normalize_username(username: str) -> str:
    """Return the canonical lookup key: NFKC, strip, then casefold."""

    return clean_username(username).casefold()


def validate_new_password(password: str) -> None:
    if not isinstance(password, str):
        raise PasswordPolicyError("密码格式无效")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"密码至少需要 {MIN_PASSWORD_LENGTH} 个字符")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"密码不能超过 {MAX_PASSWORD_LENGTH} 个字符")


def hash_password(password: str) -> str:
    """Hash a new password with Argon2id and a library-generated random salt."""

    validate_new_password(password)
    return _PASSWORD_HASHER.hash(password)


def verify_password(password_hash: str, candidate: str) -> bool:
    """Return False for a mismatch or a malformed stored hash."""

    if not isinstance(password_hash, str) or not isinstance(candidate, str):
        return False
    try:
        return bool(_PASSWORD_HASHER.verify(password_hash, candidate))
    except (InvalidHashError, VerificationError):
        return False


def verify_and_rehash(password_hash: str, candidate: str) -> PasswordVerification:
    """Verify a password and return an upgraded hash when parameters changed."""

    if not verify_password(password_hash, candidate):
        return PasswordVerification(valid=False)
    try:
        needs_rehash = _PASSWORD_HASHER.check_needs_rehash(password_hash)
    except InvalidHashError:
        return PasswordVerification(valid=False)
    replacement = _PASSWORD_HASHER.hash(candidate) if needs_rehash else None
    return PasswordVerification(valid=True, replacement_hash=replacement)


def perform_dummy_verification(candidate: str) -> None:
    """Spend normal verification work when the supplied username does not exist."""

    verify_password(_DUMMY_HASH, candidate)


__all__ = [
    "MAX_PASSWORD_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "PasswordPolicyError",
    "PasswordVerification",
    "clean_username",
    "hash_password",
    "normalize_username",
    "perform_dummy_verification",
    "validate_new_password",
    "verify_and_rehash",
    "verify_password",
]
