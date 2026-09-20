import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Dict, List, Optional
from uuid import uuid4

from agent import process_query
from database import execute_db, init_db, query_db
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
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
    init_db()

    query = """
        SELECT o.id, o.customer_id, s.courier 
        FROM orders o
        LEFT JOIN shipments s ON o.id = s.order_id
        WHERE o.status = 'PROACTIVE_ALERT' OR s.status = 'PROACTIVE_ALERT'
    """
    try:
        from database import scan_proactive_delays
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


class ChatRequest(BaseModel):
    user_input: str
    session_id: Optional[str] = Field(
        default=None,
        description="Unique session token for dialogue continuity",
    )


@app.post("/api/chat", tags=["Agent Operations"])
def chat_with_agent(
    payload: ChatRequest,
    x_company_id: Optional[str] = Header(default="COMP-ALPHA", alias="X-Company-ID"),
):
    """Processes natural language user prompt through LangChain Agent and records audit logs."""
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


@app.get("/ui", response_class=HTMLResponse, tags=["Web Interface"])

def serve_chat_ui():
    """Serves the web chat frontend interface with dynamic table fitting, primary blue headers, and conditional scrolling."""
    return """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Luintix | Smart Order Assistant</title>
    
    <!-- Google Fonts: Red Hat Display, Poppins, Inter -->
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Poppins:wght@400;500;600&family=Red+Hat+Display:wght@600;700&display=swap" rel="stylesheet">

    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <style>
        :root {
            --primary-blue: #2E5FEA;     
            --deep-navy: #0A0F1F;        
            --accent-blue: #4D7BF3;      
            --bg-white: #FFFFFF;         
            --chat-bg: #F8FAFC;          

            --font-heading: 'Red Hat Display', sans-serif;
            --font-body: 'Poppins', sans-serif;
            --font-utility: 'Inter', sans-serif;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }
        
        body {
            font-family: var(--font-body);
            background-color: #F1F5F9;
            color: var(--deep-navy);
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            padding: 16px;
            -webkit-font-smoothing: antialiased;
        }

        .chat-container {
            width: 100%;
            max-width: 480px; /* Slightly wider container for optimal reading */
            height: 85vh;
            background-color: var(--bg-white);
            border-radius: 24px;
            display: flex;
            flex-direction: column;
            box-shadow: 0 15px 35px rgba(10, 15, 31, 0.08);
            border: 1px solid #E2E8F0;
            overflow: hidden;
            position: relative;
        }

        .chat-header {
            background-color: var(--primary-blue);
            padding: 16px 20px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        .header-info {
            display: flex;
            flex-direction: column;
            gap: 2px;
        }

        .brand-title {
            font-family: var(--font-heading);
            font-size: 1.25rem;
            font-weight: 700;
            color: var(--bg-white);
            letter-spacing: -0.01em;
        }

        .status-badge {
            font-family: var(--font-utility);
            display: inline-flex;
            align-items: center;
            gap: 6px;
            font-size: 0.78rem;
            color: rgba(255, 255, 255, 0.95);
            font-weight: 500;
        }

        .status-dot {
            width: 8px;
            height: 8px;
            background-color: #34C759;
            border-radius: 50%;
            box-shadow: 0 0 6px #34C759;
        }

        .test-data-btn {
            background-color: rgba(255, 255, 255, 0.2);
            color: var(--bg-white);
            border: 1px solid rgba(255, 255, 255, 0.4);
            padding: 6px 12px;
            border-radius: 20px;
            font-family: var(--font-utility);
            font-size: 0.78rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s ease;
            display: flex;
            align-items: center;
            gap: 4px;
        }

        .test-data-btn:hover {
            background-color: rgba(255, 255, 255, 0.35);
        }

        .modal-overlay {
            display: none;
            position: absolute;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(10, 15, 31, 0.4);
            backdrop-filter: blur(3px);
            z-index: 100;
            justify-content: center;
            align-items: center;
            padding: 20px;
        }

        .modal-content {
            background: var(--bg-white);
            border-radius: 18px;
            width: 100%;
            max-height: 80%;
            overflow-y: auto;
            padding: 20px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.15);
            display: flex;
            flex-direction: column;
            gap: 12px;
        }

        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid #E2E8F0;
            padding-bottom: 10px;
        }

        .modal-header h3 {
            font-family: var(--font-heading);
            font-size: 1.1rem;
            color: var(--deep-navy);
        }

        .close-btn {
            background: none;
            border: none;
            font-size: 1.2rem;
            cursor: pointer;
            color: #64748B;
        }

        .sample-item {
            background: #F8FAFC;
            border: 1px solid #E2E8F0;
            border-radius: 10px;
            padding: 10px 14px;
            cursor: pointer;
            transition: all 0.2s ease;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .sample-item:hover {
            border-color: var(--primary-blue);
            background: #F0F4FF;
        }

        .sample-id {
            font-family: var(--font-utility);
            font-weight: 600;
            color: var(--primary-blue);
            font-size: 0.88rem;
        }

        .sample-desc {
            font-size: 0.8rem;
            color: #64748B;
        }
        
        .chat-box {
            flex: 1;
            padding: 16px;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 16px;
            background-color: var(--bg-white);
        }

        .message {
            max-width: 88%;
            padding: 14px 16px;
            border-radius: 18px;
            font-family: var(--font-body);
            font-size: 0.88rem;
            line-height: 1.5;
            word-wrap: break-word;
            overflow-wrap: break-word;
        }

        .user-msg {
            background-color: var(--primary-blue);
            color: var(--bg-white);
            align-self: flex-end;
            border-bottom-right-radius: 4px;
        }

        .agent-msg {
            background: var(--chat-bg);
            color: var(--deep-navy);
            align-self: flex-start;
            border-bottom-left-radius: 4px;
            border: 1px solid #E2E8F0;
            width: auto;
            max-width: 96%; /* Allow agent bubbles with tables to utilize full width */
            box-sizing: border-box;
        }

        .agent-msg .table-wrapper {
            width: 100%;
            overflow-x: auto; /* Scrollbar shows ONLY if table naturally overflows */
            margin: 12px 0 6px 0;
            border-radius: 10px;
            border: 1px solid #CBD5E1;
            background: #FFFFFF;
            box-shadow: 0 3px 10px rgba(10, 15, 31, 0.04);
            -webkit-overflow-scrolling: touch;
        }

        .agent-msg .table-wrapper::-webkit-scrollbar {
            height: 5px;
        }

        .agent-msg .table-wrapper::-webkit-scrollbar-track {
            background: #F1F5F9;
            border-radius: 10px;
        }

        .agent-msg .table-wrapper::-webkit-scrollbar-thumb {
            background: #CBD5E1;
            border-radius: 10px;
        }

        .agent-msg .table-wrapper::-webkit-scrollbar-thumb:hover {
            background: var(--primary-blue);
        }

        .agent-msg table {
            width: 100%;
            border-collapse: separate;
            border-spacing: 0;
            font-size: 0.82rem;
            font-family: var(--font-utility);
            background: #FFFFFF;
            table-layout: auto;
        }

        .agent-msg th, 
        .agent-msg td {
            padding: 10px 12px;
            text-align: left;
            vertical-align: top;
            line-height: 1.45;
            border-bottom: 1px solid #E2E8F0;
            border-right: 1px solid #E2E8F0;
        }

        .agent-msg th:last-child,
        .agent-msg td:last-child {
            border-right: none;
        }

        .agent-msg tr:last-child td {
            border-bottom: none;
        }

        .agent-msg th {
            background-color: var(--primary-blue);
            color: #FFFFFF;
            font-weight: 600;
            font-family: var(--font-heading);
            font-size: 0.83rem;
            letter-spacing: 0.01em;
            white-space: nowrap;
        }

        .agent-msg th:first-child {
            border-top-left-radius: 8px;
        }

        .agent-msg th:last-child {
            border-top-right-radius: 8px;
        }

        .agent-msg td {
            white-space: normal;
            word-break: normal;
            color: var(--deep-navy);
        }

        .agent-msg td:first-child {
            font-weight: 600;
            color: #1E293B;
            width: 28%;
        }

        .agent-msg ul, .agent-msg ol {
            margin: 6px 0 6px 18px;
            padding: 0;
        }

        .agent-msg li {
            margin-bottom: 4px;
        }

        .input-area {
            display: flex;
            padding: 16px 20px;
            background: var(--bg-white);
            border-top: 1px solid #E2E8F0;
            gap: 10px;
        }

        .input-area input[type="text"] {
            flex: 1;
            padding: 12px 16px;
            border-radius: 12px;
            border: 1.5px solid #E2E8F0;
            background: #F8FAFC;
            color: var(--deep-navy);
            font-family: var(--font-body);
            font-size: 0.9rem;
            outline: none;
        }

        .input-area input[type="text"]:focus {
            border-color: var(--primary-blue);
            background: var(--bg-white);
        }

        .input-area button {
            background-color: var(--primary-blue);
            color: var(--bg-white);
            border: none;
            padding: 12px 20px;
            border-radius: 12px;
            font-family: var(--font-body);
            font-weight: 600;
            font-size: 0.9rem;
            cursor: pointer;
        }

        .input-area button:hover {
            background-color: var(--accent-blue);
        }
    </style>
</head>
<body>

<div class="chat-container">
    <div class="chat-header">
        <div class="header-info">
            <span class="brand-title">Luintix Order Agent</span>
            <div class="status-badge">
                <span class="status-dot"></span> Active & Ready
            </div>
        </div>
        <button class="test-data-btn" onclick="toggleModal(true)"> Test Data</button>
    </div>

    <div class="modal-overlay" id="sampleModal">
        <div class="modal-content">
            <div class="modal-header">
                <h3>Select Sample Order to Test</h3>
                <button class="close-btn" onclick="toggleModal(false)">&times;</button>
            </div>
            
            <div class="sample-item" onclick="selectSample('What is the status of my order ORD-5001?')">
                <div>
                    <div class="sample-id">ORD-5001 (Wireless Headphones)</div>
                    <div class="sample-desc">Status: DELIVERED | $45.00</div>
                </div>
            </div>

            <div class="sample-item" onclick="selectSample('Can I get a refund for ORD-5002?')">
                <div>
                    <div class="sample-id">ORD-5002 (Smart Gaming Monitor)</div>
                    <div class="sample-desc">Status: DELIVERED | $250.00 (Requires Escalation)</div>
                </div>
            </div>

            <div class="sample-item" onclick="selectSample('Where is my package for order ORD-5003?')">
                <div>
                    <div class="sample-id">ORD-5003 (USB-C Hub)</div>
                    <div class="sample-desc">Status: PROACTIVE_ALERT / DELAYED</div>
                </div>
            </div>

            <div class="sample-item" onclick="selectSample('Track order ORD-5004')">
                <div>
                    <div class="sample-id">ORD-5004 (Ergonomic Keyboard)</div>
                    <div class="sample-desc">Status: PROCESSING</div>
                </div>
            </div>

            <div class="sample-item" onclick="selectSample('Check status of order ORD-5005')">
                <div>
                    <div class="sample-id">ORD-5005 (Mechanical Mouse)</div>
                    <div class="sample-desc">Status: SHIPPED / IN_TRANSIT</div>
                </div>
            </div>
        </div>
    </div>

    <div class="chat-box" id="chatBox">
        <div class="message agent-msg">
            Hello! I am <strong>Luintix</strong>, your order resolution assistant. How can I help you with your order today?
        </div>
    </div>

    <form class="input-area" id="chatForm">
        <input type="text" id="userInput" placeholder="Ask about orders (e.g. ORD-5001)..." required autocomplete="off">
        <button type="submit">Send</button>
    </form>
</div>

<script>
    marked.setOptions({ gfm: true, breaks: true });
    let sessionId = localStorage.getItem('luintix_session_id');

    function toggleModal(show) {
        document.getElementById('sampleModal').style.display = show ? 'flex' : 'none';
    }

    function selectSample(text) {
        document.getElementById('userInput').value = text;
        toggleModal(false);
        document.getElementById('chatForm').dispatchEvent(new Event('submit'));
    }

    document.getElementById('chatForm').addEventListener('submit', async function(e) {
        e.preventDefault();
        
        const inputEl = document.getElementById('userInput');
        const chatBox = document.getElementById('chatBox');
        const text = inputEl.value.trim();
        
        if (!text) return;

        const userDiv = document.createElement('div');
        userDiv.className = 'message user-msg';
        userDiv.textContent = text;
        chatBox.appendChild(userDiv);
        
        inputEl.value = '';
        chatBox.scrollTop = chatBox.scrollHeight;

        const loadingDiv = document.createElement('div');
        loadingDiv.className = 'message agent-msg';
        loadingDiv.textContent = 'Luintix is checking...';
        chatBox.appendChild(loadingDiv);
        chatBox.scrollTop = chatBox.scrollHeight;

        try {
            const res = await fetch('/api/chat', {
                method: 'POST',
                headers: { 
                    'Content-Type': 'application/json',
                    'X-Company-ID': 'COMP-ALPHA'
                },
                body: JSON.stringify({ user_input: text, session_id: sessionId })
            });

            if (!res.ok) throw new Error(`Server returned error: ${res.status}`);

            const data = await res.json();
            
            if (data.session_id) {
                sessionId = data.session_id;
                localStorage.setItem('luintix_session_id', sessionId);
            }

            let parsedHtml = marked.parse(data.agent_response || 'No response returned.');
            
            const tempDiv = document.createElement('div');
            tempDiv.innerHTML = parsedHtml;

            const tables = tempDiv.querySelectorAll('table');
            tables.forEach(table => {
                if (!table.parentElement.classList.contains('table-wrapper')) {
                    const wrapper = document.createElement('div');
                    wrapper.className = 'table-wrapper';
                    table.parentNode.insertBefore(wrapper, table);
                    wrapper.appendChild(table);
                }
            });

            loadingDiv.innerHTML = tempDiv.innerHTML;
        } catch (err) {
            loadingDiv.textContent = `Error: ${err.message || 'Could not connect to backend server.'}`;
        }
        
        chatBox.scrollTop = chatBox.scrollHeight;
    });
</script>
</body>
</html>
"""


@app.get("/", include_in_schema=False)
def root_redirect():
    return RedirectResponse(url="/ui")