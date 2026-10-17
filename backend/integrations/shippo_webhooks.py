"""
Shippo track_updated webhook ingestion (per tenant: POST /api/webhooks/shippo/{company_id}).

Receive (synchronous, fast):
  1. Tenant must have an ACTIVE Shippo integration with a webhook secret.
  2. Authenticate BEFORE parsing JSON:
       hmac      (default, spec): header Shippo-Auth-Signature "t=<unix>,v1=<hex>",
                 v1 = HMAC-SHA256(secret, f"{t}.{raw_body}"), timestamp within tolerance.
       url_token (Shippo's documented alternative while HMAC is being enabled by Shippo):
                 ?token=<secret> on the webhook URL, compared in constant time.
  3. Only event "track_updated" is accepted; the raw payload is stored with a dedupe key.
  4. 2xx is returned immediately; processing happens in the background.

Process (background): normalize -> update shipment_tracking_state (ignoring out-of-order
events) -> alert on EXCEPTION/RETURNED/DELIVERED -> mark PROCESSED, or FAILED with the
error (dead letter) so replay_failed_events() can reprocess it.
"""
import hashlib
import hmac
import json
import sqlite3
import sys
import time
from typing import Any, Dict, Optional, Tuple

from backend.database import execute_db, query_db
from backend.integrations import credential_store
from backend.integrations.audit import AuditLogger
from backend.integrations.providers.shippo import ShippoConnector

PROVIDER = "SHIPPO"
SIGNATURE_TOLERANCE_SECONDS = 300
ALERT_STATUSES = {"EXCEPTION", "RETURNED", "DELIVERED"}


def _log(msg: str) -> None:
    sys.__stdout__.write(f"{msg}\n")
    sys.__stdout__.flush()


def _mask(tracking_number: Optional[str]) -> str:
    value = str(tracking_number or "")
    return value[:4] + "***" + value[-2:] if len(value) > 6 else "***"


def verify_hmac_signature(secret: str, raw_body: bytes, header: Optional[str], now: Optional[float] = None) -> bool:
    if not secret or not header:
        return False
    parts = dict(p.split("=", 1) for p in header.replace(" ", "").split(",") if "=" in p)
    timestamp, signature = parts.get("t"), parts.get("v1")
    if not timestamp or not signature or not timestamp.isdigit():
        return False
    if abs((now or time.time()) - int(timestamp)) > SIGNATURE_TOLERANCE_SECONDS:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _event_key(company_id: str, track: Dict[str, Any]) -> str:
    status = track.get("tracking_status") if isinstance(track.get("tracking_status"), dict) else {}
    substatus = status.get("substatus")
    substatus = substatus.get("code") if isinstance(substatus, dict) else substatus
    material = "|".join(str(x or "") for x in (
        company_id, track.get("carrier"), track.get("tracking_number"),
        status.get("object_id"), status.get("status"), substatus, status.get("status_date"),
    ))
    return hashlib.sha256(material.encode()).hexdigest()


def receive_shippo_webhook(
    company_id: str, headers: Dict[str, str], raw_body: bytes, url_token: Optional[str] = None
) -> Tuple[int, Dict[str, Any], Optional[int]]:
    """Returns (http_status, response_body, event_id_to_process_or_None)."""
    headers = {k.lower(): v for k, v in headers.items()}
    row = query_db("SELECT * FROM shipping_integrations WHERE company_id = ?", (company_id,), one=True)
    # Same response for unknown tenant / missing secret / bad signature: no tenant enumeration.
    unauthorized = (401, {"detail": "Webhook authentication failed"}, None)
    if not row or (row.get("provider_type") or "").upper() != PROVIDER or row.get("credential_status") != "ACTIVE":
        return unauthorized
    secret = credential_store.decrypt(row.get("webhook_secret"))
    if not secret:
        return unauthorized
    if (row.get("webhook_auth_mode") or "hmac") == "url_token":
        authenticated = bool(url_token) and hmac.compare_digest(secret, url_token)
    else:
        authenticated = verify_hmac_signature(secret, raw_body, headers.get("shippo-auth-signature"))
    if not authenticated:
        _log(f"[SHIPPO WEBHOOK]: rejected for tenant {company_id} (authentication failed)")
        return unauthorized

    try:
        payload = json.loads(raw_body or b"{}")
    except ValueError:
        return 400, {"detail": "Invalid JSON payload"}, None
    if not isinstance(payload, dict) or payload.get("event") != "track_updated":
        return 200, {"status": "ignored", "reason": "unsupported event"}, None
    track = payload.get("data") if isinstance(payload.get("data"), dict) else None
    if not track or not track.get("tracking_number") or not track.get("carrier"):
        return 400, {"detail": "track_updated payload has no tracking data"}, None
    metadata = str(track.get("metadata") or "")
    if metadata.startswith("luintix:") and metadata != f"luintix:{company_id}":
        _log(f"[SHIPPO WEBHOOK]: rejected for tenant {company_id} (event registered by another tenant)")
        return 403, {"detail": "Event does not belong to this tenant"}, None

    try:
        event_id = execute_db(
            "INSERT INTO tracking_events (company_id, provider, event_key, carrier_slug, tracking_number, raw_payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (company_id, PROVIDER, _event_key(company_id, track), track.get("carrier"), track.get("tracking_number"),
             raw_body.decode("utf-8", errors="replace")),
        )
    except sqlite3.IntegrityError:
        return 200, {"status": "duplicate"}, None
    _log(f"[SHIPPO WEBHOOK]: tenant={company_id} stored event {event_id} carrier={track.get('carrier')} "
         f"tracking={_mask(track.get('tracking_number'))}")
    return 200, {"status": "accepted", "event_id": event_id}, event_id


