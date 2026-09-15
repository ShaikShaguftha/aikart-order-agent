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
    """Serves the web chat frontend interface."""
    return """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Luintix | Smart Order Assistant</title>
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background: linear-gradient(135deg, #e0f2fe 0%, #e0e7ff 50%, #f3e8ff 100%);
            color: #1e1b4b;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            padding: 16px;
        }
        .chat-container {
            width: 100%;
            max-width: 460px;
            height: 85vh;
            background: linear-gradient(180deg, #f0f9ff 0%, #f5f3ff 100%);
            border-radius: 24px;
            display: flex;
            flex-direction: column;
            box-shadow: 0 20px 40px rgba(125, 211, 252, 0.3), 0 10px 25px rgba(192, 132, 252, 0.2);
            overflow: hidden;
            border: 1px solid rgba(186, 230, 253, 0.8);
        }
        .chat-header {
            background: linear-gradient(135deg, #a5f3fc 0%, #c084fc 100%);
            padding: 18px 22px;
            display: flex;
            align-items: center;
            box-shadow: 0 4px 15px rgba(165, 243, 252, 0.4);
        }
        .header-info { display: flex; flex-direction: column; }
        .brand-title {
            font-size: 1.35rem;
            font-weight: 500;
            color: #2e1065;
            letter-spacing: -0.01em;
        }
        .chat-box {
            flex: 1;
            padding: 20px;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 14px;
            background: rgba(255, 255, 255, 0.4);
        }
        .message {
            max-width: 86%;
            padding: 12px 16px;
            border-radius: 18px;
            font-size: 0.92rem;
            line-height: 1.5;
            word-wrap: break-word;
        }
        .user-msg {
            background: linear-gradient(135deg, #38bdf8 0%, #818cf8 100%);
            color: #ffffff;
            align-self: flex-end;
            border-bottom-right-radius: 4px;
            box-shadow: 0 4px 12px rgba(56, 189, 248, 0.25);
        }
        .agent-msg {
            background: linear-gradient(135deg, #e0f2fe 0%, #f3e8ff 100%);
            color: #1e1b4b;
            align-self: flex-start;
            border-bottom-left-radius: 4px;
            border: 1px solid #c7d2fe;
            box-shadow: 0 2px 8px rgba(147, 51, 234, 0.05);
        }
        
        .agent-msg p { margin-bottom: 8px; }
        .agent-msg p:last-child { margin-bottom: 0; }
        .agent-msg strong { color: #6366f1; font-weight: 600; }
        .agent-msg table {
            width: 100%;
            border-collapse: collapse;
            margin: 10px 0;
            font-size: 0.82rem;
            background: rgba(255, 255, 255, 0.7);
            border-radius: 8px;
            overflow: hidden;
            width: 100% !important;
            max-width: 100% !important;
            table-layout: fixed !important;
            word-wrap: break-word !important;
        }
        
        .agent-msg th, .agent-msg td {
            border: 1px solid #cbd5e1;
            padding: 8px 10px;
            text-align: left;
        }
        .agent-msg th { background: #dbeafe; color: #1e40af; font-weight: 600; }
        .agent-msg ul, .agent-msg ol { margin-left: 18px; margin-bottom: 8px; }
        .input-area {
            display: flex;
            padding: 14px 16px;
            background: #f8fafc;
            border-top: 1px solid #e0e7ff;
            gap: 10px;
        }
        .input-area input[type="text"] {
            flex: 1;
            padding: 12px 18px;
            border-radius: 20px;
            border: 1.5px solid #bae6fd;
            background: #ffffff;
            color: #1e1b4b;
            font-size: 0.9rem;
            outline: none;
            transition: all 0.2s ease;
        }
        .input-area input[type="text"]::placeholder { color: #94a3b8; }
        .input-area input[type="text"]:focus {
            border-color: #818cf8;
            box-shadow: 0 0 0 3px rgba(129, 140, 248, 0.25);
        }
        .input-area button {
            background: linear-gradient(135deg, #a855f7 0%, #38bdf8 100%);
            color: #ffffff;
            border: none;
            padding: 12px 22px;
            border-radius: 20px;
            font-weight: 600;
            font-size: 0.9rem;
            cursor: pointer;
            box-shadow: 0 4px 12px rgba(168, 85, 247, 0.3);
            transition: transform 0.1s ease, opacity 0.2s ease, box-shadow 0.2s ease;
        }
        .input-area button:hover {
            opacity: 0.95;
            transform: translateY(-1px);
            box-shadow: 0 6px 16px rgba(168, 85, 247, 0.4);
        }
        .input-area button:active { transform: translateY(0); }
    </style>
</head>
<body>
<div class="chat-container">
    <div class="chat-header">
        <div class="header-info">
            <span class="brand-title">Luintix Order Agent</span>
        </div>
    </div>
    <div class="chat-box" id="chatBox">
        <div class="message agent-msg">
            Hello! I am <strong>Luintix</strong>, your order resolution assistant. How can I help you with your order today?
        </div>
    </div>
    <form class="input-area" id="chatForm">
        <input type="text" id="userInput" placeholder="Ask about orders, tracking, returns..." required autocomplete="off">
        <button type="submit">Send</button>
    </form>
</div>

<script>
    marked.setOptions({ gfm: true, breaks: true });
    
    let sessionId = localStorage.getItem('luintix_session_id');

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

            if (!res.ok) {
                throw new Error(`Server returned error status: ${res.status}`);
            }

            const data = await res.json();
            
            if (data.session_id) {
                sessionId = data.session_id;
                localStorage.setItem('luintix_session_id', sessionId);
            }


            
            loadingDiv.innerHTML = marked.parse(data.agent_response || 'No response returned.');
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