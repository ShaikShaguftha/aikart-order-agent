"""
Single place that loads the project's .env so direct Python scripts, pytest,
Uvicorn and /api/chat all see the same provider configuration.

Lookup order: <project>/.env, then <project>/../.env (the current dev layout keeps
secrets one level above the repo). Variables already exported in the shell win
(override=False).
"""
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
_CANDIDATES = [PROJECT_DIR / ".env", PROJECT_DIR.parent / ".env"]

ENV_FILE = next((p for p in _CANDIDATES if p.is_file()), None)
if ENV_FILE:
    load_dotenv(ENV_FILE, override=False)

PROVIDER_ENV_KEYS = [
    "SHOPIFY_SHOP_DOMAIN",
    "SHOPIFY_ACCESS_TOKEN",
    "SHOPIFY_CLIENT_ID",
    "SHOPIFY_CLIENT_SECRET",
    "WOOCOMMERCE_BASE_URL",
    "WOOCOMMERCE_SITE_URL",
    "WOOCOMMERCE_CONSUMER_KEY",
    "WOOCOMMERCE_CONSUMER_SECRET",
    "WOOCOMMERCE_WEBHOOK_SECRET",
    "LUINTIX_CREDENTIALS_KEY",
    "DELHIVERY_API_TOKEN",
    "DELHIVERY_ENV_TENANT",
    "ZOHO_CLIENT_ID",
    "ZOHO_CLIENT_SECRET",
    "ZOHO_ACCESS_TOKEN",
    "ZOHO_REFRESH_TOKEN",
    "ZOHO_API_DOMAIN",
    "SHADOWFAX_API_TOKEN",
    "SHADOWFAX_CLIENT_ID",
    "SHADOWFAX_CLIENT_SECRET",
    "SHADOWFAX_API_BASE_URL",
    "SHADOWFAX_ENV_TENANT",
    "HUBSPOT_ACCESS_TOKEN",
    "HUBSPOT_ENV_TENANT",
    "SALESFORCE_ACCESS_TOKEN",
    "SALESFORCE_INSTANCE_URL",
    "SALESFORCE_API_VERSION",
    "SALESFORCE_ENV_TENANT",
    "RAZORPAY_KEY_ID",
    "RAZORPAY_KEY_SECRET",
    "RAZORPAY_API_KEY",
    "RAZORPAY_SECRET",
    "RAZORPAY_ENV_TENANT",
]


def provider_env_status() -> list:
    """Returns safe, secret-free status lines describing provider configuration."""
    lines = [f".env loaded from: {ENV_FILE if ENV_FILE else 'NOT FOUND'}"]
    for key in PROVIDER_ENV_KEYS:
        lines.append(f"{key} configured: {'YES' if os.getenv(key) else 'NO'}")

    woo_url = os.getenv("WOOCOMMERCE_BASE_URL") or os.getenv("WOOCOMMERCE_SITE_URL") or ""
    if os.getenv("WOOCOMMERCE_BASE_URL") and os.getenv("WOOCOMMERCE_SITE_URL"):
        lines.append("NOTE: both WOOCOMMERCE_BASE_URL and WOOCOMMERCE_SITE_URL are set; WOOCOMMERCE_BASE_URL is used.")
    if woo_url.startswith("http://"):
        lines.append(
            "WARNING: WooCommerce URL uses plain HTTP; WooCommerce then requires the consumer "
            "key/secret as query parameters. Use HTTPS."
        )
    if os.getenv("DELHIVERY_API_TOKEN"):
        lines.append(
            "Delhivery env config: base_url=" + (os.getenv("DELHIVERY_BASE_URL") or "https://track.delhivery.com (default)")
            + " environment=" + (os.getenv("DELHIVERY_ENVIRONMENT") or "production (default)")
            + " auth=" + (os.getenv("DELHIVERY_AUTH_HEADER") or "Authorization") + ": "
            + (os.getenv("DELHIVERY_AUTH_SCHEME") if os.getenv("DELHIVERY_AUTH_SCHEME") is not None else "Token") + " <token>"
        )
        if not os.getenv("DELHIVERY_ENV_TENANT"):
            lines.append("WARNING: DELHIVERY_API_TOKEN is set but DELHIVERY_ENV_TENANT is not; the env token is not "
                         "used for any tenant (no global credential fallback).")
    if os.getenv("SHADOWFAX_API_TOKEN") or (os.getenv("SHADOWFAX_CLIENT_ID") and os.getenv("SHADOWFAX_CLIENT_SECRET")):
        lines.append(
            "Shadowfax env config: base_url=" + (os.getenv("SHADOWFAX_API_BASE_URL") or "https://dale.shadowfax.in/api (documented production default)")
            + " tenant=" + (os.getenv("SHADOWFAX_ENV_TENANT") or "COMP-SHADOWFAX (default)")
        )
    if "playground.wordpress.net" in woo_url:
        lines.append(
            "WARNING: the WooCommerce URL points to WordPress Playground, which runs "
            "in the browser (WASM) and is not a remotely reachable WooCommerce REST API. "
            "Use an externally hosted WooCommerce site for live tests."
        )
    return lines
