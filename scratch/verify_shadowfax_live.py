"""
Live Verification Script for Shadowfax Logistics Integration.

Usage:
  PYTHONPATH=. ./venv/bin/python scratch/verify_shadowfax_live.py

This script:
1. Loads .env configuration (via env_config)
2. Checks for required Shadowfax credentials
3. Initializes ShadowfaxConnector
4. Executes safe, read-only API calls (e.g. check_serviceability)
5. Prints sanitized output (never logs or prints tokens/secrets)
"""
import os
import sys

# Ensure env_config is imported first to load .env
import env_config
import httpx
from integrations.providers.shadowfax import SERVICEABILITY_PATH, ShadowfaxConnector, build_url

# Read-only guard + request trace: only GET may leave the process; URL and HTTP status are
# recorded (never headers, so the token is never printed).
REQUESTS = []
_request = httpx.Client.request


def _get_only(self, method, url, *args, **kwargs):
    if str(method).upper() != "GET":
        raise RuntimeError(f"BLOCKED non-GET request: {method}")
    response = _request(self, method, url, *args, **kwargs)
    REQUESTS.append((str(method).upper(), str(response.request.url).split("?")[0], dict(kwargs.get("params") or {}),
                     response.status_code))
    return response


httpx.Client.request = _get_only


def main():
    print("=" * 65)
    print("Shadowfax Logistics Integration - Live Verification")
    print("=" * 65)

    status_lines = env_config.provider_env_status()
    print("\n[CONFIG CHECK]")
    for line in status_lines:
        if "SHADOWFAX" in line or ".env" in line:
            print(f"  {line}")

    api_token = os.getenv("SHADOWFAX_API_TOKEN")
    client_id = os.getenv("SHADOWFAX_CLIENT_ID")
    client_secret = os.getenv("SHADOWFAX_CLIENT_SECRET")
    base_url = os.getenv("SHADOWFAX_API_BASE_URL")  # None -> connector's documented production default
    tenant = os.getenv("SHADOWFAX_ENV_TENANT") or "COMP-SHADOWFAX"

    print(f"\n[.env SOURCE]: {env_config.ENV_FILE}")
    print(f"[TENANT]: {tenant}")
    print(f"[SHADOWFAX_API_BASE_URL]: {base_url or '(unset - documented production default)'}")
    print(f"[SHADOWFAX_API_TOKEN configured]: {'YES' if api_token else 'NO'}")
    print(f"[SHADOWFAX_CLIENT_ID configured]: {'YES' if client_id else 'NO'}")
    print(f"[SHADOWFAX_CLIENT_SECRET configured]: {'YES' if client_secret else 'NO'}")

    connector = ShadowfaxConnector(
        api_token=api_token,
        client_id=client_id,
        client_secret=client_secret,
        base_url=base_url,
    )

    if not connector.credentials_configured():
        print("\n[STATUS]: MISSING CREDENTIALS")
        print("  Notice: No live API credentials set for Shadowfax.")
        print("  To enable live verification, set SHADOWFAX_API_TOKEN (or SHADOWFAX_CLIENT_ID and SHADOWFAX_CLIENT_SECRET) in .env.")
        print("=" * 65)
        return

    config_err = connector.configuration_error()
    if config_err:
        print(f"\n[STATUS]: CONFIGURATION ERROR: {config_err['error']}")
        print("=" * 65)
        return

    print("\n[STATUS]: CREDENTIALS PRESENT - INITIALIZING CONNECTOR")
    print(f"[CONNECTOR BASE URL]: {connector.base_url}")
    print(f"[SERVICEABILITY ENDPOINT]: {build_url(connector.base_url, SERVICEABILITY_PATH)}")
    print("Executing safe read-only API calls (check_serviceability)...")

    try:
        # 1. Pincode Serviceability Check (Safe read-only)
        svc_result = connector.check_serviceability("400001", "560001")
        print("\n[HTTP REQUESTS SENT]:")
        for method, url, params, status in REQUESTS:
            print(f"  {method} {url} params={params} -> HTTP {status}")
        print(f"\n[SERVICEABILITY CHECK (400001 -> 560001)]:\n  {svc_result}")

        # Sanitization check
        raw_output = str(svc_result)
        if api_token and api_token in raw_output:
            print("\n[SECURITY WARNING]: API TOKEN LEAK DETECTED IN OUTPUT!")
            sys.exit(1)
        if client_secret and client_secret in raw_output:
            print("\n[SECURITY WARNING]: CLIENT SECRET LEAK DETECTED IN OUTPUT!")
            sys.exit(1)

        print("\n[VERIFICATION COMPLETE]: Read-only calls executed cleanly with no credential leakage.")

    except Exception as e:
        sanitized_msg = connector._sanitize_error(str(e))
        print(f"\n[EXECUTION ERROR]: {sanitized_msg}")

    print("=" * 65)


if __name__ == "__main__":
    main()
