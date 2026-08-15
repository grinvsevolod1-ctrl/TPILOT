# -*- coding: utf-8 -*-
"""tdata local-encryption primitives.

Ported (NOTICE items 3-5): `Storage.CreateLocalKey` (PBKDF2-HMAC-SHA512 local
key derivation), `AuthKey.prepareAES_oldmtp` (SHA1-based legacy AES key/IV
derivation), and `Storage.DecryptLocal` (AES-256-IGE decrypt + integrity
check + inner-length unwrap).

AES-IGE itself is performed by this project's EXISTING dependency
`telethon.crypto.aes.AES.decrypt_ige` -- no tgcrypto, no new pip package.

SECURITY: this module never logs a key or plaintext. Callers are responsible
for not printing return values.
"""
from __future__ import annotations

import hashlib

from telethon.crypto.aes import AES as _TelethonAES

from .qtstream import QDataStreamReadError


class TdataDecryptError(Exception):
    """Wrong passcode / wrong key / corrupted ciphertext. Message is always
    generic -- never includes key material."""


def create_local_key(salt: bytes, passcode: bytes = b"") -> bytes:
    """`Storage.CreateLocalKey`: sha512(salt || passcode || salt) as the
    PBKDF2-HMAC-SHA512 password, `salt` as the PBKDF2 salt, 1 iteration if
    passcode is empty else 100000, 256-byte output."""
    h = hashlib.sha512()
    h.update(salt)
    h.update(passcode)
    h.update(salt)
    iterations = 1 if not passcode else 100000
    return hashlib.pbkdf2_hmac("sha512", h.digest(), salt, iterations, dklen=256)


def _prepare_aes_oldmtp(key: bytes, msg_key: bytes, *, send: bool) -> tuple:
    """`AuthKey.prepareAES_oldmtp`: legacy (pre-MTProto-2.0) local-storage AES
    key/IV derivation from a 256-byte key and a 16-byte msgKey. `send=False`
    (x=8) is the only direction this project uses (decrypting local files)."""
    if len(key) != 256:
        raise TdataDecryptError("local key must be 256 bytes")
    x = 0 if send else 8
    mk = msg_key[:16]

    sha1_a = hashlib.sha1(mk + key[x:x + 32]).digest()
    sha1_b = hashlib.sha1(key[x + 32:x + 48] + mk + key[x + 48:x + 64]).digest()
    sha1_c = hashlib.sha1(key[x + 64:x + 96] + mk).digest()
    sha1_d = hashlib.sha1(mk + key[x + 96:x + 128]).digest()

    aes_key = sha1_a[:8] + sha1_b[8:20] + sha1_c[4:16]
    aes_iv = sha1_a[8:20] + sha1_b[:8] + sha1_c[16:20] + sha1_d[:8]
    return aes_key, aes_iv


def decrypt_local(encrypted: bytes, key: bytes) -> bytes:
    """`Storage.DecryptLocal`: AES-256-IGE-decrypt `encrypted` with the
    key/IV derived from `key` (a 256-byte local key) and the leading 16-byte
    msgKey, verify sha1(decrypted)[:16] == msgKey, strip the 4-byte
    little-endian inner-length prefix, and return the inner plaintext
    (everything a caller would read via a QDataStream from offset 4 in
    upstream's EncryptedDescriptor).

    Raises TdataDecryptError on any integrity failure -- including simply
    "wrong passcode" for the outer key_data container, which callers must map
    to TdataPasscodeRequired rather than a generic corruption error."""
    size = len(encrypted)
    if size <= 16 or (size & 0x0F):
        raise TdataDecryptError("bad encrypted block size")

    msg_key = encrypted[:16]
    ciphertext = encrypted[16:]

    aes_key, aes_iv = _prepare_aes_oldmtp(key, msg_key, send=False)
    try:
        decrypted = _TelethonAES.decrypt_ige(ciphertext, aes_key, aes_iv)
    except Exception as exc:  # noqa: BLE001
        raise TdataDecryptError(f"ige decrypt failed: {type(exc).__name__}")

    check_hash = hashlib.sha1(decrypted).digest()[:16]
    if check_hash != msg_key:
        raise TdataDecryptError("checksum mismatch (wrong key/passcode or corrupted data)")

    if len(decrypted) < 4:
        raise TdataDecryptError("decrypted payload too short")
    data_len = int.from_bytes(decrypted[:4], "little")
    full_len = size - 16
    if data_len > len(decrypted) or data_len <= full_len - 16 or data_len < 4:
        raise TdataDecryptError("bad decrypted payload length")

    return decrypted[4:data_len]
