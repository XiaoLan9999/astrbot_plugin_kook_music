"""Small transport-independent authentication contracts; secrets never appear in repr."""

from dataclasses import dataclass, field
from typing import Any


class AuthError(Exception):
    def __init__(self, kind: str, message: str, *, diagnostic: str = ""):
        self.kind = kind
        self.diagnostic = diagnostic
        super().__init__(message)


@dataclass
class LoginChallenge:
    challenge_id: str = field(repr=False)
    qr_bytes: bytes = field(repr=False)
    expires_in: int = 180
    opaque: Any = field(default=None, repr=False)


@dataclass
class LoginPoll:
    status: str
    credential: dict | None = field(default=None, repr=False)
    display_name: str = ""


@dataclass
class AudioResult:
    status: str
    url: str = field(default="", repr=False)
    reason: str = ""
