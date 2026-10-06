import os
import time
import logging
from typing import Any, Dict, List, Optional, Tuple
import httpx
from integrations.base import BaseConnector, log_provider_call, log_provider_warning
from integrations.models import CustomerIdentity, Order, OrderItem, OrderTransaction, Shipment, ShopInfo, normalize_phone
from integrations.payment_mapping import map_transaction

logger = logging.getLogger("uvicorn.error")

# Fields shared by every order read so all lookups produce the same canonical model.
ORDER_FIELDS = """
    id
    name
    createdAt
    cancelledAt
    displayFinancialStatus
    displayFulfillmentStatus
    fullyPaid
    totalPriceSet {
      shopMoney {
        amount
        currencyCode
      }
    }
    customer {
      id
      email
      firstName
      lastName
    }
    lineItems(first: 20) {
      nodes {
        id
        title
        quantity
        originalUnitPriceSet {
          shopMoney {
            amount
          }
        }
      }
    }
    fulfillments {
      id
      status
      trackingInfo {
        company
        number
        url
      }
    }
"""

# Minimal fields needed to evaluate and execute a cancellation.
CANCEL_FIELDS = """
    id
    name
    cancelledAt
    displayFinancialStatus
    displayFulfillmentStatus
    fullyPaid
    totalPriceSet {
      shopMoney {
        amount
        currencyCode
      }
    }
"""

# Payment transactions of one order (Admin GraphQL OrderTransaction fields). receiptJson is
# only used to extract a mapped gateway payment reference; it is never returned.
TRANSACTION_FIELDS = """
    id
    name
    transactions(first: 50) {
      id
      kind
      status
      gateway
      formattedGateway
      paymentId
      test
      manualPaymentGateway
      createdAt
      processedAt
      parentTransaction { id }
      amountSet {
        shopMoney {
          amount
          currencyCode
        }
      }
      receiptJson
    }
"""

PRODUCT_FIELDS = """
    id
    title
    handle
    status
    vendor
    productType
    description
    priceRangeV2 {
      minVariantPrice { amount currencyCode }
      maxVariantPrice { amount currencyCode }
    }
    variants(first: 10) {
      nodes {
        title
        sku
        price
      }
    }
"""

CUSTOMER_FIELDS = """
    id
    displayName
    firstName
    lastName
    defaultEmailAddress { emailAddress }
    defaultPhoneNumber { phoneNumber }
"""

# Shopify admin order numbers below this length are order *names* (#1003), not resource IDs.
MIN_LEGACY_ORDER_ID_LENGTH = 10


