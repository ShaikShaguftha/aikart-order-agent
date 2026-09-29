"""
HubSpot CRM connector - PHASE 1: READ-ONLY CRM provider.

Official HubSpot CRM API (date-versioned "latest" reference), fixed base https://api.hubapi.com:
    GET  /crm/objects/2026-09/contacts/{contactId}
    GET  /crm/objects/2026-09/contacts/{email}?idProperty=email      (exact email lookup)
    POST /crm/objects/2026-09/contacts/search                       (read-only query: phone / name)
    GET  /crm/objects/2026-09/companies/{companyId}
    GET  /crm/objects/2026-09/deals/{dealId}
    GET  /crm/objects/2026-09/tickets/{ticketId}

Authentication: "Authorization: Bearer <private app access token>" (HUBSPOT_ACCESS_TOKEN).
Minimum private-app scopes (read only): crm.objects.contacts.read, crm.objects.companies.read,
crm.objects.deals.read, crm.objects.tickets.read.

The only non-GET request is the contacts search POST, which HubSpot defines as a query (no
record is modified); any other POST is refused before sending. There is no create/update/delete
code path. Responses are normalized into CRMContact / CRMAccount (company) / CRMDeal / CRMTicket.
"""
import logging
import random
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from integrations.base import BaseConnector, log_provider_call
from integrations.models import CRMAccount, CRMContact, CRMDeal, CRMTicket

logger = logging.getLogger("uvicorn.error")

BASE_URL = "https://api.hubapi.com"  # fixed: never taken from config or the LLM
API_VERSION = "2026-09"
OBJECTS_PATH = f"crm/objects/{API_VERSION}"
SEARCH_PATH = f"{OBJECTS_PATH}/contacts/search"

CONTACT_PROPERTIES = "firstname,lastname,email,phone,mobilephone,company,associatedcompanyid,createdate"
COMPANY_PROPERTIES = "name,domain,website,phone"
DEAL_PROPERTIES = "dealname,amount,dealstage,pipeline,closedate"
TICKET_PROPERTIES = "subject,content,hs_pipeline,hs_pipeline_stage,hs_ticket_priority,createdate,hs_lastmodifieddate"

RECORD_ID_PATTERN = re.compile(r"^\d{1,20}$")
EMAIL_PATTERN = re.compile(r"^[^@\s/]+@[^@\s/]+\.[^@\s/]+$")
NAME_PATTERN = re.compile(r"^[^\W\d_](?:[^\W\d_]|[ .'-]){1,59}$", re.UNICODE)
SEARCH_LIMIT = 10
DESCRIPTION_LIMIT = 500


