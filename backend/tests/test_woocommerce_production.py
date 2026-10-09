"""
Mocked WooCommerce production-readiness tests. No real store is contacted: every HTTP
call goes through FakeWoo, which also records calls so tests can assert that no write
request was made.
"""
import base64
import datetime
import hashlib
import hmac
import io
import json
import os
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import httpx
from cryptography.fernet import Fernet

import agent
from backend.database import execute_db, init_db, query_db
from backend.integrations import credential_store
from backend.integrations.errors import TenantConfigurationError
from backend.integrations.gateway import gateway
from backend.integrations.models import CustomerIdentity
from backend.integrations.onboarding import connect_woocommerce, validate_woocommerce_connection
from backend.integrations.policy import ReturnPolicyEngine
from backend.integrations.providers.local_db import LocalDBConnector
from backend.integrations.providers.woocommerce import WooCommerceConnector
from backend.integrations.webhooks import process_woocommerce_webhook

TENANT_A = "COMP-TEST-WOO-A"
TENANT_B = "COMP-TEST-WOO-B"
URL_A = "https://shop-a.example.com"
URL_B = "https://shop-b.example.com"
KEY_A, SECRET_A = "ck_tenant_a_key_0001", "cs_tenant_a_secret_0001"
KEY_B, SECRET_B = "ck_tenant_b_key_0002", "cs_tenant_b_secret_0002"
WEBHOOK_SECRET_A = "whsec_tenant_a"
EMAIL = "alice@example.com"
PHONE = "+1 (555) 000-1234"


def _resp(status=200, body=None, headers=None, text=""):
    response = MagicMock()
    response.status_code = status
    response.headers = headers or {}
    response.text = text
    if isinstance(body, Exception):
        response.json.side_effect = body
    else:
        response.json.return_value = body
    return response


