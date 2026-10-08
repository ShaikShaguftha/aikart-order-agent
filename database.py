import os
import sqlite3
from typing import Any, Dict, List, Optional

DB_FILE = "orders.db"

DEFAULT_TENANTS = [
    ("COMP-ALPHA", "Alpha Logistics", "LOCAL_DB"),
    ("COMP-SHOPIFY", "Shopify Merchant Store", "SHOPIFY"),
    ("COMP-WOOCOMMERCE", "WooCommerce Store", "WOOCOMMERCE"),
    ("COMP-ZOHO", "Zoho CRM Tenant", "ZOHO_CRM"),
    ("COMP-SHADOWFAX", "Shadowfax Logistics", "SHADOWFAX"),
    ("COMP-HUBSPOT", "HubSpot CRM Tenant", "HUBSPOT"),
    ("COMP-SALESFORCE", "Salesforce CRM Tenant", "SALESFORCE"),
    ("COMP-RAZORPAY", "Razorpay Payments Tenant", "RAZORPAY"),
]


def get_db_connection() -> sqlite3.Connection:
    """Creates and returns a connection to the SQLite database with dictionary access and FKs enabled."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db() -> None:
    """Initializes the database schema and seeds initial data."""
    conn = get_db_connection()
    cursor = conn.cursor()

    # 1. Companies Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS companies (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            tier TEXT NOT NULL DEFAULT 'STANDARD'
        )
    """)

    # 2. Company Rules Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS company_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL,
            max_auto_refund_amount REAL NOT NULL DEFAULT 100.0,
            return_window_days INTEGER NOT NULL DEFAULT 30,
            FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
        )
    """)

    # 3. Orders Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            customer_id TEXT NOT NULL,
            status TEXT NOT NULL,
            total REAL NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
        )
    """)

    # 4. Order Items Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT NOT NULL,
            product_name TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            price REAL NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
        )
    """)

    # 5. Shipments Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS shipments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT NOT NULL,
            courier TEXT NOT NULL,
            tracking_number TEXT NOT NULL,
            status TEXT NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
        )
    """)

    # 6. Audit Logs Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT DEFAULT 'COMP-ALPHA',
            user_input TEXT NOT NULL,
            agent_response TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 7. Action Audit Logs Table for Safe Action Framework
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS action_audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            company_id TEXT NOT NULL,
            order_id TEXT NOT NULL,
            action_type TEXT NOT NULL,
            provider TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT,
            refund_issued INTEGER NOT NULL DEFAULT 0,
            details_json TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 8. Company Integrations Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS company_integrations (
            company_id TEXT PRIMARY KEY,
            provider_type TEXT NOT NULL,
            shop_domain TEXT,
            access_token TEXT,
            api_version TEXT DEFAULT '2024-04',
            consumer_key TEXT,
            consumer_secret TEXT,
            FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
        )
    """)

    # 9. Support tickets (provider-neutral human escalations)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS support_tickets (
            ticket_id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            order_id TEXT,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            source TEXT NOT NULL DEFAULT 'AGENT',
            session_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 10. Return requests recorded by Luintix for providers without a native returns API
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS return_requests (
            return_id TEXT PRIMARY KEY,
            company_id TEXT NOT NULL,
            order_id TEXT NOT NULL,
            requested_amount REAL NOT NULL,
            reason TEXT,
            status TEXT NOT NULL,
            ticket_id TEXT,
            session_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 11. Inbound provider webhook deliveries (deduplicated per tenant)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS webhook_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            delivery_id TEXT,
            topic TEXT NOT NULL,
            event_type TEXT NOT NULL,
            resource_id TEXT,
            order_number TEXT,
            status TEXT,
            refund_total REAL,
            received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (company_id, provider, delivery_id)
        )
    """)

    # 12. Helpdesk integrations (e.g. Freshdesk). Separate from company_integrations so a
    # tenant can use a store provider AND a helpdesk; credentials are stored encrypted.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS helpdesk_integrations (
            company_id TEXT PRIMARY KEY,
            provider_type TEXT NOT NULL,
            domain TEXT,
            api_key TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
        )
    """)

    # 13. Shipping/tracking integrations (e.g. Shippo). Separate from store and helpdesk so a
    # tenant can combine all three. Token and webhook secret are stored encrypted.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS shipping_integrations (
            company_id TEXT PRIMARY KEY,
            provider_type TEXT NOT NULL,
            environment TEXT NOT NULL DEFAULT 'test',
            api_token TEXT,
            webhook_secret TEXT,
            webhook_auth_mode TEXT NOT NULL DEFAULT 'hmac',
            allow_tracking_registration INTEGER NOT NULL DEFAULT 0,
            credential_status TEXT NOT NULL DEFAULT 'ACTIVE',
            last_verified_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
        )
    """)

    # 14. Tracking registrations: the record is claimed BEFORE calling the provider so a
    # tracking number is never registered twice for the same merchant (Shippo webhooks
    # are not idempotent).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS shipment_tracking_registrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            environment TEXT NOT NULL,
            carrier_slug TEXT NOT NULL,
            tracking_number TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error_code TEXT,
            order_reference TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (company_id, provider, environment, carrier_slug, tracking_number)
        )
    """)

    # 15. Raw inbound tracking webhook events (deduplicated; kept for replay/audit).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tracking_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            event_key TEXT NOT NULL,
            carrier_slug TEXT,
            tracking_number TEXT,
            raw_payload TEXT NOT NULL,
            processing_status TEXT NOT NULL DEFAULT 'RECEIVED',
            processing_error TEXT,
            received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            processed_at TIMESTAMP,
            UNIQUE (company_id, provider, event_key)
        )
    """)

    # 16. Latest normalized tracking state per shipment (the shipment timeline head).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS shipment_tracking_state (
            company_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            carrier_slug TEXT NOT NULL,
            tracking_number TEXT NOT NULL,
            status TEXT NOT NULL,
            substatus TEXT,
            action_required INTEGER NOT NULL DEFAULT 0,
            estimated_delivery TEXT,
            current_location TEXT,
            source_updated_at TEXT,
            last_event_id INTEGER,
            alert TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (company_id, provider, carrier_slug, tracking_number)
        )
    """)

    # 17. Direct carrier integrations (e.g. Delhivery). Keyed by tenant AND provider so a tenant
    # can combine an aggregator (Shippo, shipping_integrations) with direct carriers.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS carrier_integrations (
            company_id TEXT NOT NULL,
            provider_type TEXT NOT NULL,
            environment TEXT NOT NULL DEFAULT 'production',
            base_url TEXT,
            api_token TEXT,
            auth_header TEXT,
            auth_scheme TEXT,
            credential_status TEXT NOT NULL DEFAULT 'ACTIVE',
            last_verified_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (company_id, provider_type),
            FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
        )
    """)

    # Schema migration check for existing DB files
    try:
        cursor.execute("ALTER TABLE company_integrations ADD COLUMN webhook_secret TEXT;")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE company_integrations ADD COLUMN consumer_key TEXT;")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE company_integrations ADD COLUMN consumer_secret TEXT;")
    except Exception:
        pass

    # Seed Sample Data if empty
    cursor.execute("SELECT COUNT(*) FROM companies")
    if cursor.fetchone()[0] == 0:
        cursor.execute(
            "INSERT INTO companies (id, name, tier) VALUES ('COMP-ALPHA', 'Alpha Logistics', 'ENTERPRISE')"
        )
        cursor.execute(
            "INSERT INTO company_rules (company_id, max_auto_refund_amount, return_window_days) VALUES ('COMP-ALPHA', 100.0, 30)"
        )
        cursor.execute(
            "INSERT INTO company_integrations (company_id, provider_type) VALUES ('COMP-ALPHA', 'LOCAL_DB')"
        )

        cursor.execute(
            "INSERT INTO companies (id, name, tier) VALUES ('COMP-SHOPIFY', 'Shopify Merchant Store', 'ENTERPRISE')"
        )
        cursor.execute(
            "INSERT INTO company_rules (company_id, max_auto_refund_amount, return_window_days) VALUES ('COMP-SHOPIFY', 100.0, 30)"
        )
        cursor.execute(
            "INSERT INTO company_integrations (company_id, provider_type) VALUES ('COMP-SHOPIFY', 'SHOPIFY')"
        )

        cursor.execute(
            "INSERT INTO companies (id, name, tier) VALUES ('COMP-WOOCOMMERCE', 'WooCommerce Store', 'ENTERPRISE')"
        )
        cursor.execute(
            "INSERT INTO company_rules (company_id, max_auto_refund_amount, return_window_days) VALUES ('COMP-WOOCOMMERCE', 100.0, 30)"
        )
        cursor.execute(
            "INSERT INTO company_integrations (company_id, provider_type) VALUES ('COMP-WOOCOMMERCE', 'WOOCOMMERCE')"
        )

        cursor.execute(
            "INSERT INTO companies (id, name, tier) VALUES ('COMP-SHADOWFAX', 'Shadowfax Logistics', 'ENTERPRISE')"
        )
        cursor.execute(
            "INSERT INTO company_rules (company_id, max_auto_refund_amount, return_window_days) VALUES ('COMP-SHADOWFAX', 100.0, 30)"
        )
        cursor.execute(
            "INSERT INTO carrier_integrations (company_id, provider_type) VALUES ('COMP-SHADOWFAX', 'SHADOWFAX')"
        )


        # ORD-5001
        cursor.execute(
            "INSERT INTO orders (id, company_id, customer_id, status, total) VALUES ('ORD-5001', 'COMP-ALPHA', 'CUST-101', 'DELIVERED', 45.0)"
        )
        cursor.execute(
            "INSERT INTO order_items (order_id, product_name, quantity, price) VALUES ('ORD-5001', 'Wireless Headphones', 1, 45.0)"
        )
        cursor.execute(
            "INSERT INTO shipments (order_id, courier, tracking_number, status) VALUES ('ORD-5001', 'FedEx', 'TRK-9001', 'DELIVERED')"
        )

        # ORD-5002
        cursor.execute(
            "INSERT INTO orders (id, company_id, customer_id, status, total) VALUES ('ORD-5002', 'COMP-ALPHA', 'CUST-102', 'PROCESSING', 250.0)"
        )
        cursor.execute(
            "INSERT INTO order_items (order_id, product_name, quantity, price) VALUES ('ORD-5002', 'Smart Gaming Monitor', 1, 250.0)"
        )
        cursor.execute(
            "INSERT INTO shipments (order_id, courier, tracking_number, status) VALUES ('ORD-5002', 'UPS', 'TRK-9002', 'LABEL_CREATED')"
        )

        # ORD-5003
        cursor.execute(
            "INSERT INTO orders (id, company_id, customer_id, status, total) VALUES ('ORD-5003', 'COMP-ALPHA', 'CUST-103', 'PROACTIVE_ALERT', 35.0)"
        )
        cursor.execute(
            "INSERT INTO order_items (order_id, product_name, quantity, price) VALUES ('ORD-5003', 'USB-C Hub', 1, 35.0)"
        )
        cursor.execute(
            "INSERT INTO shipments (order_id, courier, tracking_number, status) VALUES ('ORD-5003', 'DHL', 'TRK-9003', 'DELAYED')"
        )

        # ORD-5004
        cursor.execute(
            "INSERT INTO orders (id, company_id, customer_id, status, total) VALUES ('ORD-5004', 'COMP-ALPHA', 'CUST-104', 'PROCESSING', 80.0)"
        )
        cursor.execute(
            "INSERT INTO order_items (order_id, product_name, quantity, price) VALUES ('ORD-5004', 'Ergonomic Keyboard', 1, 80.0)"
        )
        cursor.execute(
            "INSERT INTO shipments (order_id, courier, tracking_number, status) VALUES ('ORD-5004', 'FedEx', 'TRK-9004', 'LABEL_CREATED')"
        )

        # ORD-5005
        cursor.execute(
            "INSERT INTO orders (id, company_id, customer_id, status, total) VALUES ('ORD-5005', 'COMP-ALPHA', 'CUST-105', 'SHIPPED', 120.0)"
        )
        cursor.execute(
            "INSERT INTO order_items (order_id, product_name, quantity, price) VALUES ('ORD-5005', 'Mechanical Mouse', 1, 120.0)"
        )
        cursor.execute(
            "INSERT INTO shipments (order_id, courier, tracking_number, status) VALUES ('ORD-5005', 'UPS', 'TRK-9005', 'IN_TRANSIT')"
        )

    # Ensure every known tenant has a company + integration row, including DBs
    # created before company_integrations existed. Existing rows are never overwritten.
    for company_id, name, provider_type in DEFAULT_TENANTS:
        cursor.execute(
            "INSERT OR IGNORE INTO companies (id, name, tier) VALUES (?, ?, 'ENTERPRISE')",
            (company_id, name),
        )
        cursor.execute(
            "INSERT OR IGNORE INTO company_integrations (company_id, provider_type) VALUES (?, ?)",
            (company_id, provider_type),
        )
        cursor.execute("SELECT 1 FROM company_rules WHERE company_id = ?", (company_id,))
        if not cursor.fetchone():
            cursor.execute(
                "INSERT INTO company_rules (company_id, max_auto_refund_amount, return_window_days) VALUES (?, 100.0, 30)",
                (company_id,),
            )

    conn.commit()
    conn.close()