class HubSpotConnector(BaseConnector):
    provider_name = "HUBSPOT"
    MAX_RETRY_AFTER_SECONDS = 5.0
    MAX_BACKOFF_SECONDS = 8.0

    ERROR_MESSAGES = {
        "PROVIDER_AUTH_FAILED": "HubSpot authentication failed. Reconnect required.",
        "PROVIDER_FORBIDDEN": "HubSpot access denied. Required CRM scope/permission may be missing.",
        "NOT_FOUND": "No HubSpot record was found for that ID.",
        "PROVIDER_API_UNAVAILABLE": "The HubSpot API endpoint was not found.",
        "PROVIDER_VALIDATION_FAILED": "HubSpot rejected the request as invalid.",
        "RATE_LIMITED": "HubSpot is rate limiting requests right now. Please try again shortly.",
        "PROVIDER_UNAVAILABLE": "HubSpot is temporarily unavailable.",
        "PROVIDER_TIMEOUT": "HubSpot did not respond in time.",
        "PROVIDER_UNREACHABLE": "HubSpot could not be reached.",
        "PROVIDER_INVALID_RESPONSE": "HubSpot returned an unexpected response.",
        "PROVIDER_ERROR": "HubSpot returned an error.",
        "NOT_SUPPORTED": "This capability is not available for a HubSpot CRM tenant (read-only CRM, no store orders).",
    }

    def __init__(self, access_token: Optional[str], timeout: float = 10.0, retry_backoff: float = 0.5):
        self.access_token = (access_token or "").strip()
        self.timeout = timeout
        self.retry_backoff = retry_backoff

    # ---- configuration & secrets -----------------------------------------------------

    def credentials_configured(self) -> bool:
        return bool(self.access_token)

    def domain_configured(self) -> bool:
        return True  # fixed official base URL

    def configuration_error(self) -> Optional[Dict[str, str]]:
        if not self.access_token:
            return {"error": "HubSpot access token is not configured for this tenant.", "code": "PROVIDER_NOT_CONFIGURED"}
        return None

    def _sanitize_error(self, message: Any) -> str:
        text = str(message or "")
        return text.replace(self.access_token, "[REDACTED_TOKEN]") if self.access_token else text

    def _error(self, code: str, status: Any = None) -> Dict[str, Any]:
        err = {"error": self.ERROR_MESSAGES[code], "code": code}
        if code == "PROVIDER_AUTH_FAILED":
            err["reconnect_required"] = True
        if status is not None:
            err["http_status"] = status
        return err

    def _backoff(self, attempt: int, response: Optional[httpx.Response]) -> float:
        if response is not None and response.headers.get("Retry-After"):
            try:
                return min(float(response.headers["Retry-After"]), self.MAX_RETRY_AFTER_SECONDS)
            except (TypeError, ValueError):
                pass
        base = self.retry_backoff * (2 ** (attempt - 1))
        return min(base + random.uniform(0, self.retry_backoff), self.MAX_BACKOFF_SECONDS)

    # ---- transport ---------------------------------------------------------------------

    def _request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None,
                 json_body: Optional[Dict[str, Any]] = None, max_retries: int = 3) -> Any:
        """
        GET, or POST to the contacts search endpoint only (a read-only query). Both are
        safe to retry on 429 / 5xx / timeouts because neither modifies HubSpot data.
        """
        method = method.upper()
        if not (method == "GET" or (method == "POST" and path == SEARCH_PATH)):
            return {"error": "Only read operations are allowed for HubSpot.", "code": "NOT_SUPPORTED"}
        config_error = self.configuration_error()
        if config_error:
            return config_error
        # Mask whole-number record IDs and email lookups (not the "2026-09" version segment).
        operation = method + " " + re.sub(r"/[^/]+@[^/]+", "/{email}", re.sub(r"/\d+(?=/|$)", "/{id}", path))
        last_error: Dict[str, Any] = self._error("PROVIDER_ERROR")
        for attempt in range(1, max_retries + 1):
            response = None
            try:
                with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
                    response = client.request(
                        method=method, url=f"{BASE_URL}/{path}", params=params, json=json_body,
                        headers={"Authorization": f"Bearer {self.access_token}", "Accept": "application/json",
                                 "Content-Type": "application/json", "User-Agent": "Luintix-HubSpot-Connector/1.0"},
                    )
                status = response.status_code
                log_provider_call(self.provider_name, operation, status)
                if status == 200:
                    try:
                        return response.json()
                    except ValueError:
                        return self._error("PROVIDER_INVALID_RESPONSE", status)
                try:
                    category = (response.json() or {}).get("category")
                except (ValueError, AttributeError):
                    category = None
                if status == 401:
                    return self._error("PROVIDER_AUTH_FAILED", status)
                if status == 403:
                    return self._error("PROVIDER_FORBIDDEN", status)
                if status == 404:
                    # JSON (e.g. category OBJECT_NOT_FOUND) = the record; non-JSON = the route.
                    return self._error("NOT_FOUND" if category else "PROVIDER_API_UNAVAILABLE", status)
                if status == 400:
                    return self._error("PROVIDER_VALIDATION_FAILED", status)
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
                logger.warning(f"[HUBSPOT] request failed: {self._sanitize_error(type(e).__name__)}")
                return self._error("PROVIDER_ERROR")
            if attempt < max_retries:
                time.sleep(self._backoff(attempt, response))
        return last_error

    # ---- normalization -----------------------------------------------------------------

    @staticmethod
    def _props(record: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(record, dict) or not record.get("id") or not isinstance(record.get("properties", {}), dict):
            return None
        return record.get("properties") or {}

    @staticmethod
    def _text(value: Any) -> Optional[str]:
        return str(value) if value not in (None, "") else None

    def _contact(self, record: Dict[str, Any], props: Dict[str, Any]) -> Dict[str, Any]:
        return CRMContact(
            id=str(record["id"]), first_name=self._text(props.get("firstname")), last_name=self._text(props.get("lastname")),
            email=self._text(props.get("email")), phone=self._text(props.get("phone") or props.get("mobilephone")),
            company=self._text(props.get("company")), account_id=self._text(props.get("associatedcompanyid")),
            created_at=self._text(props.get("createdate") or record.get("createdAt")), provider=self.provider_name,
        ).model_dump()

    def _single(self, object_type: str, record_id: str, properties: str, build) -> Dict[str, Any]:
        rid = str(record_id or "").strip()
        if not RECORD_ID_PATTERN.match(rid):
            return {"error": "That is not a valid HubSpot record ID.", "code": "INVALID_RECORD_ID"}
        body = self._request("GET", f"{OBJECTS_PATH}/{object_type}/{rid}", {"properties": properties})
        if isinstance(body, dict) and "error" in body and "code" in body:
            return body
        props = self._props(body)
        if props is None:
            return self._error("PROVIDER_INVALID_RESPONSE")
        if str(body["id"]) != rid:
            return self._error("PROVIDER_INVALID_RESPONSE")
        return build(body, props)

    # ---- READ capabilities ---------------------------------------------------------------

    def get_contact(self, contact_id: str) -> Dict[str, Any]:
        return self._single("contacts", contact_id, CONTACT_PROPERTIES, self._contact)

    def search_contacts(self, email: Optional[str] = None, phone: Optional[str] = None,
                        name: Optional[str] = None) -> Dict[str, Any]:
        """
        email -> exact GET lookup (idProperty=email); phone / name -> contacts search (POST query).
        Returns {"contacts": [...], "count": n, "searched_by": ...}; no match -> empty list.
        """
        email, phone, name = (email or "").strip(), (phone or "").strip(), " ".join((name or "").split())
        if email:
            if not EMAIL_PATTERN.match(email):
                return {"error": "That is not a valid email address.", "code": "INVALID_ARGUMENT"}
            body = self._request("GET", f"{OBJECTS_PATH}/contacts/{quote(email, safe='@')}",
                                 {"idProperty": "email", "properties": CONTACT_PROPERTIES})
            if isinstance(body, dict) and body.get("code") == "NOT_FOUND":
                return {"contacts": [], "count": 0, "searched_by": "email"}
            if isinstance(body, dict) and "error" in body and "code" in body:
                return body
            props = self._props(body)
            if props is None:
                return self._error("PROVIDER_INVALID_RESPONSE")
            return {"contacts": [self._contact(body, props)], "count": 1, "searched_by": "email"}

        if phone:
            if len(re.sub(r"\D", "", phone)) < 7:
                return {"error": "That is not a valid phone number.", "code": "INVALID_ARGUMENT"}
            groups = [{"filters": [{"propertyName": field, "operator": "EQ", "value": phone}]}
                      for field in ("phone", "mobilephone")]  # OR across groups
            searched_by = "phone"
        elif name:
            if not NAME_PATTERN.match(name):
                return {"error": "Please give a customer name (letters only, at least 2 characters).", "code": "INVALID_ARGUMENT"}
            parts = name.split()
            if len(parts) >= 2:
                groups = [{"filters": [{"propertyName": "firstname", "operator": "EQ", "value": parts[0]},
                                       {"propertyName": "lastname", "operator": "EQ", "value": parts[-1]}]}]
            else:
                groups = [{"filters": [{"propertyName": field, "operator": "EQ", "value": parts[0]}]}
                          for field in ("firstname", "lastname")]
            searched_by = "name"
        else:
            return {"error": "Provide an email address, phone number or name to search.", "code": "INVALID_ARGUMENT"}

        body = self._request("POST", SEARCH_PATH, json_body={
            "filterGroups": groups, "properties": CONTACT_PROPERTIES.split(","), "limit": SEARCH_LIMIT})
        if isinstance(body, dict) and "error" in body and "code" in body:
            return body
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list):
            return self._error("PROVIDER_INVALID_RESPONSE")
        contacts = [self._contact(r, self._props(r)) for r in results[:SEARCH_LIMIT] if self._props(r) is not None]
        return {"contacts": contacts, "count": len(contacts), "searched_by": searched_by,
                "more_records": bool((body.get("paging") or {}).get("next"))}

    def get_company(self, company_record_id: str) -> Dict[str, Any]:
        return self._single("companies", company_record_id, COMPANY_PROPERTIES, lambda r, p: CRMAccount(
            id=str(r["id"]), name=self._text(p.get("name")), website=self._text(p.get("website") or p.get("domain")),
            phone=self._text(p.get("phone")), provider=self.provider_name).model_dump())

    def get_deal(self, deal_id: str) -> Dict[str, Any]:
        def build(r, p):
            try:
                amount = float(p["amount"]) if p.get("amount") not in (None, "") else None
            except (TypeError, ValueError):
                amount = None
            return CRMDeal(id=str(r["id"]), name=self._text(p.get("dealname")), amount=amount,
                           stage=self._text(p.get("dealstage")), closing_date=self._text(p.get("closedate")),
                           provider=self.provider_name).model_dump()
        return self._single("deals", deal_id, DEAL_PROPERTIES, build)

    def get_ticket(self, ticket_id: str) -> Dict[str, Any]:
        return self._single("tickets", ticket_id, TICKET_PROPERTIES, lambda r, p: CRMTicket(
            id=str(r["id"]), subject=self._text(p.get("subject")), stage=self._text(p.get("hs_pipeline_stage")),
            pipeline=self._text(p.get("hs_pipeline")), priority=self._text(p.get("hs_ticket_priority")),
            description=(self._text(p.get("content")) or "")[:DESCRIPTION_LIMIT] or None,
            created_at=self._text(p.get("createdate") or r.get("createdAt")),
            updated_at=self._text(p.get("hs_lastmodifieddate") or r.get("updatedAt")),
            provider=self.provider_name).model_dump())

    # ---- BaseConnector commerce capabilities: not applicable to a CRM-only tenant --------

    def _unsupported(self) -> Dict[str, Any]:
        return self._error("NOT_SUPPORTED")

    def get_shop(self, company_id: str) -> Dict[str, Any]:
        return {"name": "HubSpot CRM", "domain": BASE_URL, "provider": self.provider_name}

    def get_orders(self, company_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        return [self._unsupported()]

    def get_customer_details(self, identifier: str, company_id: str) -> List[Dict[str, Any]]:
        return []

    def get_order_details(self, order_id: str, company_id: str) -> Optional[Dict[str, Any]]:
        return self._unsupported()

    def get_shipment_status(self, order_id: str, company_id: str) -> Optional[Dict[str, Any]]:
        return self._unsupported()

    def cancel_order(self, order_id: str, company_id: str, reason: Optional[str] = "CUSTOMER") -> Dict[str, Any]:
        return {**self._unsupported(), "success": False, "status": "FAILED", "order_id": order_id}

    def request_return(self, order_id: str, refund_amount: float, company_id: str) -> Dict[str, Any]:
        return {**self._unsupported(), "status": "BLOCKED"}

    def check_proactive_notifications(self, customer_id: str, company_id: str) -> Optional[str]:
        return None

    def update_order(self, order_id: str, company_id: str, **kwargs) -> Dict[str, Any]:
        return self._unsupported()

    def escalate_to_human(self, order_id: str, reason: str, company_id: str) -> Dict[str, Any]:
        return {**self._unsupported(), "status": "ERROR"}  # never invents a ticket reference

    def get_ticket_details(self, ticket_id: str, company_id: str) -> Dict[str, Any]:
        return self._unsupported()
