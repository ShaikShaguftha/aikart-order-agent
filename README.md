# AIkart Order Resolution Agent

An AI-powered customer support agent built to resolve order tracking inquiries, process cancellations/returns, explain store policies, and proactively flag shipping delays.

## Key Features

* **Automated Customer Support:** Handles natural language queries regarding orders, shipments, returns, and store policies using LangChain.
* **Multi-Turn Conversation Memory:** Maintains context across continuous customer interactions.
* **Proactive Delay Detection:** Automatically checks the database on startup to identify delayed shipments and issue reshipment vouchers.
* **Full-Stack Execution:** Built with a FastAPI backend, an interactive embedded HTML/CSS web UI, and a SQLite database.
* **Audit Logging:** Logs all customer-agent interactions to SQLite for operations and compliance tracking.

## Tech Stack

* **Backend Framework:** FastAPI (Python)
* **AI & Agent Orchestration:** LangChain
* **Database:** SQLite
* **Frontend:** HTML, CSS, JavaScript (Embedded in FastAPI)

## Database Schema Overview

The application relies on a SQLite database (`aikart.db`) containing:
* `orders`: Order details and current status (`PROCESSING`, `DELIVERED`, `PROACTIVE_ALERT`).
* `shipments`: Tracking status and courier information.
* `order_items`: Purchased product lists and prices.
* `audit_logs`: Query and response logs for compliance.

## Setup and Installation

1. **Clone the Repository:**
   ```bash
   git clone https://github.com/ShaikShaguftha/aikart-order-agent