def process_tracking_event(event_id: int) -> Dict[str, Any]:
    """Background step: normalize and update the shipment timeline. Never raises."""
    event = query_db("SELECT * FROM tracking_events WHERE id = ?", (event_id,), one=True)
    if not event or event["processing_status"] == "PROCESSED":
        return {"status": "skipped"}
    try:
        payload = json.loads(event["raw_payload"])
        tracking = ShippoConnector.normalize_track(payload.get("data"))
        if "error" in tracking:
            raise ValueError(tracking["code"])
        key = (event["company_id"], PROVIDER, tracking["carrier_slug"], tracking["tracking_number"])
        current = query_db(
            "SELECT source_updated_at FROM shipment_tracking_state WHERE company_id = ? AND provider = ? "
            "AND carrier_slug = ? AND tracking_number = ?", key, one=True,
        )
        newer = not current or not current["source_updated_at"] or (tracking["source_updated_at"] or "") >= current["source_updated_at"]
        alert = tracking["status"] if tracking["status"] in ALERT_STATUSES else None
        if newer:
            execute_db(
                "INSERT INTO shipment_tracking_state (company_id, provider, carrier_slug, tracking_number, status, substatus, "
                "action_required, estimated_delivery, current_location, source_updated_at, last_event_id, alert, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(company_id, provider, carrier_slug, tracking_number) DO UPDATE SET status = excluded.status, "
                "substatus = excluded.substatus, action_required = excluded.action_required, "
                "estimated_delivery = excluded.estimated_delivery, current_location = excluded.current_location, "
                "source_updated_at = excluded.source_updated_at, last_event_id = excluded.last_event_id, "
                "alert = excluded.alert, updated_at = CURRENT_TIMESTAMP",
                (*key, tracking["status"], tracking["substatus"], int(tracking["action_required"]),
                 tracking["estimated_delivery"], json.dumps(tracking["current_location"]),
                 tracking["source_updated_at"], event_id, alert),
            )
            # A delivered webhook proves the tracking number is registered for this tenant.
            execute_db(
                "UPDATE shipment_tracking_registrations SET status = 'REGISTERED', updated_at = CURRENT_TIMESTAMP "
                "WHERE company_id = ? AND provider = ? AND carrier_slug = ? AND tracking_number = ? AND status = 'UNCONFIRMED'",
                key,
            )
            if alert:
                AuditLogger.log_action(
                    session_id="webhook", company_id=event["company_id"], order_id="", action_type="SHIPMENT_ALERT",
                    provider=PROVIDER, status=alert,
                    details={"carrier_slug": tracking["carrier_slug"], "substatus": tracking["substatus"], "event_id": event_id},
                )
        execute_db("UPDATE tracking_events SET processing_status = ?, processing_error = NULL, processed_at = CURRENT_TIMESTAMP "
                   "WHERE id = ?", ("PROCESSED" if newer else "PROCESSED_STALE", event_id))
        return {"status": "processed", "applied": newer, "tracking_status": tracking["status"]}
    except Exception as e:
        execute_db("UPDATE tracking_events SET processing_status = 'FAILED', processing_error = ? WHERE id = ?",
                   (type(e).__name__ + (f": {e}" if isinstance(e, ValueError) else ""), event_id))
        _log(f"[SHIPPO WEBHOOK]: event {event_id} moved to dead letter ({type(e).__name__})")
        return {"status": "failed"}


def replay_failed_events(company_id: str) -> Dict[str, Any]:
    """Reprocess dead-lettered events for one tenant."""
    failed = query_db("SELECT id FROM tracking_events WHERE company_id = ? AND provider = ? AND processing_status = 'FAILED' ORDER BY id",
                      (company_id, PROVIDER))
    results = [process_tracking_event(r["id"]) for r in failed]
    return {"replayed": len(results), "processed": sum(r["status"] == "processed" for r in results)}
