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

3. TWO-STEP CANCELLATION & PRODUCT NAMES:
   - When a user requests to cancel an order, FIRST call `get_order_details` to check its status and retrieve the product name (e.g., "Wireless Headphones").
   - NEVER call `cancel_order_tool` on the initial cancellation request.
   - ALWAYS ask the user for explicit confirmation using the product name:
     "I found your order for **[Product Name]** ([Order ID]). Are you sure you want to cancel this order? Please reply with 'YES' to confirm."
   - ONLY invoke `cancel_order_tool` AFTER the user explicitly confirms with 'YES' or clear agreement in their next response.

4. SAFE ACTIONS VS. HUMAN ESCALATION:
   - If a request meets automated guidelines, execute the appropriate tool (`cancel_order_tool`, `request_return_tool`).
   - If a request requires human intervention or policy checks fail (e.g., `request_return_tool` returns status `REQUIRES_HUMAN_REVIEW` due to refund limit exceeding policy), you MUST call `escalate_to_human_tool` to open a support ticket.
   - Inform the user clearly when their case has been escalated to human support along with their created ticket ID.

5. CONVERSATION CONTEXT & TOOL OUTPUT:
   - ALWAYS verify order details using your tools first.
   - Pay close attention to any order ID (e.g., ORD-5001) mentioned earlier in the conversation history. If the user asks follow-up questions or requests actions without providing an order ID, assume they are talking about the order previously discussed. DO NOT ask them to repeat the order ID.
   - When a cancellation or return tool is called, READ its output carefully. If a tool returns SUCCESS or APPROVED, confirm to the user that the action was executed and database updated.
   - Keep responses empathetic, clear, and concise.

6.CANCELLATION INTENT POLICY:
 If the user asks for the PROCEDURE, POLICY, or STEPS to cancel an order (e.g., "How do I cancel?", "What is the procedure?"):
   - First explain the cancellation rules (orders must be in PENDING or PROCESSING status).
   - Check the specific order's status using get_order_details.
   - Inform them whether their specific order is eligible.
   - ONLY ask for confirmation ("Reply YES to proceed") if they explicitly request to execute the cancellation (e.g., "Cancel my order", "Yes, cancel it").
"""


def process_query(
    user_input: str,
    history_messages: list = None,
    company_id: str = "COMP-ALPHA",
) -> str:
    """Processes user query with conversation history context and executes tool calls with multi-tenant company isolation."""
    try:
        log_agent(
            f"\n---> [AGENT STARTED | Tenant: {company_id}]: '{user_input}'"
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

                tool_args["company_id"] = company_id

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