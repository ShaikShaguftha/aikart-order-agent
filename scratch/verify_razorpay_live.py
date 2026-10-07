"""
READ-ONLY live Razorpay verification for tenant COMP-RAZORPAY (intended for Test Mode keys).

    PYTHONPATH=. venv/bin/python scratch/verify_razorpay_live.py --payment-id pay_XXXXXXXXXXXXXX
    PYTHONPATH=. venv/bin/python scratch/verify_razorpay_live.py --refund-id rfnd_XXXXXXXXXXXXXX

Needs RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET in .env (RAZORPAY_ENV_TENANT defaults to COMP-RAZORPAY).
Only GET requests can leave the process (anything else is blocked before sending). The key secret
and the Authorization header are never printed. No payment or refund IDs are hardcoded.
"""
import argparse
import os
import sys

sys.path.insert(0, ".")
import env_config  # noqa: E402
import httpx  # noqa: E402

TRACE = []
_request = httpx.Client.request


def _read_only(self, method, url, *args, **kwargs):
    method = str(method).upper()
    if method != "GET":
        raise RuntimeError(f"BLOCKED non-read request: {method}")
    response = _request(self, method, url, *args, **kwargs)
    TRACE.append((method, str(url).split("?")[0].split("api.razorpay.com/", 1)[-1], response.status_code))
    return response


httpx.Client.request = _read_only

from database import init_db  # noqa: E402
from integrations.errors import TenantConfigurationError  # noqa: E402
from integrations.gateway import gateway  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--payment-id")
parser.add_argument("--refund-id")
args = parser.parse_args()
tenant = os.getenv("RAZORPAY_ENV_TENANT") or "COMP-RAZORPAY"

print("[CONFIG CHECK]")
print(f"  .env loaded from: {env_config.ENV_FILE or 'NOT FOUND'}")
KEY_ID = os.getenv("RAZORPAY_KEY_ID") or os.getenv("RAZORPAY_API_KEY")
KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET") or os.getenv("RAZORPAY_SECRET")
print(f"  Key ID configured (RAZORPAY_KEY_ID or RAZORPAY_API_KEY): {'YES' if KEY_ID else 'NO'}")
print(f"  Key secret configured (RAZORPAY_KEY_SECRET or RAZORPAY_SECRET): {'YES' if KEY_SECRET else 'NO'}")
print(f"  RAZORPAY_ENV_TENANT configured: {'YES' if os.getenv('RAZORPAY_ENV_TENANT') else 'NO (default COMP-RAZORPAY)'}")
if not (KEY_ID and KEY_SECRET):
    print("\nCREDENTIALS NOT CONFIGURED - set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in .env. Nothing was sent.")
    print("Live verification: NOT RUN")
    sys.exit(2)
if not (args.payment_id or args.refund_id):
    print("\nSTOP: give --payment-id and/or --refund-id to look up (nothing is hardcoded).")
    sys.exit(2)

init_db()  # idempotent: seeds the COMP-RAZORPAY tenant row exactly as server startup does
try:
    connector = gateway.get_connector(tenant)
except TenantConfigurationError as e:
    print(f"\nSTOP: {e.message}")
    print("Live verification: FAILED")
    sys.exit(1)
print(f"\n[TENANT]: {tenant}\n[PROVIDER]: {connector.provider_name}\n[KEY MODE]: {connector.mode}\n[CREDENTIAL SOURCE]: ENV")

results = []
if args.payment_id:
    payment = gateway.razorpay_get_payment(tenant, args.payment_id)
    refunds = gateway.razorpay_get_payment_refunds(tenant, args.payment_id)
    results += [payment, refunds]
    if "error" not in payment:
        print(f"\n[PAYMENT] {payment['id']} | {payment.get('currency')} {payment.get('amount')} | status: {payment.get('status')}"
              f" | method: {payment.get('method')} | refunded: {payment.get('amount_refunded')} ({payment.get('refund_status')})")
    if "error" not in refunds:
        print(f"\n[REFUNDS] {refunds['count']} refund(s)")
        for r in refunds["refunds"]:
            print(f"  {r['id']} | {r.get('currency')} {r.get('amount')} | status: {r.get('status')} | speed: {r.get('speed')}")
if args.refund_id:
    refund = gateway.razorpay_get_refund(tenant, args.refund_id)
    results.append(refund)
    if "error" not in refund:
        print(f"\n[REFUND] {refund['id']} | payment: {refund.get('payment_id')} | {refund.get('currency')} {refund.get('amount')}"
              f" | status: {refund.get('status')}")

print("\n[PROVIDER HTTP]")
for method, path, status in TRACE:
    print(f"  {method} {path} | HTTP status: {status}")
errors = [r for r in results if "error" in r]
for err in errors:
    print(f"\nERROR: {err['code']} - {err['error']}")
assert KEY_SECRET not in str(results), "secret leaked into results"
passed = bool(TRACE) and not errors and all(status == 200 for _, _, status in TRACE)
print(f"\nLive verification: {'PASSED' if passed else 'FAILED'}")
sys.exit(0 if passed else 1)
