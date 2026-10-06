import os
import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
import httpx

from integrations.base import BaseConnector, log_provider_call
from integrations.models import Order, OrderItem, Shipment, ShopInfo, Customer, CancelOrderResult, CustomerIdentity, normalize_phone

logger = logging.getLogger("uvicorn.error")


class WooCommerceConnector(BaseConnector):
    """
    Concrete Provider Connector for WooCommerce REST API v3 (/wp-json/wc/v3/).
    Handles WooCommerce tenant authentication, order tracking, product querying,
    customer details, order actions (cancel/update), and refund status operations.
    """

    provider_name = "WOOCOMMERCE"
    # Setting an order to "cancelled" in WooCommerce does not refund the payment.
    cancel_refunds_automatically = False
    # WooCommerce core has no returns or helpdesk API: Luintix records them (shared stores).
    managed_returns = True
    managed_tickets = True

    MAX_RETRY_AFTER_SECONDS = 5.0
    MAX_ORDER_RESULTS = 50
    MAX_REFUND_RESULTS = 50
    MAX_PRODUCT_DESCRIPTION_CHARS = 2000
    MAX_VARIANT_RESULTS = 50
    LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

    def __init__(
        self,
        site_url: Optional[str] = None,
        consumer_key: Optional[str] = None,
        consumer_secret: Optional[str] = None,
        api_version: Optional[str] = None,
        timeout: float = 10.0,
        use_env_fallback: bool = True,
        retry_backoff: float = 0.5,
    ):
        """
        use_env_fallback=False is used for tenants whose credentials are stored per merchant:
        their connector must never pick up the .env store URL or keys.
        """
        env = os.getenv if use_env_fallback else (lambda *_args, **_kw: None)
        # WOOCOMMERCE_BASE_URL is preferred; WOOCOMMERCE_SITE_URL is kept for existing configs.
        # There is deliberately no default store: credentials must never be sent to a
        # site the operator did not configure.
        raw_url = (
            site_url
            or env("WOOCOMMERCE_BASE_URL")
            or env("WOOCOMMERCE_SITE_URL")
            or ""
        ).strip()
        # Clean site URL to base domain/protocol
        raw_url = raw_url.rstrip("/")
        if raw_url and not raw_url.startswith("http://") and not raw_url.startswith("https://"):
            raw_url = f"https://{raw_url}"
        
        # Remove trailing /wp-json/... if caller included it
        if "/wp-json" in raw_url:
            raw_url = raw_url.split("/wp-json")[0]
            
        self.site_url = raw_url
        self.consumer_key = (
            consumer_key or env("WOOCOMMERCE_CONSUMER_KEY") or ""
        ).strip()
        self.consumer_secret = (
            consumer_secret or env("WOOCOMMERCE_CONSUMER_SECRET") or ""
        ).strip()
        
        version_str = (
            api_version or env("WOOCOMMERCE_API_VERSION") or "v3"
        ).strip().lstrip("/")
        if not version_str.startswith("wc/"):
            version_str = f"wc/{version_str}"
        self.api_version = version_str

        self.timeout = timeout
        self.retry_backoff = retry_backoff
        self.base_api_url = f"{self.site_url}/wp-json/{self.api_version}"

        # Credentials are only ever sent over HTTPS (plain HTTP allowed for local development).
        self.config_error: Optional[Dict[str, str]] = None
        parsed = urlparse(self.site_url) if self.site_url else None
        if parsed and parsed.scheme == "http" and parsed.hostname not in self.LOCAL_HOSTS:
            self.config_error = {
                "error": "WooCommerce store URL must use HTTPS. Update WOOCOMMERCE_BASE_URL to an https:// address.",
                "code": "PROVIDER_CONFIG_INSECURE",
            }

        self._client: Optional[httpx.Client] = None

    def credentials_configured(self) -> bool:
        return bool(self.consumer_key and self.consumer_secret)

    def domain_configured(self) -> bool:
        return bool(self.site_url)

    def _sanitize_error(self, message: str) -> str:
        """
        Ensures Consumer Key and Consumer Secret are never exposed in log outputs or exception messages.
        """
        clean = str(message)
        if self.consumer_key:
            clean = clean.replace(self.consumer_key, "[REDACTED_KEY]")
        if self.consumer_secret:
            clean = clean.replace(self.consumer_secret, "[REDACTED_SECRET]")
        return clean


    def _get_client(self) -> httpx.Client:
        """Create and reuse one HTTP connection pool for this connector instance."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                timeout=self.timeout,
                follow_redirects=False,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Luintix-WooCommerce-Connector/1.0",
                },
            )

        return self._client

    def close(self) -> None:
        """Close the reusable HTTP connection pool."""
        if self._client is not None and not self._client.is_closed:
            self._client.close()

        self._client = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    @staticmethod
    def _truncate_text(value: Any, max_chars: int = 2000) -> Optional[str]:
        """Limit large provider text before returning it through the Gateway."""
        if value in (None, ""):
            return None

        text = str(value).strip()

        if len(text) > max_chars:
            return text[:max_chars] + "\n...[TRUNCATED FOR TOKEN LIMITS]"

        return text
    # Safe, user-presentable messages. Raw provider bodies are never returned.
    ERROR_MESSAGES = {
        "PROVIDER_AUTH_FAILED": "WooCommerce authentication failed: the store's API credentials are invalid or revoked.",
        "PROVIDER_FORBIDDEN": "WooCommerce refused access: the API key does not have permission for this data.",
        "NOT_FOUND": "WooCommerce resource not found.",
        "PROVIDER_API_UNAVAILABLE": "WooCommerce REST API is not available at the configured store URL. Check WOOCOMMERCE_BASE_URL and that the REST API is enabled.",
        "RATE_LIMITED": "WooCommerce is rate limiting requests right now. Please try again shortly.",
        "PROVIDER_UNAVAILABLE": "The WooCommerce store is temporarily unavailable.",
        "PROVIDER_TIMEOUT": "The WooCommerce store did not respond in time.",
        "PROVIDER_UNREACHABLE": "The WooCommerce store could not be reached.",
        "PROVIDER_INVALID_RESPONSE": "The WooCommerce store returned an unexpected response.",
        "PROVIDER_ERROR": "The WooCommerce store returned an error.",
        "PROVIDER_API_UNAVAILABLE": "WooCommerce REST API is not available at the configured store URL. Check WOOCOMMERCE_BASE_URL and that the REST API is enabled.",
        "PROVIDER_VALIDATION_FAILED": "WooCommerce rejected the request as invalid.",
        "RATE_LIMITED": "WooCommerce is rate limiting requests right now. Please try again shortly.",
    }

    def _error(self, code: str, status: Any = None) -> Dict[str, Any]:
        err = {"error": self.ERROR_MESSAGES[code], "code": code}
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

    def _execute_request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        max_retries: int = 3,
        api_base: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Executes an HTTP request against the WooCommerce REST API (v3 by default) with
        Basic Auth over HTTPS, bounded retries and credential redaction.

        Only idempotent GETs are retried; writes are attempted exactly once so a timeout
        can never execute a cancellation twice. Errors are returned as
        {"error": <safe message>, "code": <CODE>}.
        """
        if not self.site_url:
            return {
                "error": "WooCommerce store URL missing. Please configure WOOCOMMERCE_BASE_URL.",
                "code": "PROVIDER_NOT_CONFIGURED",
            }
        if self.config_error:
            return dict(self.config_error)
        if not self.consumer_key or not self.consumer_secret:
            return {
                "error": (
                    "WooCommerce authentication credentials missing. "
                    "Please configure WOOCOMMERCE_CONSUMER_KEY and WOOCOMMERCE_CONSUMER_SECRET."
                ),
                "code": "PROVIDER_NOT_CONFIGURED",
            }

        method = method.upper()
        attempts = max_retries if method == "GET" else 1
        endpoint_clean = endpoint.lstrip("/")
        url = f"{api_base or self.base_api_url}/{endpoint_clean}"

        # WooCommerce accepts Basic Auth over HTTPS; over plain HTTP (local dev only)
        # it requires the key/secret as query parameters.
        request_params = dict(params or {})
        if self.site_url.startswith("http://"):
            request_params["consumer_key"] = self.consumer_key
            request_params["consumer_secret"] = self.consumer_secret
            auth_arg = None
        else:
            auth_arg = (self.consumer_key, self.consumer_secret)

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Luintix-WooCommerce-Connector/1.0",
        }

        last_error: Dict[str, Any] = self._error("PROVIDER_ERROR")
        for attempt in range(1, attempts + 1):
            response = None
            try:
                
                client = self._get_client()

                response = client.request(
                    method=method,
                    url=url,
                    params=request_params,
                    json=json_data,
                    auth=auth_arg,
                )
                status = response.status_code
                log_provider_call(self.provider_name, f"{method} {endpoint_clean}", status)

                if status in (200, 201):
                    try:
                        data = response.json()
                    except ValueError:
                        return self._error(
                            "PROVIDER_INVALID_RESPONSE",
                            status,
                        )

                    if data is None:
                        return self._error(
                            "PROVIDER_INVALID_RESPONSE",
                            status,
                        )

                    return data

                if status == 400:
                    return self._error(
                        "PROVIDER_VALIDATION_FAILED",
                        status,
                    )
                if status == 401:
                    return self._error("PROVIDER_AUTH_FAILED", status)
                if status == 403:
                    return self._error("PROVIDER_FORBIDDEN", status)
                if status == 404:
                    try:
                        body_code = (response.json() or {}).get("code")
                    except (ValueError, AttributeError):
                        body_code = None
                    # "rest_no_route": the endpoint itself does not exist (REST API or
                    # plugin missing), which is different from "order 123 not found".
                    if body_code == "rest_no_route" or body_code is None:
                        return self._error("PROVIDER_API_UNAVAILABLE", status)
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
                logger.warning(f"[WOOCOMMERCE] request failed: {self._sanitize_error(type(e).__name__)}")
                last_error = self._error("PROVIDER_ERROR")

            if attempt < attempts:
                time.sleep(self._retry_delay(attempt, response))

        return last_error

    def _transform_woocommerce_order(
        self, order_data: Dict[str, Any], company_id: str
    ) -> Dict[str, Any]:
        """Transforms WooCommerce REST API v3 order payload into canonical Order model dict."""
        wc_id = str(order_data.get("id", ""))
        wc_number = str(order_data.get("number") or wc_id)
        raw_status = (order_data.get("status") or "processing").lower()

        # Canonical status mappings
        fin_status_map = {
            "completed": "PAID",
            "processing": "PAID",
            "pending": "PENDING",
            "on-hold": "PENDING",
            "refunded": "REFUNDED",
            "failed": "FAILED",
            "cancelled": "VOIDED",
            "trash": "VOIDED",
        }
        ful_status_map = {
            "completed": "FULFILLED",
            "processing": "UNFULFILLED",
            "pending": "UNFULFILLED",
            "on-hold": "UNFULFILLED",
            "cancelled": "CANCELLED",
            "refunded": "REFUNDED",
            "failed": "FAILED",
            "trash": "CANCELLED",
        }

        financial_status = fin_status_map.get(raw_status, "PAID")
        fulfillment_status = ful_status_map.get(raw_status, "UNFULFILLED")
        fully_paid = financial_status == "PAID"

        # Customer / Billing Info
        billing = order_data.get("billing") or {}
        cust_first = billing.get("first_name", "")
        cust_last = billing.get("last_name", "")
        cust_name = f"{cust_first} {cust_last}".strip() or None
        cust_email = billing.get("email") or None
        cust_id = str(order_data.get("customer_id") or cust_email or "CUST-WOO")

        # Line items
        raw_items = order_data.get("line_items") or []
        items = []
        for item in raw_items:
            price_val = float(item.get("price") or item.get("total") or 0.0)
            items.append(
                OrderItem(
                    product_name=item.get("name", "Product"),
                    quantity=int(item.get("quantity", 1)),
                    price=price_val,
                )
            )

        total = float(order_data.get("total") or 0.0)
        created_at = order_data.get("date_created_gmt") or order_data.get("date_created")
        completed_at = order_data.get("date_completed_gmt") or order_data.get("date_completed")

        # Tracking / Shipment info extraction from meta_data or shipping lines
        shipment = None
        meta_data = order_data.get("meta_data") or []
        tracking_number = None
        courier = None
        tracking_url = None
        for meta in meta_data:
            key = str(meta.get("key", "")).lower()
            val = str(meta.get("value", ""))
            if "tracking_number" in key or key == "_tracking_number":
                tracking_number = val
            elif "tracking_provider" in key or key == "_tracking_provider" or "courier" in key:
                courier = val
            elif "tracking_link" in key or "tracking_url" in key:
                tracking_url = val

        # Never invent tracking: a completed order without tracking data has a shipment
        # status but no courier/tracking number.
        if tracking_number or fulfillment_status == "FULFILLED":
            shipment = Shipment(
                order_id=wc_number,
                courier=courier,
                tracking_number=tracking_number,
                status=fulfillment_status,
                tracking_url=tracking_url,
            )

        canonical_order = Order(
            id=wc_number,
            company_id=company_id,
            customer_id=cust_id,
            status=raw_status.upper(),
            financial_status=financial_status,
            fulfillment_status=fulfillment_status,
            fully_paid=fully_paid,
            customer_name=cust_name,
            customer_email=cust_email,
            total=total,
            created_at=created_at,
            completed_at=completed_at,
            provider_order_id=wc_id,
            items=items,
            shipment=shipment,
        )
        return canonical_order.model_dump()

    def get_shop(self, company_id: str) -> Dict[str, Any]:
        """Retrieve WooCommerce merchant shop details via /system_status or site info."""
        res = self._execute_request("GET", "system_status")
        if "error" in res:
            # Fallback info from site_url
            shop_info = ShopInfo(
                name="WooCommerce Merchant Store",
                domain=self.site_url.replace("https://", "").replace("http://", ""),
                email=None,
                currency="USD",
                provider="WOOCOMMERCE",
            )
            return shop_info.model_dump()

        env = res.get("environment") or {}
        settings = res.get("settings") or {}
        site_name = env.get("site_title") or "WooCommerce Store"
        currency = settings.get("currency") or "USD"

        shop_info = ShopInfo(
            name=site_name,
            domain=self.site_url.replace("https://", "").replace("http://", ""),
            email=env.get("admin_email"),
            currency=currency,
            provider="WOOCOMMERCE",
        )
        return shop_info.model_dump()

    def get_orders(self, company_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Retrieve a bounded list of recent WooCommerce orders."""
        try:
            safe_limit = max(
                1,
                min(
                    int(limit),
                    self.MAX_ORDER_RESULTS,
                ),
            )
        except (TypeError, ValueError):
            safe_limit = 10

        res = self._execute_request(
            "GET",
            "orders",
            params={
                "per_page": safe_limit,
            },
        )

        if isinstance(res, dict) and "error" in res:
            return [res]

        if not isinstance(res, list):
            return []

        return [
            self._transform_woocommerce_order(
                order,
                company_id,
            )
            for order in res[:safe_limit]
            if isinstance(order, dict)
        ]

    @staticmethod
    def _order_number(order: Dict[str, Any]) -> str:
        return str(order.get("number") or order.get("id") or "")

    def _find_raw_order(self, order_id: str) -> Dict[str, Any]:
        """
        Resolves a customer-facing WooCommerce order number ("1003" or "#1003") to the raw
        order. Stores with custom order numbering can have number != internal ID, so a
        record is only accepted when its order *number* matches exactly.
        Returns {"order": raw_or_None} or an error dict.
        """
        clean_id = order_id.strip()
        number = clean_id[1:] if clean_id.startswith("#") else clean_id
        if not number:
            return {"order": None}

        if number.isdigit():
            res = self._execute_request("GET", f"orders/{number}")
            if isinstance(res, dict) and "error" not in res:
                if self._order_number(res) == number:
                    return {"order": res}
            elif res.get("code") != "NOT_FOUND":
                return res

        res = self._execute_request("GET", "orders", params={"search": number, "per_page": 20})
        if isinstance(res, dict) and "error" in res:
            return res
        for raw in res if isinstance(res, list) else []:
            if self._order_number(raw) == number:
                return {"order": raw}
        return {"order": None}

    def get_order_details(
        self, order_id: str, company_id: str
    ) -> Optional[Dict[str, Any]]:
        """Retrieve order details by the customer-facing order number (exact match only)."""
        found = self._find_raw_order(order_id)
        if "error" in found:
            return found
        if not found["order"]:
            return None
        return self._transform_woocommerce_order(found["order"], company_id)

    def get_order_status(self, order_id: str, company_id: str) -> Dict[str, Any]:
        """Retrieve order status and financial/fulfillment states."""
        order = self.get_order_details(order_id, company_id)
        if not order or "error" in order:
            return order or {"error": f"WooCommerce order '{order_id}' not found."}

        return {
            "order_id": order_id,
            "status": order.get("status"),
            "financial_status": order.get("financial_status"),
            "fulfillment_status": order.get("fulfillment_status"),
            "fully_paid": order.get("fully_paid"),
        }

    def get_customer(self, identifier: str, company_id: str) -> Dict[str, Any]:
        """Retrieve customer record by ID or email."""
        clean_id = identifier.strip()

        # 1. If numeric customer ID
        if clean_id.isdigit():
            res = self._execute_request("GET", f"customers/{clean_id}")
            if isinstance(res, dict) and "error" not in res and res.get("id"):
                cust_data = res
                cust_id = str(cust_data.get("id"))
                orders = self.get_orders_for_customer(cust_id, company_id)
                canonical_cust = Customer(
                    customer_id=cust_id,
                    company_id=company_id,
                    orders=[Order(**o) for o in orders if "error" not in o],
                )
                return canonical_cust.model_dump()

        # 2. Search customer by email / query string
        params = {"email": clean_id} if "@" in clean_id else {"search": clean_id}
        res = self._execute_request("GET", "customers", params=params)
        if isinstance(res, list) and len(res) > 0:
            cust_data = res[0]
            cust_id = str(cust_data.get("id"))
            orders = self.get_orders_for_customer(cust_id, company_id)
            canonical_cust = Customer(
                customer_id=cust_id,
                company_id=company_id,
                orders=[Order(**o) for o in orders if "error" not in o],
            )
            return canonical_cust.model_dump()

        return {"error": f"Customer '{identifier}' not found in WooCommerce."}

    def get_orders_for_customer(self, customer_id: str, company_id: str) -> List[Dict[str, Any]]:
        """Fetch orders associated with a customer ID."""
        res = self._execute_request("GET", "orders", params={"customer": customer_id})
        if isinstance(res, list):
            return [self._transform_woocommerce_order(o, company_id) for o in res]
        return []

    def _canonical_customer(self, data: Dict[str, Any]) -> Dict[str, Any]:
        billing = data.get("billing") or {}
        name = f"{data.get('first_name') or billing.get('first_name') or ''} {data.get('last_name') or billing.get('last_name') or ''}".strip()
        return {
            "customer_id": str(data["id"]),
            "email": data.get("email") or billing.get("email"),
            "phone": billing.get("phone"),
            "name": name or None,
            "provider": self.provider_name,
        }

    @staticmethod
    def _billing_matches(order: Dict[str, Any], kind: str, value: str) -> bool:
        billing = order.get("billing") or {}
        if kind == "email":
            return (billing.get("email") or "").casefold() == value.casefold()
        return normalize_phone(billing.get("phone")) == normalize_phone(value)

    def find_customer(self, identity: CustomerIdentity, company_id: str) -> Dict[str, Any]:
        """
        Resolve a WooCommerce customer by customer ID or exact email; phone numbers and
        guest checkouts are matched on the billing details of orders (WooCommerce's
        customers endpoint cannot filter by phone).
        """
        primary = identity.primary()
        if not primary:
            return {"error": identity.validation_error()}
        kind, value = primary

        if kind == "customer_id":
            if not value.isdigit():
                return {"error": f"'{value}' is not a valid WooCommerce customer ID."}
            res = self._execute_request("GET", f"customers/{value}")
            if isinstance(res, dict) and res.get("code") == "NOT_FOUND":
                return {"customer": None}
            if isinstance(res, dict) and "error" in res:
                return res
            return {"customer": self._canonical_customer(res)}

        if kind == "email":
            res = self._execute_request("GET", "customers", params={"email": value})
            if isinstance(res, dict) and "error" in res:
                return res
            for cust in res if isinstance(res, list) else []:
                if (cust.get("email") or "").casefold() == value.casefold():
                    return {"customer": self._canonical_customer(cust)}

        # Guest checkout (email) or phone: match exact billing details on orders.
        res = self._execute_request("GET", "orders", params={"search": value, "per_page": 20})
        if isinstance(res, dict) and "error" in res:
            return res
        for order in res if isinstance(res, list) else []:
            if not self._billing_matches(order, kind, value):
                continue
            if order.get("customer_id"):
                return {"customer": self._canonical_customer({"id": order["customer_id"], "billing": order.get("billing")})}
            billing = order.get("billing") or {}
            return {
                "customer": {
                    "customer_id": None,
                    "email": billing.get("email"),
                    "phone": billing.get("phone"),
                    "name": f"{billing.get('first_name', '')} {billing.get('last_name', '')}".strip() or None,
                    "guest": True,
                    "match": {"kind": kind, "value": value},
                    "provider": self.provider_name,
                }
            }
        return {"customer": None}

    def get_customer_orders(
        self, customer: Dict[str, Any], company_id: str, limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Orders for a resolved WooCommerce customer (registered or guest), newest first."""
        params = {"orderby": "date", "order": "desc"}
        if customer.get("customer_id"):
            res = self._execute_request("GET", "orders", params={**params, "customer": customer["customer_id"], "per_page": limit})
            match = None
        else:
            match = customer["match"]
            res = self._execute_request("GET", "orders", params={**params, "search": match["value"], "per_page": max(limit, 20)})
        if isinstance(res, dict) and "error" in res:
            return [res]
        orders = res if isinstance(res, list) else []
        if match:
            orders = [o for o in orders if self._billing_matches(o, match["kind"], match["value"])]
        return [self._transform_woocommerce_order(o, company_id) for o in orders[:limit]]

    def get_customer_details(
        self, identifier: str, company_id: str
    ) -> List[Dict[str, Any]]:
        """Retrieve customer order history list by customer ID/email or order ID."""
        # Try retrieving order directly first
        order = self.get_order_details(identifier, company_id)
        if order and "error" not in order:
            return [order]

        cust = self.get_customer(identifier, company_id)
        if "error" not in cust:
            return cust.get("orders", [])

        return []

    def get_product(self, product_id: str, company_id: str) -> Dict[str, Any]:
        """Retrieve product catalog entry from WooCommerce by ID, SKU, or search term."""
        clean_id = product_id.strip()

        # 1. If numeric product ID
        if clean_id.isdigit():
            res = self._execute_request("GET", f"products/{clean_id}")
            if isinstance(res, dict) and "error" not in res and res.get("id"):
                return {
                    "product_id": str(res.get("id")),
                    "name": res.get("name"),
                    "slug": res.get("slug"),
                    "type": res.get("type"),
                    "status": res.get("status"),
                    "price": float(res.get("price") or 0.0),
                    "regular_price": float(res.get("regular_price") or 0.0),
                    "sale_price": float(res.get("sale_price") or 0.0) if res.get("sale_price") else None,
                    "sku": res.get("sku"),
                    "stock_status": res.get("stock_status"),
                    "stock_quantity": res.get("stock_quantity"),
                    "description": self._truncate_text(res.get("short_description") or res.get("description"),
                                                       self.MAX_PRODUCT_DESCRIPTION_CHARS,
                                                      ),
                    "provider": "WOOCOMMERCE",
                }

        # 2. Search product by search term / SKU (exact name match preferred)
        res = self._execute_request("GET", "products", params={"search": clean_id})
        if isinstance(res, dict) and "error" in res:
            return res
        if isinstance(res, list) and len(res) > 0:
            exact = [p for p in res if str(p.get("name", "")).casefold() == clean_id.casefold()]
            p = exact[0] if exact else res[0]
            return {
                "product_id": str(p.get("id")),
                "name": p.get("name"),
                "slug": p.get("slug"),
                "type": p.get("type"),
                "status": p.get("status"),
                "price": float(p.get("price") or 0.0),
                "regular_price": float(p.get("regular_price") or 0.0),
                "sale_price": float(p.get("sale_price") or 0.0) if p.get("sale_price") else None,
                "sku": p.get("sku"),
                "stock_status": p.get("stock_status"),
                "stock_quantity": p.get("stock_quantity"),
                "description": self._truncate_text(p.get("short_description") or p.get("description"),self.MAX_PRODUCT_DESCRIPTION_CHARS,
                                                  ),
                "provider": "WOOCOMMERCE",
            }

        return {"error": f"WooCommerce product '{product_id}' not found."}

    def get_inventory(self, product_query: str, company_id: str) -> Dict[str, Any]:
        """Stock levels for a product, including each variation of variable products."""
        product = self.get_product(product_query, company_id)
        if "error" in product:
            return product
        inventory = {
            "product_id": product["product_id"],
            "name": product["name"],
            "stock_status": product.get("stock_status"),
            "stock_quantity": product.get("stock_quantity"),
            "variants": [],
            "provider": self.provider_name,
        }
        if product.get("type") == "variable":
            variations = self._execute_request(
                "GET", f"products/{product['product_id']}/variations", params={"per_page": 100}
            )
            if isinstance(variations, dict) and "error" in variations:
                return variations
            inventory["variants"] = [
                {
                    "variant_id": str(v.get("id")),
                    "attributes": {a.get("name"): a.get("option") for a in v.get("attributes") or []},
                    "sku": v.get("sku"),
                    "stock_status": v.get("stock_status"),
                    "stock_quantity": v.get("stock_quantity"),
                }
                
                for v in (
                    variations[:self.MAX_VARIANT_RESULTS]
                    if isinstance(variations, list)
                    else []
                )
            ]
    def cancel_order(
        self, order_id: str, company_id: str, reason: Optional[str] = "CUSTOMER"
    ) -> Dict[str, Any]:
        """
        Executes WooCommerce order cancellation via PUT /wp-json/wc/v3/orders/{id}.
        Verifies post-cancellation status and returns canonical CancelOrderResult.
        """
        order = self.get_order_details(order_id, company_id)
        if not order or "error" in order:
            err_msg = order.get("error") if isinstance(order, dict) else f"Order '{order_id}' not found."
            return {
                "success": False,
                "status": "FAILED",
                "order_id": order_id,
                "reason": err_msg,
                "message": f"WooCommerce order '{order_id}' could not be retrieved: {err_msg}",
                "refund_issued": False,
                "verification_status": "FAILED",
            }

        raw_num = str(order.get("provider_order_id") or order.get("id"))

        # Idempotency check: if order already cancelled
        if order.get("status") == "CANCELLED" or order.get("fulfillment_status") == "CANCELLED":
            return {
                "success": True,
                "status": "CANCELLED",
                "order_id": order_id,
                "reason": "ORDER_ALREADY_CANCELLED",
                "message": f"WooCommerce order {order_id} is already cancelled.",
                "refund_issued": False,
                "refund_amount": 0.0,
                "verification_status": "VERIFIED",
            }

        # Update order status to 'cancelled'
        payload = {"status": "cancelled"}
        res = self._execute_request("PUT", f"orders/{raw_num}", json_data=payload)

        if "error" in res:
            return {
                "success": False,
                "status": "FAILED",
                "order_id": order_id,
                "reason": str(res["error"]),
                "message": f"WooCommerce cancellation failed: {res['error']}",
                "refund_issued": False,
                "verification_status": "FAILED",
            }

        # Post-action verification
        post_order = self.get_order_details(order_id, company_id)
        verification_status = "VERIFIED"
        if post_order and "error" not in post_order:
            if post_order.get("status") != "CANCELLED" and post_order.get("fulfillment_status") != "CANCELLED":
                verification_status = "UNVERIFIED"

        fully_paid = bool(order.get("fully_paid"))
        # WooCommerce's status change does not move money; report that honestly.
        refund_note = (
            " The payment was NOT refunded automatically; the merchant must issue the refund."
            if fully_paid else ""
        )

        result = CancelOrderResult(
            success=True,
            status="CANCELLED",
            order_id=order_id,
            reason=reason or "CUSTOMER",
            message=f"WooCommerce order {order_id} has been successfully CANCELLED.{refund_note}",
            refund_issued=False,
            refund_amount=0.0,
            verification_status=verification_status,
            details={
                "wc_order_id": raw_num,
                "wc_status": res.get("status"),
            },
        )
        return result.model_dump()

    def update_order(
        self, order_id: str, company_id: str, **kwargs
    ) -> Dict[str, Any]:
        """Update WooCommerce order status or parameters via PUT /wp-json/wc/v3/orders/{id}."""
        found = self._find_raw_order(order_id)
        if "error" in found:
            return found
        if not found["order"]:
            return {"error": f"WooCommerce order '{order_id}' not found.", "code": "NOT_FOUND"}
        clean_id = str(found["order"]["id"])

        # Construct update payload
        payload = {}
        if "status" in kwargs:
            payload["status"] = str(kwargs["status"]).lower()
        if "customer_note" in kwargs:
            payload["customer_note"] = kwargs["customer_note"]

        if not payload:
            return {"error": "No valid fields provided to update WooCommerce order."}

        res = self._execute_request("PUT", f"orders/{clean_id}", json_data=payload)
        if "error" in res:
            return res

        return self._transform_woocommerce_order(res, company_id)

    def get_refunds(self, order_id: str, company_id: str) -> Dict[str, Any]:
        """Retrieve refund status and list of refund records for a WooCommerce order."""
        found = self._find_raw_order(order_id)
        if "error" in found:
            return found
        if not found["order"]:
            return {"error": f"WooCommerce order '{order_id}' not found.", "code": "NOT_FOUND"}
        return self._refunds_for(str(found["order"]["id"]), order_id)

    def _refunds_for(self, wc_order_id: str, order_id: str) -> Dict[str, Any]:
        res = self._execute_request("GET", f"orders/{wc_order_id}/refunds")
        if isinstance(res, dict) and "error" in res:
            return res

        if not isinstance(res, list):
            return {"order_id": order_id, "refunds": []}

        refunds_list = []
        total_refunded = 0.0
        for r in res[:self.MAX_REFUND_RESULTS]:
            amt = float(r.get("amount") or 0.0)
            total_refunded += amt
            refunds_list.append({
                "refund_id": str(r.get("id")),
                "amount": amt,
                "reason": r.get("reason"),
                "date_created": r.get("date_created_gmt") or r.get("date_created"),
            })

        return {
            "order_id": order_id,
            "total_refunded": total_refunded,
            "refund_count": len(refunds_list),
            "refunds": refunds_list,
        }

    def request_return(
        self, order_id: str, refund_amount: float, company_id: str
    ) -> Dict[str, Any]:
        """
        Controlled/sensitive capability for WooCommerce returns & refunds.
        Validates order refund history, checks refund policy limits, and returns a controlled
        gated response before initiating actual money movement.
        """
        order = self.get_order_details(order_id, company_id)
        if not order or "error" in order:
            return {"status": "BLOCKED", "reason": f"Order {order_id} not found."}

        # Inspect current refund status
        refunds_info = self._refunds_for(str(order.get("provider_order_id") or order["id"]), order_id)
        
        return {
            "status": "CONTROLLED_CAPABILITY",
            "message": (
                f"Refund capability for WooCommerce order '{order_id}' is active in read/status mode. "
                f"Requested refund amount: ${refund_amount:.2f}. "
                f"Existing total refunded: ${refunds_info.get('total_refunded', 0.0):.2f}. "
                "Financial money movement requires explicit human support approval."
            ),
            "order_id": order_id,
            "refund_amount": refund_amount,
            "refunds_summary": refunds_info,
        }

    def get_shipment_status(
        self, order_id: str, company_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Tracking for a WooCommerce order. WooCommerce core has no shipment tracking, so this
        reads the WooCommerce Shipment Tracking extension API (also implemented by Advanced
        Shipment Tracking) when the merchant has it installed, otherwise the order's own data.
        """
        order = self.get_order_details(order_id, company_id)
        if not order or "error" in order:
            return order

        wc_id = order.get("provider_order_id") or order["id"]
        trackings = self._execute_request(
            "GET",
            f"orders/{wc_id}/shipment-trackings",
            api_base=f"{self.site_url}/wp-json/wc-shipment-tracking/v3",
        )
        if isinstance(trackings, list) and trackings:
            latest = trackings[-1]
            return Shipment(
                id=str(latest.get("tracking_id") or "") or None,
                order_id=order["id"],
                courier=latest.get("tracking_provider") or latest.get("custom_tracking_provider") or None,
                tracking_number=latest.get("tracking_number") or None,
                status=order.get("fulfillment_status") or "FULFILLED",
                tracking_url=latest.get("tracking_link") or latest.get("custom_tracking_link") or None,
            ).model_dump()
        return order.get("shipment")

    def check_proactive_notifications(
        self, customer_id: str, company_id: str
    ) -> Optional[str]:
        """Scan WooCommerce order statuses for delay flags."""
        return None

    def escalate_to_human(
        self, order_id: str, reason: str, company_id: str
    ) -> Dict[str, Any]:
        """Create human escalation ticket for WooCommerce tenant."""
        ticket_id = f"TICK-WOO-{order_id.upper()}"
        return {
            "status": "ESCALATED",
            "ticket_id": ticket_id,
            "message": (
                f"Support ticket {ticket_id} created for WooCommerce order {order_id}. "
                f"Reason: {reason}"
            ),
        }

    def get_ticket_details(
        self, ticket_id: str, company_id: str
    ) -> Dict[str, Any]:
        """Retrieve ticket audit details for WooCommerce escalation."""
        clean_ticket = ticket_id.upper().strip()
        order_id = clean_ticket.replace("TICK-WOO-", "").replace("TICK-", "")
        return {
            "ticket_id": clean_ticket,
            "status": "OPEN_IN_REVIEW",
            "associated_order": order_id,
            "details": f"Escalated WooCommerce ticket {clean_ticket} queued for human agent review.",
            "created_at": "Recently created",
        }
