import sqlite3
from datetime import datetime

DB_NAME = "aikart_order_agent.db"

def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def query_db(query, args=(), one=False):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(query, args)
    rv = cur.fetchall()
    conn.close()
    return (dict(rv[0]) if rv else None) if one else [dict(r) for r in rv]

def execute_db(query, args=()):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(query, args)
    conn.commit()
    last_id = cur.lastrowid
    conn.close()
    return last_id

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.executescript("""
        CREATE TABLE IF NOT EXISTS customers (
            id TEXT PRIMARY KEY,
            name TEXT,
            email TEXT,
            phone TEXT
        );

        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            customer_id TEXT,
            status TEXT,
            total REAL,
            ordered_at TEXT
        );

        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT,
            product_name TEXT,
            quantity INTEGER,
            price REAL
        );

        CREATE TABLE IF NOT EXISTS shipments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT,
            courier TEXT,
            tracking_number TEXT,
            status TEXT,
            eta TEXT
        );

        CREATE TABLE IF NOT EXISTS cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id TEXT,
            order_id TEXT,
            intent TEXT,
            status TEXT,
            resolution TEXT
        );

        CREATE TABLE IF NOT EXISTS action_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER,
            action TEXT,
            result TEXT,
            timestamp TEXT
        );

        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_input TEXT NOT NULL,
            agent_response TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)

    cur.execute("SELECT COUNT(*) FROM orders")
    if cur.fetchone()[0] == 0:
        now_iso = datetime.now().isoformat()

        # Customers
        cur.execute("INSERT INTO customers VALUES ('CUST-101', 'Alex Johnson', 'alex@example.com', '+1234567890')")
        cur.execute("INSERT INTO customers VALUES ('CUST-102', 'Sarah Smith', 'sarah@example.com', '+1987654321')")
        cur.execute("INSERT INTO customers VALUES ('CUST-103', 'Michael Brown', 'michael@example.com', '+1122334455')")
        cur.execute("INSERT INTO customers VALUES ('CUST-104', 'Emily Davis', 'emily@example.com', '+1555666777')")

        # Orders
        cur.execute("INSERT INTO orders VALUES ('ORD-5001', 'CUST-101', 'PROCESSING', 150.00, ?)", (now_iso,))
        cur.execute("INSERT INTO orders VALUES ('ORD-5002', 'CUST-101', 'DELIVERED', 450.00, '2026-08-20T10:00:00')")
        cur.execute("INSERT INTO orders VALUES ('ORD-5003', 'CUST-101', 'PROCESSING', 80.00, '2026-08-25T14:30:00')")
        cur.execute("INSERT INTO orders VALUES ('ORD-5004', 'CUST-103', 'DELAYED', 300.00, '2026-08-28T09:15:00')")
        cur.execute("INSERT INTO orders VALUES ('ORD-5005', 'CUST-104', 'PROACTIVE_ALERT', 40.00, '2026-09-01T11:00:00')")

        # Items
        cur.execute("INSERT INTO order_items (order_id, product_name, quantity, price) VALUES ('ORD-5001', 'Wireless Headphones', 1, 150.00)")
        cur.execute("INSERT INTO order_items (order_id, product_name, quantity, price) VALUES ('ORD-5002', 'Smart Watch', 1, 450.00)")
        cur.execute("INSERT INTO order_items (order_id, product_name, quantity, price) VALUES ('ORD-5003', 'Bluetooth Speaker', 1, 80.00)")
        cur.execute("INSERT INTO order_items (order_id, product_name, quantity, price) VALUES ('ORD-5004', 'Gaming Monitor', 1, 300.00)")
        cur.execute("INSERT INTO order_items (order_id, product_name, quantity, price) VALUES ('ORD-5005', 'USB-C Hub', 1, 40.00)")

        # Shipments
        cur.execute("INSERT INTO shipments (order_id, courier, tracking_number, status, eta) VALUES ('ORD-5001', 'DHL', 'TRACK-999', 'IN_TRANSIT', '2026-09-08')")
        cur.execute("INSERT INTO shipments (order_id, courier, tracking_number, status, eta) VALUES ('ORD-5002', 'SMSA', 'TRACK-111', 'DELIVERED', '2026-08-27')")
        cur.execute("INSERT INTO shipments (order_id, courier, tracking_number, status, eta) VALUES ('ORD-5003', 'Aramex', 'TRACK-222', 'DELAYED', '2026-09-10')")
        cur.execute("INSERT INTO shipments (order_id, courier, tracking_number, status, eta) VALUES ('ORD-5004', 'Aramex', 'TRACK-777', 'DELAYED', '2026-09-12')")
        cur.execute("INSERT INTO shipments (order_id, courier, tracking_number, status, eta) VALUES ('ORD-5005', 'UPS', 'TRACK-666', 'PROACTIVE_ALERT', '2026-09-15')")

    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    print("Database initialized successfully.")