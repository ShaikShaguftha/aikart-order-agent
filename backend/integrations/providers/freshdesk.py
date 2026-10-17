"""
Freshdesk helpdesk connector (REST API v2, https://{subdomain}.freshdesk.com/api/v2/).

Authentication is HTTP Basic with the API key as username and "X" as password.
Credentials are always tenant-specific (stored encrypted in helpdesk_integrations);
this connector never reads credentials from the environment.

Errors follow the Luintix provider convention: {"error": <safe message>, "code": <CODE>}.
Raw Freshdesk response bodies are never returned.
"""
import base64
import logging
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from backend.integrations.base import log_provider_call

logger = logging.getLogger("uvicorn.error")

STATUS_CODES = {"open": 2, "pending": 3, "resolved": 4, "closed": 5}
PRIORITY_CODES = {"low": 1, "medium": 2, "high": 3, "urgent": 4}
STATUS_NAMES = {v: k.upper() for k, v in STATUS_CODES.items()}
PRIORITY_NAMES = {v: k.upper() for k, v in PRIORITY_CODES.items()}
SOURCE_CHAT = 7
# GET /tickets only returns tickets created in the last 30 days unless updated_since is set.
ALL_TIME = "2000-01-01T00:00:00Z"
# Custom ticket field labels recognised as "the store order reference" (merchant-created).
ORDER_FIELD_LABELS = ("order id", "order number", "order no", "order #", "order reference", "shopify order")
ORDER_FIELD_CACHE_SECONDS = 600


