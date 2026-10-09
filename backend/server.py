import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Dict, List, Optional
from uuid import uuid4
from pathlib import Path

# Ensure environment variables are loaded immediately at server startup
from backend import env_config
from backend.agent import process_query
from backend.database import execute_db, init_db, query_db
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import BaseModel, Field


# Configure Uvicorn logger
logger = logging.getLogger("uvicorn.error")


def log_terminal(msg: str):
    """Forcibly flushes logging text directly into the real terminal window."""
    sys.__stdout__.write(f"{msg}\n")
    sys.__stdout__.flush()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initializes the SQLite database and executes the proactive background scan on startup."""
    for line in env_config.provider_env_status():
        log_terminal(f"[STARTUP CONFIG]: {line}")

    init_db()

    from backend.integrations.gateway import gateway
    for line in gateway.describe_tenants():
        log_terminal(f"[STARTUP TENANTS]: {line}")

    query = """
        SELECT o.id, o.customer_id, s.courier 
        FROM orders o
        LEFT JOIN shipments s ON o.id = s.order_id
        WHERE o.status = 'PROACTIVE_ALERT' OR s.status = 'PROACTIVE_ALERT'
    """
    try:
        from backend.database import scan_proactive_delays
        scan_proactive_delays()
        log_terminal("[STARTUP SCANNER]: Proactive scanner completed and statuses updated.")
    except Exception as e:
        log_terminal(f"[STARTUP SCANNER ERROR]: {e}")

    yield

app = FastAPI(
    title="Luintix AI Order Agent API",
    description="Backend API and Interactive Web Client for Luintix Agent.",
    version="1.0.0-alpha",
    lifespan=lifespan,
)

sessions_memory: Dict[str, List[BaseMessage]] = {}

# Customer-provided identity (email/phone) remembered per (session, tenant) once it resolved
# a customer. This is NOT authentication.
sessions_identity: Dict[tuple, dict] = {}


class ChatRequest(BaseModel):
    user_input: str
    session_id: Optional[str] = Field(
        default=None,
        description="Unique session token for dialogue continuity",
    )


@app.post("/api/chat", tags=["Agent Operations"])
def chat_with_agent(
    payload: ChatRequest,
    x_company_id: Optional[str] = Header(default="COMP-SHOPIFY", alias="X-Company-ID"),
):
    """Processes natural language user prompt through LangChain Agent and records audit logs."""
    from backend.integrations.errors import TenantConfigurationError
    from backend.integrations.gateway import gateway

    try:
        # Store tenants: their store connector. Standalone logistics tenants (COMP-SHADOWFAX):
        # their provider connector. Unknown/misconfigured tenants still fail here.
        gateway.get_tenant_primary_connector(x_company_id)
    except TenantConfigurationError as e:
        log_terminal(f"[TENANT CONFIG ERROR] (Tenant: {x_company_id}): {e.message}")
        raise HTTPException(
            status_code=400,
            detail=f"Store '{x_company_id}' is not configured. Please select a configured store.",
        )

    session_id = payload.session_id or str(uuid4())
    if session_id not in sessions_memory:
        sessions_memory[session_id] = []

    history = sessions_memory[session_id]

    try:
        log_terminal(
            f"\n---> [USER QUERY RECEIVED] (Session: {session_id[:8]} | Tenant: {x_company_id}): '{payload.user_input}'"
        )

        raw_response = process_query(
            payload.user_input,
            history_messages=history,
            company_id=x_company_id,
            session_id=session_id,
            # The demo UI has no customer login, so there is no verified customer ID.
            # Wire a real authenticated session here (e.g. a signed storefront customer token).
            authenticated_customer_id=None,
            session_identity=sessions_identity.setdefault((session_id, x_company_id), {}),
        )

        if isinstance(raw_response, dict):
            response_text = str(raw_response.get("output", raw_response))
        else:
            response_text = str(raw_response)

        history.append(HumanMessage(content=payload.user_input))
        history.append(AIMessage(content=response_text))

        if len(history) > 10:
            sessions_memory[session_id] = history[-10:]

        log_terminal(f"---> [AGENT RESPONSE]: {response_text}")

        log_id = execute_db(
            "INSERT INTO audit_logs (company_id, user_input, agent_response) VALUES (?, ?, ?)",
            (x_company_id, payload.user_input, response_text),
        )

        log_terminal(
            f"---> [DATABASE SUCCESS]: Recorded in audit_logs with ID #{log_id}\n"
        )

        return {"agent_response": response_text, "session_id": session_id}

    except Exception as e:
        log_terminal(f"[SERVER ERROR]: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/orders/{order_id}", tags=["Database Views"])
def get_order_by_id(
    order_id: str,
    x_company_id: Optional[str] = Header(default="COMP-ALPHA", alias="X-Company-ID"),
):
    """Retrieves order details for a specific order ID and tenant."""
    order = query_db(
        "SELECT * FROM orders WHERE id = ? AND company_id = ?",
        (order_id.upper(), x_company_id),
        one=True,
    )
    if not order:
        raise HTTPException(
            status_code=404, detail=f"Order {order_id} not found."
        )
    items = query_db(
        "SELECT product_name, quantity, price FROM order_items WHERE order_id = ?",
        (order_id.upper(),),
    )
    order["items"] = items
    return order


@app.get("/api/logs", tags=["Database Views"])
def get_audit_logs():
    """Returns stored audit logs from SQLite database."""
    logs = query_db("SELECT * FROM audit_logs ORDER BY id DESC")
    return {"total": len(logs), "logs": logs}


@app.post("/api/webhooks/woocommerce", tags=["Webhooks"])
async def woocommerce_webhook_handler(request: Request):
    """
    WooCommerce webhook receiver. Tenant is resolved from X-WC-Webhook-Source, the HMAC
    signature is always required, and deliveries are deduplicated (see integrations/webhooks.py).
    """
    from backend.integrations.webhooks import process_woocommerce_webhook

    body_bytes = await request.body()
    status_code, result = process_woocommerce_webhook(dict(request.headers), body_bytes)
    if status_code != 200:
        raise HTTPException(status_code=status_code, detail=result.get("detail"))
    return result


@app.post("/api/webhooks/shippo/{company_id}", tags=["Webhooks"])
async def shippo_webhook_handler(company_id: str, request: Request, background_tasks: BackgroundTasks):
    """
    Shippo track_updated receiver. Authenticates the RAW body (HMAC or URL token), stores
    the event, returns 2xx immediately and normalizes it in the background
    (see integrations/shippo_webhooks.py).
    """
    from backend.integrations.shippo_webhooks import process_tracking_event, receive_shippo_webhook

    raw_body = await request.body()
    status_code, result, event_id = receive_shippo_webhook(
        company_id, dict(request.headers), raw_body, url_token=request.query_params.get("token")
    )
    if status_code >= 400:
        raise HTTPException(status_code=status_code, detail=result.get("detail"))
    if event_id:
        background_tasks.add_task(process_tracking_event, event_id)
    return result


FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app.mount(
    "/frontend",
    StaticFiles(directory=FRONTEND_DIR),
    name="frontend",
)


@app.get("/ui", tags=["Web Interface"])
def serve_chat_ui():
    """Serves the separated frontend interface."""
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/", include_in_schema=False)
def root_redirect():
    return RedirectResponse(url="/ui")
