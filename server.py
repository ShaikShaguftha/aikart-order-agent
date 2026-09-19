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
    """Serves the web chat frontend interface with Apple glassmorphism styled in Gold & Blue."""
    return """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Luintix | Smart Order Assistant</title>
    
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <style>
        :root {
            --gold-accent: #D4AF37;
            --purple-mute: #6B5B95;
            --royal-blue: #3A61D0;
            --bright-blue: #2563EB;
            --apple-dark: #1D1D1F;
            --apple-secondary: #86868B;
            
            --lux-gradient: linear-gradient(135deg, #B89255 0%, #6B5B95 35%, #3A61D0 70%, #2563EB 100%);
            --header-gradient: linear-gradient(135deg, #1E3A8A 0%, #3A61D0 40%, #6B5B95 75%, #B89255 100%);
            --bubble-user-gradient: linear-gradient(135deg, #6B5B95 0%, #3A61D0 50%, #2563EB 100%);

            --glass-bg: rgba(255, 255, 255, 0.72);
            --glass-card: rgba(255, 255, 255, 0.82);
            --glass-border: rgba(255, 255, 255, 0.65);
            --glass-shadow: 0 20px 40px rgba(15, 23, 42, 0.12);
            
            --font-apple: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "Helvetica Neue", Helvetica, Arial, sans-serif;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }
        
        body {
            font-family: var(--font-apple);
            background: #F1F5F9;
            color: var(--apple-dark);
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            padding: 16px;
            position: relative;
            overflow: hidden;
            -webkit-font-smoothing: antialiased;
        }

        .ambient-bg {
            position: absolute;
            width: 100%;
            height: 100%;
            top: 0;
            left: 0;
            z-index: 0;
            overflow: hidden;
            pointer-events: none;
        }

        .blob {
            position: absolute;
            border-radius: 50%;
            filter: blur(95px);
            opacity: 0.4;
        }

        .blob-1 {
            width: 420px;
            height: 420px;
            background: var(--gold-accent);
            top: -90px;
            left: -90px;
        }

        .blob-2 {
            width: 480px;
            height: 480px;
            background: var(--royal-blue);
            bottom: -100px;
            right: -80px;
        }

        .blob-3 {
            width: 380px;
            height: 380px;
            background: var(--purple-mute);
            top: 45%;
            left: 45%;
            transform: translate(-50%, -50%);
        }

        .chat-container {
            position: relative;
            z-index: 10;
            width: 100%;
            max-width: 450px;
            height: 85vh;
            background: var(--glass-bg);
            backdrop-filter: blur(30px) saturate(190%);
            -webkit-backdrop-filter: blur(30px) saturate(190%);
            border-radius: 28px;
            display: flex;
            flex-direction: column;
            box-shadow: var(--glass-shadow);
            border: 1px solid var(--glass-border);
            overflow: hidden;
        }

        .chat-header {
            background: var(--header-gradient);
            padding: 20px 24px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            box-shadow: 0 4px 18px rgba(30, 58, 138, 0.25);
        }

        .header-info {
            display: flex;
            flex-direction: column;
            gap: 2px;
        }

        .brand-title {
            font-size: 1.3rem;
            font-weight: 700;
            color: #FFFFFF !important;
            letter-spacing: -0.022em;
            text-shadow: 0 2px 4px rgba(15, 23, 42, 0.35);
        }

        .status-badge {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            font-size: 0.78rem;
            color: #F1F5F9;
            font-weight: 500;
            text-shadow: 0 1px 2px rgba(0, 0, 0, 0.2);
        }

        .status-dot {
            width: 8px;
            height: 8px;
            background-color: #34C759;
            border-radius: 50%;
            box-shadow: 0 0 6px rgba(52, 199, 89, 0.8);
        }

        /* CHAT BOX AREA */
        .chat-box {
            flex: 1;
            padding: 22px;
            overflow-y: auto;
            overflow-x: hidden;
            display: flex;
            flex-direction: column;
            gap: 16px;
            background: transparent;
        }

        .message {
            max-width: 86%;
            padding: 13px 17px;
            border-radius: 20px;
            font-size: 0.92rem;
            line-height: 1.45;
            word-wrap: break-word;
            overflow-wrap: break-word;
            letter-spacing: -0.01em;
        }

        .user-msg {
            background: var(--bubble-user-gradient);
            color: #FFFFFF !important;
            align-self: flex-end;
            border-bottom-right-radius: 4px;
            box-shadow: 0 4px 14px rgba(58, 97, 208, 0.25);
        }

        .agent-msg {
            background: var(--glass-card);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            color: var(--apple-dark);
            align-self: flex-start;
            border-bottom-left-radius: 4px;
            border: 1px solid var(--glass-border);
            max-width: 90%;
            box-shadow: 0 4px 14px rgba(0, 0, 0, 0.03);
        }
        
        .agent-msg p { 
            margin-bottom: 8px; 
            color: var(--apple-dark); 
        }
        .agent-msg p:last-child { margin-bottom: 0; }
        
        .agent-msg strong { 
            color: var(--royal-blue); 
            font-weight: 600; 
        }

        .agent-msg h1, .agent-msg h2, .agent-msg h3, .agent-msg h4 {
            font-weight: 700;
            margin: 14px 0 8px 0;
            letter-spacing: -0.02em;
            color: var(--royal-blue);
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .agent-msg h1::before, 
        .agent-msg h2::before, 
        .agent-msg h3::before {
            content: none !important;
        }

        .agent-msg h1 { font-size: 1.12rem; }
        .agent-msg h2 { font-size: 1.02rem; }
        .agent-msg h3 { font-size: 0.95rem; }

        .agent-msg hr {
            border: none;
            border-top: 1px solid rgba(209, 209, 214, 0.6);
            margin: 14px 0;
        }

        .agent-msg ul, .agent-msg ol {
            margin: 8px 0 12px 20px;
            padding: 0;
        }

        .agent-msg li { 
            margin-bottom: 6px; 
            line-height: 1.4;
        }

        /* TABLE WRAPPER - ONLY SCROLLS TABLES HORIZONTALLY */
        .table-wrapper {
            width: 100%;
            overflow-x: auto;
            margin: 12px 0;
            border-radius: 12px;
            border: 1px solid rgba(226, 232, 240, 0.8);
        }

        .agent-msg table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.8rem;
            background: rgba(255, 255, 255, 0.85);
        }
        
        .agent-msg th, .agent-msg td {
            border: 1px solid #E5E5EA;
            padding: 8px 10px;
            text-align: left;
            color: var(--apple-dark);
            white-space: normal;
        }

        .agent-msg th { 
            background: rgba(242, 242, 247, 0.9); 
            font-weight: 600;
            color: var(--royal-blue);
        }

        .input-area {
            display: flex;
            padding: 16px 20px;
            background: rgba(255, 255, 255, 0.55);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border-top: 1px solid rgba(226, 232, 240, 0.6);
            gap: 10px;
            align-items: center;
        }

        .input-area input[type="text"] {
            flex: 1;
            padding: 12px 18px;
            border-radius: 20px;
            border: 1px solid rgba(209, 209, 214, 0.6);
            background: rgba(255, 255, 255, 0.8);
            color: var(--apple-dark);
            font-family: var(--font-apple);
            font-size: 0.9rem;
            outline: none;
            transition: all 0.2s ease;
        }

        .input-area input[type="text"]::placeholder { 
            color: var(--apple-secondary); 
            font-size: 0.88rem;
        }

        .input-area input[type="text"]:focus {
            border-color: var(--royal-blue);
            background: #FFFFFF;
            box-shadow: 0 0 0 3px rgba(58, 97, 208, 0.15);
        }

        .input-area button {
            background: var(--lux-gradient);
            color: #FFFFFF !important;
            border: none;
            padding: 12px 20px;
            border-radius: 20px;
            font-family: var(--font-apple);
            font-weight: 600;
            font-size: 0.9rem;
            cursor: pointer;
            box-shadow: 0 4px 12px rgba(58, 97, 208, 0.25);
            transition: all 0.2s ease;
            text-shadow: 0 1px 2px rgba(0, 0, 0, 0.2);
        }

        .input-area button:hover {
            opacity: 0.94;
            transform: translateY(-1px);
        }

        .input-area button:active { transform: translateY(0); }
    </style>
</head>
<body>

<div class="ambient-bg">
    <div class="blob blob-1"></div>
    <div class="blob blob-2"></div>
    <div class="blob blob-3"></div>
</div>

<div class="chat-container">
    <div class="chat-header">
        <div class="header-info">
            <span class="brand-title">Luintix Order Agent</span>
            <div class="status-badge">
                <span class="status-dot"></span> Active & Ready
            </div>
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

            let parsedHtml = marked.parse(data.agent_response || 'No response returned.');
            
            // Wrap generated tables into .table-wrapper so only tables get horizontal scrollbars
            parsedHtml = parsedHtml.replace(/<table>/g, '<div class="table-wrapper"><table>')
                                   .replace(/<\/table>/g, '</table></div>');

            loadingDiv.innerHTML = parsedHtml;
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