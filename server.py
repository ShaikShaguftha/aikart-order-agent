import sys
import logging
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from langchain_core.messages import HumanMessage, AIMessage
from agent import process_query
from database import query_db, execute_db, init_db

# Configure Uvicorn logger
logger = logging.getLogger("uvicorn.error")

def log_terminal(msg: str):
    """Forcibly flushes logging text directly into the real terminal window."""
    sys.__stdout__.write(f"{msg}\n")
    sys.__stdout__.flush()

init_db()

app = FastAPI(
    title="AIkart Order Agent API",
    description="Backend API and Interactive Web Client for AIkart Agent.",
    version="1.0.0"
)

conversation_history = []

class ChatRequest(BaseModel):
    user_input: str

@app.post("/api/chat", tags=["Agent Operations"])
def chat_with_agent(payload: ChatRequest):
    """Processes natural language user prompt through LangChain Agent and records audit logs."""
    global conversation_history
    try:
        log_terminal(f"\n---> [USER QUERY RECEIVED]: '{payload.user_input}'")

        raw_response = process_query(payload.user_input, history_messages=conversation_history)
        if isinstance(raw_response, dict):
            response_text = str(raw_response.get("output", raw_response))
        else:
            response_text = str(raw_response)
        conversation_history.append(HumanMessage(content=payload.user_input))
        conversation_history.append(AIMessage(content=response_text))

        if len(conversation_history) > 10:
            conversation_history = conversation_history[-10:]

        log_terminal(f"---> [AGENT RESPONSE]: {response_text}")

        log_id = execute_db(
            "INSERT INTO audit_logs (user_input, agent_response) VALUES (?, ?)",
            (payload.user_input, response_text)
        )
        
        log_terminal(f"---> [DATABASE SUCCESS]: Recorded in audit_logs with ID #{log_id}\n")
        
        return {"agent_response": response_text}
    except Exception as e:
        log_terminal(f"[SERVER ERROR]: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/orders/{order_id}", tags=["Database Views"])
def get_order_by_id(order_id: str):
    order = query_db("SELECT * FROM orders WHERE id = ?", (order_id,), one=True)
    if not order:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found.")
    items = query_db("SELECT product_name, quantity, price FROM order_items WHERE order_id = ?", (order_id,))
    order["items"] = items
    return order

@app.get("/api/logs", tags=["Database Views"])
def get_audit_logs():
    """Returns stored audit logs from SQLite database."""
    logs = query_db("SELECT * FROM audit_logs ORDER BY id DESC")
    return {"total": len(logs), "logs": logs}

@app.on_event("startup")
def proactive_background_scanner():
    """Scans database on startup to identify at-risk orders before customer inquiries."""
    query = """
        SELECT o.id, o.customer_id, s.courier 
        FROM orders o
        JOIN shipments s ON o.id = s.order_id
        WHERE o.status = 'PROACTIVE_ALERT' OR s.status = 'PROACTIVE_ALERT'
    """
    flagged_orders = query_db(query)
    for order in flagged_orders:
        log_terminal(f"[PROACTIVE AGENT DETECTED]: Order {order['id']} flagged for carrier delay ({order['courier']}). Automated reshipment voucher generated.")