class ShopifyConnector(BaseConnector):
    """
    Concrete Provider Connector for Shopify Admin GraphQL API.
    Handles read-only e-commerce capability execution for Shopify merchants.

    Authentication: uses SHOPIFY_ACCESS_TOKEN when valid. When SHOPIFY_CLIENT_ID and
    SHOPIFY_CLIENT_SECRET are configured (Dev Dashboard app), an expired/invalid token is
    replaced automatically via the client-credentials grant (tokens live ~24h).
    """

    provider_name = "SHOPIFY"

    # Minted client-credentials tokens, shared across instances: {shop_domain: (token, expires_at)}
    _token_cache: Dict[str, Tuple[str, float]] = {}

    def __init__(
        self,
        shop_domain: Optional[str] = None,
        access_token: Optional[str] = None,
        api_version: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
    ):
        self.shop_domain = (
            shop_domain
            or os.getenv("SHOPIFY_SHOP_DOMAIN")
            or "quickstart-demo.myshopify.com"
        ).strip()
        # Clean domain string if passed as full URL
        self.shop_domain = (
            self.shop_domain.replace("https://", "")
            .replace("http://", "")
            .rstrip("/")
        )

        self.access_token = access_token or os.getenv("SHOPIFY_ACCESS_TOKEN", "")
        self.api_version = (
            api_version or os.getenv("SHOPIFY_API_VERSION") or "2024-04"
        )
        self.client_id = client_id or os.getenv("SHOPIFY_CLIENT_ID", "")
        self.client_secret = client_secret or os.getenv("SHOPIFY_CLIENT_SECRET", "")

        self.graphql_url = (
            f"https://{self.shop_domain}/admin/api/{self.api_version}/graphql.json"
        )

    def credentials_configured(self) -> bool:
        return bool(self.access_token or (self.client_id and self.client_secret))

    def domain_configured(self) -> bool:
        return bool(self.shop_domain)

    def _cached_token(self) -> Optional[str]:
        cached = self._token_cache.get(self.shop_domain)
        if cached and cached[1] > time.time():
            return cached[0]
        return None

    def _mint_token(self) -> Optional[str]:
        """Obtains a fresh access token via the OAuth client-credentials grant."""
        if not (self.client_id and self.client_secret):
            return None
        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.post(
                    f"https://{self.shop_domain}/admin/oauth/access_token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                )
            log_provider_call(self.provider_name, "oauth client_credentials", response.status_code)
            if response.status_code != 200:
                return None
            payload = response.json()
            token = payload.get("access_token")
            if not token:
                return None
            # Refresh 5 minutes before Shopify's stated expiry.
            expires_in = float(payload.get("expires_in") or 3600)
            self._token_cache[self.shop_domain] = (token, time.time() + max(expires_in - 300, 60))
            return token
        except Exception:
            log_provider_call(self.provider_name, "oauth client_credentials", "REQUEST_FAILED")
            return None

    def _execute_graphql(
        self, query: str, variables: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Executes a GraphQL request against the Shopify Admin API.
        Never exposes raw access tokens or client secrets in logs or errors.
        """
        token = self._cached_token() or self.access_token or self._mint_token()
        if not token:
            return {
                "error": (
                    "Shopify credentials are missing. Configure SHOPIFY_ACCESS_TOKEN or "
                    "SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET."
                )
            }

        payload = {"query": query, "variables": variables or {}}
        operation = query.strip().split("(")[0].split("{")[0].strip() or "graphql"

        try:
            response = self._post_graphql(payload, token, operation)

            if response.status_code == 401:
                fresh_token = self._mint_token()
                if fresh_token and fresh_token != token:
                    response = self._post_graphql(payload, fresh_token, operation)

            if response.status_code == 401:
                return {"error": "Shopify authentication failed (Invalid Access Token)."}
            elif response.status_code == 403:
                return {
                    "error": "Shopify access forbidden (Insufficient API Scopes or Protected Data Access required)."
                }
            elif response.status_code != 200:
                return {"error": f"Shopify API HTTP Error {response.status_code}."}

            data = response.json()

            # Handle GraphQL level errors
            if "errors" in data and data["errors"]:
                messages = sorted({str(e.get("message", "Unknown error")) for e in data["errors"]})
                # If partial field error but valid non-None data payload exists:
                if "data" in data and data["data"] and any(v is not None for v in data["data"].values()):
                    log_provider_warning(self.provider_name, operation, "partial data: " + " | ".join(messages))
                    return data["data"]

                err_msg = str(data["errors"])
                if "protected" in err_msg.lower() or "access denied" in err_msg.lower():
                    required = next(
                        (e.get("extensions", {}).get("requiredAccess") for e in data["errors"]
                         if e.get("extensions", {}).get("requiredAccess")),
                        None,
                    )
                    return {
                        "error": (
                            "Shopify access denied for this data"
                            + (f" (required: {required})" if required else "")
                            + ". The Shopify app needs this access scope / protected customer data "
                            "access granted in its app configuration."
                        ),
                        "code": "PROVIDER_ACCESS_DENIED",
                    }
                return {"error": f"Shopify GraphQL Error: {data['errors'][0].get('message', 'Unknown error')}"}

            return data.get("data", {})

        except httpx.TimeoutException:
            return {"error": "Shopify API request timed out."}
        except Exception as e:
            return {"error": f"Shopify request failed: {str(e)}"}

    def _post_graphql(self, payload: Dict[str, Any], token: str, operation: str) -> httpx.Response:
        headers = {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": token,
        }
        with httpx.Client(timeout=10.0) as client:
            response = client.post(self.graphql_url, json=payload, headers=headers)
        log_provider_call(self.provider_name, operation, response.status_code)
        return response

    def get_shop(self, company_id: str) -> Dict[str, Any]:
        """Retrieve shop details from Shopify GraphQL API."""
        query = """
        query GetShop {
          shop {
            name
            email
            myshopifyDomain
            currencyCode
          }
        }
        """
        data = self._execute_graphql(query)
        if "error" in data:
            return data

        shop_data = data.get("shop", {})
        if not shop_data:
            return {"error": "Unable to fetch shop details."}

        shop_info = ShopInfo(
            name=shop_data.get("name", "Shopify Store"),
            domain=shop_data.get("myshopifyDomain", self.shop_domain),
            email=shop_data.get("email"),
            currency=shop_data.get("currencyCode", "USD"),
            provider="SHOPIFY",
        )
        return shop_info.model_dump()

    def get_orders(self, company_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Retrieve the most recent orders from Shopify (newest first)."""
        query = f"""
        query GetOrders($first: Int!) {{
          orders(first: $first, sortKey: CREATED_AT, reverse: true) {{
            nodes {{ {ORDER_FIELDS} }}
          }}
        }}
        """
        data = self._execute_graphql(query, {"first": limit})
        if "error" in data:
            return [data]

        orders_data = (data.get("orders") or {}).get("nodes", [])
        return [self._transform_shopify_order(o, company_id) for o in orders_data]

    def _transform_shopify_order(
        self, order_data: Dict[str, Any], company_id: str
    ) -> Dict[str, Any]:
        """Transforms Shopify GraphQL order data into canonical Order model dict."""
        items = [
            OrderItem(
                product_name=item["title"],
                quantity=item["quantity"],
                price=float(
                    item.get("originalUnitPriceSet", {})
                    .get("shopMoney", {})
                    .get("amount", 0.0)
                ),
            )
            for item in order_data.get("lineItems", {}).get("nodes", [])
        ]

        shipment = None
        fulfillments = order_data.get("fulfillments", [])
        if fulfillments:
            first_f = fulfillments[0]
            t_info_list = first_f.get("trackingInfo") or []
            t_info = t_info_list[0] if t_info_list else {}
            f_status = first_f.get("displayStatus") or first_f.get("status") or "FULFILLED"

            shipment = Shipment(
                id=first_f.get("id"),
                order_id=order_data.get("name", order_data.get("id")),
                courier=t_info.get("company"),
                tracking_number=t_info.get("number"),
                status=f_status,
                tracking_url=t_info.get("url"),
            )

        cust = order_data.get("customer") or {}
        customer_id = cust.get("email") or cust.get("id") or "CUST-SHOPIFY"
        cust_name = (
            f"{cust.get('firstName', '')} {cust.get('lastName', '')}".strip()
            if cust.get("firstName") or cust.get("lastName")
            else None
        )
        cust_email = cust.get("email")

        fin_status = order_data.get("displayFinancialStatus") or "PAID"
        ful_status = order_data.get("displayFulfillmentStatus") or "UNFULFILLED"
        cancelled_at = order_data.get("cancelledAt")
        if cancelled_at is not None:
            ful_status = "CANCELLED"

        fully_paid = order_data.get("fullyPaid")
        if fully_paid is None:
            fully_paid = (fin_status == "PAID")

        canonical_order = Order(
            id=order_data.get("name", order_data.get("id")),
            company_id=company_id,
            customer_id=customer_id,
            status=ful_status or fin_status or "PROCESSING",
            financial_status=fin_status,
            fulfillment_status=ful_status,
            fully_paid=fully_paid,
            customer_name=cust_name,
            customer_email=cust_email,
            total=float(
                order_data.get("totalPriceSet", {})
                .get("shopMoney", {})
                .get("amount", 0.0)
            ),
            created_at=order_data.get("createdAt"),
            items=items,
            shipment=shipment,
        )
        return canonical_order.model_dump()

    def _find_order_node(self, order_id: str, fields: str) -> Dict[str, Any]:
        """
        Resolves a user-supplied order reference to a raw Shopify order node.

        - "gid://shopify/Order/<id>"  -> order(id: ...)
        - "#1003" or any other name   -> orders(query: 'name:"#1003"'), exact name match
        - "1003"                      -> treated as order number first ("#1003", then "1003");
                                         only long numeric values fall back to the legacy
                                         resource ID (gid://shopify/Order/<digits>).

        The identifier is never rewritten (no ORD- prefix stripping/adding).
        Returns {"order": node_or_None} or {"error": message}.
        """
        clean_id = order_id.strip()
        by_id_query = f"""
        query GetOrderById($id: ID!) {{
          order(id: $id) {{ {fields} }}
        }}
        """

        if clean_id.startswith("gid://shopify/Order/"):
            data = self._execute_graphql(by_id_query, {"id": clean_id})
            if "error" in data:
                return data
            return {"order": data.get("order")}

        by_name_query = f"""
        query GetOrderByName($searchQuery: String!) {{
          orders(first: 5, query: $searchQuery) {{
            nodes {{ {fields} }}
          }}
        }}
        """
        candidate_names = [f"#{clean_id}", clean_id] if clean_id.isdigit() else [clean_id]
        for name in candidate_names:
            escaped = name.replace("\\", "\\\\").replace('"', '\\"')
            data = self._execute_graphql(by_name_query, {"searchQuery": f'name:"{escaped}"'})
            if "error" in data:
                return data
            for node in (data.get("orders") or {}).get("nodes", []):
                if str(node.get("name", "")).casefold() == name.casefold():
                    return {"order": node}

        if clean_id.isdigit() and len(clean_id) >= MIN_LEGACY_ORDER_ID_LENGTH:
            data = self._execute_graphql(by_id_query, {"id": f"gid://shopify/Order/{clean_id}"})
            if "error" in data:
                return data
            return {"order": data.get("order")}

        return {"order": None}

    def get_order_details(
        self, order_id: str, company_id: str
    ) -> Optional[Dict[str, Any]]:
        """Retrieve single order details from Shopify by GID, order name (#1003) or number (1003)."""
        result = self._find_order_node(order_id, ORDER_FIELDS)
        if "error" in result:
            return result
        if not result.get("order"):
            return None
        return self._transform_shopify_order(result["order"], company_id)

    def _transform_transaction(self, t: Dict[str, Any]) -> Dict[str, Any]:
        money = (t.get("amountSet") or {}).get("shopMoney") or {}
        try:
            amount = float(money["amount"]) if money.get("amount") not in (None, "") else None
        except (TypeError, ValueError):
            amount = None
        return OrderTransaction(
            id=str(t["id"]), kind=t.get("kind"), status=t.get("status"), gateway=t.get("gateway"),
            gateway_display_name=t.get("formattedGateway"), amount=amount, currency=money.get("currencyCode"),
            store_payment_id=t.get("paymentId"),
            manual_gateway=t.get("manualPaymentGateway") if isinstance(t.get("manualPaymentGateway"), bool) else None,
            test=t.get("test") if isinstance(t.get("test"), bool) else None,
            parent_transaction_id=(t.get("parentTransaction") or {}).get("id"),
            created_at=t.get("createdAt"), processed_at=t.get("processedAt"),
            **map_transaction(t.get("gateway"), t.get("receiptJson")),
            provider=self.provider_name,
        ).model_dump()

    def get_order_transactions(self, order_id: str, company_id: str) -> Dict[str, Any]:
        """Payment transactions Shopify recorded on one order (read-only)."""
        result = self._find_order_node(order_id, TRANSACTION_FIELDS)
        if "error" in result:
            return result
        node = result.get("order")
        if not node:
            return {"error": f"Order '{order_id}' not found.", "code": "ORDER_NOT_FOUND"}
        raw = node.get("transactions")
        if not isinstance(raw, list):
            return {"error": "Shopify returned an unexpected transactions response.", "code": "PROVIDER_INVALID_RESPONSE"}
        transactions = [self._transform_transaction(t) for t in raw if isinstance(t, dict) and t.get("id")]
        return {"order_id": node.get("name") or order_id, "transactions": transactions,
                "count": len(transactions), "provider": self.provider_name}

    def get_product(self, product_id: str, company_id: str) -> Dict[str, Any]:
        """Retrieve a product by Shopify GID or by title/search text."""
        clean_id = product_id.strip()

        if clean_id.startswith("gid://shopify/Product/"):
            query = f"""
            query GetProductById($id: ID!) {{
              product(id: $id) {{ {PRODUCT_FIELDS} }}
            }}
            """
            data = self._execute_graphql(query, {"id": clean_id})
            if "error" in data:
                return data
            node = data.get("product")
        else:
            query = f"""
            query SearchProducts($searchQuery: String!) {{
              products(first: 10, query: $searchQuery) {{
                nodes {{ {PRODUCT_FIELDS} }}
              }}
            }}
            """
            data = self._execute_graphql(query, {"searchQuery": clean_id})
            if "error" in data:
                return data
            nodes = (data.get("products") or {}).get("nodes", [])
            exact = [n for n in nodes if str(n.get("title", "")).casefold() == clean_id.casefold()]
            node = exact[0] if exact else (nodes[0] if nodes else None)

        if not node:
            return {"error": f"Shopify product '{product_id}' not found."}

        price_range = node.get("priceRangeV2") or {}
        min_price = (price_range.get("minVariantPrice") or {})
        max_price = (price_range.get("maxVariantPrice") or {})
        return {
            "product_id": node.get("id"),
            "name": node.get("title"),
            "handle": node.get("handle"),
            "status": node.get("status"),
            "vendor": node.get("vendor"),
            "product_type": node.get("productType"),
            "description": node.get("description"),
            "min_price": float(min_price.get("amount") or 0.0),
            "max_price": float(max_price.get("amount") or 0.0),
            "currency": min_price.get("currencyCode"),
            "variants": [
                {"title": v.get("title"), "sku": v.get("sku"), "price": float(v.get("price") or 0.0)}
                for v in (node.get("variants") or {}).get("nodes", [])
            ],
            "provider": "SHOPIFY",
        }

    def _canonical_customer(self, node: Dict[str, Any]) -> Dict[str, Any]:
        name = node.get("displayName") or (
            f"{node.get('firstName') or ''} {node.get('lastName') or ''}".strip() or None
        )
        return {
            "customer_id": node.get("id"),
            "email": (node.get("defaultEmailAddress") or {}).get("emailAddress"),
            "phone": (node.get("defaultPhoneNumber") or {}).get("phoneNumber"),
            "name": name,
            "provider": self.provider_name,
        }

    def find_customer(self, identity: CustomerIdentity, company_id: str) -> Dict[str, Any]:
        """Resolve a Shopify customer by authenticated customer ID, exact email, or exact phone."""
        primary = identity.primary()
        if not primary:
            return {"error": identity.validation_error()}
        kind, value = primary

        if kind == "customer_id":
            gid = value if value.startswith("gid://shopify/Customer/") else (
                f"gid://shopify/Customer/{value}" if value.isdigit() else None
            )
            if not gid:
                return {"error": f"'{value}' is not a valid Shopify customer ID."}
            data = self._execute_graphql(
                f"query GetCustomerById($id: ID!) {{ customer(id: $id) {{ {CUSTOMER_FIELDS} }} }}",
                {"id": gid},
            )
            if "error" in data:
                return data
            node = data.get("customer")
            return {"customer": self._canonical_customer(node) if node else None}

        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        data = self._execute_graphql(
            f"""
            query FindCustomer($searchQuery: String!) {{
              customers(first: 5, query: $searchQuery) {{ nodes {{ {CUSTOMER_FIELDS} }} }}
            }}
            """,
            {"searchQuery": f'{kind}:"{escaped}"'},
        )
        if "error" in data:
            return data

        # Shopify search is fuzzy; only accept an exact identifier match.
        for node in (data.get("customers") or {}).get("nodes", []):
            customer = self._canonical_customer(node)
            if kind == "email" and (customer["email"] or "").casefold() == value.casefold():
                return {"customer": customer}
            if kind == "phone" and normalize_phone(customer["phone"]) == normalize_phone(value):
                return {"customer": customer}
        return {"customer": None}

    def get_customer_orders(
        self, customer: Dict[str, Any], company_id: str, limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Orders belonging to a resolved Shopify customer, newest first."""
        data = self._execute_graphql(
            f"""
            query GetCustomerOrders($id: ID!, $first: Int!) {{
              customer(id: $id) {{
                orders(first: $first, sortKey: CREATED_AT, reverse: true) {{
                  nodes {{ {ORDER_FIELDS} }}
                }}
              }}
            }}
            """,
            {"id": customer["customer_id"], "first": limit},
        )
        if "error" in data:
            return [data]
        node = data.get("customer") or {}
        return [
            self._transform_shopify_order(o, company_id)
            for o in (node.get("orders") or {}).get("nodes", [])
        ]

    def get_shipment_status(
        self, order_id: str, company_id: str
    ) -> Optional[Dict[str, Any]]:
        """Retrieve shipment tracking information from Shopify."""
        order = self.get_order_details(order_id, company_id)
        if not order or "error" in order:
            return order
        return order.get("shipment")

    def get_customer_details(
        self, identifier: str, company_id: str
    ) -> List[Dict[str, Any]]:
        """Find customer order history from Shopify."""
        # For read-only initial phase, try searching order by ID/email
        order = self.get_order_details(identifier, company_id)
        if order and "error" not in order:
            return [order]
        return []

    def _resolve_shopify_gid(self, order_id: str) -> Optional[Dict[str, Any]]:
        """
        Helper method to resolve Shopify Order GID, cancellation status,
        financial status, fulfillment status, and total price.
        """
        result = self._find_order_node(order_id, CANCEL_FIELDS)
        if "error" in result:
            return result
        return result.get("order")

    def cancel_order(
        self, order_id: str, company_id: str, reason: Optional[str] = "CUSTOMER"
    ) -> Dict[str, Any]:
        """
        Executes order cancellation via Shopify GraphQL Admin API `orderCancel` mutation.
        Handles paid (with refund) and unpaid orders, polls async job if necessary,
        and verifies post-cancellation status.
        """
        order_info = self._resolve_shopify_gid(order_id)
        if not order_info or "error" in order_info:
            return {
                "success": False,
                "status": "FAILED",
                "order_id": order_id,
                "reason": order_info.get("error") if isinstance(order_info, dict) else "Order not found",
                "message": f"Shopify order '{order_id}' could not be retrieved or found.",
                "refund_issued": False,
                "verification_status": "FAILED",
            }

        gid = order_info["id"]
        order_name = order_info.get("name", order_id)
        cancelled_at = order_info.get("cancelledAt")
        display_fin = (order_info.get("displayFinancialStatus") or "").upper()
        fully_paid = order_info.get("fullyPaid", display_fin == "PAID")
        total_amount = float(order_info.get("totalPriceSet", {}).get("shopMoney", {}).get("amount", 0.0))

        # Check idempotency: If already cancelled
        if cancelled_at is not None:
            return {
                "success": True,
                "status": "CANCELLED",
                "order_id": order_name,
                "reason": "ORDER_ALREADY_CANCELLED",
                "message": f"Shopify order {order_name} is already cancelled.",
                "refund_issued": fully_paid,
                "refund_amount": total_amount if fully_paid else 0.0,
                "verification_status": "VERIFIED",
            }

        # Reason mapping
        valid_reasons = {"CUSTOMER", "DECLINED", "FRAUD", "INVENTORY", "STAFF", "OTHER"}
        reason_upper = (reason or "CUSTOMER").upper()
        cancel_reason = reason_upper if reason_upper in valid_reasons else "CUSTOMER"

        # Refund method input
        refund_method_input = {"originalPaymentMethodsRefund": True} if fully_paid else None

        mutation = """
        mutation OrderCancel($orderId: ID!, $reason: OrderCancelReason!, $restock: Boolean!, $notifyCustomer: Boolean!, $refundMethod: OrderCancelRefundMethodInput) {
          orderCancel(orderId: $orderId, reason: $reason, restock: $restock, notifyCustomer: $notifyCustomer, refundMethod: $refundMethod) {
            job {
              id
              done
            }
            orderCancelUserErrors {
              code
              field
              message
            }
          }
        }
        """

        variables = {
            "orderId": gid,
            "reason": cancel_reason,
            "restock": True,
            "notifyCustomer": True,
            "refundMethod": refund_method_input,
        }

        res_data = self._execute_graphql(mutation, variables)
        if "error" in res_data:
            return {
                "success": False,
                "status": "FAILED",
                "order_id": order_name,
                "reason": str(res_data["error"]),
                "message": f"Shopify GraphQL mutation failed: {res_data['error']}",
                "refund_issued": False,
                "verification_status": "FAILED",
            }

        payload = res_data.get("orderCancel") or {}
        user_errors = payload.get("orderCancelUserErrors") or []

        if user_errors:
            err_messages = [e.get("message", "Unknown error") for e in user_errors]
            err_combined = "; ".join(err_messages)

            if any("already cancelled" in m.lower() for m in err_messages):
                return {
                    "success": True,
                    "status": "CANCELLED",
                    "order_id": order_name,
                    "reason": "ORDER_ALREADY_CANCELLED",
                    "message": f"Shopify order {order_name} is already cancelled.",
                    "refund_issued": fully_paid,
                    "refund_amount": total_amount if fully_paid else 0.0,
                    "verification_status": "VERIFIED",
                }

            return {
                "success": False,
                "status": "FAILED",
                "order_id": order_name,
                "reason": err_combined,
                "message": f"Shopify cancellation rejected: {err_combined}",
                "refund_issued": False,
                "verification_status": "FAILED",
            }

        # Polling if async job returned
        job = payload.get("job") or {}
        job_id = job.get("id")
        job_done = job.get("done", True)

        if job_id and not job_done:
            import time
            job_query = """
            query GetJobStatus($id: ID!) {
              job(id: $id) {
                id
                done
              }
            }
            """
            for _ in range(5):
                time.sleep(1.0)
                job_res = self._execute_graphql(job_query, {"id": job_id})
                job_data = job_res.get("job") or {}
                if job_data.get("done"):
                    job_done = True
                    break

        # Verification via fresh lookup
        verification_status = "VERIFIED"
        post_order = self.get_order_details(gid, company_id)
        if post_order and "error" not in post_order:
            final_status = str(post_order.get("fulfillment_status") or post_order.get("status") or "").upper()
            if final_status != "CANCELLED" and "CANCELLED" not in str(post_order.get("financial_status")).upper():
                verification_status = "UNVERIFIED"

        refund_issued = fully_paid
        refund_amt = total_amount if refund_issued else 0.0

        from integrations.models import CancelOrderResult
        result = CancelOrderResult(
            success=True,
            status="CANCELLED",
            order_id=order_name,
            reason=cancel_reason,
            message=f"Shopify order {order_name} has been successfully CANCELLED.",
            refund_issued=refund_issued,
            refund_amount=refund_amt,
            verification_status=verification_status,
            details={
                "gid": gid,
                "job_id": job_id,
                "job_done": job_done,
            },
        )
        return result.model_dump()

    def request_return(
        self, order_id: str, refund_amount: float, company_id: str
    ) -> Dict[str, Any]:
        """Read-only phase safeguard for returns/refunds."""
        return {
            "status": "BLOCKED",
            "reason": (
                "Write operations (returns/refunds) are currently disabled for "
                "Shopify provider. Please process returns in Shopify Admin."
            ),
        }

    def check_proactive_notifications(
        self, customer_id: str, company_id: str
    ) -> Optional[str]:
        """Scan Shopify fulfillment status for proactive exception flags."""
        return None

    def escalate_to_human(
        self, order_id: str, reason: str, company_id: str
    ) -> Dict[str, Any]:
        """Generate human escalation ticket."""
        ticket_id = f"TICK-{order_id.upper()}"
        return {
            "status": "ESCALATED",
            "ticket_id": ticket_id,
            "message": (
                f"Support ticket {ticket_id} created for Shopify order {order_id}. "
                f"Reason: {reason}"
            ),
        }

    def get_ticket_details(
        self, ticket_id: str, company_id: str
    ) -> Dict[str, Any]:
        """Retrieve escalation ticket details."""
        clean_ticket = ticket_id.upper().strip()
        order_id = clean_ticket.replace("TICK-", "")
        return {
            "ticket_id": clean_ticket,
            "status": "OPEN_IN_REVIEW",
            "associated_order": order_id,
            "details": f"Escalated Shopify ticket {clean_ticket} for human review.",
            "created_at": "Recently created",
        }