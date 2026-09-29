"""
Mocked Shippo tests (no real API; see conftest for DB isolation and the network block).
FakeShippo records every request including the Authorization header, so tests can prove
which tenant's token was used and that a registration was POSTed at most once.
"""
import hashlib
import hmac
import io
import json
import os
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import httpx
from cryptography.fernet import Fernet
from langchain_core.messages import AIMessage, ToolMessage

import agent
from database import execute_db, init_db, query_db
from integrations import credential_store
from integrations.errors import TenantConfigurationError
from integrations.gateway import gateway
from integrations.models import CustomerIdentity
from integrations.providers.shippo import ShippoConnector, _EndpointBudget
from integrations.providers.shopify import ShopifyConnector
from integrations.shippo_onboarding import connect_shippo, verify_shippo
from integrations.shippo_webhooks import process_tracking_event, receive_shippo_webhook, replay_failed_events
from tools import get_tracking_status, register_tracking, shippo_tools, tools

TENANT_A, TENANT_B, TENANT_NONE = "COMP-TEST-SHIP-A", "COMP-TEST-SHIP-B", "COMP-TEST-SHIP-NONE"
TOKEN_A, TOKEN_B = "shippo_test_aaaaaaaaaaaaaaaaaaaa", "shippo_test_bbbbbbbbbbbbbbbbbbbb"
SECRET_A = "whsec_shippo_tenant_a"
EMAIL = "buyer@example.com"
ME = CustomerIdentity(email=EMAIL)

STATES = {
    "SHIPPO_PRE_TRANSIT": ("PRE_TRANSIT", "information_received", False),
    "SHIPPO_TRANSIT": ("TRANSIT", "package_departed", False),
    "SHIPPO_DELIVERED": ("DELIVERED", "delivered", False),
    "SHIPPO_FAILURE": ("FAILURE", "package_lost", True),
    "SHIPPO_RETURNED": ("RETURNED", "return_to_sender", False),
    "SHIPPO_UNKNOWN": ("UNKNOWN", "other", False),
}


def track(number, carrier="shippo", status=None, substatus=None, action_required=None, updated="2026-09-27T10:00:00Z", metadata=None):
    s, sub, act = STATES.get(number, ("TRANSIT", "package_departed", False))
    status, substatus = status or s, substatus or sub
    action_required = act if action_required is None else action_required
    event = {"object_id": f"ts_{number}_{updated[-9:-1]}", "object_updated": updated, "status": status,
             "status_details": f"{status} details", "status_date": updated,
             "substatus": {"code": substatus, "text": substatus.replace("_", " "), "action_required": action_required},
             "location": {"city": "San Francisco", "state": "CA", "zip": "94103", "country": "US"}}
    return {"carrier": carrier, "tracking_number": number, "eta": "2026-10-01T18:00:00Z", "original_eta": None,
            "servicelevel": {"token": "shippo_priority", "name": "Priority"}, "metadata": metadata, "test": True,
            "tracking_status": event,
            "tracking_history": [{**event, "status": "PRE_TRANSIT", "object_id": "ts_first", "substatus": None}, event]}


def _resp(status=200, body=None, headers=None, bad_json=False):
    response = MagicMock()
    response.status_code, response.headers = status, headers or {}
    if bad_json:
        response.json.side_effect = ValueError("not json")
    else:
        response.json.return_value = body
    return response


class FakeShippo:
    def __init__(self):
        self.calls, self.overrides = [], {}

    def __call__(self, method=None, url=None, params=None, json=None, headers=None, **_):
        path = url.split("api.goshippo.com/", 1)[1]
        self.calls.append({"method": method, "path": path, "json": json, "auth": (headers or {}).get("Authorization")})
        override = self.overrides.get((method, path))
        if isinstance(override, list):
            override = override.pop(0) if override else None  # scripted responses, then defaults
        if isinstance(override, Exception):
            raise override
        if override is not None:
            return override
        if method == "GET" and path == "carrier_accounts/":
            return _resp(200, {"next": None, "results": [
                {"object_id": "ca_1", "carrier": "shippo", "carrier_name": "Shippo", "account_id": "shippo_test_acct",
                 "active": True, "test": True, "is_shippo_account": True},
                {"object_id": "ca_2", "carrier": "usps", "carrier_name": "USPS", "account_id": "SECRET-ACCOUNT-123",
                 "active": True, "test": True, "is_shippo_account": True}]})
        if method == "GET" and path.startswith("tracks/"):
            _, carrier, number = path.split("/")
            return _resp(200, track(number, carrier))
        if method == "POST" and path == "tracks/":
            return _resp(201, track(json["tracking_number"], json["carrier"], metadata=json.get("metadata")))
        return _resp(404, {"detail": "Not found"})

    def by(self, method, prefix=""):
        return [c for c in self.calls if c["method"] == method and c["path"].startswith(prefix)]


