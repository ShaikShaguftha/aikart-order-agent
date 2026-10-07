"""
Read-only LIVE verification of a WooCommerce tenant (default COMP-WOOCOMMERCE).

    venv/bin/python scratch/verify_woocommerce_live.py [COMPANY_ID]

Any non-GET HTTP request raises before it is sent, so this can never create, cancel,
refund or update anything. Keys/secrets are never printed; emails/phones are masked.
"""
import sys
from urllib.parse import urlparse

sys.path.insert(0, ".")
import env_config  # noqa: E402,F401
import httpx  # noqa: E402

_original_request = httpx.Client.request


def _get_only(self, method, url, *args, **kwargs):
    if str(method).upper() != "GET":
        raise RuntimeError(f"BLOCKED non-GET request: {method}")
    return _original_request(self, method, url, *args, **kwargs)


httpx.Client.request = _get_only

from integrations.gateway import gateway  # noqa: E402
from integrations.models import CustomerIdentity  # noqa: E402

TENANT = sys.argv[1] if len(sys.argv) > 1 else "COMP-WOOCOMMERCE"
report = {}


def mask(v):
    v = str(v or "")
    if not v:
        return "(none)"
    if "@" in v:
        user, dom = v.split("@", 1)
        return f"{user[:2]}***@{dom[:1]}***.{dom.rsplit('.', 1)[-1]}"
    return v[:3] + "***" + v[-2:]


connector = gateway.get_connector(TENANT)
url = urlparse(connector.site_url)
print(f"\n## Config\n  tenant: {TENANT} | connector: {connector.__class__.__name__} | host: {url.hostname} | https: {url.scheme == 'https'}")
print(f"  credentials configured: {connector.credentials_configured()}")
if "playground.wordpress.net" in (url.hostname or ""):
    print("  STOP: URL is WordPress Playground (browser-only, no remote REST API). Configure a real store first.")
    sys.exit(1)

print("\n## 1. Reachable (unauthenticated GET /wp-json/)")
try:
    r = httpx.get(f"{connector.site_url}/wp-json/", timeout=15, follow_redirects=True)
    report["reachable"] = r.status_code == 200 and "wc/v3" in r.json().get("namespaces", [])
    print(f"  HTTP {r.status_code} | wc/v3 present: {report['reachable']}")
except Exception as e:
    report["reachable"] = False
    print(f"  FAILED: {type(e).__name__}")

for label, endpoint, params in (("2. Auth (system_status)", "system_status", None),
                                ("3. Products", "products", {"per_page": 5}),
                                ("4. Orders", "orders", {"per_page": 20})):
    res = connector._execute_request("GET", endpoint, params=params, max_retries=1)
    ok = "error" not in res if isinstance(res, dict) else True
    report[endpoint] = ok
    print(f"\n## {label}\n  {'OK' if ok else res.get('code') + ': ' + res.get('error')}")
    if endpoint == "products" and ok:
        print("  ", [p.get("name") for p in res])
    if endpoint == "orders":
        raw_orders = res if ok else []
        for o in raw_orders:
            b = o.get("billing") or {}
            print(f"  number={o.get('number')} status={o.get('status')} customer_id={o.get('customer_id')} "
                  f"email={mask(b.get('email'))} phone={mask(b.get('phone'))}")

print("\n## 5. Identity-first: customer -> customer's orders")
sample = next((o for o in raw_orders if (o.get("billing") or {}).get("email")), None)
if not sample:
    print("  No order with a billing email; cannot test identity flow.")
else:
    expected = str(sample.get("number") or sample.get("id"))
    for kind in ("email", "phone"):
        value = (sample.get("billing") or {}).get(kind)
        if not value:
            print(f"  by {kind}: order has no billing {kind}")
            continue
        result = gateway.get_customer_orders(CustomerIdentity(**{kind: value}), TENANT, limit=10)
        if "error" in result:
            report[f"by_{kind}"] = False
            print(f"  by {kind} ({mask(value)}): {result.get('code')} {result['error']}")
            continue
        ids = [o["id"] for o in result["orders"]]
        report[f"by_{kind}"] = expected in ids
        print(f"  by {kind} ({mask(value)}): orders {ids} | {expected} present: {expected in ids}")

    print("\n## 6. Explicit order lookup + exact ID")
    for ref in (expected, f"#{expected}"):
        order = gateway.get_order_details(ref, TENANT)
        ok = bool(order) and "error" not in order and order["id"] == expected
        report[f"explicit {ref}"] = ok
        print(f"  {ref!r} -> {order['id'] + ' ' + order['status'] if ok else order}")

print("\nSUMMARY:", {k: ("PASS" if v else "FAIL") for k, v in report.items()})
