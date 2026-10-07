"""
Shippo shipping/tracking connector (https://api.goshippo.com/).

Scope (spec "Phase 3: Shippo tracking"): credential verification via carrier accounts,
tracking retrieval/history (GET /tracks/{carrier}/{tracking_number}), tracking
registration (POST /tracks/) and unified tracking normalization. Refunds are Phase 4
and deliberately not implemented.

Authentication: "Authorization: ShippoToken <token>". Tokens are tenant-specific,
stored encrypted (shipping_integrations); this connector never reads the environment.
Errors follow the Luintix provider convention {"error": <safe message>, "code": <CODE>}.
"""
import hashlib
import logging
import random
import re
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from integrations.base import log_provider_call

logger = logging.getLogger("uvicorn.error")

BASE_URL = "https://api.goshippo.com"
TEST_CARRIER = "shippo"
TEST_TRACKING_NUMBERS = (
    "SHIPPO_PRE_TRANSIT", "SHIPPO_TRANSIT", "SHIPPO_DELIVERED",
    "SHIPPO_RETURNED", "SHIPPO_FAILURE", "SHIPPO_UNKNOWN",
)

# Display carrier name (as stores record it) -> exact Shippo carrier slug.
# Only unambiguous mappings; anything else must already be a slug or is rejected.
CARRIER_SLUGS = {
    "usps": "usps",
    "ups": "ups",
    "fedex": "fedex",
    "dhl express": "dhl_express",
    "dhl_express": "dhl_express",
    "shippo": TEST_CARRIER,
}
SLUG_PATTERN = re.compile(r"^[a-z0-9_]{2,40}$")
TRACKING_NUMBER_PATTERN = re.compile(r"^[A-Za-z0-9_-]{4,64}$")

# Unified Luintix tracking statuses (spec 3.8 / 3.9).
STATUS_MAP = {
    "PRE_TRANSIT": "PRE_TRANSIT",
    "TRANSIT": "IN_TRANSIT",
    "DELIVERED": "DELIVERED",
    "FAILURE": "EXCEPTION",
    "RETURNED": "RETURNED",
    "UNKNOWN": "UNKNOWN",
}
TERMINAL_STATUSES = {"DELIVERED", "RETURNED"}

# Per-endpoint, per-environment request budgets (requests/minute) from the spec's Shippo
# table. Other endpoints are not throttled client-side and rely on HTTP 429 handling.
RATE_LIMITS = {
    ("POST", "tracks"): {"live": 750, "test": 50},
    ("GET", "tracks/{carrier}/{tracking_number}"): {"live": 500, "test": 50},
}


def carrier_slug_for(carrier: Optional[str]) -> Optional[str]:
    """Maps a carrier display name or slug to the exact Shippo slug; None if unknown."""
    value = (carrier or "").strip().casefold()
    if not value:
        return None
    if value in CARRIER_SLUGS:
        return CARRIER_SLUGS[value]
    return value if SLUG_PATTERN.match(value) and value in CARRIER_SLUGS.values() else None


def valid_tracking_number(tracking_number: Optional[str]) -> bool:
    return bool(tracking_number) and bool(TRACKING_NUMBER_PATTERN.match(str(tracking_number)))


class _EndpointBudget:
    """In-process token bucket per (account, endpoint, environment)."""

    _buckets: Dict[tuple, List[float]] = {}
    _lock = threading.Lock()

    @classmethod
    def acquire(cls, key: tuple, per_minute: int, max_wait: float) -> bool:
        with cls._lock:
            now = time.monotonic()
            tokens, updated = cls._buckets.get(key, [float(per_minute), now])
            tokens = min(float(per_minute), tokens + (now - updated) * per_minute / 60.0)
            if tokens >= 1:
                cls._buckets[key] = [tokens - 1, now]
                return True
            wait = (1 - tokens) * 60.0 / per_minute
            cls._buckets[key] = [tokens, now]
        if wait > max_wait:
            return False
        time.sleep(wait)
        return cls.acquire(key, per_minute, 0)


