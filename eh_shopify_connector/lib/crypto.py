# -*- coding: utf-8 -*-
# Part of ERP Heritage Shopify Connector. See LICENSE file for full copyright and licensing details.
"""Encryption at rest for store credentials.

Fernet (AES 128 CBC with HMAC SHA256) keyed through HKDF. The key material is
the ``eh_shopify_key`` server option when an administrator sets one, so a
database dump alone does not reveal tokens. Without that option the key is
derived from the database secret, which still keeps tokens out of plain SQL
reads, exports and backups of individual tables, and the health panel says so.
"""
import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

try:  # cryptography < 3.1 requires an explicit backend
    from cryptography.hazmat.backends import default_backend
except ImportError:  # pragma: no cover
    default_backend = None

PREFIX = "ehv1:"
_SALT = b"eh_shopify_connector/credential-vault"
_INFO = b"fernet-key-v1"


class VaultError(Exception):
    pass


def _derive(material):
    if not material:
        raise VaultError("Missing key material for the credential vault.")
    kwargs = {"algorithm": hashes.SHA256(), "length": 32, "salt": _SALT, "info": _INFO}
    if default_backend is not None:
        kwargs["backend"] = default_backend()
    raw = HKDF(**kwargs).derive(material.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


class Vault(object):
    def __init__(self, material):
        self._fernet = Fernet(_derive(material))

    def encrypt(self, plaintext):
        if plaintext in (None, False, ""):
            return False
        token = self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
        return PREFIX + token

    def decrypt(self, stored):
        if not stored:
            return None
        if not stored.startswith(PREFIX):
            raise VaultError("Credential is not in a recognised vault format.")
        try:
            return self._fernet.decrypt(stored[len(PREFIX):].encode("ascii")).decode("utf-8")
        except InvalidToken:
            raise VaultError(
                "Credential cannot be decrypted. The vault key changed "
                "(server option eh_shopify_key or database secret)."
            )


def mask(secret, keep=4):
    """Show only the last characters of a secret for display."""
    if not secret:
        return ""
    if len(secret) <= keep:
        return "*" * len(secret)
    return "*" * 8 + secret[-keep:]
