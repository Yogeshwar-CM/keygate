"""Minting, hashing and parsing of keygate virtual API keys.

A virtual key looks like::

    kg_v1_<43 url-safe base64 characters>

The random part carries 256 bits of entropy from :mod:`secrets`. Only the
SHA-256 digest of the full key is ever written to disk; the plaintext is shown
to the operator exactly once, at mint time.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

#: Prefix on every key we mint. The version segment lets a future release
#: change the token layout without breaking lookups for old keys.
KEY_PREFIX = "kg_v1_"

#: Bytes of entropy in the random segment.
TOKEN_BYTES = 32

#: Number of leading characters stored in the clear, for display and for
#: `keygate key revoke`. Long enough to be unique in practice, short enough
#: that it is useless to an attacker.
DISPLAY_PREFIX_LEN = len(KEY_PREFIX) + 8

_KEY_RE = re.compile(r"^kg_v1_[A-Za-z0-9_-]{43}$")


@dataclass(frozen=True)
class MintedKey:
    """A freshly minted key: the one-time plaintext plus what we persist."""

    plaintext: str
    key_hash: str
    display_prefix: str


def mint() -> MintedKey:
    """Generate a new virtual key."""
    plaintext = KEY_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
    return MintedKey(
        plaintext=plaintext,
        key_hash=hash_key(plaintext),
        display_prefix=display_prefix(plaintext),
    )


def hash_key(plaintext: str) -> str:
    """Return the hex SHA-256 digest used as the lookup key in the database.

    A plain (unsalted, uniterated) hash is deliberate: the input is 256 bits of
    CSPRNG output, not a human-chosen password, so there is nothing for a
    dictionary or brute-force attack to chew on and a slow KDF would only make
    every proxied request slower.
    """
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def display_prefix(plaintext: str) -> str:
    """Return the non-secret leading fragment shown in listings."""
    return plaintext[:DISPLAY_PREFIX_LEN]


def looks_like_key(candidate: str) -> bool:
    """True if ``candidate`` has the shape of a key we could have minted.

    Used to reject obvious garbage (and upstream ``sk-`` keys accidentally
    pointed at the gateway) before touching the database.
    """
    return bool(_KEY_RE.match(candidate))


def matches(plaintext: str, key_hash: str) -> bool:
    """Constant-time comparison of a presented key against a stored digest."""
    return hmac.compare_digest(hash_key(plaintext), key_hash)


def parse_bearer(header: str | None) -> str | None:
    """Extract the token from an ``Authorization`` header.

    Accepts ``Bearer <token>`` (the OpenAI convention, case-insensitive scheme)
    and a bare token, which some clients send. Returns ``None`` if the header is
    missing or empty.
    """
    if not header:
        return None
    value = header.strip()
    if not value:
        return None
    scheme, _, rest = value.partition(" ")
    if scheme.lower() == "bearer":
        token = rest.strip()
        return token or None
    return value