def signed_headers(body: bytes, secret=SECRET_A, ts=None):
    ts = str(int(ts or time.time()))
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return {"Shippo-Auth-Signature": f"t={ts},v1={sig}"}


class ShippoFixture(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.env = patch.dict(os.environ, {credential_store.KEY_ENV: Fernet.generate_key().decode()})
        cls.env.start()
        init_db()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()

    def setUp(self):
        for tenant in (TENANT_A, TENANT_B, TENANT_NONE):
            execute_db("INSERT OR IGNORE INTO companies (id, name, tier) VALUES (?, ?, 'STANDARD')", (tenant, tenant))
        for table in ("shipping_integrations", "shipment_tracking_registrations", "tracking_events", "shipment_tracking_state"):
            execute_db(f"DELETE FROM {table}")
        for tenant, token, secret in ((TENANT_A, TOKEN_A, SECRET_A), (TENANT_B, TOKEN_B, None)):
            execute_db("INSERT INTO shipping_integrations (company_id, provider_type, environment, api_token, webhook_secret) "
                       "VALUES (?, 'SHIPPO', 'test', ?, ?)",
                       (tenant, credential_store.encrypt(token), credential_store.encrypt(secret) if secret else None))
        execute_db("INSERT OR REPLACE INTO company_integrations (company_id, provider_type, shop_domain, access_token) "
                   "VALUES (?, 'SHOPIFY', 'test.myshopify.com', 'shpat_test')", (TENANT_A,))
        _EndpointBudget._buckets.clear()
        self.fake = FakeShippo()
        self.order = {"id": "#1004", "status": "FULFILLED", "financial_status": "PAID", "fulfillment_status": "FULFILLED",
                      "total": 20.0, "customer_email": EMAIL,
                      "shipment": {"id": "gid://shopify/Fulfillment/9", "order_id": "#1004", "courier": "shippo",
                                   "tracking_number": "SHIPPO_TRANSIT", "status": "FULFILLED"}}
        self.order_reads = MagicMock(side_effect=lambda order_id, company_id: dict(self.order) if order_id in ("#1004", "1004") else None)
        for target in (patch("httpx.Client.request", side_effect=self.fake),
                       patch("integrations.providers.shippo.time.sleep"),
                       patch.object(ShopifyConnector, "get_order_details", self.order_reads)):
            target.start()
            self.addCleanup(target.stop)

    def registration(self, number="SHIPPO_TRANSIT", tenant=TENANT_A):
        return query_db("SELECT * FROM shipment_tracking_registrations WHERE company_id = ? AND tracking_number = ?",
                        (tenant, number), one=True)


class CredentialAndCarrierTest(ShippoFixture):

    def test_01_credential_validation(self):
        result = gateway.shipping_verify_credentials(TENANT_A)
        self.assertEqual((result["ok"], result["environment"], result["active_carrier_slugs"]), (True, "test", ["shippo", "usps"]))
        self.assertEqual(self.fake.calls[0]["auth"], f"ShippoToken {TOKEN_A}")
        self.assertEqual(verify_shippo("not-a-shippo-token")["code"], "INVALID_TOKEN_FORMAT")
        self.assertEqual(len(self.fake.calls), 1)

    def test_02_carrier_accounts_retrieval(self):
        accounts = gateway.shipping_get_carrier_accounts(TENANT_A)
        self.assertEqual([a["carrier_slug"] for a in accounts], ["shippo", "usps"])
        self.assertNotIn("SECRET-ACCOUNT-123", json.dumps(accounts))
        self.assertNotIn("ca_1", json.dumps(accounts))


class TrackingTest(ShippoFixture):

    def test_03_tracking_status(self):
        result = gateway.shipping_get_tracking(TENANT_A, "shippo", "SHIPPO_TRANSIT")
        t = result["tracking"]
        self.assertEqual((t["status"], t["carrier_slug"], t["tracking_number"], t["estimated_delivery"]),
                         ("IN_TRANSIT", "shippo", "SHIPPO_TRANSIT", "2026-10-01T18:00:00Z"))
        self.assertNotIn("tracking_history", t)
        self.assertEqual(t["tracking_history_count"], 2)
        self.assertEqual(self.fake.by("GET")[0]["path"], "tracks/shippo/SHIPPO_TRANSIT")

    def test_04_tracking_history(self):
        history = gateway.shipping_get_tracking(TENANT_A, "shippo", "SHIPPO_TRANSIT", include_history=True)["tracking"]["tracking_history"]
        self.assertEqual([h["status"] for h in history], ["PRE_TRANSIT", "IN_TRANSIT"])

    def test_05_normalization(self):
        n = ShippoConnector.normalize_track(track("X1234", status="TRANSIT", substatus="out_for_delivery"))
        self.assertEqual(n["status"], "OUT_FOR_DELIVERY")
        self.assertEqual(n["current_location"], {"city": "San Francisco", "region": "CA", "country": "US"})
        self.assertNotIn("94103", json.dumps(n))
        n = ShippoConnector.normalize_track(track("X1234", status="TRANSIT", substatus="address_issue", action_required=True))
        self.assertEqual((n["status"], n["action_required"]), ("EXCEPTION", True))
        raw = track("X1234")
        raw["tracking_status"]["substatus"] = "delayed"  # plain-string substatus form
        self.assertEqual(ShippoConnector.normalize_track(raw)["substatus"], "delayed")
        self.assertEqual((n["carrier"], n["service_level"]), ("shippo", "Priority"))  # service level is not the carrier
        for key in ("carrier", "carrier_slug", "tracking_number", "status", "substatus", "estimated_delivery",
                    "current_location", "tracking_history", "action_required", "provider_reference"):
            self.assertIn(key, n)

    def test_06_all_simulated_states(self):
        expected = {"SHIPPO_TRANSIT": "IN_TRANSIT", "SHIPPO_DELIVERED": "DELIVERED", "SHIPPO_FAILURE": "EXCEPTION",
                    "SHIPPO_RETURNED": "RETURNED", "SHIPPO_PRE_TRANSIT": "PRE_TRANSIT", "SHIPPO_UNKNOWN": "UNKNOWN"}
        for number, status in expected.items():
            with self.subTest(number=number):
                self.assertEqual(gateway.shipping_get_tracking(TENANT_A, "shippo", number)["tracking"]["status"], status)


class RegistrationTest(ShippoFixture):

    def test_07_registration(self):
        result = gateway.shipping_register_tracking(TENANT_A, "shippo", "SHIPPO_TRANSIT", order_reference="#1004", session_id="s1")
        self.assertEqual((result["status"], result["registered_now"]), ("REGISTERED", True))
        post = self.fake.by("POST", "tracks/")[0]
        self.assertEqual(post["json"], {"carrier": "shippo", "tracking_number": "SHIPPO_TRANSIT", "metadata": f"luintix:{TENANT_A}"})
        self.assertEqual(self.registration()["status"], "REGISTERED")
        audit = query_db("SELECT status FROM action_audit_logs WHERE company_id = ? AND action_type = 'SHIPPO_REGISTER_TRACKING'", (TENANT_A,))
        self.assertEqual(audit[-1]["status"], "REGISTERED")

    def test_08_duplicate_registration_prevented(self):
        gateway.shipping_register_tracking(TENANT_A, "shippo", "SHIPPO_TRANSIT")
        again = gateway.shipping_register_tracking(TENANT_A, "Shippo", "SHIPPO_TRANSIT")
        self.assertEqual((again["status"], again["registered_now"]), ("ALREADY_REGISTERED", False))
        self.assertEqual(len(self.fake.by("POST", "tracks/")), 1)

    def test_08b_in_flight_and_unconfirmed_are_never_resent(self):
        execute_db("INSERT INTO shipment_tracking_registrations (company_id, provider, environment, carrier_slug, tracking_number, status) "
                   "VALUES (?, 'SHIPPO', 'test', 'shippo', 'SHIPPO_DELIVERED', 'PENDING')", (TENANT_A,))
        self.assertEqual(gateway.shipping_register_tracking(TENANT_A, "shippo", "SHIPPO_DELIVERED")["status"], "PENDING")
        execute_db("UPDATE shipment_tracking_registrations SET updated_at = datetime('now', '-10 minutes')")
        self.assertEqual(gateway.shipping_register_tracking(TENANT_A, "shippo", "SHIPPO_DELIVERED")["status"], "UNCONFIRMED")
        self.assertEqual(gateway.shipping_register_tracking(TENANT_A, "shippo", "SHIPPO_DELIVERED")["status"], "UNCONFIRMED")
        self.assertEqual(self.fake.by("POST"), [])

    def test_08c_rejected_request_may_be_retried_once_claimed(self):
        self.fake.overrides[("POST", "tracks/")] = [_resp(429, {}, headers={"Retry-After": "1"})]
        first = gateway.shipping_register_tracking(TENANT_A, "shippo", "SHIPPO_TRANSIT")
        self.assertEqual((first["status"], first["code"]), ("FAILED_RETRYABLE", "RATE_LIMITED"))
        second = gateway.shipping_register_tracking(TENANT_A, "shippo", "SHIPPO_TRANSIT")
        self.assertEqual(second["status"], "REGISTERED")
        self.assertEqual(len(self.fake.by("POST")), 2)
        self.assertEqual(self.registration()["attempts"], 2)

    def test_08d_live_registration_requires_merchant_approval(self):
        execute_db("UPDATE shipping_integrations SET environment = 'live' WHERE company_id = ?", (TENANT_A,))
        self.assertEqual(gateway.shipping_register_tracking(TENANT_A, "shippo", "SHIPPO_TRANSIT")["code"], "MERCHANT_APPROVAL_REQUIRED")
        self.assertEqual(self.fake.by("POST"), [])

    def test_08e_invented_or_invalid_identifiers_rejected_without_calls(self):
        self.assertEqual(gateway.shipping_register_tracking(TENANT_A, "DHL", "SHIPPO_TRANSIT")["code"], "INVALID_CARRIER")
        self.assertEqual(gateway.shipping_register_tracking(TENANT_A, "shippo", "bad number!")["code"], "INVALID_TRACKING_NUMBER")
        self.assertEqual(self.fake.calls, [])


class ErrorHandlingTest(ShippoFixture):

    def _get(self, response):
        self.fake.overrides[("GET", "tracks/shippo/SHIPPO_TRANSIT")] = response
        return gateway.shipping_get_tracking(TENANT_A, "shippo", "SHIPPO_TRANSIT")

    def test_09_401_marks_credential_invalid_and_requires_reconnect(self):
        result = self._get(_resp(401, {"detail": "Invalid token"}))
        self.assertEqual((result["code"], result["reconnect_required"]), ("PROVIDER_AUTH_FAILED", True))
        self.assertEqual(query_db("SELECT credential_status FROM shipping_integrations WHERE company_id = ?", (TENANT_A,), one=True)["credential_status"], "INVALID")
        with self.assertRaises(TenantConfigurationError):
            gateway.get_shipping_connector(TENANT_A)
        self.assertEqual(json.loads(get_tracking_status.invoke({"carrier": "shippo", "tracking_number": "SHIPPO_TRANSIT", "company_id": TENANT_A}))["code"],
                         "TENANT_CONFIGURATION_ERROR")

    def test_10_403(self):
        self.assertEqual(self._get(_resp(403, {}))["code"], "PROVIDER_FORBIDDEN")

    def test_11_404(self):
        self.assertEqual(self._get(_resp(404, {}))["code"], "NOT_FOUND")

    def test_12_429_retry_after_then_success(self):
        with patch("integrations.providers.shippo.time.sleep") as sleep:
            result = self._get([_resp(429, {}, headers={"Retry-After": "2"}), _resp(200, track("SHIPPO_TRANSIT"))])
        self.assertEqual(result["tracking"]["status"], "IN_TRANSIT")
        sleep.assert_called_once_with(2.0)

    def test_12b_persistent_429_is_bounded(self):
        with patch("integrations.providers.shippo.time.sleep") as sleep:
            result = self._get([_resp(429, {}, headers={"Retry-After": "600"})] * 3)
        self.assertEqual(result["code"], "RATE_LIMITED")
        self.assertTrue(all(c.args[0] <= ShippoConnector.MAX_RETRY_AFTER_SECONDS for c in sleep.call_args_list))

    def test_12c_per_endpoint_budget_throttles_locally(self):
        connector = ShippoConnector(TOKEN_A)
        for _ in range(50):
            self.assertTrue(_EndpointBudget.acquire((connector._account_key, "GET", "tracks/{carrier}/{tracking_number}", "test"), 50, 0))
        result = connector.get_tracking("shippo", "SHIPPO_TRANSIT")
        self.assertEqual((result["code"], result.get("throttled_locally")), ("RATE_LIMITED", True))
        self.assertEqual(self.fake.calls, [])
        self.assertIn("carrier_slug", ShippoConnector(TOKEN_A).get_carrier_accounts()[0])  # other endpoint unaffected

    def test_13_5xx_get_retried_post_never_retried(self):
        result = self._get([_resp(503, {}), _resp(200, track("SHIPPO_TRANSIT"))])
        self.assertEqual(result["tracking"]["status"], "IN_TRANSIT")
        self.fake.overrides[("POST", "tracks/")] = _resp(502, {"trace": "internal"})
        reg = gateway.shipping_register_tracking(TENANT_A, "shippo", "SHIPPO_DELIVERED")
        self.assertEqual((reg["status"], reg["code"]), ("UNCONFIRMED", "PROVIDER_UNAVAILABLE"))
        self.assertEqual(len(self.fake.by("POST")), 1)
        self.assertNotIn("trace", json.dumps(reg))
        self.assertEqual(gateway.shipping_register_tracking(TENANT_A, "shippo", "SHIPPO_DELIVERED")["status"], "UNCONFIRMED")
        self.assertEqual(len(self.fake.by("POST")), 1)

    def test_14_timeout(self):
        self.assertEqual(self._get([httpx.ReadTimeout("slow")] * 3)["code"], "PROVIDER_TIMEOUT")
        self.assertEqual(len(self.fake.by("GET")), 3)
        self.fake.overrides[("POST", "tracks/")] = httpx.ReadTimeout("slow")
        self.assertEqual(gateway.shipping_register_tracking(TENANT_A, "shippo", "SHIPPO_RETURNED")["status"], "UNCONFIRMED")
        self.assertEqual(len(self.fake.by("POST")), 1)

    def test_15_malformed_responses(self):
        self.assertEqual(self._get(_resp(200, bad_json=True))["code"], "PROVIDER_INVALID_RESPONSE")
        self.assertEqual(self._get(_resp(200, {"carrier": "shippo", "tracking_number": "SHIPPO_TRANSIT", "tracking_status": "oops"}))["code"],
                         "PROVIDER_INVALID_RESPONSE")
        self.assertEqual(self._get(_resp(200, ["not", "an", "object"]))["code"], "PROVIDER_INVALID_RESPONSE")
        self.fake.overrides[("GET", "carrier_accounts/")] = _resp(200, {"results": "nope"})
        self.assertFalse(gateway.shipping_verify_credentials(TENANT_A)["ok"])

    def test_token_never_logged_or_returned(self):
        self.fake.overrides[("GET", "tracks/shippo/SHIPPO_TRANSIT")] = RuntimeError(f"boom {TOKEN_A}")
        buffer = io.StringIO()
        with patch("sys.__stdout__", buffer), redirect_stdout(buffer), self.assertLogs("uvicorn.error", level="WARNING") as logs:
            result = gateway.shipping_get_tracking(TENANT_A, "shippo", "SHIPPO_TRANSIT")
        self.assertNotIn(TOKEN_A, buffer.getvalue() + json.dumps(result) + "".join(logs.output))


class IsolationAndCredentialTest(ShippoFixture):

    def test_16_tenant_isolation(self):
        gateway.shipping_get_tracking(TENANT_A, "shippo", "SHIPPO_TRANSIT")
        gateway.shipping_get_tracking(TENANT_B, "shippo", "SHIPPO_TRANSIT")
        self.assertEqual([c["auth"] for c in self.fake.calls], [f"ShippoToken {TOKEN_A}", f"ShippoToken {TOKEN_B}"])
        with patch.dict(os.environ, {"SHIPPO_API_TOKEN": TOKEN_A, "SHIPPO_TOKEN": TOKEN_A}):
            for tenant in (TENANT_NONE, "COMP-DOES-NOT-EXIST", ""):
                with self.assertRaises(TenantConfigurationError):
                    gateway.get_shipping_connector(tenant)
        self.assertFalse(gateway.has_shipping(TENANT_NONE))

    def test_16b_registrations_are_per_tenant(self):
        gateway.shipping_register_tracking(TENANT_A, "shippo", "SHIPPO_TRANSIT")
        b = gateway.shipping_register_tracking(TENANT_B, "shippo", "SHIPPO_TRANSIT")
        self.assertEqual(b["status"], "REGISTERED")
        self.assertEqual([c["auth"] for c in self.fake.by("POST")], [f"ShippoToken {TOKEN_A}", f"ShippoToken {TOKEN_B}"])

    def test_17_credential_encryption_and_rotation(self):
        row = query_db("SELECT api_token FROM shipping_integrations WHERE company_id = ?", (TENANT_A,), one=True)
        self.assertTrue(row["api_token"].startswith("enc:v1:"))
        self.assertNotIn(TOKEN_A, json.dumps(row))
        self.fake.overrides[("GET", "carrier_accounts/")] = _resp(401, {})
        self.assertFalse(connect_shippo(TENANT_NONE, "shippo_test_badbadbadbad")["connected"])
        self.assertIsNone(query_db("SELECT 1 FROM shipping_integrations WHERE company_id = ?", (TENANT_NONE,), one=True))
        del self.fake.overrides[("GET", "carrier_accounts/")]
        execute_db("UPDATE shipping_integrations SET credential_status = 'INVALID' WHERE company_id = ?", (TENANT_A,))
        rotated = connect_shippo(TENANT_A, "shippo_test_rotatedtoken0000")
        self.assertTrue(rotated["connected"] and rotated["rotated"])
        self.assertNotIn("rotatedtoken", json.dumps(rotated))
        self.assertEqual(gateway.get_shipping_connector(TENANT_A).api_token, "shippo_test_rotatedtoken0000")
        self.assertTrue(rotated["webhook_secret_configured"])  # existing webhook secret kept on rotation
        with patch.dict(os.environ, {credential_store.KEY_ENV: ""}):
            with self.assertRaises(TenantConfigurationError):
                connect_shippo(TENANT_NONE, TOKEN_A)


class WebhookTest(ShippoFixture):

    def body(self, number="SHIPPO_TRANSIT", event="track_updated", **kw):
        return json.dumps({"event": event, "test": True, "data": track(number, **kw)}).encode()

    def deliver(self, body, headers=None, tenant=TENANT_A, url_token=None):
        return receive_shippo_webhook(tenant, signed_headers(body) if headers is None else headers, body, url_token=url_token)

    def test_18_hmac_validation(self):
        body = self.body()
        status, result, event_id = self.deliver(body)
        self.assertEqual((status, result["status"]), (200, "accepted"))
        self.assertEqual(process_tracking_event(event_id)["tracking_status"], "IN_TRANSIT")
        state = query_db("SELECT status, alert FROM shipment_tracking_state WHERE company_id = ?", (TENANT_A,), one=True)
        self.assertEqual(state["status"], "IN_TRANSIT")
        for bad_headers in ({}, signed_headers(body, secret="wrong"), signed_headers(body, ts=time.time() - 3600),
                            {"Shippo-Auth-Signature": "garbage"}):
            self.assertEqual(self.deliver(body, headers=bad_headers)[0], 401)
        self.assertEqual(receive_shippo_webhook(TENANT_A, signed_headers(body), body + b" ")[0], 401)  # tampered
        self.assertEqual(self.deliver(body, tenant=TENANT_NONE)[0], 401)
        self.assertEqual(self.deliver(body, headers=signed_headers(body), tenant=TENANT_B)[0], 401)  # no secret

    def test_18b_url_token_mode(self):
        execute_db("UPDATE shipping_integrations SET webhook_auth_mode = 'url_token' WHERE company_id = ?", (TENANT_A,))
        body = self.body()
        self.assertEqual(self.deliver(body, headers={}, url_token=SECRET_A)[0], 200)
        self.assertEqual(self.deliver(self.body("SHIPPO_DELIVERED"), headers={}, url_token="wrong")[0], 401)

    def test_18c_event_registered_by_another_tenant_is_refused(self):
        body = self.body(metadata=f"luintix:{TENANT_B}")
        self.assertEqual(self.deliver(body)[0], 403)

    def test_19_duplicates_order_alerts_and_dead_letter(self):
        body = self.body("SHIPPO_DELIVERED", updated="2026-09-27T12:00:00Z")
        first = self.deliver(body)
        self.assertEqual(self.deliver(body)[1]["status"], "duplicate")
        process_tracking_event(first[2])
        older = self.deliver(self.body("SHIPPO_DELIVERED", status="TRANSIT", updated="2026-09-27T08:00:00Z"))
        self.assertFalse(process_tracking_event(older[2])["applied"])
        state = query_db("SELECT status, alert FROM shipment_tracking_state WHERE company_id = ?", (TENANT_A,), one=True)
        self.assertEqual((state["status"], state["alert"]), ("DELIVERED", "DELIVERED"))
        self.assertTrue(query_db("SELECT 1 FROM action_audit_logs WHERE company_id = ? AND action_type = 'SHIPMENT_ALERT'", (TENANT_A,)))
        self.assertEqual(self.deliver(self.body(event="transaction_created"))[1]["status"], "ignored")
        broken = self.deliver(self.body("SHIPPO_FAILURE", updated="2026-09-27T13:00:00Z"))
        execute_db("UPDATE tracking_events SET raw_payload = '{\"data\": {\"carrier\": \"shippo\"}}' WHERE id = ?", (broken[2],))
        self.assertEqual(process_tracking_event(broken[2])["status"], "failed")
        self.assertEqual(query_db("SELECT processing_status FROM tracking_events WHERE id = ?", (broken[2],), one=True)["processing_status"], "FAILED")
        execute_db("UPDATE tracking_events SET raw_payload = ? WHERE id = ?", (self.body("SHIPPO_FAILURE", updated="2026-09-27T13:00:00Z").decode(), broken[2]))
        self.assertEqual(replay_failed_events(TENANT_A), {"replayed": 1, "processed": 1})


class ShopifyShippoFlowTest(ShippoFixture):

    def test_20_shopify_order_to_shippo_tracking(self):
        result = gateway.shipping_track_order(ME, "#1004", TENANT_A)
        self.assertTrue(result["tracking_available"])
        ids = result["identifiers"]
        self.assertEqual((ids["order_number"], ids["tracking_number"], ids["carrier_slug"], ids["shipment_id"]),
                         ("#1004", "SHIPPO_TRANSIT", "shippo", "gid://shopify/Fulfillment/9"))
        self.assertEqual(ids["provider_reference"], result["tracking"]["provider_reference"])
        self.assertEqual(result["tracking"]["status"], "IN_TRANSIT")
        self.assertEqual(self.fake.by("GET")[0]["path"], "tracks/shippo/SHIPPO_TRANSIT")
        self.order_reads.assert_called_once_with("#1004", TENANT_A)

    def test_20b_display_carrier_name_is_mapped_to_slug(self):
        self.order["shipment"] = {**self.order["shipment"], "courier": "UPS", "tracking_number": "1Z999AA10123456784"}
        gateway.shipping_track_order(ME, "#1004", TENANT_A)
        self.assertEqual(self.fake.by("GET")[0]["path"], "tracks/ups/1Z999AA10123456784")

    def test_21_no_tracking_number_or_unsupported_carrier(self):
        self.order["shipment"] = None
        result = gateway.shipping_track_order(ME, "#1004", TENANT_A)
        self.assertEqual(result["tracking_available"], False)
        self.assertIn("not available", result["reason"])
        self.assertIsNone(result["identifiers"]["tracking_number"])
        self.order["shipment"] = {"id": "f1", "order_id": "#1004", "courier": "Royal Mail", "tracking_number": "RM123456GB", "status": "FULFILLED"}
        self.assertFalse(gateway.shipping_track_order(ME, "#1004", TENANT_A)["tracking_available"])
        self.assertEqual(self.fake.calls, [])

    def test_22_other_customer_or_tenant_cannot_see_shipment(self):
        self.order["customer_email"] = "someone.else@example.com"
        self.assertEqual(gateway.shipping_track_order(ME, "#1004", TENANT_A)["code"], "ORDER_OWNERSHIP_NOT_VERIFIED")
        self.assertEqual(gateway.shipping_track_order(CustomerIdentity(name="Buyer"), "#1004", TENANT_A)["code"], "IDENTITY_REQUIRED")
        self.assertEqual(gateway.shipping_track_order(ME, "#1004", TENANT_B)["code"], "STORE_NOT_CONFIGURED")
        self.assertEqual(self.fake.calls, [])
        # A's webhook-derived shipment state is never returned for tenant B.
        body = json.dumps({"event": "track_updated", "data": track("SHIPPO_DELIVERED")}).encode()
        process_tracking_event(receive_shippo_webhook(TENANT_A, signed_headers(body), body)[2])
        self.assertIsNotNone(gateway.shipping_get_tracking(TENANT_A, "shippo", "SHIPPO_DELIVERED")["tracking"]["latest_webhook_update"])
        self.assertIsNone(gateway.shipping_get_tracking(TENANT_B, "shippo", "SHIPPO_DELIVERED")["tracking"]["latest_webhook_update"])

    def test_register_by_order_uses_store_tracking_number(self):
        result = json.loads(register_tracking.invoke({"order_id": "#1004", "email": EMAIL, "company_id": TENANT_A}))
        self.assertEqual((result["status"], result["identifiers"]["tracking_number"]), ("REGISTERED", "SHIPPO_TRANSIT"))


class AgentShippoTest(ShippoFixture):

    def run_agent(self, script, text, tenant=TENANT_A, attr="llm_with_shipping_tools"):
        scripted = MagicMock()
        scripted.invoke.side_effect = script
        with patch.object(agent, attr, scripted):
            answer = agent.process_query(text, company_id=tenant, session_identity={"email": EMAIL})
        return answer, scripted

    def test_tools_bound_only_for_shipping_tenants(self):
        self.assertEqual({t.name for t in tools} & {t.name for t in shippo_tools}, set())
        bound = {t["function"]["name"] for t in agent.llm_with_shipping_tools.kwargs["tools"]}
        self.assertTrue({"get_tracking_status", "get_tracking_history", "register_tracking"} <= bound)
        _, scripted = self.run_agent([AIMessage(content="Hi")], "hi", tenant=TENANT_NONE, attr="llm_with_tools")
        self.assertEqual(scripted.invoke.call_args[0][0][0].content, agent.SYSTEM_PROMPT)

    def test_order_tracking_uses_session_identity(self):
        script = [AIMessage(content="", tool_calls=[{"name": "get_tracking_status", "args": {"order_id": "#1004"}, "id": "c1"}]),
                  AIMessage(content="Your order #1004 is in transit (tracking number SHIPPO_TRANSIT).")]
        answer, scripted = self.run_agent(script, "Where is my order #1004?")
        messages = scripted.invoke.call_args_list[-1][0][0]
        result = json.loads(next(m for m in messages if isinstance(m, ToolMessage)).content)
        self.assertEqual((result["tracking_available"], result["tracking"]["status"]), (True, "IN_TRANSIT"))
        self.assertIn("in transit", answer)

    def test_invented_tracking_number_blocked_without_provider_call(self):
        script = [AIMessage(content="", tool_calls=[{"name": "get_tracking_status",
                                                      "args": {"carrier": "ups", "tracking_number": "1Z000INVENTED01"}, "id": "c1"}]),
                  AIMessage(content="Could you share your tracking number?")]
        _, scripted = self.run_agent(script, "where is my package?")
        messages = scripted.invoke.call_args_list[-1][0][0]
        self.assertEqual(json.loads(next(m for m in messages if isinstance(m, ToolMessage)).content)["code"], "UNVERIFIED_TRACKING_NUMBER")
        self.assertEqual(self.fake.calls, [])

    def test_unsupported_registration_claim_is_corrected(self):
        script = [AIMessage(content="I've registered your shipment for automatic updates."),
                  AIMessage(content="Would you like me to set up automatic delivery updates for order #1004?")]
        answer, _ = self.run_agent(script, "keep me posted on #1004")
        self.assertTrue(answer.startswith("Would you like me"))



class Order1005RegressionTest(ShippoFixture):
    """Regression for the live chat on order #1005: carrier "Other", mock tracking number,
    no customer email, store tool chosen instead of Shippo, and an invented "in transit"."""

    def setUp(self):
        super().setUp()
        self.order.update(id="#1005", customer_email=None, shipment={
            "id": "gid://shopify/Fulfillment/7259276574947", "order_id": "#1005", "courier": "Other",
            "tracking_number": "SHIPPO_TRANSIT", "status": "SUCCESS", "tracking_url": None})
        self.order_reads.side_effect = lambda order_id, company_id: dict(self.order) if order_id in ("#1005", "1005") else None

    def test_mock_number_with_other_carrier_uses_test_carrier_in_test_mode(self):
        result = gateway.shipping_track_order(CustomerIdentity(), "#1005", TENANT_A)
        self.assertTrue(result["tracking_available"])
        self.assertEqual((result["identifiers"]["carrier_slug"], result["identifiers"]["carrier_slug_source"]),
                         ("shippo", "shippo_test_tracking_number"))
        self.assertEqual(self.fake.by("GET")[0]["path"], "tracks/shippo/SHIPPO_TRANSIT")

    def test_no_customer_email_is_allowed_but_flagged_unverified(self):
        result = gateway.shipping_track_order(CustomerIdentity(), "#1005", TENANT_A)
        self.assertFalse(result["ownership_verified"])
        self.order["customer_email"] = EMAIL
        self.assertEqual(gateway.shipping_track_order(CustomerIdentity(), "#1005", TENANT_A)["code"], "IDENTITY_REQUIRED")
        self.assertTrue(gateway.shipping_track_order(ME, "#1005", TENANT_A)["ownership_verified"])

    def test_other_carrier_is_never_guessed_for_real_numbers_or_live_tokens(self):
        self.order["shipment"] = {**self.order["shipment"], "tracking_number": "1Z999AA10123456784"}
        self.assertFalse(gateway.shipping_track_order(CustomerIdentity(), "#1005", TENANT_A)["tracking_available"])
        self.order["shipment"] = {**self.order["shipment"], "tracking_number": "SHIPPO_TRANSIT"}
        execute_db("UPDATE shipping_integrations SET environment = 'live' WHERE company_id = ?", (TENANT_A,))
        self.assertFalse(gateway.shipping_track_order(CustomerIdentity(), "#1005", TENANT_A)["tracking_available"])
        self.assertEqual(self.fake.calls, [])

    def test_store_shipment_tool_is_replaced_and_redirected_for_shippo_tenants(self):
        bound = {t["function"]["name"] for t in agent.llm_with_shipping_tools.kwargs["tools"]}
        self.assertNotIn("get_shipment_status", bound)
        both = {t["function"]["name"] for t in agent.llm_with_helpdesk_shipping_tools.kwargs["tools"]}
        self.assertNotIn("get_shipment_status", both)
        self.assertIn("get_shipment_status", {t["function"]["name"] for t in agent.llm_with_tools.kwargs["tools"]})
        script = [AIMessage(content="", tool_calls=[{"name": "get_shipment_status", "args": {"order_id": "#1005"}, "id": "c1"}]),
                  AIMessage(content="Your order #1005 is in transit.")]
        scripted = MagicMock()
        scripted.invoke.side_effect = script
        with patch.object(agent, "llm_with_shipping_tools", scripted):
            answer = agent.process_query("Where is my order #1005?", company_id=TENANT_A, session_identity={})
        messages = scripted.invoke.call_args_list[-1][0][0]
        result = json.loads(next(m for m in messages if isinstance(m, ToolMessage)).content)
        self.assertEqual((result["tracking_available"], result["tracking"]["status"]), (True, "IN_TRANSIT"))
        self.assertEqual(answer, "Your order #1005 is in transit.")  # backed by a real tracking status

    def test_in_transit_claim_from_fulfillment_status_is_rejected(self):
        # Tenant without Shippo: only the store fulfillment record ("status": "SUCCESS") is available.
        store_result = json.dumps(self.order["shipment"])
        script = [AIMessage(content="", tool_calls=[{"name": "get_shipment_status", "args": {"order_id": "#1005"}, "id": "c1"}]),
                  AIMessage(content="Your order #1005 has been shipped and is currently in transit."),
                  AIMessage(content="Your order #1005 has shipped (tracking number SHIPPO_TRANSIT). Live carrier status is not available.")]
        scripted = MagicMock()
        scripted.invoke.side_effect = script
        with patch.object(agent, "llm_with_tools", scripted), patch("tools.gateway.get_shipment_status", return_value=self.order["shipment"]):
            answer = agent.process_query("Where is my order #1005?", company_id=TENANT_NONE, session_identity={})
        self.assertIn("not available", answer)
        self.assertNotIn("in transit", answer)
        self.assertIn('"status": "SUCCESS"', store_result)


if __name__ == "__main__":
    unittest.main()