def query_db(query: str, args: tuple = (), one: bool = False) -> Any:
    """Executes a SQL query and returns dictionary formatted results."""
    conn = get_db_connection()
    try:
        r = conn.execute(query, args).fetchall()
    finally:
        conn.close()
    results = [dict(row) for row in r]
    return (results[0] if results else None) if one else results


def execute_db(query: str, args: tuple = ()) -> int:
    """Executes an INSERT, UPDATE, or DELETE query and returns lastrowid."""
    # Always close: a failed statement (e.g. IntegrityError) must not leave the
    # connection holding the database write lock.
    conn = get_db_connection()
    try:
        cursor = conn.execute(query, args)
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_order_details(
    order_id: str, company_id: str = "COMP-ALPHA"
) -> Optional[Dict[str, Any]]:
    """Retrieves full order details including line items and shipment data."""
    order = query_db(
        "SELECT * FROM orders WHERE id = ? AND company_id = ?",
        (order_id.upper(), company_id),
        one=True,
    )
    if not order:
        return None

    items = query_db(
        "SELECT product_name, quantity, price FROM order_items WHERE order_id = ?",
        (order_id.upper(),),
    )
    shipment = query_db(
        "SELECT courier, tracking_number, status FROM shipments WHERE order_id = ?",
        (order_id.upper(),),
        one=True,
    )

    order["items"] = items
    order["shipment"] = shipment
    return order


def get_company_rules(
    company_id: str = "COMP-ALPHA",
) -> Optional[Dict[str, Any]]:
    """Retrieves refund policy rules for a given company."""
    return query_db(
        "SELECT max_auto_refund_amount, return_window_days FROM company_rules WHERE company_id = ?",
        (company_id,),
        one=True,
    )


def scan_proactive_delays() -> None:
    """Scans for proactive alerts, runs agent resolution, updates order status, and logs audit."""
    from agent import process_query

    flagged = query_db("SELECT * FROM orders WHERE status = 'PROACTIVE_ALERT'")
    for order in flagged:
        prompt = (
            f"Order {order['id']} is delayed. Generate an automated proactive "
            "reshipment resolution."
        )

        response = process_query(prompt, company_id=order["company_id"])
        execute_db(
            "INSERT INTO audit_logs (company_id, user_input, agent_response) VALUES (?, ?, ?)",
            (order["company_id"], prompt, str(response)),
        )
        execute_db(
            "UPDATE orders SET status = 'PROACTIVE_RESOLVED' WHERE id = ?",
            (order["id"],),
        )


if __name__ == "__main__":
    init_db()