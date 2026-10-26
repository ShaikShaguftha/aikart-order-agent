import logging
import os
import smtplib
import sys
from contextlib import asynccontextmanager
from email.message import EmailMessage
from typing import Dict, List, Optional
from uuid import uuid4
from pathlib import Path

# Ensure environment variables are loaded immediately at server startup
from backend import env_config
from backend.agent import process_query
from backend.database import execute_db, init_db, query_db
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import BaseModel, Field


# Configure Uvicorn logger
logger = logging.getLogger("uvicorn.error")

def log_terminal(msg: str) -> None:
    try:
        stream = sys.__stdout__ or sys.stdout
        stream.write(f"{msg}\n")
        stream.flush()
    except (OSError, ValueError, AttributeError):
        try:
            sys.stdout.write(f"{msg}\n")
            sys.stdout.flush()
        except Exception:
            pass  # never let logging crash a request


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


MAX_CHAT_INPUT_CHARS = 1000
MAX_REQUESTS_PER_SESSION = 50

session_request_counts: Dict[str, int] = {}


@app.post("/api/chat", tags=["Agent Operations"])
def chat_with_agent(
    request: Request,
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

    origin = request.headers.get("origin", "")
    allowed_origins = env_config.get_allowed_origins_for_tenant(x_company_id)

    if origin and origin not in allowed_origins:
        log_terminal(f"[DOMAIN GUARD]: Blocked origin '{origin}' for tenant '{x_company_id}'")
        raise HTTPException(
            status_code=403,
            detail="This frontend origin is not authorized for the selected store.",
        )

    if not payload.user_input or not payload.user_input.strip():
        raise HTTPException(status_code=400, detail="Please enter a valid question.")

    if len(payload.user_input) > MAX_CHAT_INPUT_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"Input too long. Maximum allowed length is {MAX_CHAT_INPUT_CHARS} characters.",
        )

    session_id = payload.session_id or str(uuid4())
    session_request_counts[session_id] = session_request_counts.get(session_id, 0) + 1

    if session_request_counts[session_id] > MAX_REQUESTS_PER_SESSION:
        raise HTTPException(
            status_code=429,
            detail="Too many requests in this session. Please start a new session.",
        )

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
            authenticated_customer_id="CUST-101",
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


class DemoRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    storeUrl: str = Field(min_length=1, max_length=500)
    platform: List[str] = Field(default_factory=list, max_length=20)
    volume: str = Field(default="", max_length=50)
    painPoint: str = Field(default="", max_length=100)


DEMO_NOTIFY_TO = os.getenv("DEMO_NOTIFY_TO", "support@aikart.co")


def send_demo_email(request_id: int, payload: DemoRequest) -> None:
    """Emails a Book-a-Demo submission to the team via SMTP.

    Needs SMTP_HOST, SMTP_USER and SMTP_PASSWORD in the environment (SMTP_PORT
    defaults to 587 / STARTTLS). Without them the request is still saved in
    demo_requests, just not emailed.
    """
    host = os.getenv("SMTP_HOST")
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    if not (host and user and password):
        log_terminal(f"[BOOK DEMO]: SMTP not configured - request #{request_id} saved to DB only.")
        return

    msg = EmailMessage()
    msg["Subject"] = f"New Book a Demo request - {payload.email}"
    msg["From"] = os.getenv("SMTP_FROM", user)
    msg["To"] = DEMO_NOTIFY_TO
    msg["Reply-To"] = payload.email
    msg.set_content(
        "New Book a Demo request from the Luintix landing page.\n\n"
        f"Email: {payload.email}\n"
        f"Store URL: {payload.storeUrl}\n"
        f"Platform: {', '.join(payload.platform) or '-'}\n"
        f"Monthly orders: {payload.volume or '-'}\n"
        f"Biggest support headache: {payload.painPoint or '-'}\n"
    )

    try:
        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=20) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
        execute_db("UPDATE demo_requests SET emailed = 1 WHERE id = ?", (request_id,))
        log_terminal(f"[BOOK DEMO]: Request #{request_id} emailed to {DEMO_NOTIFY_TO}.")
    except Exception as e:
        log_terminal(f"[BOOK DEMO EMAIL ERROR]: Request #{request_id}: {e}")


@app.post("/api/book-demo", tags=["Landing Page"])
def book_demo(payload: DemoRequest, background_tasks: BackgroundTasks):
    """Stores a Book-a-Demo form submission and emails it to the team in the background."""
    request_id = execute_db(
        "INSERT INTO demo_requests (email, store_url, platform, volume, pain_point) VALUES (?, ?, ?, ?, ?)",
        (payload.email, payload.storeUrl, ", ".join(payload.platform), payload.volume, payload.painPoint),
    )
    log_terminal(f"[BOOK DEMO]: Saved request #{request_id} from {payload.email}")
    background_tasks.add_task(send_demo_email, request_id, payload)
    return {"ok": True, "id": request_id}


@app.get("/api/demo-requests", tags=["Database Views"])
def get_demo_requests():
    """Returns stored Book-a-Demo submissions, newest first."""
    rows = query_db("SELECT * FROM demo_requests ORDER BY id DESC")
    return {"total": len(rows), "requests": rows}


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


LANDING_DIR = FRONTEND_DIR / "landing"


# Prospect brand demo pages: /zouk, /thebearhouse, ... all serve one template
# (frontend/landing/brands/brand.html) that shows brands/shots/<slug>.jpg with
# the chat widget on top. Keep in sync with BRANDS in brand.html.
BRAND_PAGES = (
    "xyxxapparels", "thebearhouse", "consciouschemist", "countrybean", "godesi",
    "thejuneshop", "ragecoffee", "beco", "foxtale", "chemistatplay",
    "zouk", "neemans", "superbottoms", "dailyobjects", "stuffcool",
    "portronics", "koskii", "sleepycat", "headsupfortails", "dotandkey",
    "bewakoof",
)


def serve_brand_page():
    return FileResponse(LANDING_DIR / "brands" / "brand.html")


for _slug in BRAND_PAGES:
    app.add_api_route(f"/{_slug}", serve_brand_page, methods=["GET"], include_in_schema=False)
    app.add_api_route(f"/{_slug}/", serve_brand_page, methods=["GET"], include_in_schema=False)


# Marketing landing page (frontend/landing/index.html + assets) at the site root.
# Mounted last so every API/UI route above wins first and anything else
# (/, /style.css, /chat-widget.js, /assets/..., /brands/shots/...) falls through
# to the landing files.
app.mount("/", StaticFiles(directory=LANDING_DIR, html=True), name="landing")
