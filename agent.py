import logging
import os
import sys

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_groq import ChatGroq
from tools import tools, tools_dict


def log_agent(msg: str):
    """Flushes logging text directly to the terminal."""
    sys.stdout.write(f"{msg}\n")
    sys.stdout.flush()


load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
llm = ChatGroq(
    model="openai/gpt-oss-20b",
    groq_api_key=GROQ_API_KEY,
    temperature=0,
)

llm_with_tools = llm.bind_tools(tools)

SYSTEM_PROMPT = """You are AIKart's Automated Order Resolution Agent.
Your goal is to assist e-commerce customers with order tracking, delays, cancellations, returns, and damages.

GUARDRAILS & RULES:
1. ALWAYS verify order details using your tools first.
2. CONVERSATION CONTEXT: Pay close attention to any order ID (e.g., ORD-5001) mentioned earlier in the conversation history. If the user asks follow-up questions or requests actions (like cancelling or checking status) without providing an order ID, assume they are talking about the order previously discussed. DO NOT ask them to repeat the order ID.
3. When a cancellation or return tool is called, READ its output carefully.
4. If a tool returns SUCCESS or APPROVED, confirm to the user that the action was executed and database updated.
5. Keep responses empathetic, clear, and concise.
"""


def process_query(user_input: str, history_messages: list = None) -> str:
    """Processes user query with conversation history context and executes tool calls."""
    try:
        log_agent(f"\n---> [AGENT STARTED]: '{user_input}'")

        messages = [SystemMessage(content=SYSTEM_PROMPT)]

        if history_messages:
            messages.extend(history_messages)

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