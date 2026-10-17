"""
Live Shippo verification with a TEST token (refuses live tokens).

    venv/bin/python scratch/verify_shippo_live.py            # phase A (read-only) + phase B
    venv/bin/python scratch/verify_shippo_live.py --read-only

Token source: read from the prompt, or from .env key SHIPPO_API for this script only
(the Luintix runtime never reads Shippo tokens from the environment). Never printed.

Phase A (read-only): only GET requests are allowed to leave the process.
Phase B (registration + duplicate protection): runs against a throwaway temporary
database (your orders.db is untouched); only POST /tracks/ is additionally allowed.
"""
import getpass
import os
import sys
import tempfile

sys.path.insert(0, ".")
from backend import env_config  # noqa: E402,F401
import httpx  # noqa: E402
from dotenv import dotenv_values  # noqa: E402

READ_ONLY = "--read-only" in sys.argv
ALLOW_POST_TRACKS = False
SENT = []
_request = httpx.Client.request


def _guard(self, method, url, *args, **kwargs):
    method, path = str(method).upper(), str(url).split("api.goshippo.com", 1)[-1]
    if method != "GET" and not (ALLOW_POST_TRACKS and method == "POST" and path == "/tracks/"):
        raise RuntimeError(f"BLOCKED {method} {path}")
    SENT.append((method, path))
    return _request(self, method, url, *args, **kwargs)


httpx.Client.request = _guard

token = (dotenv_values(os.path.join("..", ".env")).get("SHIPPO_API") or "").strip() or getpass.getpass("Shippo TEST token: ").strip()
if not token.startswith("shippo_test_"):
    print("STOP: this verification only runs with a Shippo TEST token (shippo_test_...).")
    sys.exit(1)

from backend.integrations.providers.shippo import ShippoConnector, TEST_CARRIER  # noqa: E402

connector = ShippoConnector(token, retry_backoff=1.0)
print("\n## PHASE A - read-only")
verification = connector.verify_credentials()
print(f"  1. authentication + carrier accounts: {verification}")
accounts = connector.get_carrier_accounts()
if isinstance(accounts, list):
    print(f"  2. carrier accounts ({len(accounts)}): " + ", ".join(
        f"{a['carrier_slug']}{'' if a['active'] else '(inactive)'}" for a in accounts[:15]) + (" ..." if len(accounts) > 15 else ""))

results = {}
for number in ("SHIPPO_TRANSIT", "SHIPPO_DELIVERED", "SHIPPO_FAILURE", "SHIPPO_RETURNED", "SHIPPO_PRE_TRANSIT", "SHIPPO_UNKNOWN"):
    t = connector.get_tracking(TEST_CARRIER, number)
    if "error" in t:
        results[number] = t["code"]
        print(f"  3. {number:<20} ERROR {t['code']}")
        continue
    results[number] = t["status"]
    loc = t["current_location"] or {}
    print(f"  3. {number:<20} -> {t['status']:<16} provider={t['provider_status']:<12} substatus={t['substatus']!s:<22} "
          f"action_required={t['action_required']} eta={t['estimated_delivery']} "
          f"location={loc.get('city')},{loc.get('region')},{loc.get('country')} history={len(t['tracking_history'])} test={t['test_mode']}")
expected = {"SHIPPO_TRANSIT": {"IN_TRANSIT", "OUT_FOR_DELIVERY"}, "SHIPPO_DELIVERED": {"DELIVERED"},
            "SHIPPO_FAILURE": {"EXCEPTION"}, "SHIPPO_RETURNED": {"RETURNED"}}
phase_a = verification.get("ok") and all(results.get(n) in s for n, s in expected.items())
print(f"\n  PHASE A: {'PASS' if phase_a else 'FAIL'} | requests sent: {len(SENT)} (all GET: {all(m == 'GET' for m, _ in SENT)})")

if READ_ONLY or not phase_a:
    sys.exit(0 if phase_a else 1)

print("\n## PHASE B - registration + duplicate protection (throwaway DB)")
import database  # noqa: E402

database.DB_FILE = os.path.join(tempfile.mkdtemp(prefix="luintix-shippo-live-"), "orders.db")
database.init_db()
from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("LUINTIX_CREDENTIALS_KEY", Fernet.generate_key().decode())
from backend.integrations.gateway import gateway  # noqa: E402
from backend.integrations.shippo_onboarding import connect_shippo  # noqa: E402

database.execute_db("INSERT INTO companies (id, name, tier) VALUES ('COMP-SHIPPO-LIVE-CHECK', 'check', 'STANDARD')")
connected = connect_shippo("COMP-SHIPPO-LIVE-CHECK", token)
row = database.query_db("SELECT api_token FROM shipping_integrations WHERE company_id = 'COMP-SHIPPO-LIVE-CHECK'", one=True)
print(f"  4. onboarding: connected={connected['connected']} environment={connected.get('environment')} "
      f"token encrypted at rest={row['api_token'].startswith('enc:v1:')}")

ALLOW_POST_TRACKS = True
posts_before = sum(1 for m, _ in SENT if m == "POST")
first = gateway.shipping_register_tracking("COMP-SHIPPO-LIVE-CHECK", "shippo", "SHIPPO_TRANSIT", order_reference="LIVE-CHECK")
second = gateway.shipping_register_tracking("COMP-SHIPPO-LIVE-CHECK", "shippo", "SHIPPO_TRANSIT", order_reference="LIVE-CHECK")
posts = sum(1 for m, _ in SENT if m == "POST") - posts_before
print(f"  5. first registration : {first['status']} registered_now={first['registered_now']}"
      + (f" -> tracking status {first['tracking']['status']}" if first.get("tracking") else "")
      + (f" error={first.get('code')}" if first.get("code") else ""))
print(f"  6. second registration: {second['status']} registered_now={second['registered_now']}")
print(f"  7. POST /tracks/ requests actually sent to Shippo: {posts}")
phase_b = first["status"] == "REGISTERED" and second["status"] == "ALREADY_REGISTERED" and posts == 1
print(f"\n  PHASE B: {'PASS' if phase_b else 'FAIL'}")
sys.exit(0 if phase_b else 1)
