"""Direct provider test: gateway -> connector -> real provider API. Prints safe diagnostics only."""
import json, sys
sys.path.insert(0, ".")
from backend import env_config
from backend.database import init_db
from backend.integrations.gateway import gateway

init_db()
for l in env_config.provider_env_status(): print("[CONFIG]", l)
for l in gateway.describe_tenants(): print("[TENANT]", l)

T = "COMP-SHOPIFY"
results = {}

def check(label, ok, detail):
    results[label] = ok
    print(f"\n=== {label}: {'PASS' if ok else 'FAIL'} | {detail}")

shop = gateway.get_shop(T)
check("get_shop()", "error" not in shop, f"name={shop.get('name')} domain={shop.get('domain')} err={shop.get('error')}")

orders = gateway.get_orders(T, 5)
ok = orders and "error" not in orders[0]
check("get_orders(limit=5)", ok, [(o.get("id"), o.get("financial_status"), o.get("fulfillment_status")) for o in orders] if ok else orders)

gid = None
for ref in ["#1003", "1003"]:
    o = gateway.get_order_details(ref, T)
    ok = bool(o) and "error" not in o and o.get("id") == "#1003"
    check(f'get_order_details("{ref}")', ok, json.dumps({k: o.get(k) for k in ("id","status","financial_status","fulfillment_status","total","items")}) if o and "error" not in o else o)

# Find the real GID for #1003 via the raw node, then look it up by GID
node = gateway.get_connector(T)._find_order_node("#1003", "id name")
gid = (node.get("order") or {}).get("id")
o = gateway.get_order_details(gid, T) if gid else None
check(f'get_order_details("{gid}")', bool(o) and "error" not in o and o.get("id") == "#1003", f"resolved name={o.get('id') if o else None}")

o = gateway.get_order_details("#99999", T)
check('get_order_details("#99999") -> not found (no fallback)', o is None, f"result={o}")

p = gateway.get_product("The Videographer Snowboard", T)
check('get_product("The Videographer Snowboard")', "error" not in p, json.dumps(p)[:300])

print("\nSHOPIFY_DIRECT_TEST=" + ("PASS" if all(results.values()) else "FAIL"))
