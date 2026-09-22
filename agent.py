import logging
import os
import sys

from dotenv import load_dotenv
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_groq import ChatGroq
from tools import tools, tools_dict


def log_agent(msg: str):
    """Flushes logging text directly to the terminal."""
    sys.stdout.write(f"{msg}\n")
    sys.stdout.flush()


load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
llm = ChatGroq(
    model="openai/gpt-oss-120b",
    groq_api_key=GROQ_API_KEY,
    temperature=0,
    max_retries=3,
    request_timeout=60
)

llm_with_tools = llm.bind_tools(tools)

SYSTEM_PROMPT = """You are Luintix (AIKart's Automated Order Resolution Agent).
Your goal is to assist e-commerce customers with order tracking, delays, cancellations, returns, and damages.

STRICT GUARDRAILS & RULES:

1. OUT-OF-CONTEXT GUARDRAILS (PREVENT TOKEN BURNOUT):
   - You MUST ONLY answer queries directly related to order resolution, shipment tracking, returns, cancellations, and customer account support.
   - DO NOT answer questions regarding general knowledge, world events, programming/coding, jokes, recipes, math, or trivia.
   - If a user asks an out-of-context question (e.g., "who is the president", "write python code", "tell me a joke"), reply EXACTLY with:
     "I am specialized strictly in Luintix order resolution. I cannot assist with off-topic queries or general knowledge. How can I help you with your order today?"

2. PROCEDURAL QUESTIONS VS. EXECUTING ACTIONS:
   - If a user asks general procedural questions (e.g., "if I want to cancel an order what is the procedure?" or "how do returns work?"), explain the step-by-step policy directly.
   - DO NOT execute tool actions like `cancel_order_tool` or `request_return_tool` when the user is merely asking for instructions or procedural advice.

3. CANCELLATION INTENT POLICY & TWO-STEP CONFIRMATION:
   - If a user asks for policy/steps (e.g., "How do I cancel?"), explain the rules (orders must be in PENDING or PROCESSING status), check eligibility via `get_order_details`, and do NOT ask for immediate execution confirmation unless requested.
   - When a user explicitly requests to cancel an order:
     a. FIRST call `get_order_details` to check status and retrieve the product name (e.g., "Wireless Headphones").
     b. NEVER call `cancel_order_tool` on the initial cancellation request.
     c. ALWAYS ask for explicit confirmation: "I found your order for **[Product Name]** ([Order ID]). Are you sure you want to cancel this order? Please reply with 'YES' to confirm."
     d. ONLY invoke `cancel_order_tool` AFTER the user explicitly confirms with 'YES' or clear agreement in their next response.

4. SAFE ACTIONS VS. HUMAN ESCALATION (SALESFORCE AGENTFORCE ALIGNMENT):
   - AUTOMATED REFUNDS: You can auto-approve refunds up to $100. If a refund/return request exceeds $100 or if `request_return_tool` returns status `REQUIRES_HUMAN_REVIEW`, you MUST immediately call `escalate_to_human_tool`.
   - DISSATISFIED CUSTOMERS: If a user expresses extreme dissatisfaction, demands legal action, or asks for human management, call `escalate_to_human_tool`.
   - ALWAYS inform the user clearly when their case has been escalated to human support and provide their created ticket ID (`ticket_id`).

5. CONVERSATION CONTEXT & TOOL OUTPUT:
   - ALWAYS verify order details using your tools first. Never hallucinate tracking status, order totals, or policies. Always rely strictly on database outputs.
   - Pay close attention to any order ID (e.g., ORD-5001) mentioned earlier in the conversation history. If the user asks follow-up questions without providing an order ID, assume they are referencing the previously discussed order. DO NOT ask them to repeat it.
   - When a cancellation or return tool is called, READ its output carefully. If a tool returns SUCCESS or APPROVED, confirm to the user that the action was executed and the database has been updated.
   - Keep responses empathetic, clear, and concise.
"""


def process_query(
    user_input: str,
    history_messages: list = None,
    user_id: str = "USER-101",
    **kwargs,
) -> str:
    """Processes user query with conversation history context."""
    try:
        log_agent(
            f"\n---> [AGENT STARTED | Tenant: {user_id}]: '{user_input}'"
        )

        messages = [SystemMessage(content=SYSTEM_PROMPT)]

        if history_messages:
            messages.extend(history_messages[-4:])

        messages.append(HumanMessage(content=user_input))

        max_iterations = 5
        iteration = 0

        while iteration < max_iterations:
            ai_msg = llm_with_tools.invoke(messages)

            if not ai_msg.tool_calls:
                res_text = (
                    ai_msg.content if ai_msg.content else "Request completed."
                )
                log_agent(f"---> [AGENT COMPLETED]: {res_text}\n")
                return res_text

            messages.append(ai_msg)

            for tool_call in ai_msg.tool_calls:
                tool_name = tool_call["name"]
                tool_args = tool_call["args"]
                tool_call_id = tool_call["id"]

                tool_args["user_id"] = user_id

                log_agent(f"[Executing Tool]: {tool_name}({tool_args})")

                if tool_name in tools_dict:
                    selected_tool = tools_dict[tool_name]
                    tool_output = selected_tool.invoke(tool_args)
                else:
                    tool_output = f"Error: Tool {tool_name} not found."

                log_agent(f"[Tool Result]: {tool_output}")

                messages.append(
                    ToolMessage(
                        content=str(tool_output), tool_call_id=tool_call_id
                    )
                )

            iteration += 1

        final_msg = llm.invoke(messages)
        res_text = (
            final_msg.content
            if final_msg.content
            else "Request completed after maximum tool executions."
        )
        log_agent(f"---> [AGENT COMPLETED]: {res_text}\n")
        return res_text

    except Exception as e:
        log_agent(f"[AGENT ERROR]: {e}")
        return f"Error processing query: {str(e)}"