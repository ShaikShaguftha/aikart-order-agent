"""
Connect (or rotate) a tenant's Shippo API token.

  python -m integrations.shippo_onboarding --company-id COMP-SHOPIFY --verify-only
  python -m integrations.shippo_onboarding --company-id COMP-SHOPIFY [--webhook-auth hmac|url_token]
      [--allow-tracking-registration]

Flow (spec 3.2): authenticate with the token -> GET /carrier_accounts/ -> confirm the
credential works -> only then encrypt and store it for that tenant. Re-running replaces
the token (credential rotation) and re-activates an INVALID credential. The token is
read from a hidden prompt and never printed.

Webhook authentication:
  hmac      - the shared secret Shippo issues once HMAC is enabled for the account
              (Shippo enables HMAC on request; it can take several business days).
  url_token - Shippo's documented alternative: a random token appended to the webhook URL.
There is intentionally no HTTP endpoint: storing credentials needs admin authentication.
"""
import argparse
import getpass
import secrets
import sys
from typing import Any, Dict, Optional

from database import execute_db, init_db, query_db
from integrations import credential_store
from integrations.audit import AuditLogger
from integrations.errors import TenantConfigurationError
from integrations.providers.shippo import ShippoConnector


def verify_shippo(api_token: str) -> Dict[str, Any]:
    if not (api_token or "").startswith(("shippo_test_", "shippo_live_")):
        return {"ok": False, "error": "That does not look like a Shippo API token (shippo_test_... or shippo_live_...).",
                "code": "INVALID_TOKEN_FORMAT"}
    return ShippoConnector(api_token).verify_credentials()


def connect_shippo(
    company_id: str,
    api_token: str,
    webhook_secret: Optional[str] = None,
    webhook_auth_mode: str = "hmac",
    allow_tracking_registration: bool = False,
) -> Dict[str, Any]:
    if not credential_store.encryption_available():
        raise TenantConfigurationError(
            f"Set {credential_store.KEY_ENV} before connecting Shippo; tokens are never stored in plaintext."
        )
    if webhook_auth_mode not in ("hmac", "url_token"):
        raise TenantConfigurationError("webhook_auth_mode must be 'hmac' or 'url_token'.")
    init_db()
    if not query_db("SELECT 1 FROM companies WHERE id = ?", (company_id,), one=True):
        raise TenantConfigurationError(f"Tenant '{company_id}' does not exist.")

    verification = verify_shippo(api_token)
    if not verification.get("ok"):
        return {"connected": False, **verification}

    environment = ShippoConnector.environment_for_token(api_token)
    existing = query_db("SELECT webhook_secret FROM shipping_integrations WHERE company_id = ?", (company_id,), one=True)
    stored_secret = credential_store.encrypt(webhook_secret) if webhook_secret else (existing or {}).get("webhook_secret")
    execute_db(
        "INSERT INTO shipping_integrations (company_id, provider_type, environment, api_token, webhook_secret, "
        "webhook_auth_mode, allow_tracking_registration, credential_status, last_verified_at) "
        "VALUES (?, 'SHIPPO', ?, ?, ?, ?, ?, 'ACTIVE', CURRENT_TIMESTAMP) "
        "ON CONFLICT(company_id) DO UPDATE SET provider_type = 'SHIPPO', environment = excluded.environment, "
        "api_token = excluded.api_token, webhook_secret = excluded.webhook_secret, "
        "webhook_auth_mode = excluded.webhook_auth_mode, allow_tracking_registration = excluded.allow_tracking_registration, "
        "credential_status = 'ACTIVE', last_verified_at = CURRENT_TIMESTAMP",
        (company_id, environment, credential_store.encrypt(api_token), stored_secret, webhook_auth_mode,
         int(allow_tracking_registration)),
    )
    AuditLogger.log_action(
        session_id="onboarding", company_id=company_id, order_id="",
        action_type="ROTATE_SHIPPING_CREDENTIAL" if existing else "CONNECT_SHIPPING",
        provider="SHIPPO", status="CONNECTED",
        details={"environment": environment, "carrier_account_count": verification["carrier_account_count"],
                 "webhook_auth_mode": webhook_auth_mode, "webhook_secret_configured": bool(stored_secret)},
    )
    return {"connected": True, "company_id": company_id, "rotated": bool(existing), **verification,
            "webhook_auth_mode": webhook_auth_mode, "webhook_secret_configured": bool(stored_secret)}


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Connect or rotate a tenant's Shippo API token.")
    parser.add_argument("--company-id", required=True)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--webhook-auth", choices=("hmac", "url_token"), default="hmac")
    parser.add_argument("--allow-tracking-registration", action="store_true",
                        help="Live tokens only: merchant approves registering tracking numbers (may incur Shippo fees).")
    args = parser.parse_args()

    import env_config  # noqa: F401  (loads LUINTIX_CREDENTIALS_KEY)

    token = getpass.getpass("Shippo API token (shippo_test_... for development): ").strip()
    if args.verify_only:
        result = verify_shippo(token)
        print({k: v for k, v in result.items()})
        return 0 if result.get("ok") else 1

    webhook_secret, generated = None, False
    if args.webhook_auth == "hmac":
        webhook_secret = getpass.getpass("Shippo HMAC webhook secret (Enter to keep existing / skip): ").strip() or None
    else:
        webhook_secret, generated = secrets.token_urlsafe(32), True
    result = connect_shippo(args.company_id, token, webhook_secret, args.webhook_auth, args.allow_tracking_registration)
    print({k: v for k, v in result.items()})
    if result.get("connected") and generated:
        print("\nConfigure this webhook URL in Shippo (Event: Track Updated). Keep it secret; it is shown once:")
        print(f"  https://<your-luintix-host>/api/webhooks/shippo/{args.company_id}?token={webhook_secret}")
    return 0 if result.get("connected") else 1


if __name__ == "__main__":
    sys.exit(_cli())