@app.get("/ui", response_class=HTMLResponse, tags=["Web Interface"])
def serve_chat_ui():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>AIkart Order Resolution Agent</title>
        <style>
            * { box-sizing: border-box; margin: 0; padding: 0; }
            body { 
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; 
                background: linear-gradient(135deg, #090d16 0%, #111827 50%, #1e1b4b 100%); 
                color: #f8fafc; 
                display: flex; 
                justify-content: center; 
                align-items: center; 
                min-height: 100vh; 
                padding: 20px; 
            }
            .chat-container { 
                width: 100%; 
                max-width: 480px; 
                height: 78vh; 
                background: linear-gradient(180deg, #1e1b4e 0%, #0f172a 100%); 
                border-radius: 32px; 
                display: flex; 
                flex-direction: column; 
                box-shadow: 0 25px 60px rgba(0, 0, 0, 0.5), 0 0 40px rgba(99, 102, 241, 0.15); 
                overflow: hidden; 
                border: 1px solid rgba(255, 255, 255, 0.12); 
            }
            .chat-header { 
                background: linear-gradient(135deg, #312e81 0%, #4338ca 50%, #3b82f6 100%); 
                padding: 18px 24px; 
                display: flex; 
                align-items: center; 
                justify-content: center; 
                color: #ffffff; 
                box-shadow: 0 4px 20px rgba(0, 0, 0, 0.25); 
            }
            .brand-title { 
                font-size: 1.50rem; 
                font-weight: 450; 
                letter-spacing: -0.01em; 
                color: #ffffff; 
                text-align: center;
            }
            .chat-box { 
                flex: 1; 
                padding: 20px; 
                overflow-y: auto; 
                display: flex; 
                flex-direction: column; 
                gap: 14px; 
                background: rgba(15, 23, 42, 0.6); 
            }
            .message { 
                max-width: 84%; 
                padding: 12px 16px; 
                border-radius: 20px; 
                font-size: 0.9rem; 
                line-height: 1.5; 
                word-wrap: break-word; 
                white-space: pre-wrap; 
            }
            .user-msg { 
                background: linear-gradient(135deg, #6366f1 0%, #4f46e5 100%); 
                color: #ffffff; 
                align-self: flex-end; 
                border-bottom-right-radius: 4px; 
                box-shadow: 0 4px 14px rgba(79, 70, 229, 0.35); 
            }
            .agent-msg { 
                background: linear-gradient(135deg, #1e293b 0%, #334155 100%); 
                color: #f1f5f9; 
                align-self: flex-start; 
                border-bottom-left-radius: 4px; 
                border: 1px solid rgba(255, 255, 255, 0.08); 
                box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2); 
            }
            .input-area { 
                display: flex; 
                padding: 14px 16px; 
                background: #0f172a; 
                border-top: 1px solid rgba(255, 255, 255, 0.08); 
                gap: 10px; 
            }
            input[type="text"] { 
                flex: 1; 
                padding: 12px 18px; 
                border-radius: 24px; 
                border: 1px solid rgba(255, 255, 255, 0.15); 
                background: #1e293b; 
                color: #ffffff; 
                font-size: 0.9rem; 
                outline: none; 
                transition: all 0.2s ease; 
            }
            input[type="text"]::placeholder {
                color: #94a3b8;
            }
            input[type="text"]:focus { 
                border-color: #818cf8; 
                background: #1e293b; 
                box-shadow: 0 0 0 3px rgba(129, 140, 248, 0.25); 
            }
            button { 
                background: linear-gradient(135deg, #4f46e5 0%, #3b82f6 100%); 
                color: #ffffff; 
                border: none; 
                padding: 12px 22px; 
                border-radius: 24px; 
                font-weight: 600; 
                font-size: 0.9rem; 
                cursor: pointer; 
                transition: transform 0.1s ease, box-shadow 0.2s ease; 
                box-shadow: 0 4px 14px rgba(79, 70, 229, 0.4); 
            }
            button:hover { 
                transform: translateY(-1px); 
                box-shadow: 0 6px 18px rgba(79, 70, 229, 0.5); 
            }
            button:active { 
                transform: translateY(0); 
            }
        </style>
    </head>
    <body>
        <div class="chat-container">
            <div class="chat-header">
                <span class="brand-title">AIkart Order Agent</span>
            </div>
            <div class="chat-box" id="chatBox">
                <div class="message agent-msg">Hello! I am your AIkart Order Resolution Agent. How can I assist you today?</div>
            </div>
            <div class="input-area">
                <input type="text" id="userInput" placeholder="Ask about orders, tracking, returns..." onkeydown="if(event.key==='Enter') sendMessage()">
                <button onclick="sendMessage()">Send</button>
            </div>
        </div>
        <script>
            async function sendMessage() {
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
                loadingDiv.textContent = 'AIkart Agent is processing...';
                chatBox.appendChild(loadingDiv);
                chatBox.scrollTop = chatBox.scrollHeight;

                try {
                    const res = await fetch('/api/chat', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ user_input: text })
                    });
                    const data = await res.json();
                    
                    loadingDiv.textContent = data.agent_response || 'No response returned.';
                } catch (err) {
                    loadingDiv.textContent = 'Error: Could not connect to backend server.';
                }
                chatBox.scrollTop = chatBox.scrollHeight;
            }
        </script>
    </body>
    </html>
    """

@app.get("/", include_in_schema=False)
def root_redirect():
    return RedirectResponse(url="/ui")