class FreshdeskConnector:
    """Tenant-scoped Freshdesk API v2 client with Luintix-normalized results."""

    provider_name = "FRESHDESK"
    MAX_RETRY_AFTER_SECONDS = 5.0
    # {subdomain: (order_field_name_or_None, expires_at)}
    _order_field_cache: Dict[str, Any] = {}
    SUBDOMAIN_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

    ERROR_MESSAGES = {
        "PROVIDER_AUTH_FAILED": "Freshdesk authentication failed: the helpdesk API key is invalid or revoked.",
        "PROVIDER_FORBIDDEN": "Freshdesk refused access: the API key's agent lacks permission for this action.",
        "NOT_FOUND": "Freshdesk resource not found.",
        "RATE_LIMITED": "Freshdesk is rate limiting requests right now. Please try again shortly.",
        "PROVIDER_UNAVAILABLE": "Freshdesk is temporarily unavailable.",
        "PROVIDER_TIMEOUT": "Freshdesk did not respond in time.",
        "PROVIDER_UNREACHABLE": "Freshdesk could not be reached.",
        "PROVIDER_INVALID_RESPONSE": "Freshdesk returned an unexpected response.",
        "PROVIDER_VALIDATION_FAILED": "Freshdesk rejected the request as invalid.",
        "PROVIDER_ERROR": "Freshdesk returned an error.",
    }

    def __init__(self, domain: Optional[str], api_key: Optional[str], timeout: float = 10.0, retry_backoff: float = 0.5):
        self.subdomain = self.normalize_subdomain(domain)
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        self.retry_backoff = retry_backoff
        self.base_api_url = f"https://{self.subdomain}.freshdesk.com/api/v2" if self.subdomain else ""
        self._client: Optional[httpx.Client] = None

    # ---- configuration -------------------------------------------------------------

    @classmethod
    def normalize_subdomain(cls, domain: Optional[str]) -> Optional[str]:
        """
        Accepts "acme", "acme.freshdesk.com" or "https://acme.freshdesk.com/...".
        Only *.freshdesk.com hosts are allowed, so the API key can never be sent elsewhere.
        """
        value = (domain or "").strip().lower()
        if not value:
            return None
        host = urlparse(value if "://" in value else f"https://{value}").hostname or ""
        if host.endswith(".freshdesk.com"):
            host = host[: -len(".freshdesk.com")]
        elif "." in host:
            return None
        return host if cls.SUBDOMAIN_PATTERN.match(host) else None

    def credentials_configured(self) -> bool:
        return bool(self.api_key)

    def _get_client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                timeout=self.timeout,
                headers={"Content-Type": "application/json", "User-Agent": "Luintix-Freshdesk-Connector/1.0"},
                auth=(self.api_key, "X"),
            )
        return self._client

    def close(self) -> None:
        """Closes the underlying httpx.Client connection pool if open."""
        if self._client is not None and not self._client.is_closed:
            self._client.close()
        self._client = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        
    def domain_configured(self) -> bool:
        return bool(self.subdomain)

    def _sanitize_error(self, message: str) -> str:
        clean = str(message)
        if self.api_key:
            clean = clean.replace(self.api_key, "[REDACTED_API_KEY]")
            clean = clean.replace(base64.b64encode(f"{self.api_key}:X".encode()).decode(), "[REDACTED_AUTH]")
        return clean

    def _error(self, code: str, status: Any = None, **extra) -> Dict[str, Any]:
        err = {"error": self.ERROR_MESSAGES[code], "code": code, **extra}
        if status is not None:
            err["http_status"] = status
        return err

    def _retry_delay(self, attempt: int, response: Optional[httpx.Response] = None) -> float:
        delay = self.retry_backoff * attempt
        if response is not None:
            try:
                delay = max(delay, float(response.headers.get("Retry-After", 0)))
            except (TypeError, ValueError):
                pass
        return min(delay, self.MAX_RETRY_AFTER_SECONDS)

    # ---- transport -----------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        max_retries: int = 3,
    ) -> Any:
        """
        Retry policy:
          - 429: retried for any method (Freshdesk did not process the request),
                 honouring Retry-After up to MAX_RETRY_AFTER_SECONDS.
          - 5xx / timeout / network: retried only for idempotent GET and PUT.
                 POST (create ticket, add note) is attempted once so it can never duplicate.
        """
        if not self.subdomain:
            return {"error": "Freshdesk domain is missing or invalid for this tenant.", "code": "PROVIDER_NOT_CONFIGURED"}
        if not self.api_key:
            return {"error": "Freshdesk API key is not configured for this tenant.", "code": "PROVIDER_NOT_CONFIGURED"}

        method = method.upper()
        idempotent = method in ("GET", "PUT")
        url = f"{self.base_api_url}/{path.lstrip('/')}"
        operation = method + " " + re.sub(r"/\d+", "/{id}", path.lstrip("/"))

        last_error: Dict[str, Any] = self._error("PROVIDER_ERROR")
        for attempt in range(1, max_retries + 1):
            response = None
            retryable = False
            try:
                client = self._get_client()
                response = client.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_data,
                )
                status = response.status_code
                log_provider_call(self.provider_name, operation, status)

                if status in (200, 201):
                    try:
                        return response.json()
                    except ValueError:
                        return self._error("PROVIDER_INVALID_RESPONSE", status)
                if status == 204:
                    return {}
                if status == 400:
                    try:
                        fields = sorted({e.get("field") for e in (response.json() or {}).get("errors", []) if e.get("field")})
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
                    last_error, retryable = self._error("RATE_LIMITED", status), True
                elif status >= 500:
                    last_error, retryable = self._error("PROVIDER_UNAVAILABLE", status), idempotent
                else:
                    return self._error("PROVIDER_ERROR", status)
            except httpx.TimeoutException:
                last_error, retryable = self._error("PROVIDER_TIMEOUT"), idempotent
            except httpx.NetworkError:
                last_error, retryable = self._error("PROVIDER_UNREACHABLE"), idempotent
            except Exception as e:
                logger.warning(f"[FRESHDESK] request failed: {self._sanitize_error(type(e).__name__)}")
                return self._error("PROVIDER_ERROR")

            if not retryable or attempt == max_retries:
                return last_error
            time.sleep(self._retry_delay(attempt, response))
        return last_error

    # ---- normalization -------------------------------------------------------------
    @staticmethod
    def _truncate_text(text: Optional[str], max_chars: int = 2000) -> Optional[str]:
        if not text:
            return text
        clean_text = text.strip()
        if len(clean_text) > max_chars:
            return clean_text[:max_chars] + "\n...[TRUNCATED FOR TOKEN LIMITS]"
        return clean_text
    
    @staticmethod
    def _valid_ticket_id(ticket_id: Any) -> Optional[int]:
        text = str(ticket_id).strip().lstrip("#")
        return int(text) if text.isdigit() and int(text) > 0 else None

    @staticmethod
    def _contact(data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "contact_id": str(data.get("id")),
            "name": data.get("name"),
            "email": data.get("email"),
            "phone": data.get("phone"),
            "mobile": data.get("mobile"),
            "provider": "FRESHDESK",
        }

    @staticmethod
    def _ticket(data: Dict[str, Any]) -> Dict[str, Any]:
        status, priority = data.get("status"), data.get("priority")
        return {
            "ticket_id": str(data.get("id")),
            "subject": data.get("subject"),
            "status": STATUS_NAMES.get(status, f"STATUS_{status}"),
            "priority": PRIORITY_NAMES.get(priority, f"PRIORITY_{priority}"),
            "type": data.get("type"),
            "requester_id": str(data.get("requester_id")) if data.get("requester_id") is not None else None,
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
            "due_by": data.get("due_by"),
            "tags": data.get("tags") or [],
            # Raw values; the gateway maps the order field and never returns the rest.
            "custom_fields": data.get("custom_fields") or {},
            "provider": "FRESHDESK",
        }

    @classmethod
    def _conversation(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        # Addresses (from_email/to_emails/cc) are deliberately dropped.
        return {
            "conversation_id": str(data.get("id")),
            "body_text": cls._truncate_text(data.get("body_text")),
            "private": bool(data.get("private")),
            "from_customer": bool(data.get("incoming")),
            "created_at": data.get("created_at"),
        }

    # ---- READ capabilities ---------------------------------------------------------

    def verify_credentials(self) -> Dict[str, Any]:
        """Credential check via GET /api/v2/ticket_fields."""
        res = self._request("GET", "ticket_fields", max_retries=1)
        if isinstance(res, dict) and "error" in res:
            return {"ok": False, **res}
        return {"ok": True, "ticket_field_count": len(res) if isinstance(res, list) else 0}

    def get_ticket_fields(self) -> Any:
        res = self._request("GET", "ticket_fields")
        if isinstance(res, dict) and "error" in res:
            return res
        return [
            {
                "name": f.get("name"),
                "label": f.get("label_for_customers") or f.get("label"),
                "type": f.get("type"),
                "required_for_customers": bool(f.get("required_for_customers")),
                "choices": f.get("choices") if isinstance(f.get("choices"), (list, dict)) else None,
            }
            for f in (res if isinstance(res, list) else [])
        ]

    def get_order_field(self) -> Optional[str]:
        """
        Name of the merchant's custom text field holding the store order reference
        (e.g. cf_order_id labelled "Order ID"), or None if the merchant has not created one.
        Luintix never creates ticket fields itself.
        """
        cached = self._order_field_cache.get(self.subdomain)
        if cached and cached[1] > time.time():
            return cached[0]
        res = self._request("GET", "ticket_fields")
        if isinstance(res, dict):
            return None  # do not cache failures
        candidates = [
            f for f in res
            if str(f.get("name", "")).startswith("cf_")
            and f.get("type") == "custom_text"
            and str(f.get("label") or "").strip().casefold() in ORDER_FIELD_LABELS
        ]
        candidates.sort(key=lambda f: ORDER_FIELD_LABELS.index(str(f.get("label")).strip().casefold()))
        name = candidates[0]["name"] if candidates else None
        self._order_field_cache[self.subdomain] = (name, time.time() + ORDER_FIELD_CACHE_SECONDS)
        return name

    def get_contact_by_id(self, contact_id: Any) -> Dict[str, Any]:
        cid = self._valid_ticket_id(contact_id)
        if cid is None:
            return {"error": "Contact ID must be a positive number.", "code": "INVALID_ARGUMENT"}
        res = self._request("GET", f"contacts/{cid}")
        return res if "error" in res else {"contact": self._contact(res)}

    def get_contact(self, email: Optional[str] = None, phone: Optional[str] = None) -> Dict[str, Any]:
        """Exact-match contact lookup by email, else phone/mobile. Returns {"contact": ...|None}."""
        if email:
            res = self._request("GET", "contacts", params={"email": email})
            if isinstance(res, dict) and "error" in res:
                return res
            for c in res if isinstance(res, list) else []:
                if (c.get("email") or "").casefold() == email.casefold():
                    return {"contact": self._contact(c)}
            return {"contact": None}
        if phone:
            wanted = re.sub(r"\D", "", phone)
            for field in ("phone", "mobile"):
                res = self._request("GET", "contacts", params={field: phone})
                if isinstance(res, dict) and "error" in res:
                    return res
                for c in res if isinstance(res, list) else []:
                    if wanted and re.sub(r"\D", "", c.get(field) or "") == wanted:
                        return {"contact": self._contact(c)}
            return {"contact": None}
        return {"error": "An email address or phone number is required to find a contact.", "code": "IDENTITY_REQUIRED"}

    def get_tickets_by_customer(self, requester_id: str, limit: int = 10) -> Any:
        res = self._request(
            "GET",
            "tickets",
            params={
                "requester_id": requester_id,
                "order_by": "created_at",
                "order_type": "desc",
                "per_page": max(1, min(int(limit), 100)),
                "updated_since": ALL_TIME,
            },
        )
        if isinstance(res, dict) and "error" in res:
            return res
        return [self._ticket(t) for t in (res if isinstance(res, list) else [])]

    def get_ticket_details(self, ticket_id: Any) -> Dict[str, Any]:
        tid = self._valid_ticket_id(ticket_id)
        if tid is None:
            return {"error": "Ticket ID must be a positive number.", "code": "INVALID_TICKET_ID"}
        res = self._request("GET", f"tickets/{tid}")
        if "error" in res:
            return res
        return {**self._ticket(res), "description_text":self._truncate_text(res.get("description_text")),
               }
    def get_ticket_conversations(self, ticket_id: Any, max_messages: int = 10) -> Dict[str, Any]:
        """Ticket plus its conversations via GET /tickets/{id}?include=conversations."""
        tid = self._valid_ticket_id(ticket_id)

        if tid is None:
            return {
                "error": "Ticket ID must be a positive number.",
                "code": "INVALID_TICKET_ID",
            }

        res = self._request(
            "GET",
            f"tickets/{tid}",
            params={"include": "conversations"},
        )

        if "error" in res:
            return res

        conversations = res.get("conversations") or []

        if not isinstance(conversations, list):
            return self._error("PROVIDER_INVALID_RESPONSE")

        try:
            message_limit = max(1, min(int(max_messages), 10))
        except (TypeError, ValueError):
            message_limit = 10

        recent_conversations = conversations[-message_limit:]

        return {
            **self._ticket(res),
            "description_text": self._truncate_text(
                res.get("description_text")
            ),
            "conversations": [
                self._conversation(conversation)
                for conversation in recent_conversations
                if isinstance(conversation, dict)
            ],
        }

    # ---- WRITE capabilities --------------------------------------------------------

    def create_ticket(
        self,
        subject: str,
        description: str,
        priority: str,
        requester: Dict[str, Any],
        tags: Optional[List[str]] = None,
        custom_fields: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """requester: {"requester_id": ...} or {"email": ...} or {"phone": ..., "name": ...}."""
        if priority not in PRIORITY_CODES:
            return {"error": f"Invalid priority '{priority}'.", "code": "INVALID_ARGUMENT"}
        payload = {
            "subject": subject,
            "description": description,
            "status": STATUS_CODES["open"],
            "priority": PRIORITY_CODES[priority],
            "source": SOURCE_CHAT,
            "tags": tags or ["luintix"],
        }
        if custom_fields:
            payload["custom_fields"] = custom_fields
        if requester.get("requester_id"):
            payload["requester_id"] = int(requester["requester_id"])
        elif requester.get("email"):
            payload["email"] = requester["email"]
        elif requester.get("phone") and requester.get("name"):
            payload.update(phone=requester["phone"], name=requester["name"])
        else:
            return {"error": "A requester email, phone+name or contact is required.", "code": "IDENTITY_REQUIRED"}
        res = self._request("POST", "tickets", json_data=payload)
        return res if "error" in res else self._ticket(res)

    def update_ticket(self, ticket_id: Any, status: Optional[str] = None, priority: Optional[str] = None) -> Dict[str, Any]:
        tid = self._valid_ticket_id(ticket_id)
        if tid is None:
            return {"error": "Ticket ID must be a positive number.", "code": "INVALID_TICKET_ID"}
        payload = {}
        if status is not None:
            if status not in STATUS_CODES:
                return {"error": f"Invalid status '{status}'.", "code": "INVALID_ARGUMENT"}
            payload["status"] = STATUS_CODES[status]
        if priority is not None:
            if priority not in PRIORITY_CODES:
                return {"error": f"Invalid priority '{priority}'.", "code": "INVALID_ARGUMENT"}
            payload["priority"] = PRIORITY_CODES[priority]
        if not payload:
            return {"error": "Nothing to update.", "code": "INVALID_ARGUMENT"}
        res = self._request("PUT", f"tickets/{tid}", json_data=payload)
        return res if "error" in res else self._ticket(res)

    def set_custom_field(self, ticket_id: Any, field: str, value: Any) -> Dict[str, Any]:
        """Sets one custom field (e.g. linking an existing ticket to its order)."""
        tid = self._valid_ticket_id(ticket_id)
        if tid is None:
            return {"error": "Ticket ID must be a positive number.", "code": "INVALID_TICKET_ID"}
        if not str(field).startswith("cf_"):
            return {"error": "Only custom fields can be set.", "code": "INVALID_ARGUMENT"}
        res = self._request("PUT", f"tickets/{tid}", json_data={"custom_fields": {field: value}})
        return res if "error" in res else self._ticket(res)

    def update_ticket_status(self, ticket_id: Any, status: str) -> Dict[str, Any]:
        return self.update_ticket(ticket_id, status=status)

    def update_ticket_priority(self, ticket_id: Any, priority: str) -> Dict[str, Any]:
        return self.update_ticket(ticket_id, priority=priority)

    def add_internal_note(self, ticket_id: Any, body: str) -> Dict[str, Any]:
        """Private (agent-only) note via POST /api/v2/tickets/{id}/notes."""
        tid = self._valid_ticket_id(ticket_id)
        if tid is None:
            return {"error": "Ticket ID must be a positive number.", "code": "INVALID_TICKET_ID"}
        if not (body or "").strip():
            return {"error": "Note body is empty.", "code": "INVALID_ARGUMENT"}
        res = self._request("POST", f"tickets/{tid}/notes", json_data={"body": body, "private": True})
        if "error" in res:
            return res
        return {"note_id": str(res.get("id")), "ticket_id": str(tid), "private": bool(res.get("private", True)), "created_at": res.get("created_at")}