class ShippoConnector:
    provider_name = "SHIPPO"
    MAX_RETRY_AFTER_SECONDS = 5.0
    MAX_BACKOFF_SECONDS = 8.0
    MAX_THROTTLE_WAIT_SECONDS = 2.0

    ERROR_MESSAGES = {
        "PROVIDER_AUTH_FAILED": "Shippo rejected the API token. The merchant must reconnect Shippo.",
        "PROVIDER_FORBIDDEN": "Shippo refused access: this account is not entitled to that resource or carrier.",
        "NOT_FOUND": "Shippo could not find that tracking number or resource.",
        "RATE_LIMITED": "Shippo is rate limiting requests right now. Please try again shortly.",
        "PROVIDER_UNAVAILABLE": "Shippo is temporarily unavailable.",
        "PROVIDER_TIMEOUT": "Shippo did not respond in time.",
        "PROVIDER_UNREACHABLE": "Shippo could not be reached.",
        "PROVIDER_INVALID_RESPONSE": "Shippo returned an unexpected response.",
        "PROVIDER_VALIDATION_FAILED": "Shippo rejected the request as invalid.",
        "PROVIDER_ERROR": "Shippo returned an error.",
    }

    def __init__(self, api_token: Optional[str], environment: Optional[str] = None,
                 timeout: float = 10.0, retry_backoff: float = 0.5):
        self.api_token = (api_token or "").strip()
        self.environment = environment or self.environment_for_token(self.api_token)
        self.timeout = timeout
        self.retry_backoff = retry_backoff
        self._account_key = hashlib.sha256(self.api_token.encode()).hexdigest()[:16]

    @staticmethod
    def environment_for_token(token: str) -> str:
        """Shippo tokens are prefixed shippo_test_ / shippo_live_."""
        return "test" if (token or "").startswith("shippo_test_") else "live"

    def credentials_configured(self) -> bool:
        return bool(self.api_token)

    def _sanitize_error(self, message: str) -> str:
        return str(message).replace(self.api_token, "[REDACTED_TOKEN]") if self.api_token else str(message)

    def _error(self, code: str, status: Any = None, **extra) -> Dict[str, Any]:
        err = {"error": self.ERROR_MESSAGES[code], "code": code, **extra}
        if code == "PROVIDER_AUTH_FAILED":
            err["reconnect_required"] = True
        if status is not None:
            err["http_status"] = status
        return err

    def _backoff(self, attempt: int, response: Optional[httpx.Response]) -> float:
        """Retry-After when provided, else exponential backoff with jitter (spec 1.3)."""
        if response is not None and response.headers.get("Retry-After"):
            try:
                return min(float(response.headers["Retry-After"]), self.MAX_RETRY_AFTER_SECONDS)
            except (TypeError, ValueError):
                pass
        base = self.retry_backoff * (2 ** (attempt - 1))
        return min(base + random.uniform(0, self.retry_backoff), self.MAX_BACKOFF_SECONDS)

    # ---- transport -----------------------------------------------------------------

    def _request(self, method: str, path: str, route: str, params=None, json_data=None, max_retries: int = 3) -> Any:
        """
        GET: retried on 429 / 5xx / timeout / network errors (idempotent).
        POST: attempted exactly once. Callers (tracking registration) own idempotency.
        """
        if not self.api_token:
            return {"error": "Shippo is not configured for this tenant.", "code": "PROVIDER_NOT_CONFIGURED"}
        method = method.upper()
        attempts = max_retries if method == "GET" else 1
        limit = RATE_LIMITS.get((method, route), {}).get(self.environment)
        if limit and not _EndpointBudget.acquire((self._account_key, method, route, self.environment), limit,
                                                 self.MAX_THROTTLE_WAIT_SECONDS):
            return self._error("RATE_LIMITED", throttled_locally=True)

        last_error: Dict[str, Any] = self._error("PROVIDER_ERROR")
        for attempt in range(1, attempts + 1):
            response = None
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.request(
                        method=method,
                        url=f"{BASE_URL}/{path}",
                        params=params,
                        json=json_data,
                        headers={
                            "Authorization": f"ShippoToken {self.api_token}",
                            "Content-Type": "application/json",
                            "User-Agent": "Luintix-Shippo-Connector/1.0",
                        },
                    )
                status = response.status_code
                log_provider_call(self.provider_name, f"{method} {route}", status)
                if status in (200, 201):
                    try:
                        return response.json()
                    except ValueError:
                        return self._error("PROVIDER_INVALID_RESPONSE", status)
                if status == 400:
                    try:
                        body = response.json()
                        fields = sorted(k for k in body if isinstance(body, dict) and k not in ("detail", "message"))
                    except (ValueError, AttributeError, TypeError):
                        fields = []
                    return self._error("PROVIDER_VALIDATION_FAILED", status, fields=fields)
                if status == 401:
                    return self._error("PROVIDER_AUTH_FAILED", status)
                if status == 403:
                    return self._error("PROVIDER_FORBIDDEN", status)
                if status == 404:
                    return self._error("NOT_FOUND", status)
                if status == 429:
                    last_error = self._error("RATE_LIMITED", status)
                elif status >= 500:
                    last_error = self._error("PROVIDER_UNAVAILABLE", status)
                else:
                    return self._error("PROVIDER_ERROR", status)
            except httpx.TimeoutException:
                last_error = self._error("PROVIDER_TIMEOUT")
            except httpx.TransportError:
                last_error = self._error("PROVIDER_UNREACHABLE")
            except Exception as e:
                logger.warning(f"[SHIPPO] request failed: {self._sanitize_error(type(e).__name__)}")
                return self._error("PROVIDER_ERROR")
            if attempt < attempts:
                time.sleep(self._backoff(attempt, response))
        return last_error

    # ---- normalization -------------------------------------------------------------

    @staticmethod
    def _location(raw: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(raw, dict):
            return None
        loc = {"city": raw.get("city") or None, "region": raw.get("state") or None, "country": raw.get("country") or None}
        return loc if any(loc.values()) else None

    @classmethod
    def _event(cls, raw: Dict[str, Any]) -> Dict[str, Any]:
        provider_status = str(raw.get("status") or "UNKNOWN").upper()
        substatus = raw.get("substatus")
        if isinstance(substatus, dict):
            code, text, action_required = substatus.get("code"), substatus.get("text"), bool(substatus.get("action_required"))
        else:
            code, text, action_required = (substatus or None), None, False
        status = STATUS_MAP.get(provider_status, "UNKNOWN")
        if provider_status == "TRANSIT" and code == "out_for_delivery":
            status = "OUT_FOR_DELIVERY"
        if action_required and status not in TERMINAL_STATUSES:
            status = "EXCEPTION"
        return {
            "status": status,
            "provider_status": provider_status,
            "substatus": code,
            "substatus_text": text,
            "action_required": action_required,
            "status_details": raw.get("status_details"),
            "status_date": raw.get("status_date"),
            "location": cls._location(raw.get("location")),
            "provider_reference": raw.get("object_id"),
        }

    @classmethod
    def normalize_track(cls, raw: Any) -> Dict[str, Any]:
        """Shippo Track object -> unified Luintix tracking model (spec 3.8)."""
        if not isinstance(raw, dict) or not raw.get("tracking_number") or not raw.get("carrier"):
            return {"error": cls.ERROR_MESSAGES["PROVIDER_INVALID_RESPONSE"], "code": "PROVIDER_INVALID_RESPONSE"}
        current = raw.get("tracking_status")
        if current is not None and not isinstance(current, dict):
            return {"error": cls.ERROR_MESSAGES["PROVIDER_INVALID_RESPONSE"], "code": "PROVIDER_INVALID_RESPONSE"}
        history_raw = raw.get("tracking_history") or []
        if not isinstance(history_raw, list):
            return {"error": cls.ERROR_MESSAGES["PROVIDER_INVALID_RESPONSE"], "code": "PROVIDER_INVALID_RESPONSE"}
        now = cls._event(current) if current else cls._event({"status": "UNKNOWN"})
        service = raw.get("servicelevel") if isinstance(raw.get("servicelevel"), dict) else {}
        return {
            "provider": "shippo",
            "carrier": raw.get("carrier"),
            "carrier_slug": raw.get("carrier"),
            "service_level": service.get("name") or None,
            "tracking_number": raw.get("tracking_number"),
            "status": now["status"],
            "provider_status": now["provider_status"],
            "substatus": now["substatus"],
            "substatus_text": now["substatus_text"],
            "status_details": now["status_details"],
            "action_required": now["action_required"],
            "estimated_delivery": raw.get("eta"),
            "original_estimated_delivery": raw.get("original_eta"),
            "current_location": now["location"],
            "tracking_history": [cls._event(e) for e in history_raw if isinstance(e, dict)],
            "source_updated_at": (current or {}).get("object_updated") or (current or {}).get("status_date"),
            "provider_reference": now["provider_reference"],
            "test_mode": bool(raw.get("test")),
        }

    # ---- READ capabilities ---------------------------------------------------------

    def get_carrier_accounts(self) -> Any:
        """GET /carrier_accounts/ (first page) -> carrier slugs the merchant can use."""
        res = self._request("GET", "carrier_accounts/", "carrier_accounts", params={"results": 100})
        if isinstance(res, dict) and "error" in res:
            return res
        results = res.get("results") if isinstance(res, dict) else None
        if not isinstance(results, list):
            return self._error("PROVIDER_INVALID_RESPONSE")
        return [
            {
                "carrier_slug": a.get("carrier"),
                "carrier_name": a.get("carrier_name") or a.get("carrier"),
                "active": bool(a.get("active")),
                "test": bool(a.get("test")),
                "is_shippo_account": bool(a.get("is_shippo_account")),
            }
            for a in results if isinstance(a, dict)
        ]

    def verify_credentials(self) -> Dict[str, Any]:
        accounts = self.get_carrier_accounts()
        if isinstance(accounts, dict):
            return {"ok": False, **accounts}
        return {
            "ok": True,
            "environment": self.environment,
            "carrier_account_count": len(accounts),
            "active_carrier_slugs": sorted({a["carrier_slug"] for a in accounts if a["active"] and a["carrier_slug"]}),
        }

    def get_tracking(self, carrier_slug: str, tracking_number: str) -> Dict[str, Any]:
        """GET /tracks/{carrier}/{tracking_number}, normalized (status, ETA, location, history)."""
        if not carrier_slug or not SLUG_PATTERN.match(carrier_slug):
            return {"error": "Unknown carrier for tracking.", "code": "INVALID_CARRIER"}
        if not valid_tracking_number(tracking_number):
            return {"error": "That is not a valid tracking number.", "code": "INVALID_TRACKING_NUMBER"}
        res = self._request("GET", f"tracks/{quote(carrier_slug)}/{quote(tracking_number)}",
                            "tracks/{carrier}/{tracking_number}")
        if isinstance(res, dict) and "error" in res:
            return res
        return self.normalize_track(res)

    # ---- WRITE capability ----------------------------------------------------------

    def register_tracking(self, carrier_slug: str, tracking_number: str, metadata: Optional[str] = None) -> Dict[str, Any]:
        """POST /tracks/ once. Must only be called through the gateway's registration record."""
        if not carrier_slug or not SLUG_PATTERN.match(carrier_slug):
            return {"error": "Unknown carrier for tracking.", "code": "INVALID_CARRIER"}
        if not valid_tracking_number(tracking_number):
            return {"error": "That is not a valid tracking number.", "code": "INVALID_TRACKING_NUMBER"}
        body = {"carrier": carrier_slug, "tracking_number": tracking_number}
        if metadata:
            body["metadata"] = metadata[:100]
        res = self._request("POST", "tracks/", "tracks", json_data=body)
        if isinstance(res, dict) and "error" in res:
            return res
        return self.normalize_track(res)