class FakeWoo:
    """Routes (method, path-after-/wp-json/, params predicate) to canned responses."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method=None, url=None, params=None, json=None, auth=None, **_):
        host, path = url.split("://", 1)[1].split("/wp-json/", 1)
        self.calls.append({"method": method, "host": host, "path": path, "params": dict(params or {}), "auth": auth})
        for route_method, route_path, predicate, response in self.routes:
            if route_method == method and route_path == path and (predicate is None or predicate(params or {})):
                result = response() if isinstance(response, types.FunctionType) else response
                if isinstance(result, Exception):
                    raise result
                return result
        return _resp(404, {"code": "woocommerce_rest_shop_order_invalid_id"})

    @property
    def write_calls(self):
        return [c for c in self.calls if c["method"] != "GET"]


def woo_order(wc_id, number, status="completed", email=EMAIL, phone=PHONE, customer_id=45,
              total="40.00", completed_days_ago=3, refunds=None):
    completed = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=completed_days_ago))
    return {
        "id": wc_id, "number": str(number), "status": status, "customer_id": customer_id, "total": total,
        "date_created_gmt": (completed - datetime.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S"),
        "date_completed_gmt": completed.strftime("%Y-%m-%dT%H:%M:%S") if status == "completed" else None,
        "billing": {"first_name": "Alice", "last_name": "Wonder", "email": email, "phone": phone},
        "line_items": [{"name": "Ski Wax", "quantity": 1, "price": total}],
        "meta_data": [], "refunds": refunds or [],
    }


CUSTOMER = {"id": 45, "email": EMAIL, "first_name": "Alice", "last_name": "Wonder", "billing": {"phone": PHONE}}


def connector(**kw):
    return WooCommerceConnector(site_url=URL_A, consumer_key=KEY_A, consumer_secret=SECRET_A,
                                use_env_fallback=False, retry_backoff=0, **kw)


class WooTenantFixture(unittest.TestCase):
    """Two merchant tenants with encrypted stored credentials."""

    @classmethod
    def setUpClass(cls):
        cls.env_patch = patch.dict(os.environ, {credential_store.KEY_ENV: Fernet.generate_key().decode()})
        cls.env_patch.start()
        init_db()
        for tenant, url, key, secret, wh in ((TENANT_A, URL_A, KEY_A, SECRET_A, WEBHOOK_SECRET_A),
                                             (TENANT_B, URL_B, KEY_B, SECRET_B, None)):
            execute_db("INSERT OR IGNORE INTO companies (id, name, tier) VALUES (?, ?, 'STANDARD')", (tenant, tenant))
            execute_db("DELETE FROM company_rules WHERE company_id = ?", (tenant,))
            execute_db("INSERT INTO company_rules (company_id, max_auto_refund_amount, return_window_days) VALUES (?, 100.0, 30)", (tenant,))
            execute_db(
                "INSERT OR REPLACE INTO company_integrations (company_id, provider_type, shop_domain, api_version, consumer_key, consumer_secret, webhook_secret) "
                "VALUES (?, 'WOOCOMMERCE', ?, '2024-04', ?, ?, ?)",
                (tenant, url, credential_store.encrypt(key), credential_store.encrypt(secret),
                 credential_store.encrypt(wh) if wh else None),
            )

    @classmethod
    def tearDownClass(cls):
        for table, column in (("company_integrations", "company_id"), ("company_rules", "company_id"),
                              ("support_tickets", "company_id"), ("return_requests", "company_id"),
                              ("webhook_events", "company_id"), ("action_audit_logs", "company_id"),
                              ("companies", "id")):
            for tenant in (TENANT_A, TENANT_B, "COMP-TEST-WOO-NEW", "COMP-TEST-WOO-C"):
                execute_db(f"DELETE FROM {table} WHERE {column} = ?", (tenant,))
        cls.env_patch.stop()

    def fake(self, routes):
        fake = FakeWoo(routes)
        patcher = patch("httpx.Client.request", side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleep = patch("integrations.providers.woocommerce.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)
        return fake


class IdentityFlowTest(WooTenantFixture):

    def test_customer_lookup_by_email_then_orders(self):
        fake = self.fake([
            ("GET", "wc/v3/customers", lambda p: p.get("email") == EMAIL, _resp(200, [CUSTOMER])),
            ("GET", "wc/v3/orders", lambda p: p.get("customer") == "45",
             _resp(200, [woo_order(88, 1003), woo_order(77, 1001, completed_days_ago=20)])),
        ])
        result = gateway.get_customer_orders(CustomerIdentity(email=EMAIL), TENANT_A)
        self.assertEqual([o["id"] for o in result["orders"]], ["1003", "1001"])
        self.assertEqual(result["customer"]["customer_id"], "45")
        orders_call = [c for c in fake.calls if c["path"] == "wc/v3/orders"][0]
        self.assertEqual((orders_call["params"]["orderby"], orders_call["params"]["order"]), ("date", "desc"))

    def test_customer_lookup_by_phone(self):
        other = woo_order(90, 1010, email="bob@example.com", phone="+1 555 999 0000", customer_id=0)
        mine = woo_order(91, 1011, customer_id=0)
        self.fake([("GET", "wc/v3/orders", None, _resp(200, [other, mine]))])
        result = gateway.get_customer_orders(CustomerIdentity(phone="15550001234"), TENANT_A)
        self.assertEqual([o["id"] for o in result["orders"]], ["1011"])

    def test_latest_order_is_re_read_fresh(self):
        fake = self.fake([
            ("GET", "wc/v3/customers", None, _resp(200, [CUSTOMER])),
            ("GET", "wc/v3/orders", lambda p: "customer" in p, _resp(200, [woo_order(88, 1003, status="processing")])),
            ("GET", "wc/v3/orders/1003", None, _resp(404, {"code": "woocommerce_rest_shop_order_invalid_id"})),
            ("GET", "wc/v3/orders", lambda p: p.get("search") == "1003", _resp(200, [woo_order(88, 1003, status="completed")])),
        ])
        result = gateway.get_latest_order(CustomerIdentity(email=EMAIL), TENANT_A)
        self.assertEqual(result["order"]["id"], "1003")
        self.assertEqual(result["order"]["fulfillment_status"], "FULFILLED")
        self.assertTrue(result["fresh_provider_lookup"])
        self.assertTrue(any(c["params"].get("search") == "1003" for c in fake.calls))

    def test_llm_cannot_invent_customer_identity(self):
        args = {"customer_id": "45", "authenticated": True}
        agent.apply_identity(args, None, {})
        self.assertEqual((args["customer_id"], args["authenticated"]), ("", False))
        fake = self.fake([])
        result = gateway.get_customer_orders(CustomerIdentity(customer_id="45"), TENANT_A)
        self.assertEqual(result["code"], "IDENTITY_REQUIRED")
        self.assertEqual(fake.calls, [])

    def test_name_alone_is_rejected(self):
        self.fake([])
        result = gateway.get_customer_orders(CustomerIdentity(name="Alice Wonder"), TENANT_A)
        self.assertEqual(result["code"], "IDENTITY_REQUIRED")


class OrderLookupTest(WooTenantFixture):

    def test_explicit_order_lookup_and_exact_number_preserved(self):
        self.fake([("GET", "wc/v3/orders/1003", None, _resp(200, woo_order(1003, 1003)))])
        for ref in ("1003", "#1003"):
            order = gateway.get_order_details(ref, TENANT_A)
            self.assertEqual(order["id"], "1003")

    def test_custom_order_numbering_does_not_return_wrong_order(self):
        # Internal ID 1003 is a different order (number 1050); order number 1003 has ID 57.
        self.fake([
            ("GET", "wc/v3/orders/1003", None, _resp(200, woo_order(1003, 1050, email="other@example.com"))),
            ("GET", "wc/v3/orders", lambda p: p.get("search") == "1003", _resp(200, [woo_order(57, 1003)])),
        ])
        order = gateway.get_order_details("1003", TENANT_A)
        self.assertEqual((order["id"], order["provider_order_id"]), ("1003", "57"))

    def test_search_never_returns_a_non_matching_order(self):
        self.fake([("GET", "wc/v3/orders", None, _resp(200, [woo_order(5, 10030, email="other@example.com")]))])
        self.assertIsNone(gateway.get_order_details("1003", TENANT_A))

    def test_ord_prefix_is_not_rewritten(self):
        fake = self.fake([("GET", "wc/v3/orders", None, _resp(200, []))])
        self.assertIsNone(gateway.get_order_details("ORD-1003", TENANT_A))
        self.assertEqual([c["params"].get("search") for c in fake.calls], ["ORD-1003"])

    def test_no_fabricated_tracking(self):
        self.fake([
            ("GET", "wc/v3/orders/1003", None, _resp(200, woo_order(1003, 1003))),
            ("GET", "wc-shipment-tracking/v3/orders/1003/shipment-trackings", None, _resp(404, {"code": "rest_no_route"})),
        ])
        shipment = gateway.get_shipment_status("1003", TENANT_A)
        self.assertEqual(shipment["status"], "FULFILLED")
        self.assertIsNone(shipment["tracking_number"])
        self.assertIsNone(shipment["courier"])

    def test_tracking_from_shipment_tracking_extension(self):
        self.fake([
            ("GET", "wc/v3/orders/1003", None, _resp(200, woo_order(1003, 1003))),
            ("GET", "wc-shipment-tracking/v3/orders/1003/shipment-trackings", None, _resp(200, [
                {"tracking_id": "t1", "tracking_provider": "UPS", "tracking_number": "1Z999", "tracking_link": "https://ups.example/1Z999"},
            ])),
        ])
        shipment = gateway.get_shipment_status("#1003", TENANT_A)
        self.assertEqual((shipment["courier"], shipment["tracking_number"], shipment["order_id"]), ("UPS", "1Z999", "1003"))


class ProductTest(WooTenantFixture):

    def test_product_lookup_prefers_exact_name(self):
        self.fake([("GET", "wc/v3/products", None, _resp(200, [
            {"id": 1, "name": "Ski Wax Pro", "price": "30", "stock_status": "instock"},
            {"id": 2, "name": "Ski Wax", "price": "20", "stock_status": "instock"},
        ]))])
        product = gateway.get_product("Ski Wax", TENANT_A)
        self.assertEqual(product["product_id"], "2")

    def test_inventory_includes_variations(self):
        self.fake([
            ("GET", "wc/v3/products", None, _resp(200, [{"id": 7, "name": "Hoodie", "type": "variable", "stock_status": "instock"}])),
            ("GET", "wc/v3/products/7/variations", None, _resp(200, [
                {"id": 71, "sku": "HD-S", "stock_status": "instock", "stock_quantity": 4, "attributes": [{"name": "Size", "option": "S"}]},
                {"id": 72, "sku": "HD-L", "stock_status": "outofstock", "stock_quantity": 0, "attributes": [{"name": "Size", "option": "L"}]},
            ])),
        ])
        inventory = gateway.get_inventory("Hoodie", TENANT_A)
        self.assertEqual([(v["attributes"]["Size"], v["stock_quantity"]) for v in inventory["variants"]], [("S", 4), ("L", 0)])


class ErrorHandlingTest(WooTenantFixture):

    def _single(self, response):
        return self.fake([("GET", "wc/v3/orders/1003", None, response)])

    def test_authentication_failure(self):
        self._single(_resp(401, {"code": "woocommerce_rest_cannot_view"}, text=f"bad key {KEY_A}"))
        result = connector().get_order_details("1003", TENANT_A)
        self.assertEqual(result["code"], "PROVIDER_AUTH_FAILED")
        self.assertNotIn(KEY_A, json.dumps(result))

    def test_permission_failure(self):
        self._single(_resp(403, {"code": "woocommerce_rest_cannot_view"}))
        self.assertEqual(connector().get_order_details("1003", TENANT_A)["code"], "PROVIDER_FORBIDDEN")

    def test_404_resource_vs_missing_api(self):
        self.fake([
            ("GET", "wc/v3/orders/1003", None, _resp(404, {"code": "woocommerce_rest_shop_order_invalid_id"})),
            ("GET", "wc/v3/orders", None, _resp(200, [])),
        ])
        self.assertIsNone(connector().get_order_details("1003", TENANT_A))
        self._single(_resp(404, {"code": "rest_no_route"}))
        self.assertEqual(connector().get_order_details("1003", TENANT_A)["code"], "PROVIDER_API_UNAVAILABLE")

    def test_rate_limit_retries_then_succeeds_and_honours_retry_after(self):
        responses = iter([_resp(429, {}, headers={"Retry-After": "2"}), _resp(200, woo_order(1003, 1003))])
        fake = self._single(lambda: next(responses))
        with patch("integrations.providers.woocommerce.time.sleep") as sleep:
            order = connector().get_order_details("1003", TENANT_A)
        self.assertEqual(order["id"], "1003")
        sleep.assert_called_once_with(2.0)
        self.assertEqual(len(fake.calls), 2)

    def test_persistent_rate_limit(self):
        self._single(_resp(429, {}, headers={"Retry-After": "600"}))
        with patch("integrations.providers.woocommerce.time.sleep") as sleep:
            result = connector().get_order_details("1003", TENANT_A)
        self.assertEqual(result["code"], "RATE_LIMITED")
        self.assertTrue(all(call.args[0] <= WooCommerceConnector.MAX_RETRY_AFTER_SECONDS for call in sleep.call_args_list))

    def test_provider_unavailable_timeout_and_network(self):
        cases = [
            (_resp(503, {}, text="<html>stack trace secret</html>"), "PROVIDER_UNAVAILABLE"),
            (httpx.ReadTimeout("slow"), "PROVIDER_TIMEOUT"),
            (httpx.ConnectError(f"cannot connect with {SECRET_A}"), "PROVIDER_UNREACHABLE"),
            (_resp(200, ValueError("not json")), "PROVIDER_INVALID_RESPONSE"),
        ]
        for response, code in cases:
            with self.subTest(code=code):
                fake = FakeWoo([("GET", "wc/v3/orders/1003", None, response)])
                with patch("httpx.Client.request", side_effect=fake), patch("integrations.providers.woocommerce.time.sleep"):
                    result = connector().get_order_details("1003", TENANT_A)
                self.assertEqual(result["code"], code)
                self.assertNotIn(SECRET_A, json.dumps(result))
                self.assertNotIn("stack trace", json.dumps(result))

    def test_writes_are_never_retried(self):
        fake = self.fake([
            ("GET", "wc/v3/orders/1004", None, _resp(200, woo_order(1004, 1004, status="pending"))),
            ("PUT", "wc/v3/orders/1004", None, _resp(503, {})),
        ])
        result = connector().cancel_order("1004", TENANT_A)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(len(fake.write_calls), 1)

    def test_plain_http_store_is_refused_without_sending_credentials(self):
        fake = self.fake([])
        insecure = WooCommerceConnector(site_url="http://shop.example.com", consumer_key=KEY_A,
                                        consumer_secret=SECRET_A, use_env_fallback=False)
        self.assertEqual(insecure.get_orders(TENANT_A)[0]["code"], "PROVIDER_CONFIG_INSECURE")
        self.assertEqual(fake.calls, [])

    def test_credentials_never_logged(self):
        self.fake([("GET", "wc/v3/orders/1003", None, httpx.ConnectError(f"boom {KEY_A} {SECRET_A}"))])
        buffer = io.StringIO()
        with patch("sys.__stdout__", buffer), redirect_stdout(buffer):
            connector().get_order_details("1003", TENANT_A)
        self.assertNotIn(KEY_A, buffer.getvalue())
        self.assertNotIn(SECRET_A, buffer.getvalue())


class TenantIsolationTest(WooTenantFixture):

    def test_each_tenant_uses_only_its_own_store_and_credentials(self):
        fake = self.fake([("GET", "wc/v3/orders/1003", None, _resp(200, woo_order(1003, 1003)))])
        gateway.get_order_details("1003", TENANT_A)
        gateway.get_order_details("1003", TENANT_B)
        self.assertEqual([(c["host"], c["auth"]) for c in fake.calls],
                         [("shop-a.example.com", (KEY_A, SECRET_A)), ("shop-b.example.com", (KEY_B, SECRET_B))])

    def test_stored_credentials_are_encrypted_at_rest(self):
        row = query_db("SELECT consumer_key, consumer_secret FROM company_integrations WHERE company_id = ?", (TENANT_A,), one=True)
        self.assertTrue(row["consumer_key"].startswith("enc:v1:"))
        self.assertNotIn(KEY_A, json.dumps(row))
        self.assertNotIn(SECRET_A, json.dumps(row))

    def test_incomplete_stored_credentials_never_mix_with_env(self):
        execute_db("INSERT OR IGNORE INTO companies (id, name, tier) VALUES ('COMP-TEST-WOO-C', 'C', 'STANDARD')")
        execute_db("INSERT OR REPLACE INTO company_integrations (company_id, provider_type, shop_domain) VALUES ('COMP-TEST-WOO-C', 'WOOCOMMERCE', 'https://shop-c.example.com')")
        with self.assertRaises(TenantConfigurationError):
            gateway.get_connector("COMP-TEST-WOO-C")

    def test_other_woo_tenant_cannot_use_env_store(self):
        execute_db("INSERT OR IGNORE INTO companies (id, name, tier) VALUES ('COMP-TEST-WOO-C', 'C', 'STANDARD')")
        execute_db("INSERT OR REPLACE INTO company_integrations (company_id, provider_type) VALUES ('COMP-TEST-WOO-C', 'WOOCOMMERCE')")
        with self.assertRaises(TenantConfigurationError):
            gateway.get_connector("COMP-TEST-WOO-C")

    @patch.object(LocalDBConnector, "get_order_details")
    def test_no_local_db_fallback(self, local_lookup):
        self.fake([("GET", "wc/v3/orders/1003", None, _resp(503, {}))])
        for tenant in (TENANT_A, "COMP-WOOCOMMERCE"):
            result = gateway.get_order_details("1003", tenant)
            self.assertIn("code", result)
        local_lookup.assert_not_called()

    def test_tickets_are_tenant_scoped(self):
        self.fake([])
        ticket = gateway.create_support_ticket("1003", "Damaged item", TENANT_A)
        self.assertEqual(gateway.get_ticket_details(ticket["ticket_id"], TENANT_A)["reason"], "Damaged item")
        self.assertEqual(gateway.get_ticket_details(ticket["ticket_id"], TENANT_B)["code"], "NOT_FOUND")


class SensitiveActionTest(WooTenantFixture):

    def test_paid_order_cancellation_requires_human_review_and_makes_no_write(self):
        fake = self.fake([("GET", "wc/v3/orders/1004", None, _resp(200, woo_order(1004, 1004, status="processing")))])
        result = gateway.cancel_order("1004", TENANT_A, session_id="s1", confirmed=False)
        self.assertEqual(result["status"], "REQUIRES_HUMAN_REVIEW")
        confirmed = gateway.cancel_order("1004", TENANT_A, session_id="s1", confirmed=True)
        self.assertNotEqual(confirmed.get("status"), "CANCELLED")
        self.assertEqual(fake.write_calls, [])

    def test_unpaid_order_needs_confirmation_and_never_claims_refund(self):
        state = {"status": "pending"}
        fake = self.fake([
            ("GET", "wc/v3/orders/1005", None, lambda: _resp(200, woo_order(1005, 1005, status=state["status"]))),
            ("PUT", "wc/v3/orders/1005", None, lambda: (state.update(status="cancelled"), _resp(200, {"id": 1005, "status": "cancelled"}))[1]),
        ])
        prep = gateway.cancel_order("1005", TENANT_A, session_id="s2", confirmed=False)
        self.assertEqual(prep["status"], "REQUIRES_CONFIRMATION")
        self.assertEqual(fake.write_calls, [])
        done = gateway.cancel_order("1005", TENANT_A, session_id="s2", confirmed=True)
        self.assertEqual((done["status"], done["refund_issued"], done["verification_status"]), ("CANCELLED", False, "VERIFIED"))
        audit = query_db("SELECT status FROM action_audit_logs WHERE company_id = ? AND order_id = '1005' ORDER BY id", (TENANT_A,))
        self.assertEqual([a["status"] for a in audit], ["STAGED_FOR_CONFIRMATION", "CANCELLED"])

    def _return_routes(self, order):
        return [
            ("GET", f"wc/v3/orders/{order['id']}", None, _resp(200, order)),
            ("GET", f"wc/v3/orders/{order['id']}/refunds", None, _resp(200, [])),
        ]

    def test_return_of_unfulfilled_order_is_rejected(self):
        fake = self.fake(self._return_routes(woo_order(2001, 2001, status="processing")))
        result = gateway.request_return("2001", 10.0, TENANT_A)
        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(fake.write_calls, [])

    def test_small_return_is_submitted_not_refunded(self):
        fake = self.fake(self._return_routes(woo_order(2002, 2002)))
        result = gateway.request_return("2002", 20.0, TENANT_A, reason="Wrong size")
        self.assertEqual((result["status"], result["refund_issued"]), ("SUBMITTED", False))
        self.assertTrue(result["return_id"].startswith("RET-"))
        self.assertEqual(fake.write_calls, [])
        again = gateway.request_return("2002", 20.0, TENANT_A)
        self.assertEqual(again["status"], "ALREADY_REQUESTED")

    def test_large_return_requires_human_review_with_ticket(self):
        self.fake(self._return_routes(woo_order(2003, 2003, total="250.00")))
        result = gateway.request_return("2003", 250.0, TENANT_A)
        self.assertEqual(result["status"], "REQUIRES_HUMAN_REVIEW")
        self.assertIsNotNone(gateway.get_ticket_details(result["ticket_id"], TENANT_A).get("ticket_id"))

    def test_return_window_and_refundable_balance(self):
        rules = {"return_window_days": 30, "max_auto_refund_amount": 100}
        old = gateway._woocommerce_connector._transform_woocommerce_order(woo_order(1, 1, completed_days_ago=45), TENANT_A)
        self.assertFalse(ReturnPolicyEngine.evaluate(old, rules).eligible)
        recent = gateway._woocommerce_connector._transform_woocommerce_order(woo_order(2, 2, total="40.00"), TENANT_A)
        self.assertFalse(ReturnPolicyEngine.evaluate(recent, rules, requested_amount=30, already_refunded=20).eligible)
        self.assertTrue(ReturnPolicyEngine.evaluate(recent, rules, requested_amount=20, already_refunded=20).eligible)


def _signed(body: bytes, secret: str) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


class WebhookTest(WooTenantFixture):

    def _deliver(self, payload, topic="order.updated", delivery="d1", source=URL_A + "/", secret=WEBHOOK_SECRET_A, sign=True):
        body = json.dumps(payload).encode()
        headers = {"X-WC-Webhook-Topic": topic, "X-WC-Webhook-Source": source, "X-WC-Webhook-Delivery-ID": delivery}
        if sign:
            headers["X-WC-Webhook-Signature"] = _signed(body, secret)
        return process_woocommerce_webhook(headers, body)

    def test_status_change_refund_and_dedupe(self):
        self.assertEqual(self._deliver(woo_order(3001, 3001, status="processing"), "order.created", "w1")[1]["event_type"], "ORDER_CREATED")
        self.assertEqual(self._deliver(woo_order(3001, 3001, status="completed"), delivery="w2")[1]["event_type"], "ORDER_STATUS_CHANGED")
        refunded = woo_order(3001, 3001, status="completed", refunds=[{"id": 1, "total": "-10.00"}])
        self.assertEqual(self._deliver(refunded, delivery="w3")[1]["event_type"], "REFUND_RECORDED")
        self.assertEqual(self._deliver(refunded, delivery="w3")[1]["status"], "duplicate")
        stored = query_db("SELECT * FROM webhook_events WHERE company_id = ?", (TENANT_A,))
        self.assertNotIn(EMAIL, json.dumps(stored))

    def test_unsigned_or_wrongly_signed_webhooks_are_rejected(self):
        self.assertEqual(self._deliver(woo_order(1, 1), sign=False)[0], 401)
        self.assertEqual(self._deliver(woo_order(1, 1), secret="wrong")[0], 401)

    def test_unknown_source_and_tenant_without_secret_are_rejected(self):
        self.assertEqual(self._deliver(woo_order(1, 1), source="https://evil.example.com")[0], 401)
        self.assertEqual(self._deliver(woo_order(1, 1), source=URL_B, secret="anything")[0], 401)

    def test_ping(self):
        self.assertEqual(process_woocommerce_webhook({}, b"webhook_id=12"), (200, {"status": "ping"}))


class OnboardingTest(WooTenantFixture):

    def test_validation_is_read_only(self):
        fake = self.fake([
            ("GET", "wc/v3/orders", None, _resp(200, [])),
            ("GET", "wc/v3/products", None, _resp(200, [])),
            ("GET", "wc/v3/customers", None, _resp(200, [])),
        ])
        result = validate_woocommerce_connection("https://new-shop.example.com", "ck_new", "cs_new")
        self.assertTrue(result["ok"])
        self.assertEqual(fake.write_calls, [])

    def test_connect_stores_encrypted_credentials(self):
        self.fake([
            ("GET", "wc/v3/orders", None, _resp(200, [])),
            ("GET", "wc/v3/products", None, _resp(200, [])),
            ("GET", "wc/v3/customers", None, _resp(200, [])),
        ])
        result = connect_woocommerce("COMP-TEST-WOO-NEW", "New Shop", "https://new-shop.example.com/", "ck_new_123", "cs_new_456")
        self.assertTrue(result["connected"])
        self.assertNotIn("cs_new_456", json.dumps(result))
        row = query_db("SELECT * FROM company_integrations WHERE company_id = 'COMP-TEST-WOO-NEW'", one=True)
        self.assertTrue(row["consumer_secret"].startswith("enc:v1:"))
        self.assertEqual(gateway.get_connector("COMP-TEST-WOO-NEW").base_api_url, "https://new-shop.example.com/wp-json/wc/v3")

    def test_failed_validation_stores_nothing(self):
        self.fake([("GET", "wc/v3/orders", None, _resp(401, {"code": "woocommerce_rest_cannot_view"}))])
        result = connect_woocommerce("COMP-TEST-WOO-NEW", "New Shop", "https://bad.example.com", "ck_bad", "cs_bad")
        self.assertFalse(result["connected"])
        self.assertEqual(result["checks"]["orders"], "PROVIDER_AUTH_FAILED")

    def test_connect_refused_without_encryption_key(self):
        with patch.dict(os.environ, {credential_store.KEY_ENV: ""}):
            with self.assertRaises(TenantConfigurationError):
                connect_woocommerce("COMP-TEST-WOO-NEW", "X", "https://x.example.com", "ck", "cs")


if __name__ == "__main__":
    unittest.main()
