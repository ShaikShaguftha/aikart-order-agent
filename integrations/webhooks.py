"""
WooCommerce webhook ingestion.

WooCommerce topics to register per merchant (delivery URL: /api/webhooks/woocommerce):
  order.created   -> ORDER_CREATED
  order.updated   -> ORDER_STATUS_CHANGED when the status differs from the last event,
                     REFUND_RECORDED when the order's refunds total grew (WooCommerce core
                     has no refund topic; refunds arrive as order updates), else ORDER_UPDATED.
                     WooCommerce core has no fulfillment/shipping topic either: "completed"
                     status changes and tracking-plugin updates also arrive as order.updated.
  order.deleted / order.restored
  action.woocommerce_order_refunded (optional custom action topic) -> REFUND_RECORDED

Security:
  - The tenant is resolved from X-WC-Webhook-Source against each merchant's configured
    store URL; unknown or ambiguous sources are rejected.
  - Every delivery must carry a valid X-WC-Webhook-Signature (HMAC-SHA256, base64) made
    with that tenant's webhook secret. Tenants without a secret cannot receive webhooks.
  - Deliveries are deduplicated per tenant by X-WC-Webhook-Delivery-ID.
  - No customer PII (email, phone, address) is stored or logged.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from database import execute_db, query_db
from integrations import credential_store
from integrations.audit import AuditLogger

PROVIDER = "WOOCOMMERCE"
PING_BODY = re.compile(rb"^webhook_id=\d+$")


def _log(msg: str) -> None:
    try:
        stream = sys.stdout or sys.__stdout__
        if stream is not None:
            stream.write(f"{msg}\n")
            stream.flush()
    except Exception:
        pass


def _normalize_source(url: Optional[str]) -> str:
    if not url:
        return ""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return f"{(parsed.hostname or '').lower()}{parsed.path.rstrip('/')}"


def resolve_webhook_tenant(source: Optional[str]) -> Optional[Tuple[str, Optional[str]]]:
    """(company_id, webhook_secret) for the single tenant whose store URL matches source."""
    wanted = _normalize_source(source)
    if not wanted:
        return None
    env_tenant = os.getenv("WOOCOMMERCE_ENV_TENANT", "COMP-WOOCOMMERCE")
    matches = []
    for row in query_db("SELECT * FROM company_integrations WHERE UPPER(provider_type) = 'WOOCOMMERCE'"):
        if row.get("shop_domain"):
            url, secret = row["shop_domain"], credential_store.decrypt(row.get("webhook_secret"))
        elif row["company_id"] == env_tenant:
            url = os.getenv("WOOCOMMERCE_BASE_URL") or os.getenv("WOOCOMMERCE_SITE_URL")
            secret = os.getenv("WOOCOMMERCE_WEBHOOK_SECRET")
        else:
            continue
        if _normalize_source(url) == wanted:
            matches.append((row["company_id"], secret))
    return matches[0] if len(matches) == 1 else None


def verify_signature(secret: str, body: bytes, signature: Optional[str]) -> bool:
    if not secret or not signature:
        return False
    expected = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected, signature)


def _refund_total(payload: Dict[str, Any]) -> float:
    return round(sum(abs(float(r.get("total") or 0)) for r in payload.get("refunds") or []), 2)


def _classify(company_id: str, topic: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    resource, _, event = topic.partition(".")
    if resource == "order":
        number = str(payload.get("number") or payload.get("id") or "")
        status = payload.get("status")
        refund_total = _refund_total(payload)
        previous = query_db(
            "SELECT status, refund_total FROM webhook_events WHERE company_id = ? AND provider = ? "
            "AND order_number = ? ORDER BY id DESC LIMIT 1",
            (company_id, PROVIDER, number),
            one=True,
        )
        if event == "created":
            event_type = "ORDER_CREATED"
        elif event in ("deleted", "restored"):
            event_type = f"ORDER_{event.upper()}"
        elif refund_total > float((previous or {}).get("refund_total") or 0):
            event_type = "REFUND_RECORDED"
        elif previous and previous.get("status") != status:
            event_type = "ORDER_STATUS_CHANGED"
        else:
            event_type = "ORDER_UPDATED"
        return {
            "event_type": event_type, "resource_id": str(payload.get("id") or ""),
            "order_number": number, "status": status, "refund_total": refund_total,
        }
    if resource == "action" and "refund" in event:
        return {"event_type": "REFUND_RECORDED", "resource_id": str(payload.get("arg") or "")}
    return {"event_type": f"{resource.upper()}_{event.upper()}", "resource_id": str(payload.get("id") or "")}


def process_woocommerce_webhook(headers: Dict[str, str], body: bytes) -> Tuple[int, Dict[str, Any]]:
    """Validates, deduplicates and records one WooCommerce webhook delivery."""
    headers = {k.lower(): v for k, v in headers.items()}

    # WooCommerce sends an unsigned "webhook_id=<n>" ping when a webhook is saved.
    if PING_BODY.match(body.strip()):
        return 200, {"status": "ping"}

    tenant = resolve_webhook_tenant(headers.get("x-wc-webhook-source"))
    if not tenant:
        _log("[WOOCOMMERCE WEBHOOK]: rejected (unknown or ambiguous source)")
        return 401, {"detail": "Unknown webhook source"}
    company_id, secret = tenant

    if not verify_signature(secret, body, headers.get("x-wc-webhook-signature")):
        _log(f"[WOOCOMMERCE WEBHOOK]: rejected for tenant {company_id} (missing/invalid signature or no secret configured)")
        return 401, {"detail": "Invalid webhook signature"}

    topic = headers.get("x-wc-webhook-topic") or ""
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        return 400, {"detail": "Invalid JSON payload"}
    if not topic or not isinstance(payload, dict):
        return 400, {"detail": "Missing webhook topic"}

    event = _classify(company_id, topic, payload)
    delivery_id = headers.get("x-wc-webhook-delivery-id")
    try:
        execute_db(
            "INSERT INTO webhook_events (company_id, provider, delivery_id, topic, event_type, resource_id, "
            "order_number, status, refund_total) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (company_id, PROVIDER, delivery_id, topic, event["event_type"], event.get("resource_id"),
             event.get("order_number"), event.get("status"), event.get("refund_total")),
        )
    except sqlite3.IntegrityError:
        return 200, {"status": "duplicate", "delivery_id": delivery_id}

    if event["event_type"] in ("ORDER_STATUS_CHANGED", "REFUND_RECORDED", "ORDER_DELETED"):
        AuditLogger.log_action(
            session_id="webhook", company_id=company_id,
            order_id=event.get("order_number") or event.get("resource_id") or "",
            action_type=f"WEBHOOK_{event['event_type']}", provider=PROVIDER,
            status=str(event.get("status") or "RECORDED"),
            details={"topic": topic, "delivery_id": delivery_id, "refund_total": event.get("refund_total")},
        )
    _log(f"[WOOCOMMERCE WEBHOOK]: tenant={company_id} topic={topic} event={event['event_type']} "
         f"order={event.get('order_number') or '-'} status={event.get('status') or '-'}")
    return 200, {"status": "success", "topic": topic, "event_type": event["event_type"]}
