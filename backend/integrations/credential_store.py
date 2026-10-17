"""
Encryption at rest for merchant credentials stored in company_integrations.

Values are stored as "enc:v1:<fernet token>" using the key in LUINTIX_CREDENTIALS_KEY
(generate one with: python -m integrations.credential_store generate-key).
Plaintext values are never written by Luintix; legacy plaintext rows are still readable
so existing development databases keep working.
"""
import os
from typing import Optional

from backend.integrations.errors import TenantConfigurationError

PREFIX = "enc:v1:"
KEY_ENV = "LUINTIX_CREDENTIALS_KEY"


def _fernet():
    key = os.getenv(KEY_ENV)
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet

        return Fernet(key.encode())
    except Exception:
        return None


def encryption_available() -> bool:
    return _fernet() is not None


def encrypt(value: Optional[str]) -> Optional[str]:
    if not value:
        return value
    fernet = _fernet()
    if fernet is None:
        raise TenantConfigurationError(
            f"Credential encryption is not configured. Set {KEY_ENV} before storing merchant credentials."
        )
    return PREFIX + fernet.encrypt(value.encode()).decode()


def decrypt(value: Optional[str]) -> Optional[str]:
    if not value or not value.startswith(PREFIX):
        return value
    fernet = _fernet()
    if fernet is None:
        raise TenantConfigurationError(
            f"Stored credentials are encrypted but {KEY_ENV} is not configured."
        )
    try:
        return fernet.decrypt(value[len(PREFIX):].encode()).decode()
    except Exception:
        raise TenantConfigurationError(
            f"Stored credentials could not be decrypted with the configured {KEY_ENV}."
        )


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["generate-key"]:
        from cryptography.fernet import Fernet

        print(Fernet.generate_key().decode())
    else:
        print("usage: python -m integrations.credential_store generate-key")
