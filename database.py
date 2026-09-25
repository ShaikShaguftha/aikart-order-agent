import os
import sqlite3
from typing import Any, Dict, List, Optional

DB_FILE = "orders.db"


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

    # Seed Sample Data if empty
    cursor.execute("SELECT COUNT(*) FROM companies")
    if cursor.fetchone()[0] == 0:
        cursor.execute(
            "INSERT INTO companies (id, name, tier) VALUES ('COMP-ALPHA', 'Alpha Logistics', 'ENTERPRISE')"
        )
        cursor.execute(
            "INSERT INTO company_rules (company_id, max_auto_refund_amount, return_window_days) VALUES ('COMP-ALPHA', 100.0, 30)"
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
            "INSERT INTO orders (id, company_id, customer_id, status, total) VALUES ('ORD-5002', 'COMP-ALPHA', 'CUST-102', 'DELIVERED', 250.0)"
        )
        cursor.execute(
            "INSERT INTO order_items (order_id, product_name, quantity, price) VALUES ('ORD-5002', 'Smart Gaming Monitor', 1, 250.0)"
        )
        cursor.execute(
            "INSERT INTO shipments (order_id, courier, tracking_number, status) VALUES ('ORD-5002', 'UPS', 'TRK-9002', 'DELIVERED')"
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

    conn.commit()
    conn.close()


def query_db(query: str, args: tuple = (), one: bool = False) -> Any:
    """Executes a SQL query and returns dictionary formatted results."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(query, args)
    r = cursor.fetchall()
    conn.close()
    results = [dict(row) for row in r]
    return (results[0] if results else None) if one else results


def execute_db(query: str, args: tuple = ()) -> int:
    """Executes an INSERT, UPDATE, or DELETE query and returns lastrowid."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(query, args)
    conn.commit()
    last_id = cursor.lastrowid
    conn.close()
    return last_id


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